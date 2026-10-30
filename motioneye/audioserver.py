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

"""The audio stream of a camera on a port of its own, for other programs.

This is the counterpart of the stream port of the video stream: one port per
camera, with optional basic authentication, and no motionEye login. It serves
the sound at ``/`` and the picture with the sound at ``/video``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import logging
from typing import TYPE_CHECKING

from tornado.httpserver import HTTPServer
from tornado.web import Application, HTTPError

from motioneye import audio, config
from motioneye.handlers.audio import AudioHandler

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ('AudioServerHandler', 'check_credentials', 'stop', 'sync')

_REALM = 'motionEye audio'

# camera id -> (settings the server depends on, the server)
_servers: dict[int, tuple[tuple, HTTPServer]] = {}


def check_credentials(authorization: str | None, expected: str) -> bool:
    """Tells if an Authorization header carries the expected user:password."""

    if not authorization or not expected:
        return False

    scheme, _sep, value = authorization.partition(' ')
    if scheme.lower() != 'basic':
        return False

    try:
        given = base64.b64decode(value.strip(), validate=True)

    except (binascii.Error, ValueError):
        return False

    return hmac.compare_digest(given, expected.encode())


class AudioServerHandler(AudioHandler):
    def initialize(self, camera_id: int) -> None:  # type: ignore[override]
        super().initialize()
        self.camera_id = camera_id

    async def get(self, op: str = 'stream') -> None:  # type: ignore[override]
        camera_config = config.get_camera(self.camera_id)
        settings = audio.get_settings(camera_config)

        if not settings.streaming:
            raise HTTPError(404, 'audio streaming is not enabled for this camera')

        if settings.streaming_auth_mode == 'basic' and not check_credentials(
            self.request.headers.get('Authorization'),
            settings.streaming_authentication,
        ):
            self.set_header('WWW-Authenticate', f'Basic realm="{_REALM}"')
            raise HTTPError(401, 'unauthorized')

        await self.serve(self.camera_id, camera_config, op)


def _make_app(camera_id: int) -> Application:
    kwargs = {'camera_id': camera_id}

    return Application(
        [
            (r'^/?$', AudioServerHandler, kwargs),
            (r'^/(?P<op>stream)/?$', AudioServerHandler, kwargs),
            (r'^/(?P<op>level)/?$', AudioServerHandler, kwargs),
            (r'^/(?P<op>video)/?$', AudioServerHandler, kwargs),
        ]
    )


def _close(server: HTTPServer) -> None:
    server.stop()
    # the streams that are running end as well
    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().create_task(server.close_all_connections())


def sync(wanted: Iterable[tuple[int, dict]]) -> None:
    """Starts, restarts and stops the servers to match the camera configs."""

    wanted_settings: dict[int, tuple] = {}
    for camera_id, camera_config in wanted:
        settings = audio.get_settings(camera_config)
        if settings.streaming and 1024 <= settings.streaming_port <= 65535:
            wanted_settings[camera_id] = (
                settings.streaming_port,
                settings.streaming_auth_mode,
                settings.streaming_authentication,
            )

    for camera_id, (current, server) in list(_servers.items()):
        if wanted_settings.get(camera_id) != current:
            logging.info(f'audio stream server of camera {camera_id} stopped')
            _close(server)
            del _servers[camera_id]

    for camera_id, current in wanted_settings.items():
        if camera_id in _servers:
            continue

        server = HTTPServer(_make_app(camera_id))
        try:
            server.listen(current[0])

        except OSError as e:
            logging.error(
                f'could not start the audio stream server of camera {camera_id} '
                f'on port {current[0]}: {e}'
            )
            continue

        _servers[camera_id] = (current, server)
        logging.info(
            f'audio stream server of camera {camera_id} listening on port {current[0]}'
        )


def stop() -> None:
    for _current, server in _servers.values():
        _close(server)

    _servers.clear()
