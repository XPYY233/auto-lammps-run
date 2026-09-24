"""Synthetic source archives and help text; no compiler or physics invocation."""
import io
import tarfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps.engine_preparation import (EnginePreparation, EnginePreparationError,
    requirements_from_models, inspect_help, assess_engine, unpack_source, cmake_plan)

COMMIT = 'a'*40
MODEL = {'state':'finished', 'bindings':[{'required_package':'MEAM','pair_style':'meam'}]}
HELP = b'''Large-scale Atomic/Molecular Massively Parallel Simulator - 2 Aug 2023
MPI v3.1: Synthetic MPI
Installed packages:

MEAM MANYBODY

List of individual style options included in this LAMMPS executable
* Pair styles:
meam meam/spline zero
* Bond styles:
zero
'''


def source_archive(path, *, release='2 Aug 2023', extra=None):
    with tarfile.open(path, 'w:gz') as archive:
        files = {'LICENSE': b'Synthetic license, not LAMMPS code',
                 'src/version.h': ('#define LAMMPS_VERSION "'+release+'"\n').encode(),
                 'src/MEAM/pair_meam.cpp': b'// non-executable test data',
                 'cmake/CMakeLists.txt': b'# never executed',
                 'examples/in.any': b'never copy target inputs'}
        for name, data in files.items():
            item=tarfile.TarInfo('lammps-'+COMMIT+'/'+name);item.size=len(data)
            archive.addfile(item,io.BytesIO(data))
        if extra:
            archive.addfile(extra,io.BytesIO(b'x') if extra.isfile() else None)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.requirements=requirements_from_models(MODEL,release='2 Aug 2023',cores=8)

    def fetch(self,url,destination,*,maximum):
        self.assertEqual(url,'https://codeload.github.com/lammps/lammps/tar.gz/'+COMMIT)
        source_archive(destination)

    def test_prepare_pinned_source_without_running_it(self):
        service=EnginePreparation(self.root/'audit',fetch=self.fetch)
        with patch('subprocess.Popen',side_effect=AssertionError('no compiler or simulator')):
            report=service.prepare_source(self.requirements,commit=COMMIT,storage_bytes=16*1024*1024)
        self.assertEqual(report['state'],'source_ready')
        self.assertEqual(report['files'],4)
        folder=service.root/report['id']
        self.assertFalse((folder/'source/examples').exists())
        self.assertTrue((folder/'inventory.json').exists())
        self.assertFalse(report['built']);self.assertFalse(report['environment_verified'])
        self.assertFalse(report['execution_authorized'])

    def test_wrong_release_retains_failure_and_original_archive(self):
        def wrong(url,destination,*,maximum):source_archive(destination,release='28 Mar 2023')
        service=EnginePreparation(self.root/'audit',fetch=wrong)
        report=service.prepare_source(self.requirements,commit=COMMIT,storage_bytes=16*1024*1024)
        self.assertEqual(report['state'],'failed')
        self.assertIn('release',report['failure'])
        self.assertTrue((service.root/report['id']/'source.tar.gz').exists())
        self.assertTrue((service.root/report['id']/'result.json').exists())

    def test_traversal_links_duplicate_and_oversize_rejected(self):
        extras=[]
        for name in ('../escape','lammps-'+COMMIT+'/src/../../escape','lammps-'+COMMIT+'/LICENSE'):
            item=tarfile.TarInfo(name);item.size=1;extras.append(item)
        for type_ in (tarfile.SYMTYPE,tarfile.LNKTYPE):
            item=tarfile.TarInfo('lammps-'+COMMIT+'/src/link');item.type=type_;item.linkname='/etc/passwd';extras.append(item)
        for i,item in enumerate(extras):
            archive=self.root/f'{i}.tar.gz';source_archive(archive,extra=item)
            with self.assertRaises((EnginePreparationError,FileExistsError)):
                unpack_source(archive,self.root/f'out{i}',COMMIT)
        archive=self.root/'limit.tar.gz';source_archive(archive)
        with self.assertRaisesRegex(EnginePreparationError,'budget'):
            unpack_source(archive,self.root/'limited',COMMIT,maximum=10)
        self.assertFalse((self.root/'escape').exists())

    def test_internal_symlink_materialized_and_escape_or_chain_rejected(self):
        item=tarfile.TarInfo('lammps-'+COMMIT+'/src/copied-version.h')
        item.type=tarfile.SYMTYPE;item.linkname='version.h'
        archive=self.root/'internal.tar.gz';source_archive(archive,extra=item)
        report=unpack_source(archive,self.root/'internal',COMMIT)
        copied=self.root/'internal/src/copied-version.h'
        self.assertFalse(copied.is_symlink())
        self.assertEqual(copied.read_bytes(),(copied.parent/'version.h').read_bytes())
        record=next(r for r in report['files'] if r['path']=='src/copied-version.h')
        self.assertEqual(record['archive_symlink'],'version.h')
        for i,target in enumerate(('../../outside','../examples/in.any','copied-version.h')):
            item.linkname=target
            archive=self.root/f'link-{i}.tar.gz';source_archive(archive,extra=item)
            with self.assertRaises(EnginePreparationError):
                unpack_source(archive,self.root/f'rejected-{i}',COMMIT)

    def test_declared_MPI_package_and_exact_update_must_match(self):
        observed=inspect_help(HELP)
        result=assess_engine(self.requirements,observed)
        self.assertTrue(result['metadata_matches']);self.assertFalse(result['environment_verified'])
        cases=[HELP.replace(b'MEAM MANYBODY',b'MANYBODY'),HELP.replace(b'meam meam/spline',b'meam/spline'),
               HELP.replace(b'Synthetic MPI',b'LAMMPS MPI STUBS'),HELP.replace(b'2 Aug 2023',b'2 Aug 2023 - Update 1')]
        for raw in cases:
            self.assertFalse(assess_engine(self.requirements,inspect_help(raw))['metadata_matches'])
        with self.assertRaises(EnginePreparationError):inspect_help(HELP.split(b'* Pair styles')[0])
        with self.assertRaises(EnginePreparationError):inspect_help(HELP+HELP)

    def test_build_plan_uses_MPI_MEAM_and_never_target_input(self):
        plan=cmake_plan(self.requirements,source='/private/source',build='/private/build',
                        cmake='/usr/bin/cmake',compiler='/usr/bin/g++',mpi_compiler='/trusted/bin/mpicxx')
        self.assertIn('BUILD_MPI=ON',plan['configure']);self.assertIn('PKG_MEAM=ON',plan['configure'])
        self.assertEqual(plan['build'][-2:],['--parallel','8'])
        self.assertTrue(plan['accounting_required']);self.assertFalse(plan['execution_authorized'])
        self.assertFalse(plan['target_input_allowed'])
        with self.assertRaises(EnginePreparationError):
            cmake_plan(self.requirements,source='/private/source',build='/private/source/build',
                       cmake='/usr/bin/cmake',compiler='/usr/bin/g++',mpi_compiler='/trusted/bin/mpicxx')

    def test_missing_requirements_unknown_packages_or_budget_are_not_defaults(self):
        for cores in (0,9,True):
            with self.assertRaises(EnginePreparationError):requirements_from_models(MODEL,release='2 Aug 2023',cores=cores)
        with self.assertRaises(EnginePreparationError):requirements_from_models({},release='2 Aug 2023',cores=8)
        service=EnginePreparation(self.root/'audit',fetch=self.fetch)
        for budget in (0,True,17*1024**3):
            with self.assertRaises(EnginePreparationError):service.prepare_source(self.requirements,commit=COMMIT,storage_bytes=budget)


