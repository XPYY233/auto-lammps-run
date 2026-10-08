"""Synthetic human A evidence checks; no model, scheduler or physics calls."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.ledger import Ledger
from auto_lammps.manifest import canonical, sha256
from auto_lammps.papers import PaperStore
from auto_lammps.reference_evidence import ReferenceEvidenceViews
from auto_lammps.results import ResultUnavailable
from auto_lammps.runtime_launcher import ExecutionDenied
from auto_lammps.tasks import TaskStore
from test_ledger import H1, H2, POLICY, RESOURCE


class ReferenceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.tasks = TaskStore(self.root / 'tasks.sqlite')
        self.task = self.tasks.create('Human reference', 'B_FROZEN_PRIVATE_CANARY', 'reproduction')
        self.ledger = Ledger(self.root / 'ledger.sqlite')
        self.ledger.create_campaign('synthetic', replace(POLICY, total_storage_bytes=100000))
        self.papers = PaperStore(self.tasks, ledger=self.ledger)
        self.paper = self.papers.add('Synthetic full published title', '10.1234/reference', 'Complete scope', 'PRIVATE_NOTE')
        self.paper = self.papers.select(self.paper['id'], self.paper['revision'])
        self.evaluation = self.ledger.register_evaluation('synthetic', task_sha256=H1,
                                      repetition=0, role='reference', system_sha256=H2)
        self.papers.link_reference_task(self.paper['id'], self.task['id'], self.evaluation)
        self.request = self.ledger.reserve(self.evaluation, 'author-a', H2, RESOURCE)
        self.ledger.begin_dispatch(self.request['id'])
        self.ledger.accepted(self.request['id'], '123', {'private_path': '/PRIVATE_AUTHOR_CODE_CANARY'})
        self.ledger.observe(self.request['id'], '123', 'completed', {'private_log': 'PRIVATE_RESULT_CANARY'})
        self.ledger.account(self.request['id'], 12, H1)
        self.directory = self.root / 'evidence'; self.directory.mkdir(mode=0o700)
        parent = self.directory / self.task['id']; parent.mkdir(mode=0o700)
        self.folder = parent / 'reference-evidence'; self.folder.mkdir(mode=0o700)
        self.contents = {
            'curve.png': b'synthetic-image',
            'data.csv': b'cycle,A_energy,P_energy\n0,1,P_ANSWER_CANARY\n1,,P_ANSWER_CANARY\n2,3,P_ANSWER_CANARY\n',
            'method.json': canonical({'private_path': '/PRIVATE_METHOD_CANARY'}),
            'report.md': b'Synthetic declared A diagnostic.'}
        self.document = dict(version=1, task_id=self.task['id'], paper_id=self.paper['id'],
            title=self.paper['title'], doi=self.paper['doi'], role='reference_evidence_human_only',
            evaluation_id=self.evaluation, request_id=self.request['id'], job_id='123', input_manifest_sha256=H2,
            scientific_status='not_evaluated', output_status='complete', output_valid=True,
            source_receipt_file='source.json', source_sha256='', scope='Original author A scope',
            summary='Existing A data only; scientific validity remains unassessed.',
            limitations=['Missing cells stay missing; frames are not independent replicates.'],
            coverage=[dict(label='states', required=3, available=3, unit='state')],
            methods=[dict(id='existing-statistics',label='Existing statistics',
                description='Read the recorded analysis without recomputation.',unit='eV/atom',source_sha256=H1)],
            files=[], figures=[dict(name='curve.png',label='A curve',caption='Synthetic A curve')],
            views=[dict(id='energy',title='A energy',description='Only A columns, despite the mixed source CSV.',
                figures=[dict(name='curve.png',role='reference')],tables=[dict(name='data.csv',role='reference',
                    label='A source values',columns=[dict(key='cycle',label='MC cycle',unit='1',kind='number'),
                                                   dict(key='A_energy',label='A energy',unit='eV/atom',kind='number')])])])
        self.save()
        self.views = ReferenceEvidenceViews(self.directory, self.papers)

    def save(self, *, state='completed'):
        public = {'curve.png', 'data.csv', 'report.md'}
        files = [dict(name=name,label=name,size=len(data),sha256=sha256(data),
                      visibility='human' if name in public else 'private_receipt')
                 for name,data in self.contents.items() if name != 'source.json']
        receipt = {key: self.document[key] for key in ('task_id','evaluation_id','request_id','job_id',
            'input_manifest_sha256','output_status','output_valid','coverage','methods')}
        receipt.update(version=1,role='reference_source_receipt',
            paper=dict(title=self.paper['title'],doi=self.paper['doi']),scheduler_state=state,
            files=[{key:item[key] for key in ('name','size','sha256')} for item in files],
            private_path='/PRIVATE_SOURCE_RECEIPT_CANARY',author_code='PRIVATE_AUTHOR_CODE_CANARY',
            P_answer='P_RECEIPT_CANARY')
        self.contents['source.json'] = canonical(receipt)
        self.document['source_sha256'] = sha256(self.contents['source.json'])
        files.append(dict(name='source.json',label='Private source receipt',size=len(self.contents['source.json']),
                          sha256=self.document['source_sha256'],visibility='private_receipt'))
        self.document['files'] = files
        for name,data in self.contents.items():
            path=self.folder/name; path.write_bytes(data);path.chmod(0o600)
        self.write_manifest()

    def write_manifest(self):
        path=self.folder/'manifest.json';path.write_bytes(canonical(self.document));path.chmod(0o600)

    def test_complete_A_is_bound_to_real_role_and_hashes_not_scientific_success(self):
        before_task=self.tasks.get(self.task['id']);before_events=self.ledger.events(self.request['id'])
        report=self.views.get(self.task['id'])
        self.assertEqual(report['role'],'reference');self.assertEqual(report['scheduler_state'],'completed')
        self.assertEqual(report['output_status'],'complete');self.assertTrue(report['output_valid'])
        self.assertEqual(report['scientific_status'],'not_evaluated');self.assertFalse(report['execution_authorized'])
        self.assertEqual(report['actual_core_hours'],12/3600)
        self.assertEqual(report['request_id'],self.request['id']);self.assertEqual(report['job_id'],'123')
        self.assertEqual(report['raw_output_status'],'not_published')
        self.assertEqual(report['views'][0]['tables'][0]['columns'][1]['unit'],'eV/atom')
        self.assertEqual(report['views'][0]['tables'][0]['rows'],[['0','1'],['1',''],['2','3']])
        self.assertEqual(self.views.download(self.task['id'],'data.csv'),self.contents['data.csv'])
        self.assertEqual(self.tasks.get(self.task['id']),before_task)
        self.assertEqual(self.ledger.events(self.request['id']),before_events)

    def test_source_receipts_and_raw_logs_are_not_public_or_model_context(self):
        report=self.views.get(self.task['id'])
        self.assertEqual({item['name'] for item in report['files']},{'curve.png','data.csv','report.md'})
        for name in ('source.json','method.json','manifest.json','unknown.csv'):
            with self.subTest(name=name),self.assertRaises(ResultUnavailable):self.views.download(self.task['id'],name)
        encoded=json.dumps(report)
        self.assertNotIn('PRIVATE_',encoded)
        context=self.views.assistant_context(self.task['id'])
        encoded=json.dumps(context)
        for private in ('PRIVATE_','P_ANSWER_CANARY','P_RECEIPT_CANARY','B_FROZEN_PRIVATE_CANARY','P_energy','curve.png','report.md'):
            self.assertNotIn(private,encoded)
        self.assertNotIn('frozen_scientific_conditions',context)
        self.assertEqual(context['role'],'author_reference_A_human_only')
        self.assertEqual(context['source_tables'][0]['rows'],[['0','1'],['1',''],['2','3']])
        self.assertEqual(context['analysis_role'],'human_author_reference_only')

    def test_failed_partial_is_visible_only_with_missing_scope_and_no_validity_claim(self):
        # Use a distinct accounted failed A to retain completed A history.
        row=self.ledger.reserve(self.evaluation,'author-a-failed',H2,RESOURCE)
        self.ledger.begin_dispatch(row['id']);self.ledger.accepted(row['id'],'124',{'synthetic':True})
        self.ledger.observe(row['id'],'124','failed',{'private':'PRIVATE_FAILURE_CANARY'})
        self.ledger.account(row['id'],15,H1)
        self.document.update(request_id=row['id'],job_id='124',output_status='partial',output_valid=False)
        self.document['coverage']=[dict(label='all insertion states',required=50,available=1,unit='state')]
        self.save(state='failed')
        report=self.views.get(self.task['id'])
        self.assertEqual((report['scheduler_state'],report['output_status'],report['output_valid']),('failed','partial',False))
        self.assertEqual(len(report['history']),2)
        self.assertEqual(report['coverage'][0]['available'],1)
        context=self.views.assistant_context(self.task['id'])
        self.assertEqual(context['scientific_status'],'not_evaluated')
        self.assertFalse(context['output_valid'])
        self.document.update(output_status='complete',output_valid=True);self.save(state='failed')
        with self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])

    def test_accounting_and_reference_binding_missing_fail_closed(self):
        with patch.object(self.papers,'ledger',None),self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])
        original=self.ledger.get(self.request['id'])
        for changes in ({'state':'running'},{'accounted':0},{'job_id':'999'},
                        {'evaluation':'f'*64},{'manifest_sha256':'d'*64}):
            changed=dict(original,**changes)
            with self.subTest(changes=changes),patch.object(self.ledger,'get',return_value=changed),self.assertRaises(ResultUnavailable):
                self.views.get(self.task['id'])

    def test_wrong_task_paper_request_or_scientific_verdict_is_rejected(self):
        original=deepcopy(self.document)
        for changes in ({'task_id':'f'*32},{'title':'Different paper'},{'doi':'10.1234/other'},
                        {'request_id':'e'*32},{'role':'agent'},{'scientific_status':'passed'},
                        {'source_sha256':'d'*64},{'input_manifest_sha256':'d'*64}):
            self.document=deepcopy(original);self.document.update(changes);self.write_manifest()
            with self.subTest(changes=changes),self.assertRaises((ResultUnavailable,KeyError)):
                self.views.get(self.task['id'])
        self.document=original;self.write_manifest()
        other=self.tasks.create('Other task','No A evidence','reproduction')
        self.assertIsNone(self.views.get(other['id']))
        with self.assertRaises(ResultUnavailable):self.views.assistant_context(other['id'])

    def test_unknown_manifest_fields_cannot_enter_A_context(self):
        self.document.update(P_target_answer='P_MANIFEST_CANARY',author_code='PRIVATE_CODE_CANARY',
                             frozen_scientific_conditions={'B_target':'B_CONDITION_CANARY'})
        self.write_manifest()
        encoded=json.dumps(self.views.assistant_context(self.task['id']))
        for canary in ('P_MANIFEST_CANARY','PRIVATE_CODE_CANARY','B_CONDITION_CANARY'):
            self.assertNotIn(canary,encoded)

    def test_P_or_B_columns_and_view_roles_cannot_be_declared_A(self):
        original=deepcopy(self.document)
        changes=[lambda d:d['views'][0]['figures'][0].update(role='paper'),
            lambda d:d['views'][0]['tables'][0].update(role='agent'),
            lambda d:d['views'][0]['tables'][0]['columns'][1].update(key='P_energy'),
            lambda d:d['views'][0]['tables'][0]['columns'][1].update(source_role='paper'),
            lambda d:d['views'][0]['tables'][0]['columns'][1].pop('unit'),
            lambda d:d['views'][0].update(stress_curve=True)]
        for change in changes:
            self.document=deepcopy(original);change(self.document);self.write_manifest()
            with self.subTest(change=change),self.assertRaises((ResultUnavailable,KeyError)):
                self.views.assistant_context(self.task['id'])

    def test_hash_receipt_visibility_and_coverage_cannot_silently_change(self):
        original=deepcopy(self.document)
        changes=[lambda d:d['files'][-1].update(visibility='human'),
            lambda d:d['coverage'][0].update(available=1),
            lambda d:d['methods'][0].update(description='Unbound replacement method'),
            lambda d:d['files'][0].update(sha256='d'*64),
            lambda d:d['files'][0].update(name='../outside.png')]
        for change in changes:
            self.document=deepcopy(original);change(self.document);self.write_manifest()
            with self.subTest(change=change),self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])
        self.document=original;self.write_manifest()
        (self.folder/'data.csv').write_bytes(b'cycle,A_energy,P_energy\n0,99,ANSWER\n')
        with self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])

    def test_receipt_boolean_and_integer_identity_are_not_interchangeable(self):
        receipt=json.loads(self.contents['source.json']);receipt['output_valid']=1
        self.contents['source.json']=canonical(receipt)
        path=self.folder/'source.json';path.write_bytes(self.contents['source.json'])
        self.document['source_sha256']=sha256(self.contents['source.json'])
        item=self.document['files'][-1];item.update(size=len(self.contents['source.json']),sha256=self.document['source_sha256'])
        self.write_manifest()
        with self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])

    def test_full_model_rows_are_not_the_twelve_row_page_preview(self):
        self.contents['data.csv']=('cycle,A_energy,P_energy\n'+''.join(f'{i},{i+1},P_CANARY\n' for i in range(30))).encode()
        self.save()
        public=self.views.get(self.task['id'])['views'][0]['tables'][0]
        self.assertTrue(public['truncated']);self.assertEqual(len(public['rows']),12)
        full=self.views.assistant_context(self.task['id'])['source_tables'][0]
        self.assertFalse(full['truncated']);self.assertEqual(len(full['rows']),30)
        self.assertEqual(full['rows'][14],['14','15'])

    def test_oversized_context_rejects_instead_of_silent_sampling(self):
        self.contents['data.csv']=('cycle,A_energy,P_energy\n'+''.join(f'{i},{i+1},P_CANARY\n' for i in range(200))).encode()
        self.save()
        with patch('auto_lammps.reference_evidence.MAX_ASSISTANT_CELLS',100),self.assertRaisesRegex(ResultUnavailable,'silently omitted'):
            self.views.assistant_context(self.task['id'])
        with patch('auto_lammps.reference_evidence.MAX_ASSISTANT_BYTES',100),self.assertRaisesRegex(ResultUnavailable,'silently omitted'):
            self.views.assistant_context(self.task['id'])

    def test_malformed_nonfinite_and_duplicate_headers_reject(self):
        for content in (b'cycle,A_energy,P_energy\n0,nan,P\n',b'cycle,A_energy,A_energy\n0,1,2\n',
                        b'cycle,A_energy,P_energy\n0,1\n',b'cycle,A_energy,P_energy\n0,1,P,extra\n'):
            self.contents['data.csv']=content;self.save()
            with self.subTest(content=content),self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])

    def test_symlink_and_other_task_folder_cannot_supply_A_bytes(self):
        path=self.folder/'curve.png';path.unlink()
        outside=self.root/'outside';outside.write_bytes(b'synthetic-image');outside.chmod(0o600);path.symlink_to(outside)
        with self.assertRaises((ExecutionDenied,OSError)):self.views.get(self.task['id'])

    def test_complete_reference_cannot_claim_success_with_empty_or_private_data(self):
        self.document['views']=[];self.write_manifest()
        with self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])

    def test_complete_reference_rejects_an_empty_declared_csv(self):
        self.contents['data.csv']=b'cycle,A_energy,P_energy\n';self.save()
        with self.assertRaises(ResultUnavailable):self.views.get(self.task['id'])


if __name__ == '__main__':
    unittest.main()
