from __future__ import annotations

import array
import shutil
import struct
import unittest
from json import loads

import tornado.testing
from tornado.ioloop import IOLoop

from motioneye import audiocapture
from motioneye.handlers.audio import AudioHandler
from tests.test_handlers import _FAKE_CAMERA_CONFIG, HandlerTestCase


def _block(capture, amplitude=3000):
    return array.array('h', [amplitude] * (capture.block_size // 2)).tobytes()


class AudioHandlerTest(HandlerTestCase):
    handler_cls = AudioHandler

    def setUp(self):
        super().setUp()
        self.capture = audiocapture.AudioCapture(
            1, {**_FAKE_CAMERA_CONFIG, '@audio_enabled': True, '@audio_quality': 'low'}
        )
        audiocapture._captures[1] = self.capture
        self.cookie = self.make_session_cookie('normal')

    def tearDown(self):
        audiocapture._captures.pop(1, None)
        _FAKE_CAMERA_CONFIG['@admin_only'] = False
        super().tearDown()

    def _feed(self, blocks, delay=0.3):
        # the stream starts listening when the request arrives
        def feed():
            for _ in range(blocks):
                self.capture._on_block(_block(self.capture))
            self.capture._publish(None)

        IOLoop.current().call_later(delay, feed)

    def test_login_is_required(self):
        self.assertEqual(403, self.fetch('/audio/1/level/').code)

    def test_unknown_camera(self):
        response = self.fetch('/audio/7/level/', headers={'Cookie': self.cookie})

        self.assertEqual(404, response.code)

    def test_no_capture_means_no_audio(self):
        audiocapture._captures.pop(1)

        response = self.fetch('/audio/1/stream/', headers={'Cookie': self.cookie})

        self.assertEqual(404, response.code)
        self.assertIn('not enabled', loads(response.body)['error'])

    def test_admin_only_camera_is_denied_to_a_normal_user(self):
        _FAKE_CAMERA_CONFIG['@admin_only'] = True

        response = self.fetch('/audio/1/level/', headers={'Cookie': self.cookie})

        self.assertEqual(403, response.code)

    def test_unknown_format(self):
        response = self.fetch(
            '/audio/1/stream/?format=flac', headers={'Cookie': self.cookie}
        )

        self.assertEqual(400, response.code)

    def test_level(self):
        self.capture._on_block(_block(self.capture, 3276))  # about -20 dB

        response = self.fetch('/audio/1/level/', headers={'Cookie': self.cookie})

        data = loads(response.body)
        self.assertEqual(200, response.code)
        self.assertAlmostEqual(-20, data['level'], delta=0.5)
        self.assertFalse(data['exceeded'])
        self.assertEqual(-30, data['threshold'])

    def test_wav_stream(self):
        self._feed(3)

        response = self.fetch(
            '/audio/1/stream/?format=wav', headers={'Cookie': self.cookie}
        )

        self.assertEqual(200, response.code)
        self.assertEqual('audio/wav', response.headers['Content-Type'])
        self.assertEqual(b'RIFF', response.body[:4])
        self.assertEqual(16000, struct.unpack('<I', response.body[24:28])[0])
        self.assertEqual(44 + 3 * self.capture.block_size, len(response.body))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is not installed')
    def test_mp3_stream_is_the_default_format_of_the_camera(self):
        self._feed(20)

        response = self.fetch('/audio/1/stream/', headers={'Cookie': self.cookie})

        self.assertEqual(200, response.code)
        self.assertEqual('audio/mpeg', response.headers['Content-Type'])
        self.assertGreater(len(response.body), 500)
        # MPEG audio frame sync
        self.assertEqual(0xFF, response.body[0])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is not installed')
    def test_opus_stream(self):
        self._feed(20)

        response = self.fetch(
            '/audio/1/stream/?format=opus', headers={'Cookie': self.cookie}
        )

        self.assertEqual('audio/ogg', response.headers['Content-Type'])
        self.assertEqual(b'OggS', response.body[:4])

    def test_the_stream_ends_with_the_capture(self):
        # nothing is sent after the capture stopped
        self._feed(0)

        response = self.fetch(
            '/audio/1/stream/?format=wav', headers={'Cookie': self.cookie}
        )

        self.assertEqual(44, len(response.body))


if __name__ == '__main__':
    tornado.testing.main()
