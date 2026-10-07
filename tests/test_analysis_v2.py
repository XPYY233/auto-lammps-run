"""Synthetic model and scheduler; actual freezing, byte collection and report reading."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from auto_lammps import analysis as legacy
from auto_lammps import analysis_v2 as native
from auto_lammps import outputs
from auto_lammps.following import FollowingService
from auto_lammps.ledger import Conflict, Ledger, Policy
from auto_lammps.manifest import canonical, sha256
from auto_lammps.manifest import freeze
from auto_lammps.reconciliation import ReconciliationService
from auto_lammps.slurm_read import Observation, SlurmReader
import test_agent_candidates as candidates
import test_outputs as collected
import test_runtime_launcher as runtime
import test_analysis as numeric
import test_coordination_analysis as ordering

TABLE=dict(file='trajectory.dump',format='lammps_ave_time_scalar',
    headers=['# Time-averaged data for fix curve','# TimeStep v_strain v_stress'],
    columns=[dict(name='step',unit='step',source='TimeStep'),
             dict(name='strain',unit='1',source='v_strain'),dict(name='stress',unit='GPa',source='v_stress')],
    steps=dict(first=100,last=300,stride=100))
PLAN=dict(tables=[TABLE],operations=[dict(id=method,method=method,file=TABLE['file'],
    x='strain',y='stress',window=[0,2]) for method in ('summary','last','linear_fit')])
DATA=b'# Time-averaged data for fix curve\n# TimeStep v_strain v_stress\n100 0 1\n200 1 3\n300 2 5\n'


class NativePipelineTests(unittest.TestCase):
    def prepare(self, *, mixed=False):
        gen=candidates.AgentCandidateTests();gen.setUp();self.addCleanup(gen.doCleanups)
        gen.resources=runtime.RESOURCES
        plan=deepcopy(PLAN)
        if mixed:
            table=deepcopy(numeric.TABLE);table['file']='labeled.dat';plan['tables'].append(table)
            op=deepcopy(numeric.PLAN['operations'][1]);op.update(id='labeled_last',file='labeled.dat')
            plan['operations'].append(op)
        gen.value['analysis']=dict(quantity='synthetic curve',method='frozen arithmetic',
            files=[t['file'] for t in plan['tables']],plan=plan)
        gen.value['workflow']='run 0\n'+'\n'.join('print "synthetic" file /output/'+t['file'] for t in plan['tables'])
        with patch('subprocess.Popen',side_effect=AssertionError('No engine or process during candidate generation')):
            result=gen.generate()
        self.snapshot=result['snapshot'];self.gen=gen
        self.assertFalse(result['execution_authorized'])
        spec=json.loads((self.snapshot.path/'analysis.json').read_bytes())
        self.assertEqual(spec['implementation_status'],'numeric_tables_v2')
        self.assertEqual(spec['adapter_identity'],native.adapter_identity())
        self.assertEqual(spec['proposal']['plan'],plan)
        self.assertIn('native fix ave/time scalar',gen.transport.call_args.args[0].decode())
        f=collected.OutputTests();f.setUp();self.addCleanup(f.doCleanups)
        self.f=f;self.root=f.root
        # Prepare synthetic transport fixture before collection; never alter a real task.
        old=f.digest;f.digest=self.snapshot.digest
        f.local_command=[f.digest if value==old else value for value in f.local_command]
        (f.case/'manifest.json').unlink()
        f.private(f.case/'manifest.json',(self.snapshot.path/'manifest.json').read_bytes())
        intent=json.loads((f.case/'execution-intent.json').read_bytes())
        intent.update(manifest_sha256=f.digest,outputs=spec['outputs'])
        f.private(f.case/'execution-intent.json',canonical(intent))
        with f.ledger._transaction() as db:db.execute('UPDATE requests SET manifest_sha256=? WHERE id=?',(f.digest,f.request_id))
        f.private(f.case/'output/trajectory.dump',DATA)
        if mixed:f.private(f.case/'output/labeled.dat',numeric.DATA)
        self.service=native.VersionedAnalysisService(f.collector,self.root/'reports')

    def analyze(self):
        with self.f.local_transfer():return self.service.run(self.f.request_id,self.snapshot)

    def test_candidate_native_and_labeled_outputs_share_persistent_report(self):
        self.prepare(mixed=True)
        first=self.analyze();report=first['report']
        self.assertEqual(report['status'],'analyzed')
        self.assertEqual(report['scientific_status'],'not_evaluated')
        self.assertEqual(report['results'][2]['values']['slope'],2)
        self.assertEqual(report['results'][2]['source_line_ranges'],[[3,5]])
        self.assertEqual(report['results'][2]['units_origin'],'declared_only_not_present_in_scalar_header')
        self.assertEqual(report['results'][3]['units_origin'],'declared_and_header_matched')
        self.assertEqual(report['sources'][0]['steps'],TABLE['steps'])
        self.assertEqual(report['sources'][0]['sha256'],sha256(DATA))
        self.service=native.VersionedAnalysisService(self.f.collector,self.root/'reports')
        with patch.object(outputs,'transfer',side_effect=AssertionError('No repeated download')):
            self.assertEqual(self.service.run(self.f.request_id,self.snapshot),first)
        kinds=[e['kind'] for e in self.f.ledger.events(self.f.request_id)]
        for kind in ('dispatch_intent','analysis_reserved','analysis_saved','output_fetch_started'):
            self.assertEqual(kinds.count(kind),1)

    def test_incomplete_scalar_is_retained_as_failure_without_partial_properties(self):
        self.prepare();self.f.private(self.f.case/'output/trajectory.dump',DATA.replace(b'200 1 3\n',b''))
        result=self.analyze();self.assertEqual(result['report']['status'],'analysis_failed')
        self.assertNotIn('results',result['report']);self.assertEqual(self.analyze(),result)

    def test_failed_execution_never_produces_properties(self):
        self.prepare();p=self.f.case/'execution-result.json';result=json.loads(p.read_bytes())
        result['returncode']=1;self.f.private(p,canonical(result))
        self.assertEqual(self.analyze()['report']['status'],'analysis_failed')

    def test_changed_parser_version_is_rejected(self):
        self.prepare()
        changed={**native.scalar.adapter_identity(),'source_sha256':'f'*64}
        with patch.object(native.scalar,'adapter_identity',return_value=changed):result=self.analyze()
        self.assertEqual(result['report']['status'],'analysis_failed')
        self.assertIn('frozen plan',result['report']['reason'])

    def test_tampered_report_is_not_trusted(self):
        self.prepare();result=self.analyze();p=self.root/'reports'/(result['context']['analysis_id']+'.json')
        value=json.loads(p.read_bytes());value['report']['results'][0]['values']['max']=999
        p.write_bytes(canonical(value))
        with self.assertRaises(Conflict):self.analyze()

    def test_write_failure_resumes_without_new_charge_or_compute(self):
        self.prepare()
        with patch.object(native,'_write_new',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):self.analyze()
        self.assertEqual(self.analyze()['report']['status'],'analyzed')
        kinds=[e['kind'] for e in self.f.ledger.events(self.f.request_id)]
        self.assertEqual(kinds.count('analysis_reserved'),1);self.assertEqual(kinds.count('dispatch_intent'),1)

    def test_aggregate_bytes_are_bounded_before_parsing(self):
        self.prepare()
        with patch.object(native,'MAX_TABLE_BYTES',10):result=self.analyze()
        self.assertEqual(result['report']['status'],'analysis_failed')
        self.assertIn('Aggregate',result['report']['reason'])

    def test_real_follower_and_results_projection_from_generated_candidate(self):
        self.prepare();f=self.f
        ledger=Ledger(self.root/'following.sqlite')
        ledger.create_campaign('synthetic',Policy(1000,8,120,128*1024*1024,16*1024*1024,1,'a'*64))
        evaluation=ledger.register_evaluation('synthetic',task_sha256='a'*64,repetition=0,role='agent',system_sha256='a'*64)
        row=ledger.reserve(evaluation,'one',self.snapshot.digest,runtime.RESOURCES);request=row['id']
        old=f.request_id;new=f.case.parent/request;f.case.rename(new);f.case=new
        f.local_command=[request if v==old else v for v in f.local_command]
        for name in ('execution-intent.json','execution-result.json'):
            p=new/name;value=json.loads(p.read_bytes());value['request_id']=request;f.private(p,canonical(value))
        f.ledger=ledger;f.collector.ledger=ledger;f.request_id=request
        ledger.begin_dispatch(request);ledger.accepted(request,'123',{'synthetic':True})
        reader=SlurmReader(f.endpoint.host_alias,self.root/'queries',max_bytes=1024)
        def reopen():return FollowingService(ledger,ReconciliationService(ledger,reader),
            native.VersionedAnalysisService(f.collector,self.root/'reports'),self.snapshot.path.parent,max_polls=3)
        service=reopen()
        with patch.object(reader,'lookup',return_value=Observation('completed','123',1,evidence_sha256='a'*64)),f.local_transfer():
            result=service.advance(request)
        self.assertEqual(result['state'],'analyzed');self.assertEqual(result['scientific_status'],'not_evaluated')
        before=ledger.events(request)
        with patch.object(outputs,'transfer',side_effect=AssertionError('no duplicate transport')):
            self.assertEqual(reopen().advance(request),result)
        self.assertEqual(ledger.events(request),before)
        self.assertEqual(sum(e['kind']=='dispatch_intent' for e in before),1)
        from auto_lammps.results import ResultsReader
        from auto_lammps.tasks import TaskStore
        from test_candidate_jobs import frozen_research
        from test_results import link_synthetic_candidate
        tasks=TaskStore(self.root/'tasks.sqlite');doc=frozen_research(tasks)
        link_synthetic_candidate(tasks,doc,self.snapshot.digest)
        view=ResultsReader(tasks,ledger,self.root/'collected',self.root/'reports').task(doc['id'])
        report=view['evaluations'][0]['requests'][0]['reports'][0]
        self.assertEqual(report['status'],'analyzed')
        self.assertIn('不含单位',report['notes'][1])
        self.assertEqual(report['results'][2]['values']['slope'],2)

    def test_existing_v1_uses_unchanged_identity_and_report(self):
        f=numeric.AnalysisIntegrationTests();f.setUp();self.addCleanup(f.doCleanups)
        first=f.run_analysis();f.service=native.VersionedAnalysisService(f.fixture.collector,f.root/'reports')
        self.assertEqual(f.run_analysis(),first)
        self.assertEqual(first['report']['adapter_identity'],legacy.adapter_identity())

    def test_bad_native_plan_is_rejected_by_static_validator(self):
        for change in ('format','steps','column','file'):
            plan=deepcopy(PLAN)
            if change=='format':plan['tables'][0]['format']='vector'
            if change=='steps':plan['tables'][0]['steps']['stride']=0
            if change=='column':plan['tables'][0]['columns'][0]['source']='NotTimeStep'
            if change=='file':plan['tables'][0]['file']='unlisted.dat'
            with self.subTest(change=change),self.assertRaises(legacy.AnalysisError):
                native.validate_plan(plan,['trajectory.dump'])


@unittest.skipUnless(ordering._ovito_ready(), 'Application OVITO geometry runtime is optional')
class StructuralPipelineTests(unittest.TestCase):
    """Real collection/analysis/Follower, synthetic scheduler and geometry only."""
    def prepare(self, *, mixed=False, data=None):
        f=collected.OutputTests();f.setUp();self.addCleanup(f.doCleanups)
        self.f=f;self.root=f.root
        table=deepcopy(ordering.TABLE);table['file']='trajectory.dump'
        operation=deepcopy(ordering.OPERATION);operation['file']=table['file']
        plan=dict(tables=[table],operations=[operation])
        if mixed:
            for index in range(2):
                name=f'numeric_{index}.dat'
                plan['tables'].append(dict(numeric.TABLE,file=name))
                plan['operations'].append(dict(numeric.PLAN['operations'][0],id=f'mean_{index}',file=name))
        outputs=runtime.OUTPUTS+[t['file'] for t in plan['tables'] if t['file'] not in runtime.OUTPUTS]
        spec=dict(proposal=dict(quantity='synthetic ordering',method='explicit first shell',
                               files=[t['file'] for t in plan['tables']],plan=plan),
                  outputs=outputs,implementation_status='numeric_tables_v3',adapter_identity=native.adapter_identity())
        source=self.root/'structural-input';source.mkdir()
        (source/'input.in').write_bytes(b'# synthetic packaging only; never executed\n')
        (source/'analysis.json').write_bytes(canonical(spec))
        self.snapshot=freeze(source,self.root/'structural-snapshots',
            files={'input.in':'lammps_input','analysis.json':'analysis_spec'},entrypoint='input.in',
            resources=runtime.RESOURCES,provenance=dict(task_sha256='a'*64,software_sha256='b'*64,
                                                       analysis_sha256=sha256(canonical(spec))))
        old=f.digest;f.digest=self.snapshot.digest
        f.local_command=[f.digest if value==old else value for value in f.local_command]
        (f.case/'manifest.json').unlink()
        f.private(f.case/'manifest.json',(self.snapshot.path/'manifest.json').read_bytes())
        intent=json.loads((f.case/'execution-intent.json').read_bytes())
        intent.update(manifest_sha256=f.digest,outputs=outputs)
        f.private(f.case/'execution-intent.json',canonical(intent))
        with f.ledger._transaction() as db:
            db.execute('UPDATE requests SET manifest_sha256=? WHERE id=?',(f.digest,f.request_id))
        f.private(f.case/'output/trajectory.dump',data or ordering.synthetic_bcc_dump())
        if mixed:
            for index in range(2):f.private(f.case/'output'/f'numeric_{index}.dat',numeric.DATA)
        self.service=native.VersionedAnalysisService(f.collector,self.root/'reports')

    def analyze(self):
        with self.f.local_transfer():return self.service.run(self.f.request_id,self.snapshot)

    def test_structural_only_collection_persists_complete_values_and_resumes_once(self):
        self.prepare()
        saved=self.analyze();report=saved['report']
        self.assertEqual(report['status'],'analyzed')
        self.assertEqual(report['results'],[])
        self.assertEqual(report['sources'][0]['format'],'lammps_dump')
        self.assertNotIn('columns',report['sources'][0])
        structural=report['structural_results'][0]
        self.assertEqual(structural['source']['sha256'],sha256(ordering.synthetic_bcc_dump()))
        self.assertEqual(structural['chart']['values'],[1.,-1.,1.])
        self.assertEqual(structural['frames'][0]['frame'],0)
        self.assertEqual(structural['frames'][0]['timestep'],0)
        self.service=native.VersionedAnalysisService(self.f.collector,self.root/'reports')
        with patch.object(outputs,'transfer',side_effect=AssertionError('No repeated download')):
            self.assertEqual(self.service.run(self.f.request_id,self.snapshot),saved)
        events=self.f.ledger.events(self.f.request_id)
        self.assertEqual(sum(e['kind']=='analysis_reserved' for e in events),1)
        self.assertEqual(sum(e['kind']=='analysis_saved' for e in events),1)
        self.assertEqual(sum(e['kind']=='dispatch_intent' for e in events),1)

    def test_v3_numeric_tables_are_calculated_per_file_without_aggregate_byte_cap(self):
        self.prepare(mixed=True)
        self.assertLess(len(numeric.DATA),80)
        self.assertGreater(2*len(numeric.DATA),80)
        with patch.object(native,'MAX_TABLE_BYTES',80):saved=self.analyze()
        self.assertEqual(saved['report']['status'],'analyzed')
        self.assertEqual(len(saved['report']['structural_results']),1)
        self.assertEqual([r['values']['mean'] for r in saved['report']['results']],[3.,3.])

    def test_invalid_geometry_is_retained_as_failure_without_partial_structural_results(self):
        bad=ordering.synthetic_bcc_dump(field_change=lambda rows:rows[0].__setitem__(1,3))
        self.prepare(data=bad)
        saved=self.analyze()
        self.assertEqual(saved['report']['status'],'analysis_failed')
        self.assertIn('composition',saved['report']['reason'])
        self.assertNotIn('structural_results',saved['report'])
        self.assertEqual(self.analyze(),saved)

    def test_normal_following_reaches_structural_analysis_without_querying_completed_job(self):
        self.prepare();f=self.f
        reader=SlurmReader(f.endpoint.host_alias,self.root/'queries',max_bytes=1024)
        follower=FollowingService(f.ledger,ReconciliationService(f.ledger,reader),self.service,
                                  self.snapshot.path.parent,max_polls=3)
        with patch.object(reader,'lookup',side_effect=AssertionError('Already accounted synthetic completion')),f.local_transfer():
            result=follower.advance(f.request_id)
        self.assertEqual(result['state'],'analyzed')
        before=f.ledger.events(f.request_id)
        with patch.object(outputs,'transfer',side_effect=AssertionError('No repeated transfer')):
            self.assertEqual(follower.advance(f.request_id),result)
        self.assertEqual(before,f.ledger.events(f.request_id))
        report_path=next((self.root/'reports').glob('*.json'))
        saved=json.loads(report_path.read_bytes())
        self.assertEqual(saved['report']['structural_results'][0]['chart']['values'],[1.,-1.,1.])
