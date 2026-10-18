"""Ordinary recovery action against real retained P/inventory; fake transport."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import unittest
from unittest.mock import patch

from auto_lammps.manifest import sha256
import test_workbench_targets_web as fixture


@unittest.skipUnless(importlib.util.find_spec('auto_research') is not None,
    'separately installed literature workbench is optional')
class ReferenceTargetUnknownReissueWebTests(unittest.TestCase):
    def test_same_original_body_survives_later_publication_without_rewriting_old_unknown(self):
        f=fixture.WorkbenchTargetsWebTests();f.setUp();self.addCleanup(f.doCleanups)
        f.complete_inventory()
        base='/api/tasks/'+f.task['id']+'/reference-target-analysis'
        parent='e'*32;child='f'*32;sent=[]
        def uncertain(body,*args):
            sent.append(body)
            raise TimeoutError('Synthetic unknown; no external request')
        with patch('auto_lammps.model_connections.https_transport',uncertain):
            old=f.client.post(base,json=dict(request_id=parent),headers=fixture.HEADERS)
        self.assertEqual(old.status_code,200,old.text)
        self.assertEqual(old.json()['state'],'unknown')
        charged=f.f.calls.status()['used_requests']
        publications=f.bridge.history(f.task['id'])
        newer=deepcopy(publications[-1]);newer['request_id']='b'*32
        with patch.object(f.bridge,'history',return_value=publications+[newer]):
            menu=f.client.get(base).json()
            option=next(row for row in menu['unknown_reissues'] if row['unknown_reissue_of']==parent)
            self.assertTrue(option['enabled'],option)
            self.assertEqual(f.f.calls.status()['used_requests'],charged)
            reader=f.bridge.runtime.native_paper_sources_reader
            changed=deepcopy(reader(f.binding.workbench_paper_id));changed['pdf_sha256']='0'*64
            data=dict(request_id=child,unknown_reissue_of=parent,supplier_checked=True,retain_unknown_charge=True)
            with patch.object(f.bridge,'runtime',replace(f.bridge.runtime,native_paper_sources_reader=lambda _:changed)):
                denied=f.client.post(base+'/unknown-reissue',json=data,headers=fixture.HEADERS)
                self.assertEqual(denied.status_code,422,denied.text)
            self.assertEqual(f.f.calls.status()['used_requests'],charged)
            denied=f.client.post(base+'/unknown-reissue',json={**data,'supplier_checked':False},headers=fixture.HEADERS)
            self.assertEqual(denied.status_code,422,denied.text)
            self.assertEqual(f.f.calls.status()['used_requests'],charged)
            recovered=f.client.post(base+'/unknown-reissue',json=data,headers=fixture.HEADERS)
            self.assertEqual(recovered.status_code,200,recovered.text)
            self.assertEqual(recovered.json()['state'],'completed',recovered.json())
            self.assertEqual(f.f.calls.lookup(parent)['request_sha256'],sha256(sent[0]))
            self.assertEqual(f.f.calls.lookup(child)['request_sha256'],sha256(sent[0]))
            self.assertEqual(f.f.calls.lookup(parent)['receipt']['state'],'unknown')
            self.assertEqual(f.f.calls.status()['used_requests'],charged+1)
            menu=f.client.get(base).json();last=menu['history'][-1]
            self.assertEqual(last['state'],'completed',last)
            self.assertTrue(last['original_source_preserved'])
            again=f.client.post(base+'/unknown-reissue',json=data,headers=fixture.HEADERS)
            self.assertEqual(again.status_code,200,again.text)
            self.assertEqual(f.f.calls.status()['used_requests'],charged+1)

    def test_known_invalid_recovery_revises_complete_original_proposal_in_round_three(self):
        f=fixture.WorkbenchTargetsWebTests();f.setUp();self.addCleanup(f.doCleanups)
        f.complete_inventory();base='/api/tasks/'+f.task['id']+'/reference-target-analysis'
        parent='e'*32;recovery='f'*32;revision='1'*32
        with patch('auto_lammps.model_connections.https_transport',side_effect=TimeoutError('Synthetic unknown')):
            first=f.client.post(base,json=dict(request_id=parent),headers=fixture.HEADERS)
        self.assertEqual(first.json()['state'],'unknown',first.text)
        publications=f.bridge.history(f.task['id']);newer=deepcopy(publications[-1]);newer['request_id']='b'*32
        def invalid_quote(*args):
            status,body=f.response(*args);value=json.loads(body)
            answer=json.loads(value['choices'][0]['message']['content'])
            answer['targets'][0]['citations'][0]['quote']='SYNTHETIC_QUOTE_NOT_PRESENT'
            value['choices'][0]['message']['content']=json.dumps(answer)
            return status,json.dumps(value).encode()
        with patch.object(f.bridge,'history',return_value=publications+[newer]):
            with patch('auto_lammps.model_connections.https_transport',invalid_quote):
                second=f.client.post(base+'/unknown-reissue',json=dict(request_id=recovery,
                    unknown_reissue_of=parent,supplier_checked=True,retain_unknown_charge=True),headers=fixture.HEADERS)
            self.assertEqual(second.json()['state'],'unavailable',second.text)
            charged=f.f.calls.status()['used_requests']
            third=f.client.post(base+'/'+recovery+'/revise-original',json=dict(request_id=revision),headers=fixture.HEADERS)
            self.assertEqual(third.status_code,200,third.text)
            self.assertEqual(third.json()['state'],'completed',third.json())
            self.assertTrue(third.json()['original_source_preserved'])
            payload=json.loads(f.sent[-1]['messages'][-1]['content'])
            self.assertEqual(payload['reference_target_revision']['complete_previous_answer']['targets'][0]['citations'][0]['quote'],
                'SYNTHETIC_QUOTE_NOT_PRESENT')
            self.assertEqual(f.f.calls.status()['used_requests'],charged+1)
            self.assertEqual(len(f.client.get(base).json()['history']),3)
            again=f.client.post(base+'/'+recovery+'/revise-original',json=dict(request_id=revision),headers=fixture.HEADERS)
            self.assertEqual(again.status_code,200,again.text)
            self.assertEqual(f.f.calls.status()['used_requests'],charged+1)
            fourth=f.client.post(base+'/'+recovery+'/revise-original',json=dict(request_id='2'*32),headers=fixture.HEADERS)
            self.assertEqual(fourth.status_code,422,fourth.text)
            self.assertEqual(f.f.calls.status()['used_requests'],charged+1)
            menu=f.client.get(base).json();last=menu['history'][-1]
            self.assertEqual(last['state'],'completed',last)
            self.assertTrue(last['original_source_preserved'])
