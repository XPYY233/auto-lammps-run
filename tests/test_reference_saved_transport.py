"""Saved HPC destinations for existing reference tools, using synthetic I/O."""
import base64
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from auto_lammps.author_source_reader import AuthorSourceReader
from auto_lammps.author_source_worker import AuthorSourceError
from auto_lammps.hpc_connections import HPCConnections
from auto_lammps.hpc_transport import SavedHPCTransport
from auto_lammps.manifest import canonical
from auto_lammps.potential_acquisition import AcquisitionError
from auto_lammps.remote_resources import RemoteResources
from auto_lammps.tasks import TaskStore
import test_remote_resources as resources_fixture
import test_author_source_reader as source_fixture
from test_source_discovery import NAME, COMMIT


def saved_transport(root, alias):
    connections=HPCConnections(TaskStore(root/'transport-tasks.sqlite'))
    profile=dict(label='Synthetic saved destination',host='saved.example.edu',port=2244,
        username='scientist',work_directory=str(root),partition='compute',account='',authentication='agent')
    connections.save(profile,0,known_hosts='SYNTHETIC_HOST_KEY')
    return SavedHPCTransport(connections,1,alias)


class ReferenceSavedTransportTests(unittest.TestCase):
    def setUp(self):
        self.resources=resources_fixture.RemoteResourcesTests();self.resources.setUp()
        self.addCleanup(self.resources.doCleanups)
        self.root=self.resources.root
        self.transport=saved_transport(self.root,self.resources.policy['host_alias'])

    def acquire(self,transport=None,capture=None,policy=None):
        return RemoteResources(self.root/'audit',policy or self.resources.policy,
            capture=capture or self.resources.capture,transport=transport).acquire(NAME,COMMIT)

    def test_saved_destination_is_pinned_and_audit_does_not_copy_credentials(self):
        seen=[]
        def capture(command,**options):
            seen.append(command)
            self.assertIn('saved.example.edu',command)
            self.assertIn('2244',command)
            return self.resources.capture(command,**options)
        report=self.acquire(self.transport,capture)
        self.assertEqual(report['state'],'finished')
        self.assertEqual(self.acquire(self.transport,capture),report)
        for path in (self.root/'audit').rglob('*.json'):
            self.assertNotIn('SYNTHETIC_HOST_KEY',path.read_text())
        intent=json.loads(next((self.root/'audit').rglob('intent.json')).read_text())
        self.assertEqual(set(intent['transport_binding']),{'identity_sha256','revision'})
        self.assertEqual(intent['transport_binding']['revision'],1)

    def test_legacy_and_saved_operations_cannot_migrate_in_either_direction(self):
        self.acquire()
        capture=Mock()
        with self.assertRaises(AcquisitionError):self.acquire(self.transport,capture)
        capture.assert_not_called()
        self.resources.policy['operation_id']='d'*32
        self.acquire(self.transport)
        with self.assertRaises(AcquisitionError):self.acquire(None,capture)
        capture.assert_not_called()

    def test_changed_saved_credentials_are_rejected_before_capture(self):
        credential=self.transport.connections.directory/(self.transport.identity['credential_id']+'.json')
        data=json.loads(credential.read_text());data['known_hosts']='CHANGED'
        credential.write_bytes(canonical(data))
        capture=Mock()
        with self.assertRaisesRegex(AcquisitionError,'command_rejected'):
            self.acquire(self.transport,capture)
        capture.assert_not_called()

    def test_mismatched_alias_or_non_descendant_resource_root_is_rejected(self):
        capture=Mock()
        for policy in (dict(self.resources.policy,host_alias='other-alias'),
                       dict(self.resources.policy,remote_directory=str(self.root)),
                       dict(self.resources.policy,remote_directory='/outside/resources')):
            with self.subTest(policy=policy),self.assertRaises(AcquisitionError):
                self.acquire(self.transport,capture,policy)
        capture.assert_not_called()

    def test_transport_failure_retains_no_raw_source_or_error_text(self):
        secret='PRIVATE_SOURCE_AND_CREDENTIAL_CANARY'
        capture=Mock(return_value=dict(returncode=255,failure='unsafe '+secret,stdout=secret,stderr=secret))
        report=self.acquire(self.transport,capture)
        self.assertEqual(report['state'],'unknown')
        self.assertEqual(report['observation_failure'],'transport_failed')
        for path in (self.root/'audit').rglob('*.json'):
            self.assertNotIn(secret,path.read_text())


class SourceSavedTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture=source_fixture.AuthorSourceReaderTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.transport=saved_transport(self.fixture.root,self.fixture.policy['host_alias'])
        self.captures=[]

    def capture(self,command,**options):
        self.captures.append(command)
        self.assertIn('saved.example.edu',command)
        envelope=json.loads(b''.join(options['input_chunks']))
        report=self.fixture.worker(envelope['request'])
        return dict(returncode=0,failure='',stdout=base64.b64encode(canonical(report)).decode(),stderr='')

    def service(self,transport=None,capture=None):
        return AuthorSourceReader(self.fixture.root/'audit',self.fixture.policy,
            capture=capture or self.capture,transport=transport)

    def read(self,service):
        return service.read(self.fixture.receipt,[dict(path='README.md',start_line=1,end_line=2)],
            purpose='author_reference')

    def test_saved_reader_returns_transient_text_without_raw_audit(self):
        result=self.read(self.service(self.transport))
        self.assertEqual(result['state'],'read')
        self.assertIn('Synthetic source',result['files'][0]['text'])
        self.assertEqual(len(self.captures),1)
        for path in (self.fixture.root/'audit').rglob('*.json'):
            value=path.read_text()
            self.assertNotIn('Synthetic source',value)
            self.assertNotIn('SYNTHETIC_HOST_KEY',value)

    def test_legacy_reader_cannot_adopt_saved_connection_after_sidecar_absence(self):
        self.read(self.service(capture=self.fixture.capture))
        folder=self.fixture.root/'audit/source-transport-bindings'
        next(folder.iterdir()).unlink()  # Simulate the old no-sidecar audit layout.
        capture=Mock()
        with self.assertRaises(AuthorSourceError):self.read(self.service(self.transport,capture))
        capture.assert_not_called()

    def test_saved_reader_cannot_revert_to_alias_or_other_identity(self):
        self.read(self.service(self.transport))
        capture=Mock()
        with self.assertRaises(AcquisitionError):self.read(self.service(capture=capture))
        different=deepcopy(self.transport.identity)
        different['revision']=2
        self.transport.identity=different
        with self.assertRaises(AcquisitionError):self.read(self.service(self.transport,capture))
        capture.assert_not_called()
