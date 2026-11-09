import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import placekeeper.plugins.kindle.registration as device


class RegistrationTests(unittest.TestCase):
    def test_reviewed_registration_saves_separate_private_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'.private').mkdir()
            source = root/'.private/amazon-device.json'
            source.write_text(json.dumps({'expires': time.time()+3600}))
            original = source.read_bytes()
            destination = root/'.private/new-device.json'
            auth = Mock(access_token='existing-secret')
            response = Mock(status_code=200)
            response.json.return_value = {'response': {'success': {
                'tokens': {'mac_dms': {'adp_token': 'new-secret', 'device_private_key': 'new-key'},
                           'bearer': {'access_token': 'bearer-secret', 'refresh_token': 'refresh-secret', 'expires_in': 3600}},
                'extensions': {'device_info': {'device_type': 'AGZZ1ST63LKBW'}}}}}
            with patch.object(device, 'default_data_root', return_value=root/'.private'), patch.object(device, 'DESTINATION', destination), \
                    patch.object(device.Authenticator, 'from_dict', return_value=auth), \
                    patch.object(device.httpx, 'post', return_value=response) as post:
                result = device.register(device.plan())
            self.assertTrue(result['registered'])
            self.assertNotIn('secret', json.dumps(result))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(post.call_args.args[0], 'https://api.amazon.com/auth/register')
            self.assertEqual(post.call_args.kwargs['json']['registration_data']['device_name'], 'Placekeeper')
            self.assertEqual(post.call_args.kwargs['json']['requested_token_type'], ['bearer', 'mac_dms'])

    def test_unreviewed_destination_rejected_before_using_credentials(self):
        plan = device.plan()
        plan['registration_endpoint'] = 'https://example.com/auth/register'
        with patch.object(device.httpx, 'post') as post:
            with self.assertRaisesRegex(ValueError, 'reviewed'):
                device.register(plan)
            post.assert_not_called()

    def test_existing_download_device_is_not_overwritten_or_registered_again(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/'device.json'
            destination.write_text('existing')
            with patch.object(device, 'DESTINATION', destination), patch.object(device.httpx, 'post') as post:
                with self.assertRaisesRegex(ValueError, 'already exist'):
                    device.register(device.plan())
                post.assert_not_called()
                self.assertEqual(destination.read_text(), 'existing')

    def test_fresh_code_flow_does_not_use_or_refresh_old_session(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/'device.json'
            response = Mock(status_code=200)
            response.json.return_value = {'response': {'success': {'tokens': {
                'mac_dms': {'adp_token': 'test-token', 'device_private_key': 'test-key'},
                'bearer': {'expires_in': 3600}}, 'extensions': {'device_info': {}}}}}
            authorization = {'client_id': 'test-client', 'authorization_code': 'test-code',
                             'code_verifier': 'test-verifier', 'code_algorithm': 'SHA-256',
                             'client_domain': 'DeviceLegacy'}
            with patch.object(device, 'DESTINATION', destination), \
                    patch.object(device.Authenticator, 'from_dict') as old_auth, \
                    patch.object(device.httpx, 'post', return_value=response) as post:
                device.register(device.plan(), authorization=authorization)
                old_auth.assert_not_called()
                self.assertEqual(post.call_args.kwargs['json']['auth_data'], authorization)


if __name__ == '__main__':
    unittest.main()
