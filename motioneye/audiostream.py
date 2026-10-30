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

"""Live audio and video+audio streams.

The sound comes from the capture of the camera (see ``audiocapture``). Every
client gets its own ffmpeg, which encodes the sound for it; WAV needs no
encoder. The video+audio stream takes the JPEG frames from the MJPEG client
motionEye already has for the camera, so the video is copied and not
re-encoded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import struct
from collections.abc import AsyncIterator

from motioneye import audio, mediafiles, mjpgclient

__all__ = (
    'StreamUnavailableError',
    'audio_stream',
    'content_type',
    'codec_arguments',
    'video_audio_stream',
    'wav_header',
)

VIDEO_AUDIO_CONTENT_TYPE = 'video/x-matroska'
_VIDEO_AUDIO_FORMAT = 'aac'
_FIRST_FRAME_TIMEOUT = 8.0
_READ_SIZE = 4096
_OPUS_SAMPLE_RATES = (8000, 12000, 16000, 24000, 48000)


class StreamUnavailableError(Exception):
    """The stream cannot be provided (e.g. there is no video frame)."""


def content_type(fmt: str) -> str:
    return audio.AUDIO_FORMATS[fmt][2]


def wav_header(sample_rate: int) -> bytes:
    """A header of a WAV file of unknown length, for players of live streams."""

    unknown = 0xFFFFFFFF

    return (
        b'RIFF'
        + struct.pack('<I', unknown)
        + b'WAVEfmt '
        + struct.pack('<IHHIIHH', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b'data'
        + struct.pack('<I', unknown)
    )


def codec_arguments(settings: audio.AudioSettings, fmt: str) -> list[str]:
    """The ffmpeg output arguments that encode the sound as the given format."""

    arguments = ['-c:a', audio.AUDIO_FORMATS[fmt][0]]

    if fmt != 'wav':
        arguments += ['-b:a', f'{settings.bitrate}k']

    if fmt == 'opus' and settings.sample_rate not in _OPUS_SAMPLE_RATES:
        arguments += ['-ar', '48000']

    if fmt == 'mp3':
        arguments += ['-write_xing', '0', '-id3v2_version', '0']

    return arguments


def _ffmpeg() -> str:
    binary = mediafiles.find_ffmpeg()[0]
    if not binary:
        raise StreamUnavailableError('ffmpeg is not available')

    return binary


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()

    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), 5)


async def _quiet(coroutine) -> None:
    # a feeder fails when its ffmpeg is gone, which the stream notices on its own
    try:
        await coroutine

    except asyncio.CancelledError:
        raise

    except Exception as e:
        logging.debug(f'audio stream feeder stopped: {e}')


async def _feed_pcm(queue: asyncio.Queue, write, close) -> None:
    while True:
        pcm = await queue.get()
        if pcm is None:  # the capture stopped: let the encoder finish
            close()
            return

        await write(pcm)


async def audio_stream(capture, fmt: str) -> AsyncIterator[bytes]:
    """Yields the sound of a camera encoded as the given format."""

    settings = capture.settings
    queue = capture.subscribe()
    process = None
    feeder = None
    try:
        if fmt == 'wav':
            yield wav_header(capture.sample_rate)
            while True:
                pcm = await queue.get()
                if pcm is None:
                    return

                yield pcm

        command = [
            _ffmpeg(),
            '-hide_banner',
            '-nostdin',
            '-loglevel',
            'error',
            '-f',
            's16le',
            '-ar',
            str(capture.sample_rate),
            '-ac',
            '1',
            '-i',
            'pipe:0',
            '-vn',
            *codec_arguments(settings, fmt),
            '-flush_packets',
            '1',
            '-f',
            audio.AUDIO_FORMATS[fmt][1],
            'pipe:1',
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdin, stdout = process.stdin, process.stdout
        assert stdin is not None and stdout is not None  # nosec: B101

        async def write(pcm: bytes) -> None:
            stdin.write(pcm)
            await stdin.drain()

        feeder = asyncio.ensure_future(_quiet(_feed_pcm(queue, write, stdin.close)))
        while True:
            data = await stdout.read(_READ_SIZE)
            if not data:
                return

            yield data

    finally:
        capture.unsubscribe(queue)
        if feeder:
            feeder.cancel()

        if process:
            await _kill(process)


async def video_audio_stream(
    capture, camera_id: int, framerate: float
) -> AsyncIterator[bytes]:
    """Yields a Matroska stream with the MJPEG picture of a camera and its sound."""

    settings = capture.settings
    loop = asyncio.get_running_loop()

    # the first frame, so that ffmpeg gets the picture size right away
    deadline = loop.time() + _FIRST_FRAME_TIMEOUT
    frame = mjpgclient.get_jpg(camera_id)
    while not frame and loop.time() < deadline:
        await asyncio.sleep(0.2)
        frame = mjpgclient.get_jpg(camera_id)

    if not frame:
        raise StreamUnavailableError('no picture from the camera')

    queue = capture.subscribe()
    pcm_read, pcm_write = os.pipe()
    process = None
    tasks: list[asyncio.Future] = []
    try:
        command = [
            _ffmpeg(),
            '-hide_banner',
            '-nostdin',
            '-loglevel',
            'error',
            '-use_wallclock_as_timestamps',
            '1',
            '-probesize',
            '200000',
            '-analyzeduration',
            '500000',
            '-f',
            'image2pipe',
            '-c:v',
            'mjpeg',
            '-i',
            'pipe:0',
            '-f',
            's16le',
            '-ar',
            str(capture.sample_rate),
            '-ac',
            '1',
            '-i',
            f'pipe:{pcm_read}',
            '-map',
            '0:v',
            '-map',
            '1:a',
            '-c:v',
            'copy',
            *codec_arguments(settings, _VIDEO_AUDIO_FORMAT),
            '-shortest',
            '-flush_packets',
            '1',
            '-f',
            'matroska',
            'pipe:1',
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            pass_fds=(pcm_read,),
        )
        os.close(pcm_read)
        pcm_read = -1
        stdin, stdout = process.stdin, process.stdout
        assert stdin is not None and stdout is not None  # nosec: B101

        async def write_pcm(pcm: bytes) -> None:
            if pcm_write >= 0:
                await loop.run_in_executor(None, os.write, pcm_write, pcm)

        def close_pcm() -> None:
            nonlocal pcm_write
            if pcm_write >= 0:
                with contextlib.suppress(OSError):
                    os.close(pcm_write)

                pcm_write = -1

            # the picture ends with the sound
            stdin.close()

        async def feed_frames() -> None:
            last = None
            interval = 1 / max(1.0, framerate)
            current = frame
            while True:
                if current is not None and current is not last:
                    stdin.write(current)
                    await stdin.drain()
                    last = current

                await asyncio.sleep(interval)
                current = mjpgclient.get_jpg(camera_id)

        tasks = [
            asyncio.ensure_future(_quiet(_feed_pcm(queue, write_pcm, close_pcm))),
            asyncio.ensure_future(_quiet(feed_frames())),
        ]

        while True:
            data = await stdout.read(_READ_SIZE)
            if not data:
                return

            yield data

    finally:
        capture.unsubscribe(queue)
        for task in tasks:
            task.cancel()

        if process:
            await _kill(process)

        for fd in (pcm_read, pcm_write):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)

        logging.debug(f'video and audio stream of camera {camera_id} closed')
