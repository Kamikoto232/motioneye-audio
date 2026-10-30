from __future__ import annotations

import array
import asyncio
import math
import shutil
import unittest
from unittest import mock

from motioneye import audio, audiocapture


def _pcm(amplitude: int, samples: int = 1600) -> bytes:
    return array.array('h', [amplitude] * samples).tobytes()


class LevelTest(unittest.TestCase):
    def test_silence_is_the_floor(self):
        self.assertEqual(audio.DB_FLOOR, audiocapture.block_db(_pcm(0)))
        self.assertEqual(audio.DB_FLOOR, audiocapture.block_db(b''))

    def test_full_scale_is_zero_db(self):
        self.assertAlmostEqual(0.0, audiocapture.block_db(_pcm(-32768)), places=3)

    def test_half_the_amplitude_is_six_db_lower(self):
        self.assertAlmostEqual(
            20 * math.log10(0.5), audiocapture.block_db(_pcm(16384)), places=2
        )

    def test_a_partial_trailing_sample_is_ignored(self):
        self.assertAlmostEqual(
            audiocapture.block_db(_pcm(1000)),
            audiocapture.block_db(_pcm(1000) + b'\x01'),
        )


class CaptureTest(unittest.IsolatedAsyncioTestCase):
    def _capture(self) -> audiocapture.AudioCapture:
        capture = audiocapture.AudioCapture(
            1, {'@id': 1, '@audio_enabled': True, '@audio_quality': 'low'}
        )
        capture.input_arguments = [
            '-re',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=440:duration=4',
        ]

        return capture

    def test_level_is_unknown_before_any_sound(self):
        capture = audiocapture.AudioCapture(1, {'@id': 1})

        self.assertIsNone(capture.level)
        self.assertIsNone(capture.get_state().level)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is not installed')
    async def test_measures_a_sine_wave(self):
        capture = self._capture()
        capture.start()
        try:
            await asyncio.sleep(1.5)
            level = capture.level
            peak = capture.peak
        finally:
            await capture.stop()

        # ffmpeg's sine is 1/8 of the full scale, i.e. about -21 dB RMS
        self.assertIsNotNone(level)
        self.assertAlmostEqual(-21, level, delta=2)
        self.assertIsNotNone(peak)

    async def test_update_starts_and_stops_captures(self):
        config_a = {
            '@id': 1,
            '@enabled': True,
            '@audio_enabled': True,
            '@audio_source': 'hw:1,0',
            'videodevice': '/dev/video0',
        }
        configs = {1: config_a}

        with (
            mock.patch.object(audiocapture.config, 'get_camera_ids', lambda: list(configs)),
            mock.patch.object(audiocapture.config, 'get_camera', configs.get),
            mock.patch.object(audiocapture.AudioCapture, 'start'),
        ):
            await audiocapture._update()
            first = audiocapture.get_capture(1)
            self.assertIsNotNone(first)

            await audiocapture._update()  # nothing changed
            self.assertIs(first, audiocapture.get_capture(1))

            configs[1] = {**config_a, '@audio_source': 'hw:2,0'}
            await audiocapture._update()
            self.assertIsNot(first, audiocapture.get_capture(1))

            configs[1] = {**config_a, '@audio_enabled': False}
            await audiocapture._update()
            self.assertIsNone(audiocapture.get_capture(1))


if __name__ == '__main__':
    unittest.main()
