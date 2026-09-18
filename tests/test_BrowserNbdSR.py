import contextlib
import json
import tempfile
import unittest
import xmlrpc.client
from types import SimpleNamespace
from unittest import mock

import testlib
import BrowserNbdSR as driver
import util

UUID = 'd92b0a98-b17c-4a83-8339-dbd66de6de67'
CONFIG = {'host': 'xo.example', 'port': '10809', 'password': 'a' * 64, 'size': '32768'}


class TestBrowserNbd(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch = mock.patch.object(driver, 'RUNTIME', directory.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.manager = SimpleNamespace(FILE_LOCK=contextlib.nullcontext(),
                                       _find_unused_nbd_device=mock.Mock(return_value='/dev/nbd3'))
        patch = mock.patch.dict('sys.modules', nbd_client_manager=self.manager)
        patch.start()
        self.addCleanup(patch.stop)
        self.vdi = driver.BrowserNbdVDI(SimpleNamespace(media_size=32768, nbd_config=CONFIG, session=mock.Mock()), UUID)

    def test_tls_is_default_and_reconnect_is_disabled(self):
        command = driver.command(CONFIG, '/dev/nbd3')
        self.assertIn('-enable-tls', command)
        self.assertIn('-readonly', command)
        self.assertNotIn('-persist', command)
        self.assertEqual(command[command.index('-tlshostname') + 1], CONFIG['host'])
        self.assertNotIn('-enable-tls', driver.command(dict(CONFIG, allow_plaintext='true'), '/dev/nbd3'))

    def test_invalid_config_does_not_disclose_capability(self):
        for change in [{'host': '-unsafe'}, {'port': '0'}, {'size': '32769'}, {'password': 'secret'}]:
            with self.assertRaises(util.SMException) as error:
                driver.validate(dict(CONFIG, **change))
            self.assertNotIn('secret', str(error.exception))
            self.assertNotIn(CONFIG['password'], str(error.exception))

    def test_xapi_secret_is_resolved_without_mutating_logged_config(self):
        sr = object.__new__(driver.BrowserNbdSR)
        sr.dconf = dict(CONFIG, password_secret='secret-ref')
        del sr.dconf['password']
        sr.session = mock.Mock()
        with mock.patch.object(driver.util, 'get_secret', return_value=CONFIG['password']) as secret, mock.patch.object(driver, 'Lock'):
            sr.load(UUID)
        secret.assert_called_once_with(sr.session, 'secret-ref')
        self.assertNotIn('password', sr.dconf)
        self.assertEqual(sr.nbd_config['password'], CONFIG['password'])

    def test_standard_attach_and_idempotence(self):
        with mock.patch.object(driver, 'run', return_value=b'32768') as run, mock.patch.object(driver, 'identity', return_value='42'):
            result, _ = xmlrpc.client.loads(self.vdi.attach(UUID, UUID))
            self.assertEqual(result[0]['params'], '/dev/nbd3')
            self.assertNotIn('params_nbd', result[0])
            self.vdi.attach(UUID, UUID)
            self.manager._find_unused_nbd_device.assert_called_once()
            connections = [call for call in run.call_args_list if '-name' in call.args[0]]
            self.assertEqual(len(connections), 1)
            self.vdi.detach(UUID, UUID)
            self.assertIsNone(self.vdi.state())
            run.assert_called_with(['/usr/sbin/nbd-client', '-disconnect', '/dev/nbd3'], timeout=10)

    def test_wrong_size_disconnects_allocated_device(self):
        with mock.patch.object(driver, 'run', return_value=b'65536') as run, mock.patch.object(driver, 'identity', return_value='42'):
            with self.assertRaises(util.SMException):
                self.vdi.attach(UUID, UUID)
            run.assert_called_with(['/usr/sbin/nbd-client', '-disconnect', '/dev/nbd3'], timeout=10)
        self.assertIsNone(self.vdi.state())

    def test_detach_does_not_disconnect_reused_device(self):
        with open(self.vdi.state_path, 'w') as state:
            json.dump({'device': '/dev/nbd3', 'pid': '42', 'fingerprint': 'old'}, state)
        with mock.patch.object(driver, 'identity', return_value='43'), mock.patch.object(driver, 'run') as run:
            self.vdi.detach(UUID, UUID)
        run.assert_not_called()
        self.assertIsNone(self.vdi.state())

    def test_changed_configuration_cannot_reuse_connected_device(self):
        with open(self.vdi.state_path, 'w') as state:
            json.dump({'device': '/dev/nbd3', 'pid': '42', 'fingerprint': 'different'}, state)
        with mock.patch.object(driver, 'identity', return_value='42'), mock.patch.object(driver, 'run'):
            with self.assertRaisesRegex(util.SMException, 'differs'):
                self.vdi.attach(UUID, UUID)
        self.manager._find_unused_nbd_device.assert_not_called()
