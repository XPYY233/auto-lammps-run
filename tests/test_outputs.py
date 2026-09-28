"""Synthetic output transfer through real local subprocesses; no physics/SSH."""
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_runtime_launcher as fixtures
from auto_lammps import outputs, runtime_launcher as runtime
from auto_lammps.ledger import Conflict, Ledger, LimitExceeded, Policy
from auto_lammps.staging import StageEndpoint


class OutputTests(unittest.TestCase):
    private = fixtures.RuntimeTests.private

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        self.ledger = Ledger(self.root/'ledger.sqlite')
        self.ledger.create_campaign('synthetic', Policy(1000, 8, 120, 128*1024*1024, 16*1024*1024, 1, fixtures.H))
        self.evaluation = self.ledger.register_evaluation('synthetic', task_sha256=fixtures.H,
            repetition=0, role='agent', system_sha256=fixtures.H)
        row = self.ledger.reserve(self.evaluation, 'first', self.digest, fixtures.RESOURCES)
        self.request_id = row['id']
        target = self.case.parent/self.request_id
        self.case.rename(target)
        self.case = target
        self.endpoint = StageEndpoint('fixture-login','/usr/bin/python3','/approved/runtime_launcher.py',
                                      fixtures.H,self.profile['requests_root'])
        self.collector = outputs.OutputCollector(self.ledger,self.endpoint,self.root/'collected')
        self.launcher = self.root/'runtime_launcher.py'
        self.launcher.write_bytes(Path(runtime.__file__).read_bytes())
        self.local_command = [sys.executable,'-I',str(self.launcher),'--collect','--root',self.profile['requests_root'],
                              '--request-id',self.request_id,'--manifest-sha256',self.digest,'--job-id','123']
        self.ledger.begin_dispatch(self.request_id)
        self.ledger.accepted(self.request_id,'123',{'synthetic':True})
        self.ledger.observe(self.request_id,'123','completed',{'synthetic':True})
        self.ledger.account(self.request_id,1,fixtures.H)
        self.private(self.case/'execution-intent.json',runtime.canonical(dict(request_id=self.request_id,
            manifest_sha256=self.digest,job_id='123',outputs=fixtures.OUTPUTS)))
        self.private(self.case/'execution-result.json',runtime.canonical(dict(request_id=self.request_id,
            job_id='123',returncode=0,timed_out=False,scientific_status='not_evaluated')))
        (self.case/'output').mkdir(mode=0o700)
        for name in fixtures.OUTPUTS:
            self.private(self.case/'output'/name,b'synthetic fixture bytes\n')
        self.private(self.case/'scheduler.stdout',b'synthetic scheduler stream\n')
        self.private(self.case/'scheduler.stderr',b'')

    def wire(self):
        stream = io.BytesIO()
        header = runtime.collect(self.profile_path,self.request_id,self.digest,'123',
                                 self.profile['requests_root'],stream)
        return header,stream.getvalue()

    def context(self):
        return dict(request_id=self.request_id,manifest_sha256=self.digest,job_id='123',
                    payload_bytes=fixtures.RESOURCES.storage_bytes)

    def local_transfer(self):
        real_transfer = outputs.transfer
        return patch.object(outputs,'transfer',side_effect=lambda argv,receiver,**kwargs:
                            real_transfer(self.local_command,receiver,**kwargs))

    def test_full_local_helper_transfer_duplicate_and_history_do_not_resubmit(self):
        large = bytes(range(256))*1025
        self.private(self.case/'output/trajectory.dump',large)
        initial = self.ledger.summary('synthetic')
        with self.local_transfer() as transfer:
            first = self.collector.fetch(self.request_id)
            second = self.collector.fetch(self.request_id)
        self.assertEqual(first,second)
        self.assertEqual(transfer.call_count,1)
        self.assertEqual(first['state'],'collected')
        self.assertEqual(first['scientific_status'],'not_evaluated')
        self.assertEqual((Path(first['directory'])/'payload/output/log.lammps').read_bytes(),b'synthetic fixture bytes\n')
        self.assertEqual((Path(first['directory'])/'payload/output/trajectory.dump').read_bytes(),large)
        argv = transfer.call_args.args[0]
        self.assertEqual(argv[0],'ssh')
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertIn('--collect',argv[-1])
        self.assertNotIn('sbatch',argv[-1])
        final = self.ledger.summary('synthetic')
        self.assertEqual(final['reserved_storage_bytes']-initial['reserved_storage_bytes'],fixtures.RESOURCES.storage_bytes+262144)
        self.assertEqual(self.ledger.get(self.request_id)['actual_core_seconds'],1)
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],1)
        self.assertEqual([e['kind'] for e in self.ledger.events(self.request_id)].count('output_fetch_finished'),1)

    def test_live_unaccounted_and_conflicted_jobs_do_not_call_transport(self):
        for state,accounted in [('running',0),('completed',0),('reconcile_required',1)]:
            with self.subTest(state=state,accounted=accounted):
                with self.ledger._transaction() as db:
                    db.execute('UPDATE requests SET state=?,accounted=? WHERE id=?',(state,accounted,self.request_id))
                with patch.object(outputs,'transfer') as transfer:
                    with self.assertRaises(Conflict):self.collector.fetch(self.request_id)
                    transfer.assert_not_called()

    def test_failed_and_incomplete_execution_are_not_scientific_success(self):
        self.private(self.case/'execution-result.json',runtime.canonical(dict(request_id=self.request_id,
            job_id='123',returncode=1,timed_out=False,scientific_status='not_evaluated')))
        header,_ = self.wire()
        self.assertEqual(header['execution']['returncode'],1)
        (self.case/'execution-result.json').unlink()
        (self.case/'output/trajectory.dump').unlink()
        with self.local_transfer():result=self.collector.fetch(self.request_id)
        self.assertEqual(result['state'],'collected')
        self.assertEqual(result['header']['execution']['state'],'incomplete')
        self.assertEqual(result['header']['missing_outputs'],['output/trajectory.dump'])
        self.assertEqual(result['scientific_status'],'not_evaluated')

    def test_failure_before_execution_collects_only_scheduler_diagnostics(self):
        (self.case/'execution-intent.json').unlink()
        (self.case/'execution-result.json').unlink()
        self.private(self.case/'scheduler-result.json',runtime.canonical(dict(state='accepted',
            request_id=self.request_id,manifest_sha256=self.digest,job_id='123')))
        with self.local_transfer():result=self.collector.fetch(self.request_id)
        self.assertEqual(result['state'],'collected')
        self.assertEqual({v['path'] for v in result['header']['files']},{'scheduler.stdout','scheduler.stderr'})
        self.assertEqual(result['header']['execution']['state'],'incomplete')

    def test_source_links_wrong_job_and_undeclared_files(self):
        self.private(self.case/'output/hidden.txt',b'must never be transferred')
        header,_ = self.wire()
        self.assertNotIn('output/hidden.txt',[item['path'] for item in header['files']])
        with self.assertRaises(runtime.ExecutionDenied):
            runtime.collect(self.profile_path,self.request_id,self.digest,'999',self.profile['requests_root'],io.BytesIO())
        target=self.case/'output/log.lammps'; original=target.read_bytes();target.unlink()
        target.symlink_to(self.case/'scheduler.stdout')
        with self.assertRaises(OSError):self.wire()
        target.unlink();os.link(self.case/'scheduler.stdout',target)
        with self.assertRaises(runtime.ExecutionDenied):self.wire()
        target.unlink();self.private(target,original)
        folder=self.case/'output';folder.rename(self.case/'original-output');folder.symlink_to(self.case/'original-output')
        with self.assertRaises(OSError):self.wire()

    def test_remote_mutation_after_header_is_detected(self):
        target=self.case/'output/log.lammps'
        class MutatingStream(io.BytesIO):
            def write(stream,block):
                if stream.tell()==0:target.write_bytes(b'changed\n')
                return super().write(block)
        with self.assertRaises(runtime.ExecutionDenied):
            runtime.collect(self.profile_path,self.request_id,self.digest,'123',self.profile['requests_root'],MutatingStream())

    def test_truncation_retains_failed_copy_charge_and_retry_never_resubmits(self):
        _,wire=self.wire()
        broken=self.root/'broken-wire';broken.write_bytes(wire[:-4])
        real_transfer=outputs.transfer
        command=[sys.executable,'-I','-c','import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())',str(broken)]
        with patch.object(outputs,'transfer',side_effect=lambda argv,receiver,**kw:real_transfer(command,receiver,**kw)):
            failed=self.collector.fetch(self.request_id)
        self.assertEqual(failed['state'],'collection_failed')
        with self.local_transfer():retried=self.collector.fetch(self.request_id)
        self.assertEqual(retried['state'],'collected')
        self.assertNotEqual(failed['directory'],retried['directory'])
        self.assertTrue((Path(failed['directory'])/'receipt.json').is_file())
        self.assertEqual(self.ledger.get(self.request_id)['charge_storage_bytes'],
                         fixtures.RESOURCES.storage_bytes+2*(fixtures.RESOURCES.storage_bytes+262144))
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],1)

    def test_transport_timeout_and_stderr_limit_reap_local_child(self):
        for label,code,timeout in [('timeout','import time;time.sleep(3)',.05),
                ('stderr','import sys;sys.stderr.write("x"*100000)',1)]:
            receiver=outputs.Receiver(self.root/label,self.context())
            result=outputs.transfer([sys.executable,'-I','-c',code],receiver,timeout=timeout,max_bytes=1000)
            receiver.close()
            self.assertTrue(result['failure'])
            self.assertIsNotNone(result['returncode'])

    def test_parser_rejects_identity_escape_budget_hash_and_trailing_bytes(self):
        original,wire=self.wire()
        for index,kind in enumerate(('identity','escape','budget','duplicate','missing_receipt','hash','trailing','oversized_header')):
            header=json.loads(json.dumps(original));prefix=4+struct.unpack('!I',wire[:4])[0]
            if kind=='identity':header['request_id']='f'*32
            if kind=='escape':header['files'][0]['path']='../outside'
            if kind=='budget':header['files'][0]['size']=fixtures.RESOURCES.storage_bytes+1
            if kind=='duplicate':header['files'].append(header['files'][0])
            if kind=='missing_receipt':header['files']=[v for v in header['files'] if v['path']!='execution-result.json']
            if kind=='hash':header['files'][-1]['sha256']='0'*64
            encoded=runtime.canonical(header)
            modified=struct.pack('!I',len(encoded))+encoded+wire[prefix:]
            if kind=='trailing':modified+=b'extra'
            if kind=='oversized_header':modified=struct.pack('!I',65537)
            receiver=outputs.Receiver(self.root/('reject-'+str(index)),self.context())
            try:
                with self.subTest(kind=kind),self.assertRaises(ValueError):
                    # Exercise fragmented length, header and file boundaries.
                    for start in range(0,len(modified),17):receiver.feed(modified[start:start+17])
                    receiver.finish()
            finally:receiver.close()

    def test_cached_tamper_blocks_reuse_and_audit_failure_blocks_network(self):
        with patch.object(outputs,'_write_new',side_effect=OSError('synthetic disk full')),patch.object(outputs,'transfer') as transfer:
            with self.assertRaises(OSError):self.collector.fetch(self.request_id)
            transfer.assert_not_called()
        with self.local_transfer():result=self.collector.fetch(self.request_id)
        target=Path(result['directory'])/'payload/output/log.lammps'
        target.chmod(0o600);target.write_bytes(b'tampered')
        with patch.object(outputs,'transfer') as transfer:
            with self.assertRaises(ValueError):self.collector.fetch(self.request_id)
            transfer.assert_not_called()

    def test_storage_reservation_is_atomic_and_prevents_download(self):
        allowance=fixtures.RESOURCES.storage_bytes+262144
        # Charge previously retained copies until just less than one more fits.
        with self.ledger._transaction() as db:
            db.execute('UPDATE requests SET charge_storage_bytes=? WHERE id=?',
                       (16*1024*1024-allowance+1,self.request_id))
        with patch.object(outputs,'transfer') as transfer:
            with self.assertRaises(LimitExceeded):self.collector.fetch(self.request_id)
            transfer.assert_not_called()

    def test_concurrent_reservations_cannot_spend_the_last_copy_allowance_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        allowance=fixtures.RESOURCES.storage_bytes+262144
        with self.ledger._transaction() as db:
            db.execute('UPDATE requests SET charge_storage_bytes=? WHERE id=?',
                       (16*1024*1024-allowance,self.request_id))
        ready=threading.Barrier(2)
        def reserve():
            ready.wait(timeout=5)
            try:
                self.ledger.begin_output_fetch(self.request_id)
                return 'reserved'
            except LimitExceeded:
                return 'denied'
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:reserve(),range(2)))
        self.assertCountEqual(results,['reserved','denied'])
        self.assertEqual(self.ledger.summary('synthetic')['reserved_storage_bytes'],16*1024*1024)

    def test_worker_process_crash_keeps_charge_and_releases_collection_lock(self):
        # A real worker exits after writing synthetic partial bytes. No child
        # engine, network connection or scheduler is involved.
        code='''import os,sys
from auto_lammps import outputs
from auto_lammps.ledger import Ledger
from auto_lammps.staging import StageEndpoint
def crash(argv,receiver,**kwargs):
    (receiver.folder/'output/partial.txt').write_bytes(b'synthetic interrupted transfer')
    os._exit(7)
outputs.transfer=crash
endpoint=StageEndpoint('fixture-login','/usr/bin/python3','/approved/runtime_launcher.py','a'*64,sys.argv[3])
outputs.OutputCollector(Ledger(sys.argv[1]),endpoint,sys.argv[2]).fetch(sys.argv[4])
'''
        process=subprocess.run([sys.executable,'-c',code,str(self.ledger.path),str(self.collector.directory),
                                self.profile['requests_root'],self.request_id],capture_output=True,timeout=10)
        self.assertEqual(process.returncode,7,process.stderr.decode())
        starts=[json.loads(e['payload']) for e in self.ledger.events(self.request_id) if e['kind']=='output_fetch_started']
        self.assertEqual(len(starts),1)
        self.assertTrue((self.collector.directory/starts[0]['ticket']/'payload/output/partial.txt').is_file())
        with self.local_transfer():result=self.collector.fetch(self.request_id)
        self.assertEqual(result['state'],'collected')
        self.assertEqual(self.ledger.get(self.request_id)['charge_storage_bytes'],
                         fixtures.RESOURCES.storage_bytes+2*(fixtures.RESOURCES.storage_bytes+262144))
        self.assertEqual(self.ledger.evaluation_snapshot(self.evaluation)['dispatch_claims'],1)

    def test_conflicting_collection_completion_cannot_replace_history(self):
        context=self.ledger.begin_output_fetch(self.request_id)
        self.ledger.finish_output_fetch(self.request_id,context['ticket'],collected=False,evidence_sha256=fixtures.H)
        self.ledger.finish_output_fetch(self.request_id,context['ticket'],collected=False,evidence_sha256=fixtures.H)
        with self.assertRaises(Conflict):
            self.ledger.finish_output_fetch(self.request_id,context['ticket'],collected=True,evidence_sha256=fixtures.H)
        context=self.ledger.begin_output_fetch(self.request_id)
        with self.ledger._transaction() as db:db.execute("UPDATE requests SET state='reconcile_required' WHERE id=?",(self.request_id,))
        with self.assertRaises(Conflict):
            self.ledger.finish_output_fetch(self.request_id,context['ticket'],collected=True,evidence_sha256=fixtures.H)


if __name__=='__main__':
    unittest.main()
