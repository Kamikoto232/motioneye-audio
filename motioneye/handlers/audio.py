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

from __future__ import annotations

import logging

from tornado.iostream import StreamClosedError
from tornado.web import HTTPError

from motioneye import audio, audiocapture, audiostream, config, remote, utils
from motioneye.handlers.base import BaseHandler

__all__ = ('AudioHandler',)


class AudioHandler(BaseHandler):
    """Live sound of a camera: an audio stream, a picture+sound stream and the
    current level. They are in addition to (and do not change) the picture
    API of the original motionEye."""

    def initialize(self) -> None:
        self._closed = False

    def on_connection_close(self) -> None:
        self._closed = True

    def compute_etag(self):
        return None

    @BaseHandler.auth()
    @BaseHandler.peer_allowed()
    async def get(self, camera_id: str, op: str) -> None:
        camera_id_int = int(camera_id)
        if camera_id_int not in config.get_camera_ids():
            raise HTTPError(404, 'no such camera')

        camera_config: dict = config.get_camera(camera_id_int)

        # block access to admin-only cameras for non-admin users
        if camera_config.get('@admin_only') and self.current_user not in [
            'admin',
            'peer',
        ]:
            raise HTTPError(
                403,
                f'GET access denied to admin-only camera "{camera_id}" for operation "{op}"',
            )

        fmt = self.get_argument('format', None)
        if fmt is not None and fmt not in audio.AUDIO_FORMATS:
            raise HTTPError(400, 'unknown format')

        if utils.is_remote_camera(camera_config):
            await self.proxy(camera_config, op, fmt)
            return

        if not utils.is_local_motion_camera(camera_config):
            raise HTTPError(400, 'no audio for this camera')

        capture = audiocapture.get_capture(camera_id_int)
        if capture is None:
            raise HTTPError(404, 'audio is not enabled for this camera')

        if op == 'level':
            state = capture.get_state()
            self.set_header('Cache-Control', 'no-store')
            self.finish_json(
                {
                    'level': state.level,
                    'peak': state.peak,
                    'exceeded': state.exceeded,
                    'threshold': capture.settings.threshold,
                }
            )

        elif op == 'stream':
            fmt = fmt or capture.settings.streaming_format
            await self.stream(audiostream.audio_stream(capture, fmt), fmt)

        elif op == 'video':
            try:
                framerate = float(camera_config.get('stream_maxrate') or 5)

            except (TypeError, ValueError):
                framerate = 5.0

            await self.stream(
                audiostream.video_audio_stream(capture, camera_id_int, framerate),
                None,
            )

        else:
            raise HTTPError(400, 'unknown operation')

    async def stream(self, chunks, fmt: str | None) -> None:
        self.set_header(
            'Content-Type',
            audiostream.content_type(fmt) if fmt else audiostream.VIDEO_AUDIO_CONTENT_TYPE,
        )
        self.set_header('Cache-Control', 'no-store, must-revalidate')
        self.set_header('Pragma', 'no-cache')
        self.set_header('Expires', '0')
        self.set_header('X-Accel-Buffering', 'no')

        started = False
        try:
            async for chunk in chunks:
                if self._closed:
                    break

                self.write(chunk)
                await self.flush()
                started = True

        except audiostream.StreamUnavailableError as e:
            if not started:
                raise HTTPError(503, str(e)) from e

        except StreamClosedError:
            pass

        finally:
            await chunks.aclose()

        if not self._closed:
            self.finish()

    async def proxy(self, local_config: dict, op: str, fmt: str | None) -> None:
        """Passes the stream of the camera of a remote motionEye server on."""

        if op not in ('stream', 'video'):
            raise HTTPError(400, 'unknown operation')

        failed: list[str] = []
        headers_sent = False

        def on_header(line: str) -> None:
            nonlocal failed
            line = line.strip()
            if line.startswith('HTTP/'):
                code = line.split(' ', 2)[1] if ' ' in line else ''
                failed = [] if code == '200' else [line]

            elif line.lower().startswith('content-type:') and not failed:
                self.set_header('Content-Type', line.split(':', 1)[1].strip())

        def on_chunk(chunk: bytes) -> None:
            nonlocal headers_sent
            if self._closed:
                # makes the request to the remote server end
                raise StreamClosedError()

            if failed:
                return

            headers_sent = True
            self.set_header('Cache-Control', 'no-store, must-revalidate')
            self.write(chunk)
            self.flush()

        error = await remote.stream_audio(
            local_config, op, {'format': fmt} if fmt else {}, on_header, on_chunk
        )

        if self._closed:
            return

        if failed or (error and not headers_sent):
            logging.error(
                f'could not get the {op} of remote camera {remote.pretty_camera_url(local_config)}: '
                f'{failed[0] if failed else error}'
            )
            raise HTTPError(502, 'could not get the audio of the remote camera')

        self.finish()
