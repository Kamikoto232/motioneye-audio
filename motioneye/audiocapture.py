# Copyright (c) 2013 Calin Crisan
# This file is part of motionEye.
#
# motionEye is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Capturing the sound of the cameras.

One ffmpeg process per camera with audio enabled decodes the audio source to
raw PCM (signed 16 bit, mono). The PCM blocks are measured here (loudness in
dB relative to full scale) and handed to whoever needs the sound: the
threshold trigger, the movie recorder and the live listeners.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import math
import os
import re
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass

from tornado.ioloop import IOLoop

from motioneye import audio, audiomux, config, mediafiles, motionctl, utils

__all__ = (
    'AudioCapture',
    'AudioState',
    'ThresholdTrigger',
    'block_db',
    'get_capture',
    'get_state',
    'on_event_end',
    'on_event_start',
    'on_movie_end',
    'will_add_sound',
    'start',
    'stop',
    'update',
)

# the loudness is measured on blocks of this duration
BLOCK_SECONDS = 0.1
# the reported loudness is the average power of this many blocks
_SMOOTHING_BLOCKS = 3
# the sound kept from before an event starts, besides the pre-capture of motion
_PRE_ROLL_SECONDS = 8
_MAX_PRE_ROLL_SECONDS = 120
# the recording of an event is kept this long after the event for the movie to end
_RECORDING_GRACE_SECONDS = 60
# a longer recording is not kept (about 1 GB of 48 kHz sound for 3 hours)
_MAX_RECORDING_SECONDS = 3 * 3600
# no sound received for this long: the level is unknown
_STALE_SECONDS = 3.0
# the volume counts as above the threshold this long after the last loud block
_EXCEEDED_HOLD_SECONDS = 2.0
# no sound received for this long while ffmpeg is running: restart it
_WATCHDOG_SECONDS = 20.0
_RETRY_DELAYS = (2, 5, 10, 30, 60)

_captures: dict[int, AudioCapture] = {}
_updating = False


@dataclass(frozen=True)
class AudioState:
    """What the web interface shows for a camera."""

    level: float | None  # dB relative to full scale, None if there is no sound
    peak: float | None  # the highest block level of the last few seconds
    exceeded: bool  # the volume is above the threshold


def block_power(pcm: bytes) -> float:
    """Mean power of signed 16 bit little endian samples, 0..1 (1: full scale)."""

    samples = array.array('h')
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == 'big':
        samples.byteswap()

    if not samples:
        return 0.0

    return sum(s * s for s in samples) / len(samples) / (32768.0 * 32768.0)


def power_to_db(power: float) -> float:
    if power <= 0:
        return float(audio.DB_FLOOR)

    return max(float(audio.DB_FLOOR), min(0.0, 10 * math.log10(power)))


def block_db(pcm: bytes) -> float:
    return power_to_db(block_power(pcm))


class ThresholdTrigger:
    """Tells when the volume goes above a threshold and when it stays below it
    for long enough to end the event (the same way as the event gap of motion)."""

    def __init__(self, threshold: float, hold: float) -> None:
        self.threshold = threshold
        self.hold = hold
        self.active = False
        self._last_loud: float | None = None

    def update(self, level: float, now: float) -> str | None:
        """Returns 'start' or 'end' when the event starts or ends."""

        if level > self.threshold:
            self._last_loud = now
            if not self.active:
                self.active = True
                return 'start'

        elif self.active and now - (self._last_loud or now) >= self.hold:
            self.active = False
            return 'end'

        return None

    def exceeded(self, now: float) -> bool:
        return (
            self._last_loud is not None
            and now - self._last_loud < _EXCEEDED_HOLD_SECONDS
        )

    def reset(self) -> str | None:
        was_active = self.active
        self.active = False
        self._last_loud = None

        return 'end' if was_active else None


