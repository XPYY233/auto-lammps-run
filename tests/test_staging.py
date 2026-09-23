"""File-only transfer checks. No scheduler or physics engine is invoked."""
from dataclasses import replace
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import shlex
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
        for path in ('../escape', '/escape', 'output/trajectory', 'job.sh', 'allocation.json', 'a//x', 'a/../x'):
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

    def test_failed_upload_keeps_charge_and_is_not_retried(self):
        ledger, row = self.reserved_ledger()
        client = StageClient(self.endpoint,self.root / 'audit')
        service = StagingService(ledger,client)
        with patch.object(client,'upload',side_effect=UploadUncertain('synthetic disconnect')) as upload:
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
        self.assertEqual(plan.sha256,sha256(plan.script))
        with self.assertRaises(ValueError):
            render_batch(Submission(REQUEST,self.snapshot.digest,replace(RESOURCE,memory_bytes=100)),environment)
        with self.assertRaises(ValueError):
            render_batch(Submission(REQUEST,self.snapshot.digest,replace(RESOURCE,wall_seconds=61)),environment)
        with self.assertRaises(ValueError):
            replace(environment,partition='valid\n#SBATCH --exclusive')
        with self.assertRaises(ValueError):
            replace(environment,launcher_path='/service/requests/malicious.py')


if __name__ == '__main__':
    unittest.main()
