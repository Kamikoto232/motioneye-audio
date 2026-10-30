from __future__ import annotations

import array
import asyncio
import base64
import socket
import unittest
from unittest import mock

from tornado.httpclient import AsyncHTTPClient

from motioneye import audiocapture, audioserver


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))

        return s.getsockname()[1]


def _basic(userpass: str) -> dict:
    return {'Authorization': 'Basic ' + base64.b64encode(userpass.encode()).decode()}


class CredentialsTest(unittest.TestCase):
    def test_matching_credentials(self):
        self.assertTrue(
            audioserver.check_credentials(_basic('u:p')['Authorization'], 'u:p')
        )

    def test_wrong_credentials(self):
        header = _basic('u:wrong')['Authorization']

        self.assertFalse(audioserver.check_credentials(header, 'u:p'))

    def test_missing_malformed_or_other_scheme(self):
        self.assertFalse(audioserver.check_credentials(None, 'u:p'))
        self.assertFalse(audioserver.check_credentials('Basic !!!', 'u:p'))
        self.assertFalse(audioserver.check_credentials('Bearer abc', 'u:p'))

    def test_no_credentials_configured_never_matches(self):
        self.assertFalse(
            audioserver.check_credentials(_basic(':')['Authorization'], '')
        )


class AudioServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = _free_port()
        self.config = {
            '@id': 1,
            '@enabled': True,
            '@audio_enabled': True,
            '@audio_quality': 'low',
            '@audio_streaming': True,
            '@audio_streaming_port': self.port,
            '@audio_streaming_auth_mode': 'basic',
            '@audio_streaming_authentication': 'listener:secret',
            'netcam_url': 'rtsp://camera/stream',
        }
        self.capture = audiocapture.AudioCapture(1, self.config)
        audiocapture._captures[1] = self.capture
        patches = [
            mock.patch(
                'motioneye.config.get_camera', side_effect=lambda _id: self.config
            ),
            mock.patch('motioneye.config.get_camera_ids', return_value=[1]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        audioserver.sync([(1, self.config)])
        self.addCleanup(audioserver.stop)
        self.addCleanup(audiocapture._captures.pop, 1, None)

    async def _get(self, path='/', headers=None):
        return await AsyncHTTPClient().fetch(
            f'http://127.0.0.1:{self.port}{path}',
            headers=headers or {},
            raise_error=False,
        )

    async def test_credentials_are_required(self):
        response = await self._get()

        self.assertEqual(401, response.code)
        self.assertIn('Basic', response.headers['WWW-Authenticate'])

    async def test_wrong_credentials(self):
        response = await self._get(headers=_basic('listener:nope'))

        self.assertEqual(401, response.code)

    async def test_the_sound_is_served(self):
        pcm = array.array('h', [3000] * (self.capture.block_size // 2)).tobytes()

        async def feed():
            await asyncio.sleep(0.3)
            for _ in range(3):
                self.capture._publish(pcm)
            self.capture._publish(None)

        feeder = asyncio.ensure_future(feed())
        response = await self._get('/?format=wav', headers=_basic('listener:secret'))
        await feeder

        self.assertEqual(200, response.code)
        self.assertEqual('audio/wav', response.headers['Content-Type'])
        self.assertEqual(44 + 3 * self.capture.block_size, len(response.body))

    async def test_level(self):
        self.capture._on_block(
            array.array('h', [3276] * (self.capture.block_size // 2)).tobytes()
        )

        response = await self._get('/level', headers=_basic('listener:secret'))

        self.assertEqual(200, response.code)
        self.assertIn(b'"level": -', response.body)

    async def test_no_authentication(self):
        self.config['@audio_streaming_auth_mode'] = 'disabled'

        response = await self._get('/level')

        self.assertEqual(200, response.code)

    async def test_streaming_switched_off(self):
        self.config['@audio_streaming'] = False

        response = await self._get('/level')

        self.assertEqual(404, response.code)

    async def test_the_server_follows_the_settings(self):
        self.assertIn(1, audioserver._servers)

        self.config['@audio_streaming'] = False
        audioserver.sync([(1, self.config)])

        self.assertNotIn(1, audioserver._servers)


if __name__ == '__main__':
    unittest.main()
