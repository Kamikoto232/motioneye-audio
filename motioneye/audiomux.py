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

"""Adding the recorded sound to the movies written by motion.

Motion records silent movies. While an event is running the sound is saved as
raw PCM, and once motion has closed the movie, the two are put together with
ffmpeg: the video is copied and only the sound is encoded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re

from motioneye import audio, mediafiles

__all__ = ('container_format', 'mux', 'probe')

# the audio formats a movie container can hold, the best one first
_CONTAINER_FORMATS: dict[str, tuple[str, ...]] = {
    '.mp4': ('aac', 'mp3'),
    '.m4v': ('aac', 'mp3'),
    '.mov': ('aac', 'mp3', 'wav'),
    '.mkv': ('aac', 'mp3', 'opus', 'wav'),
    '.webm': ('opus',),
    '.avi': ('mp3', 'aac', 'wav'),
    '.flv': ('mp3', 'aac'),
    '.swf': ('mp3',),
    '.ogg': ('opus',),
    '.ogv': ('opus',),
}
_FASTSTART_EXTENSIONS = ('.mp4', '.m4v', '.mov')
_OPUS_SAMPLE_RATES = (8000, 12000, 16000, 24000, 48000)
# differences smaller than this between the video and the sound are not corrected
_SYNC_TOLERANCE = 0.05
_TIMEOUT = 600

_DURATION_RE = re.compile(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)')


def container_format(extension: str, preferred: str) -> str:
    """The audio format to use in a movie file with the given extension."""

    allowed = _CONTAINER_FORMATS.get(extension.lower())
    if allowed is None:
        return 'aac'

    return preferred if preferred in allowed else allowed[0]


def _parse_probe(output: str) -> tuple[float | None, bool]:
    duration = None
    match = _DURATION_RE.search(output)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    return duration, bool(re.search(r'Stream #\S+.*: Audio:', output))


async def _run(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, error = await asyncio.wait_for(process.communicate(), _TIMEOUT)

    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()

        await process.wait()

        return -1, 'timeout'

    return process.returncode or 0, error.decode(errors='replace')


async def probe(binary: str, path: str) -> tuple[float | None, bool]:
    """Returns the duration of a media file in seconds and whether it has sound."""

    _code, output = await _run([binary, '-hide_banner', '-nostdin', '-i', path])

    return _parse_probe(output)


def build_command(
    binary: str,
    movie: str,
    pcm: str,
    output: str,
    *,
    video_duration: float | None,
    pcm_bytes: int,
    settings: audio.AudioSettings,
) -> list[str]:
    rate = settings.sample_rate
    audio_duration = pcm_bytes / (rate * 2)
    extension = os.path.splitext(movie)[1].lower()
    fmt = container_format(extension, settings.format)
    encoder = audio.AUDIO_FORMATS[fmt][0]

    # the sound and the movie end at the same time, so the shift is made at
    # the start: skip the sound that came before the movie, or delay it
    # (-t keeps ffmpeg from reading what came after the movie ended)
    input_options = ['-t', f'{audio_duration:.3f}']
    if video_duration is not None:
        difference = video_duration - audio_duration
        if difference > _SYNC_TOLERANCE:
            input_options = ['-itsoffset', f'{difference:.3f}', *input_options]

        elif difference < -_SYNC_TOLERANCE:
            input_options = [
                '-ss',
                f'{-difference:.3f}',
                '-t',
                f'{video_duration:.3f}',
            ]

    command = [
        binary,
        '-hide_banner',
        '-nostdin',
        '-loglevel',
        'error',
        '-y',
        '-i',
        movie,
        *input_options,
        '-f',
        's16le',
        '-ar',
        str(rate),
        '-ac',
        '1',
        '-i',
        pcm,
        '-map',
        '0',
        '-map',
        '1:a',
        '-c',
        'copy',
        '-c:a',
        encoder,
    ]

    if fmt != 'wav':
        command += ['-b:a', f'{settings.bitrate}k']

    if fmt == 'opus' and rate not in _OPUS_SAMPLE_RATES:
        command += ['-ar', '48000']

    if extension in _FASTSTART_EXTENSIONS:
        command += ['-movflags', '+faststart']

    command.append(output)

    return command


async def mux(
    movie: str, pcm: str, pcm_bytes: int, settings: audio.AudioSettings
) -> bool:
    """Adds the sound to the movie in place. Returns True if it was added."""

    binary = mediafiles.find_ffmpeg()[0]
    if not binary:
        logging.error('cannot add the sound to a movie: ffmpeg is not available')
        return False

    if not os.path.isfile(movie) or pcm_bytes < settings.sample_rate:  # < 0.5 s
        return False

    duration, has_audio = await probe(binary, movie)
    if has_audio:
        logging.debug(f'movie {movie} has sound already')
        return False

    directory, name = os.path.split(movie)
    stem, extension = os.path.splitext(name)
    output = os.path.join(directory, f'.{stem}.audio{extension}')

    command = build_command(
        binary,
        movie,
        pcm,
        output,
        video_duration=duration,
        pcm_bytes=pcm_bytes,
        settings=settings,
    )

    code, error = await _run(command)
    if code != 0 or not os.path.isfile(output):
        logging.error(f'could not add the sound to movie {movie}: {error.strip()}')
        with contextlib.suppress(OSError):
            os.remove(output)

        return False

    try:
        with contextlib.suppress(OSError):
            os.chmod(output, os.stat(movie).st_mode & 0o7777)

        os.replace(output, movie)

    except OSError as e:
        logging.error(f'could not replace movie {movie}: {e}')
        with contextlib.suppress(OSError):
            os.remove(output)

        return False

    logging.debug(f'added the sound to movie {movie}')

    return True
