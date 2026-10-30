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

"""Audio settings of a camera.

All audio options live in the camera config as ``@audio_*`` comments (the same
way as the other motionEye specific options), so motion itself never sees them.
The runtime part (capturing, metering, recording, streaming) is implemented in
``audiocapture`` and ``audiostream``.
"""

from __future__ import annotations

import base64
import http.client
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

__all__ = (
    'AUDIO_FORMATS',
    'AUDIO_QUALITIES',
    'DB_FLOOR',
    'AudioSettings',
    'default_settings',
    'get_settings',
    'input_arguments',
    'merge_stream_authentication',
    'probe_content_type',
    'raw_pcm_arguments',
    'settings_to_ui',
    'source_of',
    'stream_credentials_to_ui',
    'ui_to_settings',
)

# the level reported for digital silence, and the lowest selectable threshold
DB_FLOOR = -90

# quality name -> (sample rate in Hz, encoder bitrate in kbit/s)
AUDIO_QUALITIES: dict[str, tuple[int, int]] = {
    'low': (16000, 48),
    'medium': (32000, 96),
    'high': (48000, 192),
}

# format name -> (ffmpeg encoder, ffmpeg muxer, mime type, file extension)
AUDIO_FORMATS: dict[str, tuple[str, str, str, str]] = {
    'aac': ('aac', 'adts', 'audio/aac', 'aac'),
    'mp3': ('libmp3lame', 'mp3', 'audio/mpeg', 'mp3'),
    'opus': ('libopus', 'ogg', 'audio/ogg', 'ogg'),
    'wav': ('pcm_s16le', 'wav', 'audio/wav', 'wav'),
}

STREAM_AUTH_MODES = ('disabled', 'basic')

_SOURCE_ALSA_RE = re.compile(r'^(alsa:)?(hw|plughw|sysdefault|default)(:[\w.,=-]+)?$')
_SOURCE_PULSE_RE = re.compile(r'^pulse(:[\w.,=-]*)?$')


@dataclass(frozen=True)
class AudioSettings:
    enabled: bool = False
    source: str = ''
    quality: str = 'medium'
    format: str = 'aac'
    record: bool = True
    trigger: bool = False
    threshold: int = -30
    show_level: bool = True
    streaming: bool = False
    streaming_format: str = 'mp3'
    streaming_port: int = 0
    streaming_auth_mode: str = 'disabled'
    streaming_authentication: str = ''

    @property
    def sample_rate(self) -> int:
        return AUDIO_QUALITIES[self.quality][0]

    @property
    def bitrate(self) -> int:
        return AUDIO_QUALITIES[self.quality][1]


def default_settings(camera_id: int) -> dict:
    """Defaults for the camera config (data) keys."""

    return {
        '@audio_enabled': False,
        '@audio_source': '',
        '@audio_quality': 'medium',
        '@audio_format': 'aac',
        '@audio_record': True,
        '@audio_trigger': False,
        '@audio_threshold': -30,
        '@audio_show_level': True,
        '@audio_streaming': False,
        '@audio_streaming_format': 'mp3',
        '@audio_streaming_port': 9180 + camera_id,
        '@audio_streaming_auth_mode': 'disabled',
        '@audio_streaming_authentication': '',
        '@streaming_audio': True,
    }


def _choice(value, allowed, default):
    return value if value in allowed else default


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.lower() in ('1', 'true', 'on', 'yes')

    return default if value is None else bool(value)


def _int(value, default: int, low: int, high: int) -> int:
    try:
        value = int(value)

    except (TypeError, ValueError):
        return default

    return max(low, min(high, value))