class _Recording:
    """The raw sound of the running event, to be put in the movie."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.size = 0
        self._file = open(path, 'wb')  # noqa: SIM115

    def write(self, pcm: bytes) -> None:
        self._file.write(pcm)
        self.size += len(pcm)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._file.close()

        with contextlib.suppress(OSError):
            os.remove(self.path)


class AudioCapture:
    def __init__(self, camera_id: int, camera_config: dict) -> None:
        self.camera_id = camera_id
        self.settings = audio.get_settings(camera_config)
        self.input_arguments = audio.input_arguments(camera_config) or []
        self.signature = signature(camera_config)
        self.trigger = ThresholdTrigger(
            self.settings.threshold, _event_gap(camera_config)
        )
        self._trigger_lock = asyncio.Lock()

        self._ring: deque[bytes] = deque()
        self._recording: _Recording | None = None
        self._event_active = False
        self._close_handle: asyncio.TimerHandle | None = None
        self._target_dir = ''
        self._configure_recording(camera_config)
        self._remove_stale_recording()

        self.sample_rate = self.settings.sample_rate
        self.block_size = int(self.sample_rate * BLOCK_SECONDS) * 2

        self._powers: deque[float] = deque(maxlen=_SMOOTHING_BLOCKS)
        self._peaks: deque[tuple[float, float]] = deque()
        self._last_data = 0.0
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopped = True
        self._kill()

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

        self._task = None
        self._end_event()
        self._close_recording()

    @property
    def level(self) -> float | None:
        if time.monotonic() - self._last_data > _STALE_SECONDS or not self._powers:
            return None

        return power_to_db(sum(self._powers) / len(self._powers))

    @property
    def peak(self) -> float | None:
        if self.level is None or not self._peaks:
            return None

        return max(db for _t, db in self._peaks)

    def apply_config(self, camera_config: dict) -> None:
        """Takes over the settings that do not need a restart of the capture."""

        self.settings = audio.get_settings(camera_config)
        self.trigger.threshold = self.settings.threshold
        self.trigger.hold = _event_gap(camera_config)
        self._configure_recording(camera_config)

        if not self.settings.trigger:
            self._end_event()

        if not self.settings.record:
            self._close_recording()

    def get_state(self) -> AudioState:
        exceeded = self.settings.trigger and self.trigger.exceeded(time.monotonic())

        return AudioState(level=self.level, peak=self.peak, exceeded=bool(exceeded))

    def _configure_recording(self, camera_config: dict) -> None:
        try:
            pre_capture = float(camera_config.get('pre_capture') or 0)
            framerate = max(1.0, float(camera_config.get('framerate') or 1))

        except (TypeError, ValueError):
            pre_capture, framerate = 0.0, 1.0

        seconds = min(_MAX_PRE_ROLL_SECONDS, _PRE_ROLL_SECONDS + pre_capture / framerate)
        self._ring = deque(self._ring, maxlen=math.ceil(seconds / BLOCK_SECONDS))
        self._target_dir = str(camera_config.get('target_dir') or '')

    def _recording_path(self) -> str:
        directory = self._target_dir
        if not os.path.isdir(directory):
            directory = tempfile.gettempdir()

        return os.path.join(directory, f'.audio-{self.camera_id}.pcm')

    def _remove_stale_recording(self) -> None:
        with contextlib.suppress(OSError):
            os.remove(self._recording_path())

    def event_started(self) -> None:
        """A motion event started: keep the sound for the movie."""

        self._cancel_close()
        self._event_active = True
        if not self.settings.record or self._recording is not None:
            return

        try:
            self._recording = _Recording(self._recording_path())

        except OSError as e:
            logging.error(f'could not record the sound of camera {self.camera_id}: {e}')
            return

        # the sound from before the event
        for pcm in self._ring:
            self._recording.write(pcm)

    def event_ended(self) -> None:
        self._event_active = False
        self._cancel_close()
        if self._recording is not None:
            # the movie may end after the event, or in several parts
            self._close_handle = asyncio.get_running_loop().call_later(
                _RECORDING_GRACE_SECONDS, self._close_recording
            )

    async def movie_ended(self, movie: str) -> bool:
        """Puts the sound in the movie that motion has just closed."""

        recording = self._recording
        if recording is None:
            return False

        recording.flush()
        try:
            return await audiomux.mux(movie, recording.path, recording.size, self.settings)

        finally:
            if not self._event_active:
                self._close_recording()

    def has_recording(self) -> bool:
        return self._recording is not None

    def _cancel_close(self) -> None:
        if self._close_handle is not None:
            self._close_handle.cancel()
            self._close_handle = None

    def _close_recording(self) -> None:
        self._cancel_close()
        if self._recording is not None:
            self._recording.close()
            self._recording = None

    def _kill(self) -> None:
        process = self._process
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    def _command(self) -> list[str]:
        binary = mediafiles.find_ffmpeg()[0]
        if not binary:
            raise RuntimeError('ffmpeg is not available')

        return [
            binary,
            '-hide_banner',
            '-nostdin',
            '-loglevel',
            'error',
            *self.input_arguments,
            '-vn',
            '-sn',
            '-dn',
            '-ac',
            '1',
            '-ar',
            str(self.sample_rate),
            '-f',
            's16le',
            'pipe:1',
        ]

    async def _run(self) -> None:
        failures = 0
        while not self._stopped:
            started = time.monotonic()
            try:
                await self._capture()

            except asyncio.CancelledError:
                raise

            except Exception as e:
                logging.error(f'audio capture of camera {self.camera_id} failed: {e}')

            if self._stopped:
                break

            # a capture that ran for a while was working, so start over quickly
            failures = 0 if time.monotonic() - started > 60 else failures + 1
            delay = _RETRY_DELAYS[min(failures, len(_RETRY_DELAYS)) - 1]
            logging.warning(
                f'audio capture of camera {self.camera_id} ended, retrying in {delay} s'
            )
            self._reset_level()
            await asyncio.sleep(delay)

    async def _capture(self) -> None:
        command = self._command()
        logging.debug(
            f'starting audio capture of camera {self.camera_id}: {_redact(command)}'
        )

        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        process = self._process
        assert process.stdout is not None  # nosec: B101
        assert process.stderr is not None  # nosec: B101

        stderr_task = asyncio.ensure_future(process.stderr.read(4096))
        try:
            while True:
                try:
                    pcm = await asyncio.wait_for(
                        process.stdout.readexactly(self.block_size), _WATCHDOG_SECONDS
                    )

                except asyncio.IncompleteReadError:
                    break

                except asyncio.TimeoutError:
                    logging.warning(
                        f'no audio from camera {self.camera_id} for {_WATCHDOG_SECONDS:.0f} s'
                    )
                    break

                self._on_block(pcm)

        finally:
            self._kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 5)

            error = b''
            with contextlib.suppress(Exception):
                error = await asyncio.wait_for(stderr_task, 1)

            if error.strip():
                logging.warning(
                    f'audio capture of camera {self.camera_id}: '
                    + error.decode(errors='replace').strip().splitlines()[-1]
                )

    def _reset_level(self) -> None:
        self._powers.clear()
        self._peaks.clear()
        self._end_event()

    def _end_event(self) -> None:
        if self.trigger.reset():
            self._fire('eventend')

    def _fire(self, action: str) -> None:
        asyncio.ensure_future(self._trigger_action(action))

    async def _trigger_action(self, action: str) -> None:
        # a lock keeps the start and the end of an event in order
        async with self._trigger_lock:
            logging.info(f'sound event {action} on camera {self.camera_id}')
            await motionctl.trigger_action(self.camera_id, action)

    def _on_block(self, pcm: bytes) -> None:
        now = time.monotonic()
        power = block_power(pcm)

        self._last_data = now
        self._powers.append(power)

        self._ring.append(pcm)
        recording = self._recording
        if recording is not None:
            if recording.size >= _MAX_RECORDING_SECONDS * self.sample_rate * 2:
                logging.warning(f'the sound of camera {self.camera_id} is too long to keep')
                self._close_recording()

            else:
                recording.write(pcm)

        self._peaks.append((now, power_to_db(power)))
        while self._peaks and now - self._peaks[0][0] > 3:
            self._peaks.popleft()

        if self.settings.trigger and self.level is not None:
            change = self.trigger.update(self.level, now)
            if change == 'start':
                self._fire('eventstart')

            elif change == 'end':
                self._fire('eventend')


def _redact(command: list[str]) -> str:
    return re.sub(r'(://)[^/@\s]+@', r'\1***@', ' '.join(command))


def _event_gap(camera_config: dict) -> float:
    try:
        return max(1.0, float(camera_config.get('event_gap', 30)))

    except (TypeError, ValueError):
        return 30.0


def signature(camera_config: dict) -> tuple:
    """What a running capture depends on; it is restarted when this changes."""

    settings = audio.get_settings(camera_config)

    return (
        settings.enabled,
        settings.source,
        settings.quality,
        tuple(audio.input_arguments(camera_config) or ()),
    )


def on_event_start(camera_id: int) -> None:
    capture = _captures.get(camera_id)
    if capture:
        capture.event_started()


def on_event_end(camera_id: int) -> None:
    capture = _captures.get(camera_id)
    if capture:
        capture.event_ended()


def will_add_sound(camera_id: int) -> bool:
    """Tells if the sound of the running event is kept for the next movie."""

    capture = _captures.get(camera_id)

    return bool(capture and capture.has_recording())


async def on_movie_end(camera_id: int, movie: str) -> bool:
    capture = _captures.get(camera_id)

    return bool(capture and await capture.movie_ended(movie))


def get_capture(camera_id: int) -> AudioCapture | None:
    return _captures.get(camera_id)


def get_state(camera_id: int) -> AudioState | None:
    capture = _captures.get(camera_id)

    return capture.get_state() if capture else None


async def _update() -> None:
    """Starts, restarts and stops the captures to match the camera configs."""

    global _updating

    if _updating:
        return

    _updating = True
    try:
        wanted: dict[int, dict] = {}
        for camera_id in config.get_camera_ids():
            camera_config = config.get_camera(camera_id)
            if (
                camera_config
                and utils.is_local_motion_camera(camera_config)
                and camera_config.get('@enabled')
                and audio.get_settings(camera_config).enabled
                and audio.input_arguments(camera_config)
            ):
                wanted[camera_id] = camera_config

        for camera_id, capture in list(_captures.items()):
            camera_config = wanted.get(camera_id)
            if camera_config is None or capture.signature != signature(camera_config):
                await capture.stop()
                del _captures[camera_id]

            else:
                capture.apply_config(camera_config)

        for camera_id, camera_config in wanted.items():
            if camera_id not in _captures:
                capture = AudioCapture(camera_id, camera_config)
                _captures[camera_id] = capture
                capture.start()
                logging.info(f'audio capture of camera {camera_id} started')

    except Exception as e:
        logging.error(f'could not update the audio captures: {e}', exc_info=True)

    finally:
        _updating = False


def update() -> None:
    """Applies the camera configs, once the IO loop is running."""

    IOLoop.current().add_callback(_update)


def start() -> None:
    update()


def stop() -> None:
    for capture in _captures.values():
        capture._stopped = True  # noqa: SLF001
        capture._kill()  # noqa: SLF001

    _captures.clear()
