"""Synthetic original-source/binary binding; no interpreter or model execution."""
from copy import deepcopy
from unittest.mock import Mock
import unittest

from auto_lammps.author_program_metadata import runtime_python_candidates, python_entrypoints
from auto_lammps.author_python_dependencies import FLAGS, project_capabilities
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError


class AuthorProgramMetadataTests(unittest.TestCase):
    def setUp(self):
        self.task='a'*32
        self.entry=dict(path='original/structure.py',sha256='b'*64,imports=['numpy'],
            required_distributions=['numpy'],local_files=[],missing=[],parse_status='parsed',
            source_requires_child_review=False)
        self.requirements=dict(version=1,**FLAGS,task_id=self.task,source_sha256='c'*64,
            entries=[deepcopy(self.entry)],required_distributions=['numpy'])
        self.binary=dict(id='python',path='/synthetic/bin/python',real_path='/synthetic/bin/python3',sha256='d'*64,
            status='dependencies_unverified',distributions=[dict(name='numpy',state='installed',version='synthetic')])
        self.report=dict(native_files=[dict(path='/synthetic/lmp',sha256='e'*64,status='verified')],
            python_candidates=[deepcopy(self.binary)])
        self.projection=project_capabilities(self.requirements,self.report)
        self.latest=dict(state='incomplete',request_id='f'*32,report=deepcopy(self.report),
            report_sha256=sha256(canonical(self.report)),capabilities=deepcopy(self.projection))
        self.inventory=Mock();self.inventory.capabilities.return_value=deepcopy(self.projection)
        self.complete=[{key:self.entry[key] for key in ('path','sha256')}]

    def test_optional_other_distribution_gap_does_not_promote_or_block_binary(self):
        rows,projection=runtime_python_candidates(self.inventory,self.task,self.latest)
        self.assertEqual(rows,[self.binary]);self.assertEqual(rows[0]['status'],'dependencies_unverified')
        self.assertEqual(projection,self.projection)
        self.inventory.capabilities.assert_called_once_with(self.task,'f'*32)
        self.assertEqual(python_entrypoints(rows[0],projection,self.complete),(self.complete,[]))

    def test_legacy_ready_binary_is_retained_but_does_not_gain_python_source_entries(self):
        del self.latest['capabilities'];self.latest['state']='metadata_ready'
        self.latest['report']['python_candidates'][0]['status']='verified'
        rows,projection=runtime_python_candidates(self.inventory,self.task,self.latest)
        self.assertIsNone(projection);self.inventory.capabilities.assert_not_called()
        self.assertEqual(python_entrypoints(rows[0],projection,self.complete),([],['original_python_dependency_metadata_missing']))

    def test_unknown_incomplete_legacy_and_changed_native_do_not_admit_binary(self):
        for mode in ('unknown','legacy_incomplete','native_changed'):
            with self.subTest(mode=mode):
                value=deepcopy(self.latest)
                if mode=='unknown':value['state']='unknown'
                elif mode=='legacy_incomplete':del value['capabilities']
                else:value['report']['native_files'][0]['status']='changed'
                with self.assertRaises(TaskError):runtime_python_candidates(self.inventory,self.task,value)

    def test_current_source_profile_or_connection_change_is_not_hidden_by_saved_projection(self):
        self.inventory.capabilities.side_effect=TaskError('current binding changed')
        with self.assertRaisesRegex(TaskError,'binding'):runtime_python_candidates(self.inventory,self.task,self.latest)

    def test_forged_task_report_binary_or_distribution_identity_is_rejected(self):
        for mode in ('task','report','binary','distribution','raw_status'):
            with self.subTest(mode=mode):
                value=deepcopy(self.latest)
                if mode=='task':value['capabilities']['task_id']='9'*32
                elif mode=='report':value['report_sha256']='1'*64
                elif mode=='binary':value['report']['python_candidates'][0]['sha256']='2'*64
                elif mode=='distribution':value['report']['python_candidates'][0]['distributions']=[]
                else:value['report']['python_candidates'][0]['status']='missing'
                # Rebinding both advertised/current views is still insufficient
                # if the unchanged original report's actual bytes disagree.
                self.inventory.capabilities.return_value=deepcopy(value['capabilities'])
                with self.assertRaises(TaskError):runtime_python_candidates(self.inventory,self.task,value)

    def test_incomplete_parse_dependency_child_and_virtualenv_gaps_remain_unavailable(self):
        for mode in ('partial','parse','dependency','child','venv','local'):
            with self.subTest(mode=mode):
                projection=deepcopy(self.projection);binary=deepcopy(self.binary);complete=deepcopy(self.complete)
                entry=projection['interpreters'][0]['entries'][0]
                if mode=='partial':complete[0]['sha256']='0'*64
                elif mode=='parse':entry['parse_status']='invalid'
                elif mode=='dependency':entry.update(state='missing',missing=['distribution_metadata_missing: numpy'])
                elif mode=='child':entry['source_requires_child_review']=True
                elif mode=='venv':
                    binary['path']='/unproven-venv/bin/python'
                    projection['interpreters'][0]['path']=binary['path']
                else:entry['local_files']=[dict(path='original/helper.py',sha256='3'*64)]
                admitted,missing=python_entrypoints(binary,projection,complete)
                self.assertEqual(admitted,[]);self.assertTrue(missing)

    def test_missing_analysis_dependency_does_not_hide_independent_structure_entry(self):
        requirements=deepcopy(self.requirements)
        requirements['entries'].append(dict(self.entry,path='analysis.py',required_distributions=['matplotlib']))
        requirements['required_distributions']=['matplotlib','numpy']
        projection=project_capabilities(requirements,self.report)
        admitted,missing=python_entrypoints(self.binary,projection,self.complete)
        self.assertEqual(admitted,self.complete)
        self.assertEqual(missing[0]['path'],'analysis.py')
        self.assertFalse(projection['execution_authorized']);self.assertFalse(projection['child_process_policy_verified'])


if __name__=='__main__':unittest.main()
