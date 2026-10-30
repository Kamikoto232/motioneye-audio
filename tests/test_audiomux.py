from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from motioneye import audio, audiocapture, audiomux

_FFMPEG = shutil.which('ffmpeg')


class ContainerFormatTest(unittest.TestCase):
    def test_the_chosen_format_is_used_when_the_container_holds_it(self):
        self.assertEqual('mp3', audiomux.container_format('.mp4', 'mp3'))
        self.assertEqual('opus', audiomux.container_format('.mkv', 'opus'))
        self.assertEqual('wav', audiomux.container_format('.MKV', 'wav'))

    def test_another_format_is_used_when_it_does_not(self):
        self.assertEqual('aac', audiomux.container_format('.mp4', 'opus'))
        self.assertEqual('aac', audiomux.container_format('.mp4', 'wav'))
        self.assertEqual('opus', audiomux.container_format('.webm', 'aac'))
        self.assertEqual('mp3', audiomux.container_format('.avi', 'opus'))

    def test_unknown_container(self):
        self.assertEqual('aac', audiomux.container_format('.xyz', 'mp3'))


class ProbeParsingTest(unittest.TestCase):
    def test_duration_and_audio(self):
        output = (
            '  Duration: 00:01:02.50, start: 0.000000, bitrate: 1000 kb/s\n'
            '  Stream #0:0: Video: h264, yuv420p, 640x480\n'
            '  Stream #0:1(und): Audio: aac, 16000 Hz, mono\n'
        )

        self.assertEqual((62.5, True), audiomux._parse_probe(output))

    def test_no_audio_and_no_duration(self):
        self.assertEqual(
            (None, False),
            audiomux._parse_probe('  Stream #0:0: Video: mpeg4, 640x480\n'),
        )


class CommandTest(unittest.TestCase):
    def _command(self, movie_duration, pcm_seconds, movie='/m/a.mp4', quality='low'):
        settings = audio.get_settings(
            {'@audio_quality': quality, '@audio_format': 'aac'}
        )

        return audiomux.build_command(
            'ffmpeg',
            movie,
            '/m/.a.pcm',
            '/m/.a.audio.mp4',
            video_duration=movie_duration,
            pcm_bytes=int(pcm_seconds * settings.sample_rate * 2),
            settings=settings,
        )

    def test_shorter_sound_is_delayed(self):
        command = self._command(10, 7)

        self.assertEqual('3.000', command[command.index('-itsoffset') + 1])
        self.assertNotIn('-ss', command)

    def test_longer_sound_is_trimmed_at_the_start(self):
        command = self._command(10, 14)

        self.assertEqual('4.000', command[command.index('-ss') + 1])
        self.assertEqual('10.000', command[command.index('-t') + 1])

    def test_matching_sound_is_not_shifted(self):
        command = self._command(10, 10.02)

        self.assertNotIn('-itsoffset', command)
        self.assertNotIn('-ss', command)
        self.assertEqual('10.020', command[command.index('-t') + 1])

    def test_codec_and_faststart(self):
        command = self._command(10, 10)

        self.assertEqual('aac', command[command.index('-c:a') + 1])
        self.assertEqual('48k', command[command.index('-b:a') + 1])
        self.assertIn('+faststart', command)

    def test_wav_has_no_bitrate_and_opus_gets_a_valid_rate(self):
        def build(**settings):
            return audiomux.build_command(
                'ffmpeg',
                '/m/a.mkv',
                '/p',
                '/o.mkv',
                video_duration=None,
                pcm_bytes=64000,
                settings=audio.get_settings(settings),
            )

        wav = build(**{'@audio_format': 'wav'})
        opus = build(**{'@audio_format': 'opus', '@audio_quality': 'medium'})

        self.assertNotIn('-b:a', wav)
        self.assertEqual('pcm_s16le', wav[wav.index('-c:a') + 1])
        self.assertEqual('48000', opus[opus.index('-ar', opus.index('-c:a')) + 1])


