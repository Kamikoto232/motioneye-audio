from __future__ import annotations

import unittest

from motioneye import audio, config


class SettingsTest(unittest.TestCase):
    def test_defaults_for_a_camera_without_audio_keys(self):
        settings = audio.get_settings({'@id': 3})

        self.assertFalse(settings.enabled)
        self.assertEqual('medium', settings.quality)
        self.assertEqual('aac', settings.format)
        self.assertEqual(-30, settings.threshold)
        self.assertEqual(9183, audio.default_settings(3)['@audio_streaming_port'])

    def test_invalid_values_fall_back_to_defaults(self):
        settings = audio.get_settings(
            {
                '@audio_quality': 'ultra',
                '@audio_format': 'flac',
                '@audio_threshold': 'loud',
                '@audio_streaming_auth_mode': 'digest',
            }
        )

        self.assertEqual('medium', settings.quality)
        self.assertEqual('aac', settings.format)
        self.assertEqual(-30, settings.threshold)
        self.assertEqual('disabled', settings.streaming_auth_mode)

    def test_threshold_is_clamped(self):
        self.assertEqual(-90, audio.get_settings({'@audio_threshold': -500}).threshold)
        self.assertEqual(0, audio.get_settings({'@audio_threshold': 12}).threshold)

    def test_quality_sets_sample_rate_and_bitrate(self):
        settings = audio.get_settings({'@audio_quality': 'high'})

        self.assertEqual(48000, settings.sample_rate)
        self.assertEqual(192, settings.bitrate)

    def test_ui_round_trip(self):
        ui = {
            'audio_enabled': True,
            'audio_source': ' hw:1,0 ',
            'audio_quality': 'low',
            'audio_format': 'mp3',
            'audio_record': False,
            'audio_trigger': True,
            'audio_threshold': '-42',
            'audio_show_level': False,
            'audio_streaming': True,
            'audio_streaming_format': 'wav',
            'audio_streaming_port': '9555',
            'audio_streaming_auth_mode': 'basic',
            'streaming_audio': 'without_audio',
        }

        data = audio.ui_to_settings(ui, {'@id': 1})
        back = audio.settings_to_ui(data)

        self.assertEqual('hw:1,0', back['audio_source'])
        self.assertEqual(-42, back['audio_threshold'])
        self.assertEqual(9555, back['audio_streaming_port'])
        self.assertEqual('without_audio', back['streaming_audio'])
        for key, value in ui.items():
            if key not in ('audio_source', 'audio_threshold', 'audio_streaming_port'):
                self.assertEqual(value, back[key], key)

    def test_an_older_client_keeps_the_stored_values(self):
        stored = {'@id': 1, '@audio_enabled': True, '@audio_threshold': -20}

        data = audio.ui_to_settings({}, stored)

        self.assertTrue(data['@audio_enabled'])
        self.assertEqual(-20, data['@audio_threshold'])
        self.assertTrue(data['@streaming_audio'])

    def test_stream_credentials_keep_the_unchanged_password(self):
        prev = {'@audio_streaming_authentication': 'user:secret'}

        self.assertEqual(
            'user:secret',
            audio.merge_stream_authentication(
                {'audio_streaming_username': 'user'}, prev
            ),
        )
        self.assertEqual(
            'other:new',
            audio.merge_stream_authentication(
                {
                    'audio_streaming_username': 'other',
                    'audio_streaming_password': 'new',
                },
                prev,
            ),
        )
        self.assertEqual('', audio.merge_stream_authentication({}, {}))

    def test_stream_password_is_not_sent_to_the_ui(self):
        ui = audio.stream_credentials_to_ui(
            {'@audio_streaming_authentication': 'user:secret'}
        )

        self.assertEqual({'audio_streaming_username': 'user'}, ui)


class SourceTest(unittest.TestCase):
    def test_no_source_for_a_device_camera(self):
        self.assertIsNone(audio.input_arguments({'videodevice': '/dev/video0'}))

    def test_rtsp_netcam_is_used_by_default(self):
        arguments = audio.input_arguments(
            {'netcam_url': 'rtsp://cam/stream', 'netcam_userpass': 'u:p%40'}
        )

        self.assertEqual(
            ['-rtsp_transport', 'tcp', '-i', 'rtsp://u:p%40@cam/stream'], arguments
        )

    def test_mjpeg_netcam_without_sound_is_not_used(self):
        self.assertIsNone(audio.input_arguments({'netcam_url': 'mjpeg://cam/'}))

    def test_alsa_and_pulse_sources(self):
        self.assertEqual(
            ['-f', 'alsa', '-i', 'hw:1,0'],
            audio.input_arguments({'@audio_source': 'hw:1,0'}),
        )
        self.assertEqual(
            ['-f', 'alsa', '-i', 'plughw:2'],
            audio.input_arguments({'@audio_source': 'alsa:plughw:2'}),
        )
        self.assertEqual(
            ['-f', 'pulse', '-i', 'default'],
            audio.input_arguments({'@audio_source': 'pulse'}),
        )
        self.assertEqual(
            ['-f', 'pulse', '-i', 'alsa_input.usb'],
            audio.input_arguments({'@audio_source': 'pulse:alsa_input.usb'}),
        )

    def test_explicit_source_wins_over_the_camera(self):
        arguments = audio.input_arguments(
            {'@audio_source': 'rtsp://mic/audio', 'videodevice': '/dev/video0'}
        )

        self.assertEqual(
            ['-rtsp_transport', 'tcp', '-i', 'rtsp://mic/audio'], arguments
        )


class ConfigTest(unittest.TestCase):
    def test_audio_keys_are_in_the_camera_defaults(self):
        data = {'camera_name': 'cam', '@id': 2, 'videodevice': '/dev/video0'}
        config._set_default_motion_camera(2, data)

        self.assertFalse(data['@audio_enabled'])
        self.assertEqual(9182, data['@audio_streaming_port'])
        self.assertTrue(data['@streaming_audio'])

        ui = audio.settings_to_ui(data)

        self.assertFalse(ui['audio_enabled'])
        self.assertEqual('with_audio', ui['streaming_audio'])


if __name__ == '__main__':
    unittest.main()
