"""Pure source/metadata proofs; no imports of author or scientific modules."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from auto_lammps.author_python_dependencies import (requirements_from_sources,project_capabilities,
    validate_requirements,validate_capabilities)
from auto_lammps.manifest import sha256
from auto_lammps.tasks import TaskError
import test_author_environment_inventory as inventory_fixture
import test_author_environment_workspace as workspace_fixture

TASK='a'*32


def checked_sources(texts,*,incomplete=()):
    files=[];sources=[];reads=[]
    for index,(path,text) in enumerate(texts.items()):
        raw=text.encode();pin=sha256(raw);lines=text.splitlines(keepends=True)
        file=dict(path=path,sha256=pin,size=len(raw));files.append(file)
        if path in incomplete:continue
        for start in range(0,len(lines),400):
            chunk=''.join(lines[start:start+400]);sid='source-'+str(index)+'-'+str(start)
            sources.append(dict(id=sid,text=chunk))
            reads.append(dict(file,source_id=sid,state='read',start_line=start+1,end_line=min(start+400,len(lines)),
                returned_end_line=min(start+400,len(lines)),total_lines=len(lines),truncated=start>0 or start+400<len(lines),
                text_sha256=sha256(chunk.encode()),request_sha256=sha256(sid.encode())))
    return dict(files=files,sources=sources,reads=reads,capability=dict(files=files,reference_task_id=TASK))


class AuthorPythonDependencyTests(unittest.TestCase):
    def test_complete_source_maps_aliases_and_local_closure_without_importing(self):
        checked=checked_sources({'HEA.py':'import numpy as np\nimport pandas as pd\nimport random\n',
            'plot.py':'from scipy.stats import linregress\nimport matplotlib.pyplot as plt\nfrom mpl_toolkits import axes_grid1\n',
            'driver.py':'import helper\n','helper.py':'import numpy\n'})
        with patch('subprocess.Popen',side_effect=AssertionError('No execution')):
            result=requirements_from_sources(checked,TASK)
        hea=next(row for row in result['entries'] if row['path']=='HEA.py')
        self.assertEqual(hea['required_distributions'],['numpy','pandas']);self.assertEqual(hea['parse_status'],'parsed')
        self.assertFalse(hea['source_requires_child_review']);self.assertEqual(hea['missing'],[])
        driver=next(row for row in result['entries'] if row['path']=='driver.py')
        self.assertEqual(driver['required_distributions'],['numpy']);self.assertEqual(driver['local_files'][0]['path'],'helper.py')
        self.assertEqual(result['required_distributions'],['matplotlib','numpy','pandas','scipy'])
        self.assertFalse(result['execution_authorized'])

    def test_child_network_and_dynamic_calls_include_module_aliases(self):
        examples=['import subprocess as sp\nsp.run(["ssh"])\n','from os import system as run\nrun("parallel")\n',
            'import os as system\nsystem.spawnv(0,"x",[])\n','import socket as s\n',
            'import importlib as il\nil.import_module("numpy")\n','__import__("numpy")\n',
            'import requests as net\n','from subprocess import Popen as process\n',
            'import ctypes as c\n','import runpy as runner\n','import marshal as loader\n',
            'import builtins as b\nb.__import__("numpy")\n','from builtins import eval as run\nrun("pass")\n',
            'import os as operating\ngetattr(operating,"system")("parallel")\n']
        for text in examples:
            with self.subTest(text=text):
                entry=requirements_from_sources(checked_sources({'driver.py':text}),TASK)['entries'][0]
                self.assertTrue(entry['source_requires_child_review']);self.assertEqual(entry['parse_status'],'parsed')

    def test_incomplete_parse_invalid_and_unknown_imports_never_claim_dependencies_complete(self):
        sources=checked_sources({'partial.py':'import numpy\n','bad.py':'def unfinished(\n',
            'unknown.py':'import unspecified_private_module\n'},incomplete={'partial.py'})
        req=requirements_from_sources(sources,TASK);entries={row['path']:row for row in req['entries']}
        self.assertEqual(entries['partial.py']['parse_status'],'incomplete')
        self.assertEqual(entries['partial.py']['required_distributions'],[])
        self.assertEqual(entries['bad.py']['parse_status'],'invalid')
        self.assertTrue(all(row['missing'] for row in entries.values()))
        self.assertTrue(entries['partial.py']['source_requires_child_review'])

    def test_contiguous_long_source_and_original_sha_are_required(self):
        checked=checked_sources({'long.py':'# Original inert text\n'*420+'import numpy\n'})
        self.assertEqual(requirements_from_sources(checked,TASK)['entries'][0]['parse_status'],'parsed')
        checked['sources'][-1]['text']='import pandas\n'
        with self.assertRaises(TaskError):requirements_from_sources(checked,TASK)

    def test_metadata_projection_keeps_raw_candidate_and_each_entry_independent(self):
        req=requirements_from_sources(checked_sources({'raw.py':'import numpy\nimport pandas\n',
            'stdlib.py':'import csv\n','plot.py':'import scipy\n'}),TASK)
        report=dict(state='incomplete',native_files=[dict(status='verified')],python_candidates=[dict(
            id='python',path='/fixed/python',real_path='/fixed/python',sha256='b'*64,status='dependencies_unverified',
            distributions=[dict(name=name,state='installed' if name!='scipy' else 'missing_metadata',
                version='1.0' if name!='scipy' else None) for name in req['required_distributions']])])
        before=deepcopy(report);cap=project_capabilities(req,report)
        self.assertEqual(before,report);self.assertTrue(cap['base_native_ready'])
        entries={row['path']:row for row in cap['interpreters'][0]['entries']}
        self.assertEqual(entries['raw.py']['state'],'metadata_verified');self.assertEqual(entries['stdlib.py']['state'],'metadata_verified')
        self.assertEqual(entries['plot.py']['missing'],['distribution_metadata_missing_metadata: scipy'])
        self.assertEqual(cap['interpreters'][0]['raw_status'],'dependencies_unverified')
        self.assertEqual(validate_capabilities(cap),cap)
        self.assertFalse(cap['author_entrypoints_reviewed'])

    def test_forged_flags_cross_task_and_omitted_query_union_are_rejected(self):
        req=requirements_from_sources(checked_sources({'raw.py':'import numpy\n'}),TASK)
        for change in (dict(task_id='b'*32),dict(required_distributions=[]),dict(execution_authorized=True),dict(B_input=0)):
            with self.subTest(change=change),self.assertRaises(TaskError):validate_requirements(dict(req,**change),TASK)


class EntrypointInventoryTests(unittest.TestCase):
    def setUp(self):
        self.f=inventory_fixture.AuthorEnvironmentInventoryTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.req=requirements_from_sources(checked_sources({'raw.py':'import numpy\nimport pandas\n','plot.py':'import scipy\n'}),self.f.task)
        self.f.input.update(required_distributions=self.req['required_distributions'],python_dependency_requirements=self.req)

    def test_original_inspect_queries_union_once_and_preserves_raw_missing_report(self):
        f=self.f;f.missing={'scipy'}
        result=f.inspect();raw=deepcopy(result['report'])
        self.assertEqual(result['state'],'incomplete');self.assertEqual(raw['python_candidates'][0]['status'],'dependencies_unverified')
        self.assertTrue(result['capabilities']['base_native_ready'])
        menu=f.service.planner_environments(f.task,'1'*32)
        self.assertEqual([row['kind'] for row in menu['environments'][0]['executables']],['python','lammps','shell'])
        self.assertEqual(menu['metadata_status'],'incomplete');self.assertEqual(menu['environment_evidence'],raw)
        self.assertEqual(f.service.capabilities(f.task),result['capabilities'])
        self.assertEqual(f.service.get(f.task,'1'*32)['report'],raw)
        self.assertEqual(f.capture.call_count,1);self.assertEqual(f.probe_count,1)
        self.assertEqual(f.network[0]['request']['required_distributions'],['numpy','pandas','scipy'])

    def test_current_source_connection_and_profile_changes_reject_saved_projection_without_query(self):
        f=self.f;f.inspect()
        self.f.input['python_dependency_requirements']['source_sha256']='f'*64
        with self.assertRaisesRegex(TaskError,'变化'):f.service.capabilities(f.task)
        self.f.input['python_dependency_requirements']['source_sha256']=self.req['source_sha256']
        # req and provider share the object; restore using the retained binding.
        original=f.service._stored(f.task)[0]['context']['binding']['python_dependency_requirements']
        f.input['python_dependency_requirements']=deepcopy(original)
        f.hpc.credential_sha256='e'*64
        with self.assertRaisesRegex(TaskError,'变化'):f.service.capabilities(f.task)
        self.assertEqual(f.capture.call_count,1)

    def test_legacy_receipt_is_preserved_when_new_source_requirements_arrive(self):
        f=self.f;f.input.pop('python_dependency_requirements');f.input['required_distributions']=[]
        old=f.inspect();self.assertNotIn('capabilities',old)
        f.input.update(required_distributions=self.req['required_distributions'],python_dependency_requirements=self.req)
        self.assertEqual(f.service.get(f.task,'1'*32),old)
        with self.assertRaises(TaskError):f.service.capabilities(f.task,'1'*32)
        self.assertEqual(f.capture.call_count,1)


class EntrypointWorkspaceTests(unittest.TestCase):
    def test_plot_dependency_missing_reaches_adapter_without_invalidating_base_metadata(self):
        f=workspace_fixture.AuthorEnvironmentWorkspaceTests();f.setUp();self.addCleanup(f.doCleanups)
        req=requirements_from_sources(checked_sources({'raw.py':'import numpy\n','plot.py':'import scipy\n'}),f.task)
        f.env.input.update(required_distributions=req['required_distributions'],python_dependency_requirements=req)
        f.env.missing={'scipy'};receipt=f.inspect();self.assertEqual(receipt['state'],'incomplete')
        before=f.env.capture.call_count;binding=f.check()
        metadata=binding['deployment']['python_dependency_metadata']
        self.assertTrue(metadata['base_native_ready'])
        self.assertEqual(metadata['environment_report_sha256'],receipt['report_sha256'])
        self.assertEqual(metadata['interpreters'][0]['raw_status'],'dependencies_unverified')
        from auto_lammps.author_reference_plan import _deployment
        self.assertEqual(_deployment(binding['deployment'])['python_dependency_metadata'],metadata)
        self.assertFalse(any('全部所需依赖元数据' in gap for gap in binding['deployment']['missing']))
        self.assertEqual(f.env.capture.call_count,before);f.unchanged()

if __name__=='__main__':unittest.main()
