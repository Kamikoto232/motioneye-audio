from __future__ import annotations

import array
import asyncio
import io
import shutil
import unittest
from unittest import mock

from PIL import Image

from motioneye import audio, audiocapture, audiostream


class WavHeaderTest(unittest.TestCase):
    def test_header_of_a_stream(self):
        header = audiostream.wav_header(16000)

        self.assertEqual(44, len(header))
        self.assertEqual(b'RIFF', header[:4])
        self.assertEqual(b'WAVEfmt ', header[8:16])
        self.assertEqual(b'data', header[36:40])
        self.assertEqual(b'\xff\xff\xff\xff', header[40:44])


class CodecArgumentsTest(unittest.TestCase):
    def test_bitrate_follows_the_quality(self):
        settings = audio.get_settings({'@audio_quality': 'high'})

        arguments = audiostream.codec_arguments(settings, 'mp3')

        self.assertEqual('libmp3lame', arguments[arguments.index('-c:a') + 1])
        self.assertEqual('192k', arguments[arguments.index('-b:a') + 1])

    def test_wav_has_no_bitrate(self):
        arguments = audiostream.codec_arguments(audio.get_settings({}), 'wav')

        self.assertNotIn('-b:a', arguments)

    def test_opus_gets_a_supported_sample_rate(self):
        arguments = audiostream.codec_arguments(
            audio.get_settings({'@audio_quality': 'medium'}), 'opus'
        )

        self.assertEqual('48000', arguments[arguments.index('-ar') + 1])


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new('RGB', (64, 48), (200, 30, 30)).save(out, 'JPEG')

    return out.getvalue()


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is not installed')
class VideoAudioStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_matroska_with_the_picture_and_the_sound(self):
        capture = audiocapture.AudioCapture(
            1, {'@id': 1, '@audio_enabled': True, '@audio_quality': 'low'}
        )
        jpeg = _jpeg()
        pcm = array.array('h', [4000] * (capture.block_size // 2)).tobytes()

        async def feed():
            for _ in range(30):
                capture._publish(pcm)
                await asyncio.sleep(0.1)
            capture._publish(None)

        with mock.patch.object(
            audiostream.mjpgclient,
            'get_jpg',
            side_effect=lambda _id: bytes(bytearray(jpeg)),
        ):
            feeder = asyncio.ensure_future(feed())
            chunks = audiostream.video_audio_stream(capture, 1, 10)

            async def read():
                data = b''
                async for chunk in chunks:
                    data += chunk

                return data

            try:
                data = await asyncio.wait_for(read(), 20)
            finally:
                await chunks.aclose()
                await feeder

        # EBML header of Matroska
        self.assertEqual(b'\x1a\x45\xdf\xa3', data[:4])
        self.assertIn(b'V_MJPEG', data)
        self.assertIn(b'A_AAC', data)

    async def test_no_picture(self):
        capture = audiocapture.AudioCapture(1, {'@id': 1})

        with (
            mock.patch.object(audiostream.mjpgclient, 'get_jpg', return_value=None),
            mock.patch.object(audiostream, '_FIRST_FRAME_TIMEOUT', 0.3),
            self.assertRaises(audiostream.StreamUnavailableError),
        ):
            async for _chunk in audiostream.video_audio_stream(capture, 1, 5):
                pass


if __name__ == '__main__':
    unittest.main()