class RemoteEngineTests(unittest.TestCase):
    fetch = EngineTests.fetch

    def setUp(self):
        EngineTests.setUp(self)
        self.calls=0
        self.remote=self.root/'hpc'
        self.identifier='b'*32

    def remote_capture(self, command, **options):
        import base64,json,os
        import auto_lammps.engine_source_worker as worker
        self.assertEqual(command[0],'ssh')
        self.assertIn('example-hpc',command)
        self.assertIn('StrictHostKeyChecking=yes',command)
        self.assertEqual(options['timeout'],240)
        envelope=json.loads(b''.join(options['input_chunks']))
        original=worker.EnginePreparation
        def counted(url,destination,*,maximum):
            self.calls+=1
            self.fetch(url,destination,maximum=maximum)
        with patch.object(worker.sys,'platform','linux'), patch.dict(os.environ,{'SSH_CONNECTION':'synthetic'}), patch.object(worker,'EnginePreparation',side_effect=lambda root: original(root,fetch=counted)):
            report=worker.remote_main(envelope['request'],envelope['source'])
        return dict(returncode=0,failure='',stdout=base64.b64encode(json.dumps(report).encode()).decode(),stderr='')

    def service(self, capture=None):
        from auto_lammps.engine_preparation import RemoteEnginePreparation
        return RemoteEnginePreparation(self.root/'local-audit',host_alias='example-hpc',
            remote_directory=str(self.remote),operation_id=self.identifier,capture=capture or self.remote_capture)

    def prepare(self, service):
        return service.prepare_source(self.requirements,commit=COMMIT,storage_bytes=16*1024**2)

    def test_remote_source_and_local_receipts_only_retry_is_readonly(self):
        service=self.service()
        report=self.prepare(service)
        self.assertEqual(report['state'],'source_ready')
        self.assertTrue((self.remote/self.identifier/'source.tar.gz').exists())
        self.assertFalse(list(service.root.rglob('*.tar.gz')))
        self.assertFalse(list(service.root.rglob('version.h')))
        self.assertEqual(self.prepare(service),report)
        self.assertEqual(self.calls,1)
        with self.assertRaisesRegex(EnginePreparationError,'bound'):
            service.prepare_source(self.requirements,commit='c'*40,storage_bytes=16*1024**2)

    def test_uncertain_transport_reconciles_same_operation_without_new_download(self):
        def lost(*args,**kwargs):
            self.remote_capture(*args,**kwargs)
            return dict(returncode=None,failure='timeout',stdout='',stderr='')
        first=self.prepare(self.service(lost))
        self.assertEqual(first['state'],'unknown')
        final=self.prepare(self.service())
        self.assertEqual(final['state'],'source_ready')
        self.assertEqual(self.calls,1)

    def test_existing_pending_lock_is_observed_without_execution(self):
        import json
        service=self.service()
        def locked(command,**options):
            request=json.loads(b''.join(options['input_chunks']))['request']
            self.remote.mkdir(mode=0o700)
            (self.remote/(self.identifier+'.lock.json')).write_text(json.dumps(request))
            return self.remote_capture(command,**options)
        self.assertEqual(self.prepare(self.service(locked))['state'],'unknown')
        self.assertEqual(self.calls,0)

    def test_wrong_receipt_cannot_claim_success(self):
        import base64,json
        def tampered(*args,**kwargs):
            result=self.remote_capture(*args,**kwargs)
            report=json.loads(base64.b64decode(result['stdout']))
            report['commit']='d'*40
            result['stdout']=base64.b64encode(json.dumps(report).encode()).decode()
            return result
        result=self.prepare(self.service(tampered))
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['observation_failure'],'invalid_remote_receipt')

    def test_local_real_download_refused_before_network(self):
        import auto_lammps.engine_source_worker as worker
        with patch.object(worker.sys,'platform','darwin'), patch.object(worker.urllib.request,'build_opener') as opener:
            with self.assertRaisesRegex(EnginePreparationError,'remote HPC'):
                worker.download_source('https://codeload.github.com/lammps/lammps/tar.gz/'+COMMIT,
                    self.root/'forbidden.tar.gz',maximum=100)
            opener.assert_not_called()
        self.assertFalse((self.root/'forbidden.tar.gz').exists())

    def test_paper_history_preserves_unknown_then_ready(self):
        from auto_lammps.tasks import TaskStore
        from auto_lammps.papers import PaperStore
        from auto_lammps.engine_preparation import prepare_paper_engine
        from auto_lammps.manifest import canonical
        papers=PaperStore(TaskStore(self.root/'tasks.sqlite'))
        paper=papers.add('Synthetic engine test','10.1234/synthetic','Full scope','Test')
        # Fixture inserts already-tested model acquisition; no production shortcut.
        with papers.tasks.transaction() as db:
            doc=papers._read(db,paper['id']);doc['potential_acquisition']=MODEL
            papers._write(db,doc,'synthetic_model_fixture')
        policy=dict(release='2 Aug 2023',commit=COMMIT,cores=8,storage_bytes=16*1024**2,
            host_alias='example-hpc',remote_directory=str(self.remote),operation_id=self.identifier)
        unknown=prepare_paper_engine(papers,paper['id'],self.root/'audit',policy,
            capture=lambda *args,**kw:dict(returncode=None,failure='timeout',stdout='',stderr=''))
        self.assertEqual(unknown['state'],'unknown')
        for _ in range(2):
            ready=prepare_paper_engine(papers,paper['id'],self.root/'audit',policy,capture=self.remote_capture)
        self.assertEqual(ready['state'],'source_ready')
        saved=papers.get(paper['id'])
        events=[x['event'] for x in saved['history'] if x['event'].startswith('engine_source_prepared:')]
        self.assertEqual(len(events),2)
        self.assertTrue(events[0].endswith(':unknown'));self.assertTrue(events[1].endswith(':source_ready'))
