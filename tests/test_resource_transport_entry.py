"""Real resource controller entry with synthetic GitHub/SSH, no physics."""
import json
import unittest

from auto_lammps.hpc_connections import HPCConnections
from auto_lammps.hpc_transport import SavedHPCTransport
from auto_lammps.papers import PaperStore
from auto_lammps.potential_acquisition import AcquisitionError, prepare_paper_resources
from auto_lammps.tasks import TaskStore
import test_remote_resources as resources_fixture
from test_source_discovery import SourceDiscovery, Reader, TITLE, DOI


class ResourceTransportEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = resources_fixture.RemoteResourcesTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tasks = TaskStore(self.fixture.root / 'tasks.sqlite')
        self.papers = PaperStore(self.tasks)
        self.paper = self.papers.add(TITLE, DOI, 'Synthetic resource workflow', 'Synthetic')
        source = SourceDiscovery(self.fixture.root / 'discovery', reader=Reader()).discover(TITLE, DOI)
        self.papers.record_source_search(self.paper['id'], source)
        self.connections = HPCConnections(self.tasks)
        self.connections.save(dict(label='Synthetic saved cluster', host='saved.example.edu', port=2233,
            username='scientist', work_directory=str(self.fixture.root / 'hpc'),
            partition='compute', account='', authentication='agent'), 0)
        self.transport = SavedHPCTransport(self.connections, 1, 'example-hpc')
        self.fixture.policy['remote_directory'] = str(self.fixture.root / 'hpc' / 'resources')

    def test_entry_preserves_saved_destination_and_remote_only_receipts(self):
        commands = []
        def capture(command, **options):
            commands.append(command)
            self.assertIn('saved.example.edu', command)
            self.assertIn('2233', command)
            self.assertNotIn('example-hpc', command)
            return self.fixture.capture(command, **options)
        report = prepare_paper_resources(self.papers, self.paper['id'], self.fixture.root / 'audit',
            policy=self.fixture.policy, capture=capture, transport=self.transport)
        self.assertEqual(report['state'], 'finished')
        self.assertEqual(len(commands), 1)
        saved = self.papers.get(self.paper['id'])
        self.assertEqual(saved['reference_resources']['location'], 'hpc')
        self.assertEqual(saved['potential_acquisition']['location'], 'hpc')
        self.assertEqual(saved['status'], 'pending')
        self.assertFalse(saved['reference_resources']['execution_authorized'])
        for path in (self.fixture.root / 'audit').rglob('*.json'):
            document = json.loads(path.read_text())
            self.assertNotIn('PRIVATE_KEY_CANARY', json.dumps(document))
        self.assertFalse(list((self.fixture.root / 'audit').rglob('input.lmp')))

    def test_synthetic_reader_cannot_hide_a_real_saved_transport(self):
        with self.assertRaisesRegex(AcquisitionError, 'Synthetic reader'):
            prepare_paper_resources(self.papers, self.paper['id'], self.fixture.root / 'audit',
                reader=self.fixture.reader, transport=self.transport)
        self.assertEqual(self.fixture.reader.calls, [])
        self.assertNotIn('reference_resources', self.papers.get(self.paper['id']))
