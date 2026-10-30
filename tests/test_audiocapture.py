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


class ThresholdTriggerTest(unittest.TestCase):
    def test_starts_above_and_ends_after_the_hold_time(self):
        trigger = audiocapture.ThresholdTrigger(-30, hold=5)

        self.assertIsNone(trigger.update(-50, 0))
        self.assertIsNone(trigger.update(-30, 1))  # not above
        self.assertEqual('start', trigger.update(-29.9, 2))
        self.assertIsNone(trigger.update(-20, 3))  # already started
        self.assertIsNone(trigger.update(-60, 4))
        self.assertIsNone(trigger.update(-60, 7.9))
        self.assertEqual('end', trigger.update(-60, 8))
        self.assertFalse(trigger.active)

    def test_loud_sound_extends_the_event(self):
        trigger = audiocapture.ThresholdTrigger(-30, hold=5)
        trigger.update(-10, 0)

        self.assertIsNone(trigger.update(-10, 4))
        self.assertIsNone(trigger.update(-60, 8))  # 4 s since the last loud one
        self.assertEqual('end', trigger.update(-60, 9))

    def test_exceeded_is_held_for_a_moment(self):
        trigger = audiocapture.ThresholdTrigger(-30, hold=60)

        self.assertFalse(trigger.exceeded(0))
        trigger.update(-10, 0)
        self.assertTrue(trigger.exceeded(1.9))
        self.assertFalse(trigger.exceeded(2.1))

    def test_reset_ends_an_active_event_only(self):
        trigger = audiocapture.ThresholdTrigger(-30, hold=5)
        self.assertIsNone(trigger.reset())

        trigger.update(-10, 0)
        self.assertEqual('end', trigger.reset())
        self.assertIsNone(trigger.reset())


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

    async def test_a_loud_block_starts_and_the_silence_ends_the_event(self):
        capture = audiocapture.AudioCapture(
            1,
            {
                '@id': 1,
                '@audio_enabled': True,
                '@audio_trigger': True,
                '@audio_threshold': -30,
                'event_gap': 2,
            },
        )
        loud, quiet = _pcm(8000, 1600), _pcm(0, 1600)

        with mock.patch.object(
            audiocapture.motionctl, 'trigger_action', mock.AsyncMock(return_value=None)
        ) as trigger_action:
            capture._on_block(loud)
            await asyncio.sleep(0.05)
            trigger_action.assert_awaited_once_with(1, 'eventstart')
            self.assertTrue(capture.get_state().exceeded)

            for _ in range(5):  # let the smoothing forget the loud block
                capture._on_block(quiet)

            capture.trigger.update(-90, capture.trigger._last_loud + 3)
            capture._fire('eventend')
            await asyncio.sleep(0.05)

        self.assertEqual(2, trigger_action.await_count)

    async def test_nothing_is_triggered_when_the_option_is_off(self):
        capture = audiocapture.AudioCapture(1, {'@id': 1, '@audio_enabled': True})

        with mock.patch.object(
            audiocapture.motionctl, 'trigger_action', mock.AsyncMock()
        ) as trigger_action:
            capture._on_block(_pcm(20000))
            await asyncio.sleep(0.05)

        trigger_action.assert_not_called()
        self.assertFalse(capture.get_state().exceeded)

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