def get_settings(camera_config: dict) -> AudioSettings:
    """Reads the (sanitized) audio settings out of a camera config."""

    defaults = default_settings(int(camera_config.get('@id') or 0))

    def get(key):
        return camera_config.get(f'@{key}', defaults[f'@{key}'])

    return AudioSettings(
        enabled=_bool(get('audio_enabled'), False),
        source=str(get('audio_source') or '').strip(),
        quality=_choice(get('audio_quality'), AUDIO_QUALITIES, 'medium'),
        format=_choice(get('audio_format'), AUDIO_FORMATS, 'aac'),
        record=_bool(get('audio_record'), True),
        trigger=_bool(get('audio_trigger'), False),
        threshold=_int(get('audio_threshold'), -30, DB_FLOOR, 0),
        show_level=_bool(get('audio_show_level'), True),
        streaming=_bool(get('audio_streaming'), False),
        streaming_format=_choice(get('audio_streaming_format'), AUDIO_FORMATS, 'mp3'),
        streaming_port=_int(get('audio_streaming_port'), 0, 0, 65535),
        streaming_auth_mode=_choice(
            get('audio_streaming_auth_mode'), STREAM_AUTH_MODES, 'disabled'
        ),
        streaming_authentication=str(get('audio_streaming_authentication') or ''),
    )


def ui_to_settings(ui: dict, prev_config: dict) -> dict:
    """Converts the audio part of the UI dict to camera config (data) keys.

    A client that does not know about audio (e.g. an older motionEye hub)
    does not send any of the keys, so the stored values are kept.
    """

    prev = {**default_settings(int(prev_config.get('@id') or 0)), **prev_config}

    def get(key):
        return ui.get(key, prev[f'@{key}'])

    streaming_audio = ui.get('streaming_audio')
    if streaming_audio is None:
        streaming_audio = prev['@streaming_audio']

    else:
        streaming_audio = streaming_audio != 'without_audio'

    return {
        '@audio_enabled': _bool(get('audio_enabled'), False),
        '@audio_source': str(get('audio_source') or '').strip(),
        '@audio_quality': _choice(get('audio_quality'), AUDIO_QUALITIES, 'medium'),
        '@audio_format': _choice(get('audio_format'), AUDIO_FORMATS, 'aac'),
        '@audio_record': _bool(get('audio_record'), True),
        '@audio_trigger': _bool(get('audio_trigger'), False),
        '@audio_threshold': _int(get('audio_threshold'), -30, DB_FLOOR, 0),
        '@audio_show_level': _bool(get('audio_show_level'), True),
        '@audio_streaming': _bool(get('audio_streaming'), False),
        '@audio_streaming_format': _choice(
            get('audio_streaming_format'), AUDIO_FORMATS, 'mp3'
        ),
        '@audio_streaming_port': _int(get('audio_streaming_port'), 0, 1024, 65535),
        '@audio_streaming_auth_mode': _choice(
            get('audio_streaming_auth_mode'), STREAM_AUTH_MODES, 'disabled'
        ),
        '@audio_streaming_authentication': prev['@audio_streaming_authentication'],
        '@streaming_audio': _bool(streaming_audio, True),
    }


def settings_to_ui(data: dict) -> dict:
    settings = get_settings(data)

    return {
        'audio_enabled': settings.enabled,
        'audio_source': settings.source,
        'audio_quality': settings.quality,
        'audio_format': settings.format,
        'audio_record': settings.record,
        'audio_trigger': settings.trigger,
        'audio_threshold': settings.threshold,
        'audio_show_level': settings.show_level,
        'audio_streaming': settings.streaming,
        'audio_streaming_format': settings.streaming_format,
        'audio_streaming_port': settings.streaming_port,
        'audio_streaming_auth_mode': settings.streaming_auth_mode,
        'streaming_audio': (
            'with_audio'
            if _bool(data.get('@streaming_audio', True), True)
            else 'without_audio'
        ),
    }


def merge_stream_authentication(ui: dict, prev_config: dict) -> str:
    """Builds the user:password pair of the audio stream; the UI omits the
    unchanged password, same as for the video stream."""

    prev = str(prev_config.get('@audio_streaming_authentication') or '')
    prev_username, _sep, prev_password = prev.partition(':')

    username = ui.get('audio_streaming_username') or prev_username
    password = ui.get('audio_streaming_password') or prev_password

    if username and password and ':' not in str(username):
        return f'{username}:{password}'

    return ''


