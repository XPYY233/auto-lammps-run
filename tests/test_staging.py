"""File-only transfer checks. No scheduler or physics engine is invoked."""
from dataclasses import replace
from contextlib import redirect_stderr
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import shlex
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps import remote_stage
from auto_lammps.batch_plan import BatchEnvironment, render_batch
from auto_lammps.ledger import Conflict, Ledger, Policy, Resources
from auto_lammps.manifest import canonical, freeze, sha256
from auto_lammps.slurm_read import _capture
from auto_lammps.staging import StageClient, StageEndpoint, StagingService, UploadUncertain, upload_chunks
from auto_lammps.submission import Submission

H = 'a' * 64
REQUEST = 'b' * 32
RESOURCE = Resources(2, 60, 2*1024*1024, 32768)


def receiver_worker(root, request, digest, payload, barrier, queue):
    barrier.wait(timeout=10)
    try:
        remote_stage.receive(root, request, digest, io.BytesIO(payload))
        queue.put('staged')
    except remote_stage.StageError:
        queue.put('denied')


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'input.in').write_text('# Synthetic inert text, never executed.\n')
        (self.source / 'potential').mkdir()
        (self.source / 'potential' / 'data.table').write_bytes(b'synthetic file bytes\x00\xff')
        self.snapshot = freeze(self.source, self.root / 'snapshots', files={'input.in': 'lammps_input', 'potential/data.table': 'potential'},
                               entrypoint='input.in', resources=RESOURCE,
                               provenance=dict(task_sha256=H, analysis_sha256=H, software_sha256=H))
        self.remote = self.root / 'remote'
        self.remote.mkdir(mode=0o700)
        self.policy(65536 + RESOURCE.storage_bytes)
        self.payload = b''.join(upload_chunks(self.snapshot))
        self.helper = Path(remote_stage.__file__).resolve()
        self.endpoint = StageEndpoint('synthetic-login', str(Path(sys.executable).resolve()), str(self.helper),
                                      sha256(self.helper.read_bytes()), str(self.remote))

    def policy(self, size):
        (self.remote / 'policy.json').write_bytes(canonical(dict(schema_version=1, max_total_bytes=size, approval_sha256=H)))

    def receive(self, payload=None, request=REQUEST, digest=None):
        return remote_stage.receive(str(self.remote), request, digest or self.snapshot.digest,
                                    io.BytesIO(self.payload if payload is None else payload))

    def test_roundtrip_hashes_and_non_overwrite(self):
        receipt = self.receive()
        self.assertEqual(receipt['state'], 'staged')
        self.assertEqual((self.remote / REQUEST / 'potential/data.table').read_bytes(), b'synthetic file bytes\x00\xff')
        self.assertEqual((self.remote / REQUEST / 'input.in').stat().st_mode & 0o777, 0o400)
        self.assertEqual(json.loads((self.remote / REQUEST / 'stage.json').read_text()), receipt)
        with self.assertRaises(remote_stage.StageError):
            self.receive()

    def test_bad_digest_before_creation(self):
        with self.assertRaises(remote_stage.StageError):
            self.receive(digest=H)
        self.assertFalse((self.remote / REQUEST).exists())

    def test_truncated_upload_keeps_partial_reservation(self):
        with self.assertRaises(remote_stage.StageError):
            self.receive(self.payload[:-1])
        self.assertTrue((self.remote / REQUEST / 'allocation.json').exists())
        self.assertFalse((self.remote / REQUEST / 'stage.json').exists())
        with self.assertRaises(remote_stage.StageError):
            self.receive(request='c'*32)
        self.assertFalse((self.remote / ('c'*32)).exists())

    def test_tamper_and_trailing_bytes_never_commit_stage_receipt(self):
        for index, payload in enumerate((self.payload[:-1] + bytes([self.payload[-1] ^ 1]), self.payload + b'extra')):
            self.policy(65536 + 3*RESOURCE.storage_bytes)
            request = f'{index:032x}'
            with self.assertRaises(remote_stage.StageError):
                self.receive(payload, request)
            self.assertFalse((self.remote / request / 'stage.json').exists())

    def test_oversized_header_rejected_without_allocation(self):
        with self.assertRaises(remote_stage.StageError):
            self.receive(struct.pack('!I', 1_000_001))
        self.assertFalse((self.remote / REQUEST).exists())

    def test_malicious_manifest_paths_and_metadata(self):
        original = self.snapshot.verify()
        for path in ('../escape', '/escape', 'output/trajectory', 'job.sh', 'allocation.json', 'scheduler-intent.json', 'execution-result.json', 'scheduler.stdout', 'a//x', 'a/../x'):
            doc = json.loads(json.dumps(original))
            doc['files'][0]['path'] = path
            doc['entrypoint'] = path
            raw = canonical(doc)
            with self.subTest(path=path), self.assertRaises(remote_stage.StageError):
                self.receive(struct.pack('!I',len(raw))+raw, digest=sha256(raw))
        self.assertFalse((self.remote / REQUEST).exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_duplicate_prefix_path_and_insufficient_receipt_space(self):
        original = self.snapshot.verify()
        for mutate in ('prefix', 'space'):
            doc = json.loads(json.dumps(original))
            if mutate == 'prefix':
                doc['files'][1]['path'] = doc['files'][0]['path'] + '/data'
            else:
                doc['resources']['storage_bytes'] = 1000
            raw = canonical(doc)
            with self.assertRaises(remote_stage.StageError):
                self.receive(struct.pack('!I',len(raw))+raw, digest=sha256(raw))

    def test_root_policy_and_existing_directory_symlinks_rejected(self):
        real = self.remote / 'policy.json'
        real.rename(self.root / 'policy')
        real.symlink_to(self.root / 'policy')
        with self.assertRaises(OSError):
            self.receive()
        real.unlink()
        self.policy(65536 + RESOURCE.storage_bytes)
        linked = self.remote / ('c'*32)
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(OSError):
            self.receive()
        linked.unlink()
        alias = self.root / 'alias'
        alias.symlink_to(self.remote, target_is_directory=True)
        with self.assertRaises(OSError):
            remote_stage.receive(str(alias), REQUEST, self.snapshot.digest, io.BytesIO(self.payload))

    def test_parallel_uploads_share_one_aggregate_budget(self):
        context = mp.get_context('spawn')
        barrier, queue = context.Barrier(2), context.Queue()
        workers = [context.Process(target=receiver_worker, args=(str(self.remote), ch*32, self.snapshot.digest,
                    self.payload, barrier, queue)) for ch in ('b','c')]
        for worker in workers: worker.start()
        for worker in workers:
            worker.join(15)
            if worker.is_alive(): worker.kill(); worker.join()
        for worker in workers:
            self.assertEqual(worker.exitcode,0)
        self.assertEqual(sorted(queue.get(timeout=2) for _ in workers), ['denied','staged'])
        queue.close(); queue.join_thread()

    def local_receiver(self, argv, **kwargs):
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertIn('PermitLocalCommand=no',argv)
        # Execute only the standalone file receiver on synthetic files. No SSH,
        # sbatch, shell script or LAMMPS engine runs in this integration test.
        return _capture(shlex.split(argv[-1]), **kwargs)

    def test_full_streaming_client_to_pinned_receiver_and_reserved_ledger(self):
        ledger = Ledger(self.root / 'ledger.sqlite')
        ledger.create_campaign('test', Policy(1000,8,100,RESOURCE.memory_bytes,100000,1,H))
        evaluation = ledger.register_evaluation('test',task_sha256=H,repetition=0,role='development',system_sha256=H)
        row = ledger.reserve(evaluation,'upload',self.snapshot.digest,RESOURCE)
        client = StageClient(self.endpoint,self.root / 'audit')
        service = StagingService(ledger,client)
        with patch('auto_lammps.staging._capture',side_effect=self.local_receiver) as capture:
            self.assertEqual(service.stage(row['id'],self.snapshot)['upload_state'],'staged')
            self.assertEqual(service.stage(row['id'],self.snapshot)['upload_state'],'staged')
            self.assertEqual(capture.call_count,1)
        self.assertEqual(ledger.get(row['id'])['dispatch_claimed'],0)
        self.assertEqual(ledger.summary('test')['reserved_storage_bytes'],RESOURCE.storage_bytes)
        self.assertTrue((self.remote / row['id'] / 'stage.json').exists())

    def reserved_ledger(self):
        ledger = Ledger(self.root / 'ledger.sqlite')
        ledger.create_campaign('test', Policy(1000,8,100,RESOURCE.memory_bytes,100000,1,H))
        evaluation = ledger.register_evaluation('test',task_sha256=H,repetition=0,role='development',system_sha256=H)
        row = ledger.reserve(evaluation,'upload',self.snapshot.digest,RESOURCE)
        return ledger, row

    def test_capacity_recovery_verifies_absence_and_keeps_same_request(self):
        ledger,row=self.reserved_ledger()
        service=StagingService(ledger,StageClient(self.endpoint,self.root/'recovery-audit'))
        self.policy(65536 + RESOURCE.storage_bytes - 1)
        with patch('auto_lammps.staging._capture',side_effect=self.local_receiver):
            with self.assertRaises(UploadUncertain):service.stage(row['id'],self.snapshot)
            self.assertFalse((self.remote/row['id']).exists())
            self.assertEqual(service.stage(row['id'],self.snapshot)['upload_state'],'upload_unresolved')
            self.assertEqual(len([e for e in ledger.events(row['id']) if e['kind']=='upload_intent']),1)
            self.policy(65536 + RESOURCE.storage_bytes)
            self.assertEqual(service.stage(row['id'],self.snapshot)['upload_state'],'staged')
            self.assertEqual(service.stage(row['id'],self.snapshot)['upload_state'],'staged')
        events=ledger.events(row['id'])
        self.assertEqual(len([e for e in events if e['kind']=='upload_intent']),2)
        self.assertEqual(len([e for e in events if e['kind']=='upload_failed']),1)
        self.assertEqual(len([e for e in events if e['kind']=='upload_reconciled_absent']),1)
        self.assertFalse(ledger.get(row['id'])['dispatch_claimed'])
        self.assertIsNone(ledger.get(row['id'])['job_id'])
        with self.assertRaises(Conflict):ledger.reconcile_staging_absent(row['id'],H)

    def test_partial_remote_upload_never_reuploads_or_overwrites(self):
        ledger,row=self.reserved_ledger()
        ledger.begin_staging(row['id']);ledger.staging_result(row['id'],error_type='UploadUncertain')
        with self.assertRaises(remote_stage.StageError):self.receive(self.payload[:-1],request=row['id'])
        before=(self.remote/row['id']/'allocation.json').read_bytes()
        service=StagingService(ledger,StageClient(self.endpoint,self.root/'partial-audit'))
        with patch('auto_lammps.staging._capture',side_effect=self.local_receiver):
            self.assertEqual(service.stage(row['id'],self.snapshot)['upload_state'],'upload_unresolved')
        self.assertEqual((self.remote/row['id']/'allocation.json').read_bytes(),before)
        self.assertEqual(len([e for e in ledger.events(row['id']) if e['kind']=='upload_intent']),1)

    def test_failed_upload_keeps_charge_and_is_not_retried(self):
        ledger, row = self.reserved_ledger()
        client = StageClient(self.endpoint,self.root / 'audit')
        service = StagingService(ledger,client)
        with patch.object(client,'upload',side_effect=UploadUncertain('synthetic disconnect')) as upload, \
                patch.object(client,'inspect_absence',side_effect=UploadUncertain('absence not verified')):
            with self.assertRaises(UploadUncertain):
                service.stage(row['id'],self.snapshot)
            self.assertEqual(service.stage(row['id'],self.snapshot)['upload_state'],'upload_unresolved')
            self.assertEqual(upload.call_count,1)
        self.assertEqual(ledger.get(row['id'])['charge_storage_bytes'],RESOURCE.storage_bytes)
        self.assertEqual(ledger.get(row['id'])['dispatch_claimed'],0)

    def test_upload_intent_write_failure_prevents_network(self):
        ledger, row = self.reserved_ledger()
        client = StageClient(self.endpoint,self.root / 'audit')
        with patch.object(ledger,'_event',side_effect=OSError('synthetic full disk')):
            with patch.object(client,'upload') as upload, self.assertRaises(OSError):
                StagingService(ledger,client).stage(row['id'],self.snapshot)
            upload.assert_not_called()
        self.assertFalse(any(e['kind']=='upload_intent' for e in ledger.events(row['id'])))

    def test_invalid_remote_receipt_never_marks_inputs_staged(self):
        import base64
        ledger,row=self.reserved_ledger()
        client=StageClient(self.endpoint,self.root / 'audit')
        response=dict(returncode=0,failure='',stdout=base64.b64encode(b'{"state":"staged"}').decode(),stderr='')
        with patch('auto_lammps.staging._capture',return_value=response), self.assertRaises(UploadUncertain):
            StagingService(ledger,client).stage(row['id'],self.snapshot)
        self.assertFalse(any(e['kind']=='inputs_staged' for e in ledger.events(row['id'])))

    def test_pinned_helper_mismatch_cannot_execute_receiver(self):
        client = StageClient(replace(self.endpoint,helper_sha256=H),self.root / 'audit')
        with patch('auto_lammps.staging._capture',side_effect=self.local_receiver), self.assertRaises(UploadUncertain):
            client.upload(Submission(REQUEST,self.snapshot.digest,RESOURCE),self.snapshot)
        self.assertFalse((self.remote / REQUEST).exists())

    def test_mismatched_resources_block_transport(self):
        client = StageClient(self.endpoint,self.root / 'audit')
        with patch('auto_lammps.staging._capture') as capture, self.assertRaises(Conflict):
            client.upload(Submission(REQUEST,self.snapshot.digest,replace(RESOURCE,cores=1)),self.snapshot)
        capture.assert_not_called()

    def test_streaming_input_timeout_and_large_duplex_response_are_bounded(self):
        result = _capture([sys.executable,'-c','import time; time.sleep(5)'],timeout=.1,max_bytes=1024,
                          input_chunks=(b'x'*65536 for _ in range(10)))
        self.assertEqual(result['failure'],'timeout')
        result = _capture([sys.executable,'-c','import sys; sys.stdout.write("x"*10000); sys.stdout.flush(); sys.stdin.buffer.read()'],
                          timeout=5,max_bytes=1024,input_chunks=(b'x'*65536 for _ in range(10)))
        self.assertEqual(result['failure'],'output_limit')

    def test_exact_batch_resources_and_no_shell_interpolation(self):
        environment = BatchEnvironment('test-partition','test-account','/service/requests','/usr/bin/python3',
                                       '/service/launcher.py',H)
        plan = render_batch(Submission(REQUEST,self.snapshot.digest,RESOURCE),environment)
        script=plan.script.decode()
        self.assertIn('#SBATCH --ntasks=2\n',script)
        self.assertIn('#SBATCH --time=0-00:01:00\n',script)
        self.assertIn('#SBATCH --mem=2M\n',script)
        self.assertIn('#SBATCH --export=NIL\n',script)
        self.assertIn('#SBATCH --no-requeue\n',script)
        import subprocess
        for gpu in ({'SLURM_JOB_GPUS':'0'}, {'SLURM_GPUS_ON_NODE':'1'}):
            result=subprocess.run(['/bin/sh'], input=script, text=True, capture_output=True,
                env={'PATH':'/usr/bin:/bin','SLURM_JOB_ID':'123',**gpu})
            self.assertEqual(result.returncode,96)  # Never reaches the nonexistent launcher.
        self.assertNotIn('#SBATCH --gpus',script)
        self.assertNotIn('#SBATCH --gres',script)
        self.assertEqual(plan.sha256,sha256(plan.script))
        with self.assertRaises(ValueError):
            render_batch(Submission(REQUEST,self.snapshot.digest,replace(RESOURCE,memory_bytes=100)),environment)
        with self.assertRaises(ValueError):
            render_batch(Submission(REQUEST,self.snapshot.digest,replace(RESOURCE,wall_seconds=61)),environment)
        with self.assertRaises(ValueError):
            replace(environment,partition='valid\n#SBATCH --exclusive')
        with self.assertRaises(ValueError):
            replace(environment,launcher_path='/service/requests/malicious.py')


class ExternalGeometryStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source'; self.source.mkdir(mode=0o700)
        (self.source/'input.in').write_bytes(b'# Synthetic inert input; never executed.\n')
        self.catalog = self.root/'geometry-catalog'; self.catalog.mkdir(mode=0o700)
        (self.catalog/'assets').mkdir(mode=0o700)
        self.data = b'# Synthetic inert geometry bytes; never executed.\n'
        self.asset = self.catalog/'assets'/'initial.data'; self.asset.write_bytes(self.data); self.asset.chmod(0o400)
        summary = dict(schema_version=1, parser='lammps_atomic_data', parser_version=1, parser_sha256=H,
            data_sha256=sha256(self.data), size=len(self.data), atom_style='atomic', units='metal',
            boundary=['p','p','p'], type_elements=['Fe'], atom_count=1, type_count=1, type_counts={'1':1},
            composition={'Fe':1}, cell_angstrom=[[1,0,0],[0,1,0],[0,0,1]], origin_angstrom=[0,0,0],
            tilt_angstrom=[0,0,0], masses_amu=[55.845], mass_source='file',
            coordinate_content_sha256=H, particle_id_order_sha256=H, id_policy='continuous_1_to_N_preserve_input_order',
            atom_record_columns=5, original_bytes_modified=False,
            elements_inferred=False, physical_evaluation_performed=False, scientifically_verified=False,
            image_flag_range=[-512,511],periodic_remapping_may_occur=False,fixed_boundary_geometry_verified=True)
        self.entry = dict(path='assets/initial.data', size=len(self.data), sha256=sha256(self.data), summary=summary)
        self.entry['pin'] = sha256(canonical({name:self.entry[name] for name in ('size','sha256','summary')}))
        self.catalog_value = dict(schema_version=1, entries=[self.entry])
        self.save_catalog(self.catalog_value)
        self.record = dict(path='structure.data', role='structure', size=len(self.data), sha256=sha256(self.data),
            external_source=dict(catalog_sha256=sha256((self.catalog/'catalog.json').read_bytes()), pin=self.entry['pin']))
        self.snapshot = freeze(self.source, self.root/'snapshots', files={'input.in':'lammps_input'},
            external_files={'structure.data':self.record}, entrypoint='input.in', resources=RESOURCE,
            provenance=dict(task_sha256=H,analysis_sha256=H,software_sha256=H))
        self.payload = b''.join(upload_chunks(self.snapshot))
        self.remote = self.root/'requests'; self.remote.mkdir(mode=0o700)
        (self.remote/'policy.json').write_bytes(canonical(dict(schema_version=1,
            max_total_bytes=65536+40*RESOURCE.storage_bytes, approval_sha256=H)))

    def save_catalog(self, value):
        versions = self.catalog/'versions'; versions.mkdir(mode=0o700, exist_ok=True)
        raw = canonical(value); version = versions/(sha256(raw)+'.json')
        if not version.exists(): version.write_bytes(raw); version.chmod(0o400)
        path = self.catalog/'catalog.json'
        if path.exists(): path.chmod(0o600)
        path.write_bytes(raw); path.chmod(0o400)

    def receive(self, payload=None, *, request=REQUEST, digest=None, catalog=None):
        return remote_stage.receive(str(self.remote), request, digest or self.snapshot.digest,
            io.BytesIO(self.payload if payload is None else payload),
            _geometry_catalog=str(self.catalog if catalog is None else catalog))

    def document_payload(self, value):
        raw = canonical(value)
        local = (self.source/'input.in').read_bytes()
        return struct.pack('!I', len(raw))+raw+local, sha256(raw)

    def test_sparse_roundtrip_copies_only_geometry_and_keeps_manifest_identity(self):
        document = self.snapshot.verify(); raw = (self.snapshot.path/'manifest.json').read_bytes()
        self.assertEqual(len(self.payload), 4+len(raw)+(self.source/'input.in').stat().st_size)
        self.assertFalse((self.snapshot.path/'structure.data').exists())
        receipt = self.receive(); target = self.remote/REQUEST/'structure.data'
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(target.stat().st_mode & 0o777, 0o400)
        self.assertNotEqual(target.stat().st_ino, self.asset.stat().st_ino)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual((self.remote/REQUEST/'manifest.json').read_bytes(), raw)
        self.assertEqual(receipt['manifest_sha256'], self.snapshot.digest)
        self.assertEqual(receipt['input_bytes'], sum(item['size'] for item in document['files']))
        self.assertEqual(receipt['storage_bytes'], RESOURCE.storage_bytes)
        self.assertFalse((self.remote/REQUEST/'catalog.json').exists())
        with self.assertRaises(remote_stage.StageError): self.receive()

    def test_read_only_catalog_inspection_removes_paths_and_keeps_private_source(self):
        result = remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))
        self.assertEqual(set(result), {'schema_version','catalog_sha256','entries'})
        self.assertEqual(result['catalog_sha256'], self.record['external_source']['catalog_sha256'])
        self.assertEqual(set(result['entries'][0]), {'pin','size','sha256','summary'})
        self.assertEqual(result['entries'][0]['summary'], self.entry['summary'])
        self.assertEqual(list(self.remote.iterdir()), [self.remote/'policy.json'])

    def test_pinned_standalone_helper_uses_fixed_sibling_for_sparse_upload_and_listing(self):
        import base64
        service = self.root/'service'; service.mkdir(mode=0o700)
        helper = service/'remote_stage.py'
        helper.write_bytes(Path(remote_stage.__file__).read_bytes()); helper.chmod(0o400)
        self.catalog.rename(service/'geometry-catalog'); self.catalog = service/'geometry-catalog'
        endpoint = StageEndpoint('synthetic-login', str(Path(sys.executable).resolve()), str(helper),
                                 sha256(helper.read_bytes()), str(self.remote))
        client = StageClient(endpoint, self.root/'audit')
        def local_receiver(argv, **kwargs):
            return _capture(shlex.split(argv[-1]), **kwargs)
        with patch('auto_lammps.staging._capture', side_effect=local_receiver):
            client.upload(Submission(REQUEST,self.snapshot.digest,RESOURCE), self.snapshot)
        receipt = json.loads((self.remote/REQUEST/'stage.json').read_bytes())
        self.assertEqual(receipt['input_bytes'], len(self.data)+(self.source/'input.in').stat().st_size)
        self.assertEqual((self.remote/REQUEST/'structure.data').read_bytes(), self.data)
        result = _capture([sys.executable,'-I',str(helper),'--list-geometry'],timeout=5,max_bytes=65536)
        self.assertEqual(result['returncode'],0)
        listing = json.loads(base64.b64decode(result['stdout']))
        self.assertNotIn('path', listing['entries'][0])
        self.assertEqual(listing['entries'][0]['pin'], self.entry['pin'])

    def test_catalog_and_source_identity_changes_reject_before_complete_receipt(self):
        original = self.snapshot.verify()
        changes = [lambda row:row['external_source'].update(catalog_sha256='0'*64),
                   lambda row:row['external_source'].update(pin='0'*64),
                   lambda row:row.update(size=row['size']+1), lambda row:row.update(sha256='0'*64)]
        for index, change in enumerate(changes):
            value = json.loads(json.dumps(original)); change(value['files'][1])
            payload, checksum = self.document_payload(value)
            request = f'{index:032x}'
            with self.subTest(index=index), self.assertRaises(remote_stage.StageError):
                self.receive(payload, digest=checksum, request=request)
            self.assertFalse((self.remote/request).exists())
        version = self.catalog/'versions'/(self.record['external_source']['catalog_sha256']+'.json')
        version.chmod(0o600); version.write_bytes(b'changed immutable version')
        with self.assertRaises(remote_stage.StageError): self.receive()
        self.assertFalse((self.remote/REQUEST).exists())

    def test_catalog_fields_pin_summary_size_and_limits_are_checked(self):
        cases = []
        for field, new in (('pin','0'*64), ('size',True), ('sha256','bad'), ('path','../escape')):
            value = json.loads(json.dumps(self.catalog_value)); value['entries'][0][field] = new; cases.append(value)
        value = json.loads(json.dumps(self.catalog_value)); value['entries'][0]['summary']['title'] = 'forbidden'; cases.append(value)
        value = json.loads(json.dumps(self.catalog_value)); value['entries'][0]['summary']['physical_evaluation_performed'] = True; cases.append(value)
        value = json.loads(json.dumps(self.catalog_value)); value['entries'][0]['summary']['type_elements'] = ['script();']; cases.append(value)
        value = json.loads(json.dumps(self.catalog_value)); value['entries'] *= 129; cases.append(value)
        value = json.loads(json.dumps(self.catalog_value)); value['extra'] = 'forbidden'; cases.append(value)
        for value in cases:
            self.save_catalog(value)
            with self.subTest(value=value), self.assertRaises(remote_stage.StageError):
                remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))
        oversized = self.catalog/'catalog.json'; oversized.chmod(0o600); oversized.write_bytes(b' '*1_000_001)
        with self.assertRaises(remote_stage.StageError):
            remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))

    def test_summary_cannot_export_paths_scripts_or_inconsistent_numeric_metadata(self):
        changes = [('id_policy','/private/author/solution'), ('mass_source','shell private-script'),
                   ('atom_count','/private/target'), ('type_count',True), ('type_counts',{'1':'run 0'}),
                   ('type_counts',{'1':2}), ('composition',{'Fe':True}),
                   ('cell_angstrom',[[1,0,0],[0,'/private/file',0],[0,0,1]]),
                   ('cell_angstrom',[[1,1,0],[0,1,0],[0,0,1]]), ('origin_angstrom',['script',0,0]),
                   ('tilt_angstrom',[1,0,0]), ('masses_amu',['shell']), ('masses_amu',[0]),
                   ('image_flag_range',[0,511]), ('periodic_remapping_may_occur','/private/path'),
                   ('fixed_boundary_geometry_verified',False), ('atom_record_columns','5'),
                   ('type_elements',['Xx']), ('size',True), ('parser_sha256','/private/parser')]
        for field, new in changes:
            value = json.loads(json.dumps(self.catalog_value)); entry = value['entries'][0]
            entry['summary'][field] = new
            entry['pin'] = sha256(canonical({name:entry[name] for name in ('size','sha256','summary')}))
            self.save_catalog(value)
            with self.subTest(field=field,new=new), self.assertRaises(remote_stage.StageError):
                remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))
        self.assertEqual(list(self.remote.iterdir()), [self.remote/'policy.json'])

    def test_summary_rejects_nonfinite_or_degenerate_cells_and_preserves_unknown_mass(self):
        summary = json.loads(json.dumps(self.entry['summary']))
        for cell in ([[float('inf'),0,0],[0,1,0],[0,0,1]], [[1e-10,0,0],[0,1e-10,0],[0,0,1e-10]],
                     [[1,0,0],[1e308,1,0],[0,0,1]]):
            value = {**summary,'cell_angstrom':cell,'tilt_angstrom':[cell[1][0],cell[2][0],cell[2][1]]}
            with self.subTest(cell=cell), self.assertRaises(remote_stage.StageError):
                remote_stage.validate_geometry_summary(value)
        value = {**summary,'masses_amu':None,'mass_source':'not_in_file'}
        self.assertEqual(remote_stage.validate_geometry_summary(value), value)

    def test_summary_integer_volume_overflow_is_a_controlled_error(self):
        value = json.loads(json.dumps(self.catalog_value)); entry = value['entries'][0]
        entry['summary']['cell_angstrom'] = [[10**300,0,0],[0,10**300,0],[0,0,10**300]]
        with self.assertRaisesRegex(remote_stage.StageError, 'Numerically degenerate geometry cell'):
            remote_stage.validate_geometry_summary(entry['summary'])
        entry['pin'] = sha256(canonical({name:entry[name] for name in ('size','sha256','summary')}))
        self.save_catalog(value)
        with self.assertRaisesRegex(remote_stage.StageError, 'Numerically degenerate geometry cell'):
            remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))
        self.assertEqual(list(self.remote.iterdir()), [self.remote/'policy.json'])

    def test_external_role_schema_metadata_and_storage_overflow_reject_before_creation(self):
        original = self.snapshot.verify()
        changes = [lambda doc:doc['files'][1].update(role='potential'),
                   lambda doc:doc['files'][1].update(role='unknown'), lambda doc:doc.update(schema_version=1),
                   lambda doc:doc['files'][1]['external_source'].update(path='/untrusted'),
                   lambda doc:doc['files'][1].update(size=RESOURCE.storage_bytes)]
        for index, change in enumerate(changes):
            value = json.loads(json.dumps(original)); change(value)
            payload, checksum = self.document_payload(value); request = f'{index:032x}'
            with self.subTest(index=index), self.assertRaises(remote_stage.StageError):
                self.receive(payload, digest=checksum, request=request)
            self.assertFalse((self.remote/request).exists())

    def test_source_size_or_bytes_changed_keeps_failed_allocation(self):
        for index, data in enumerate((self.data+b'changed', b'x'*len(self.data))):
            self.asset.chmod(0o600); self.asset.write_bytes(data); self.asset.chmod(0o400)
            request = f'{index:032x}'
            with self.subTest(index=index), self.assertRaises(remote_stage.StageError):
                self.receive(request=request)
            self.assertTrue((self.remote/request/'allocation.json').exists())
            self.assertFalse((self.remote/request/'stage.json').exists())

    def test_geometry_file_hard_link_symlink_and_linked_directory_rejected(self):
        outside = self.root/'outside.data'; outside.write_bytes(self.data); outside.chmod(0o400)
        self.asset.unlink(); self.asset.symlink_to(outside)
        with self.assertRaises(OSError): self.receive(request='1'*32)
        self.asset.unlink(); os.link(outside, self.asset)
        with self.assertRaises(remote_stage.StageError): self.receive(request='2'*32)
        self.asset.unlink(); self.asset.write_bytes(self.data); self.asset.chmod(0o400)
        assets = self.catalog/'assets'; assets.rename(self.root/'assets')
        assets.symlink_to(self.root/'assets', target_is_directory=True)
        with self.assertRaises(OSError): self.receive(request='3'*32)
        for request in ('1'*32,'2'*32,'3'*32):
            self.assertFalse((self.remote/request/'stage.json').exists())

    def test_catalog_symlinks_hardlinks_and_nonprivate_directories_rejected(self):
        alias = self.root/'catalog-alias'; alias.symlink_to(self.catalog, target_is_directory=True)
        with self.assertRaises(OSError): self.receive(catalog=alias)
        path = self.catalog/'catalog.json'; original = self.root/'catalog-original'; path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(OSError): remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))
        path.unlink(); os.link(original, path)
        with self.assertRaises(remote_stage.StageError): remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))
        path.unlink(); original.rename(path)
        self.catalog.chmod(0o755)
        with self.assertRaises(remote_stage.StageError): self.receive()
        self.catalog.chmod(0o700); (self.catalog/'assets').chmod(0o755)
        with self.assertRaises(remote_stage.StageError): self.receive(request='4'*32)

    def test_sparse_partial_tamper_trailing_and_extra_geometry_bytes_do_not_commit(self):
        for index, payload in enumerate((self.payload[:-1], self.payload[:-1]+b'x',
                                         self.payload+b'trailing', self.payload+self.data)):
            request = f'{index:032x}'
            with self.subTest(index=index), self.assertRaises(remote_stage.StageError):
                self.receive(payload, request=request)
            self.assertTrue((self.remote/request/'allocation.json').exists())
            self.assertFalse((self.remote/request/'stage.json').exists())

    def test_catalog_or_source_change_during_copy_never_commits(self):
        original_copy = remote_stage.copy_geometry_source
        def copy_and_change(directory, item, output):
            original_copy(directory, item, output)
            version = self.catalog/'versions'/(self.record['external_source']['catalog_sha256']+'.json')
            version.chmod(0o600); version.write_bytes(b'changed immutable version')
        with patch.object(remote_stage, 'copy_geometry_source', side_effect=copy_and_change):
            with self.assertRaises(remote_stage.StageError): self.receive()
        self.assertFalse((self.remote/REQUEST/'stage.json').exists())

    def test_new_current_catalog_keeps_prior_frozen_structure_usable(self):
        self.save_catalog(dict(schema_version=1, entries=[]))
        self.assertEqual(remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))['entries'], [])
        receipt = self.receive()
        self.assertEqual(receipt['manifest_sha256'], self.snapshot.digest)
        self.assertEqual((self.remote/REQUEST/'structure.data').read_bytes(), self.data)

    def test_current_catalog_requires_identical_fixed_version_for_listing(self):
        version = self.catalog/'versions'/(self.record['external_source']['catalog_sha256']+'.json')
        version.unlink()
        with self.assertRaises(remote_stage.StageError): remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))
        version.write_bytes(b'wrong version'); version.chmod(0o400)
        with self.assertRaises(remote_stage.StageError): remote_stage.inspect_geometry_catalog(_geometry_catalog=str(self.catalog))

    def test_version_directory_or_version_file_links_are_rejected(self):
        versions = self.catalog/'versions'; versions.rename(self.root/'versions')
        versions.symlink_to(self.root/'versions', target_is_directory=True)
        with self.assertRaises(OSError): self.receive()
        versions.unlink(); (self.root/'versions').rename(versions)
        version = versions/(self.record['external_source']['catalog_sha256']+'.json')
        outside = self.root/'catalog-version'; version.rename(outside); version.symlink_to(outside)
        with self.assertRaises(OSError): self.receive()
        version.unlink(); os.link(outside, version)
        with self.assertRaises(remote_stage.StageError): self.receive()
        version.unlink(); outside.rename(version); versions.chmod(0o755)
        with self.assertRaises(remote_stage.StageError): self.receive()

    def test_source_mutation_during_copy_is_rejected_even_with_same_bytes(self):
        fstat = os.fstat; inode = self.asset.stat().st_ino; seen = 0
        def mutate_after_read(fd):
            nonlocal seen
            info = fstat(fd)
            if info.st_ino == inode and stat.S_ISREG(info.st_mode):
                seen += 1
                if seen == 2:
                    self.asset.chmod(0o600); self.asset.write_bytes(self.data); self.asset.chmod(0o400)
                    info = fstat(fd)
            return info
        with patch.object(remote_stage.os, 'fstat', side_effect=mutate_after_read):
            with self.assertRaises(remote_stage.StageError): self.receive()
        self.assertFalse((self.remote/REQUEST/'stage.json').exists())

    def test_geometry_cli_only_lists_fixed_metadata_and_rejects_path_argument(self):
        metadata = dict(schema_version=1,catalog_sha256=H,entries=[])
        with patch.object(sys,'argv',['receiver','--list-geometry']), patch.object(remote_stage,'inspect_geometry_catalog',return_value=metadata) as inspect, patch.object(remote_stage,'receive') as receive, patch('builtins.print'):
            self.assertEqual(remote_stage.main(),0)
            inspect.assert_called_once_with(); receive.assert_not_called()
        for argv in (['receiver','--list-geometry','--geometry-catalog',str(self.catalog)],
                     ['receiver','--list-geometry','--root',str(self.remote)], ['receiver','--request-id',REQUEST]):
            with patch.object(sys,'argv',argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                remote_stage.main()
            self.assertEqual(error.exception.code,2)


if __name__ == '__main__':
    unittest.main()
