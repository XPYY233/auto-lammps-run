import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from auto_lammps import tls_context
from auto_lammps.deepseek import ModelError, https_transport
from auto_lammps.model_connections import official_request
from auto_lammps.tasks import TaskError


class TlsContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.bundle=Path(self.temp.name)/'ca.pem'
        self.bundle.write_text('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n')
        self.bundle.chmod(0o600)

    def test_explicit_override_wins_over_system_bundles(self):
        with mock.patch.dict(os.environ, {'SSL_CERT_FILE': str(self.bundle)}, clear=False):
            self.assertEqual(tls_context.ca_bundle(), str(self.bundle))

    def test_requests_override_is_accepted_when_ssl_cert_file_is_absent(self):
        environ={k:v for k,v in os.environ.items() if k!='SSL_CERT_FILE'}
        environ['REQUESTS_CA_BUNDLE']=str(self.bundle)
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(tls_context.ca_bundle(), str(self.bundle))

    def test_missing_bundle_is_reported_instead_of_skipping_verification(self):
        with mock.patch.dict(os.environ, {k:v for k,v in os.environ.items() if k not in {'SSL_CERT_FILE','REQUESTS_CA_BUNDLE'}}, clear=True), \
             mock.patch.object(tls_context, 'SYSTEM_BUNDLES', ()), \
             mock.patch.dict('sys.modules', {'certifi': None}):
            self.assertIsNone(tls_context.ca_bundle())
            with self.assertRaises(OSError):
                tls_context.ssl_context()

    def test_context_always_verifies_and_checks_the_host(self):
        if tls_context.ca_bundle() is None:
            self.skipTest('no CA bundle is installed on this host')
        context=tls_context.ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_transport_reports_the_missing_trust_store_as_its_own_class(self):
        with mock.patch('auto_lammps.deepseek.ssl_context', side_effect=OSError('no_ca_bundle_available')):
            with self.assertRaises(ModelError) as raised:
                https_transport('{}', 'sk-probe', 5)
        self.assertEqual(str(raised.exception), 'model_tls_trust_unavailable')

    def test_connection_listing_reports_the_missing_trust_store_clearly(self):
        with mock.patch('auto_lammps.model_connections.ssl_context', side_effect=OSError('no_ca_bundle_available')):
            with self.assertRaises(TaskError) as raised:
                official_request('deepseek-official', 'sk-probe', 'GET', '/models')
        self.assertIn('SSL_CERT_FILE', str(raised.exception))
        self.assertNotIn('sk-probe', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