@unittest.skipUnless(_FFMPEG, 'ffmpeg is not installed')
class MuxTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _make_movie(self, name, seconds):
        path = os.path.join(self.dir, name)
        subprocess.run(
            [
                _FFMPEG,
                '-v',
                'error',
                '-f',
                'lavfi',
                '-i',
                f'testsrc=duration={seconds}:size=160x120:rate=10',
                '-pix_fmt',
                'yuv420p',
                path,
            ],
            check=True,
        )

        return path

    def _make_pcm(self, seconds, rate):
        path = os.path.join(self.dir, 'a.pcm')
        subprocess.run(
            [
                _FFMPEG,
                '-v',
                'error',
                '-f',
                'lavfi',
                '-i',
                f'sine=frequency=440:duration={seconds}:sample_rate={rate}',
                '-f',
                's16le',
                '-ac',
                '1',
                path,
            ],
            check=True,
        )

        return path, os.path.getsize(path)

    async def _mux(self, movie_name, movie_seconds, pcm_seconds, **settings):
        movie = self._make_movie(movie_name, movie_seconds)
        settings = audio.get_settings({'@audio_quality': 'low', **settings})
        pcm, size = self._make_pcm(pcm_seconds, settings.sample_rate)

        added = await audiomux.mux(movie, pcm, size, settings)
        duration, has_audio = await audiomux.probe(_FFMPEG, movie)

        return added, duration, has_audio, movie

    async def test_adds_sound_to_an_mp4(self):
        added, duration, has_audio, _movie = await self._mux('a.mp4', 4, 6)

        self.assertTrue(added)
        self.assertTrue(has_audio)
        self.assertAlmostEqual(4, duration, delta=0.3)
        self.assertEqual(['a.mp4', 'a.pcm'], sorted(os.listdir(self.dir)))

    async def test_shorter_sound_does_not_change_the_movie_duration(self):
        added, duration, has_audio, _movie = await self._mux('a.mp4', 5, 2)

        self.assertTrue(added)
        self.assertTrue(has_audio)
        self.assertAlmostEqual(5, duration, delta=0.3)

    async def test_mkv_with_opus(self):
        added, _duration, has_audio, _movie = await self._mux(
            'a.mkv', 3, 3, **{'@audio_format': 'opus'}
        )

        self.assertTrue(added)
        self.assertTrue(has_audio)

    async def test_a_movie_with_sound_is_left_alone(self):
        _added, _duration, _has_audio, movie = await self._mux('a.mp4', 3, 3)
        before = os.path.getsize(movie)
        pcm, size = self._make_pcm(3, 16000)

        added = await audiomux.mux(
            movie, pcm, size, audio.get_settings({'@audio_quality': 'low'})
        )

        self.assertFalse(added)
        self.assertEqual(before, os.path.getsize(movie))

    async def test_a_failure_keeps_the_movie(self):
        movie = self._make_movie('a.mp4', 2)
        before = Path(movie).read_bytes()
        pcm, size = self._make_pcm(2, 16000)

        with mock.patch.object(audiomux, '_run', mock.AsyncMock()) as run:
            run.side_effect = [(0, 'Duration: 00:00:02.00'), (1, 'boom')]
            added = await audiomux.mux(
                movie, pcm, size, audio.get_settings({'@audio_quality': 'low'})
            )

        self.assertFalse(added)
        self.assertEqual(before, Path(movie).read_bytes())
        self.assertEqual(['a.mp4', 'a.pcm'], sorted(os.listdir(self.dir)))


class RecordingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _capture(self, **config):
        return audiocapture.AudioCapture(
            1,
            {
                '@id': 1,
                '@audio_enabled': True,
                '@audio_quality': 'low',
                'target_dir': self.dir,
                'pre_capture': 0,
                **config,
            },
        )

    def _block(self, capture, value=100):
        capture._on_block(bytes([value % 256, 0]) * (capture.block_size // 2))

    async def test_the_sound_from_before_the_event_is_kept(self):
        capture = self._capture()
        for _ in range(5):
            self._block(capture)

        capture.event_started()
        self.assertTrue(capture.has_recording())
        self._block(capture)

        self.assertEqual(6 * capture.block_size, capture._recording.size)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, '.audio-1.pcm')))

    async def test_no_recording_when_it_is_off(self):
        capture = self._capture(**{'@audio_record': False})
        capture.event_started()

        self.assertFalse(capture.has_recording())

    async def test_the_movie_gets_the_sound_and_the_recording_ends_with_the_event(self):
        capture = self._capture()
        capture.event_started()
        self._block(capture)

        with mock.patch.object(
            audiocapture.audiomux, 'mux', mock.AsyncMock(return_value=True)
        ) as mux:
            capture.event_ended()
            self.assertTrue(await capture.movie_ended('/m/a.mp4'))

        self.assertEqual(
            ('/m/a.mp4', os.path.join(self.dir, '.audio-1.pcm'), capture.block_size),
            mux.await_args.args[:3],
        )
        self.assertFalse(capture.has_recording())
        self.assertFalse(os.path.exists(os.path.join(self.dir, '.audio-1.pcm')))

    async def test_the_recording_goes_on_while_the_event_does(self):
        capture = self._capture()
        capture.event_started()
        self._block(capture)

        with mock.patch.object(
            audiocapture.audiomux, 'mux', mock.AsyncMock(return_value=True)
        ):
            await capture.movie_ended('/m/a.mp4')  # a part of a long event

        self.assertTrue(capture.has_recording())
        capture.event_ended()
        self.assertTrue(capture.has_recording())  # the last part may still follow

        capture._close_recording()
        self.assertFalse(capture.has_recording())

    async def test_a_stale_recording_is_removed(self):
        path = os.path.join(self.dir, '.audio-1.pcm')
        Path(path).write_bytes(b'x')

        self._capture()

        self.assertFalse(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