def stream_credentials_to_ui(data: dict) -> dict:
    username, _sep, password = str(
        data.get('@audio_streaming_authentication') or ''
    ).partition(':')

    ui = {'audio_streaming_username': username}
    # an empty string shows an empty input box, nothing shows "*****"
    if password == '':  # nosec: B105
        ui['audio_streaming_password'] = ''  # nosec: B105

    return ui


def source_of(camera_config: dict) -> str | None:
    """The audio source of a camera (with the credentials of a network camera
    in the URL), or None if the camera has no audio source."""

    source = get_settings(camera_config).source
    if source:
        return source

    url = camera_config.get('netcam_url') or ''
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ('rtsp', 'rtsps', 'rtmp', 'rtmps', 'http', 'https'):
        return None

    userpass = camera_config.get('netcam_userpass')
    if userpass and '@' not in urlsplit(url).netloc:
        parts = urlsplit(url)
        url = urlunsplit(parts._replace(netloc=f'{userpass}@{parts.netloc}'))

    return url


def input_arguments(camera_config: dict) -> list[str] | None:
    """The ffmpeg arguments (before the output) that open the audio source of
    a camera, or None if the camera has no audio source."""

    source = source_of(camera_config)

    return _source_to_arguments(source) if source else None


def probe_content_type(url: str, timeout: float = 5.0) -> str | None:
    """The Content-Type that an HTTP audio source answers with, if it answers.

    A live stream never ends, so only the headers are read.
    """

    parts = urlsplit(url)
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        return None

    connection_class = (
        http.client.HTTPSConnection
        if parts.scheme.lower() == 'https'
        else http.client.HTTPConnection
    )
    headers = {}
    if parts.username:
        credentials = f'{unquote(parts.username)}:{unquote(parts.password or "")}'
        headers['Authorization'] = (
            'Basic ' + base64.b64encode(credentials.encode()).decode()
        )

    connection = connection_class(parts.hostname, parts.port, timeout=timeout)
    try:
        path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
        connection.request('GET', path, headers=headers)
        return connection.getresponse().getheader('Content-Type')

    except (OSError, http.client.HTTPException):
        return None

    finally:
        connection.close()


def raw_pcm_arguments(content_type: str | None, url: str) -> list[str] | None:
    """The ffmpeg arguments for a source that streams bare PCM, e.g. the audio
    of the IPCamera Android app (``audio/l16;rate=11025;channels=1``).

    ffmpeg cannot detect such a stream, so its format has to be given. The
    samples are read as little endian, which is what the apps that serve them
    send, although L16 is big endian by definition.
    """

    mime, _sep, params = (content_type or '').partition(';')
    if mime.strip().lower() != 'audio/l16':
        return None

    rate = re.search(r'rate\s*=\s*(\d+)', params, re.IGNORECASE)
    channels = re.search(r'channels\s*=\s*(\d+)', params, re.IGNORECASE)

    return [
        '-f',
        's16le',
        '-ar',
        rate.group(1) if rate else '44100',
        '-ac',
        channels.group(1) if channels else '1',
        '-i',
        url,
    ]


def _source_to_arguments(source: str) -> list[str]:
    if _SOURCE_PULSE_RE.match(source):
        return ['-f', 'pulse', '-i', source.partition(':')[2] or 'default']

    alsa = _SOURCE_ALSA_RE.match(source)
    if alsa:
        return ['-f', 'alsa', '-i', source.removeprefix('alsa:')]

    arguments = []
    scheme = urlsplit(source).scheme.lower()
    if scheme in ('rtsp', 'rtsps'):
        arguments += ['-rtsp_transport', 'tcp']

    return arguments + ['-i', source]
