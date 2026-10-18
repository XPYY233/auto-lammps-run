"""Full-source synthetic parsing/statistics and pure geometric worker integration."""
from copy import deepcopy
from importlib import metadata
import io
import json
import unittest
from unittest.mock import Mock, patch

from auto_lammps import coordination_analysis as coordination
from auto_lammps import retained_analysis_bridge as bridge
from auto_lammps.analysis import AnalysisError
from auto_lammps.manifest import canonical, sha256
from auto_lammps.output_analysis import _validate_output, selection_context
import test_coordination_analysis as geometry
import test_output_analysis as fixture


def scalar_data(count=60001):
    # The unseen interior outlier detects accidentally computing from excerpts.
    return ('# Synthetic scalar samples\n# TimeStep v_signal v_other\n'+''.join(
        f'{i} {i+.25+(1000000 if i==count//2 else 0):.16e} {1.:.16e}\n'
        for i in range(count))).encode('ascii')


def scalar_table(file, count=60001):
    return dict(file=file,format='lammps_ave_time_scalar',
        headers=['# Synthetic scalar samples','# TimeStep v_signal v_other'],
        columns=[dict(name='step',unit='step',source='TimeStep'),
            dict(name='signal',unit='1',source='v_signal'),dict(name='other',unit='1',source='v_other')],
        steps=dict(first=0,last=count-1,stride=1))


def mixed_plan(files, count=60001, *, with_figure=False):
    tables=[];operations=[]
    for index, (file, data) in enumerate(files.items(),1):
        if data.startswith(b'ITEM: TIMESTEP'):
            table=deepcopy(geometry.TABLE);table['file']=file
            operation=deepcopy(geometry.OPERATION);operation.update(id='ordering'+str(index),file=file)
        else:
            table=scalar_table(file,count)
            operation=dict(id='statistics'+str(index),method='summary',file=file,x='step',y='signal',window=[0,count-1])
        tables.append(table);operations.append(operation)
    evidence=[dict(target=table['file'],citations=['method']) for table in tables]
    evidence += [dict(target=op['id'],citations=['method']) for op in operations]
    plan=dict(mode=bridge.MODE,plan=dict(tables=tables,operations=operations),figures=[],method_evidence=evidence)
    if with_figure:
        index=next(i for i,op in enumerate(operations,1) if op['method']==coordination.METHOD)
        plan['figures']=[dict(id='selected_ordering',method='scatter',table='result'+str(index),x='type_i',y='alpha_ij')]
    return plan


class RetainedBridgeContractTests(unittest.TestCase):
    def test_bounded_preview_counts_the_entire_input_and_does_not_show_interior_outlier(self):
        data=scalar_data()
        inventory,preview=bridge.describe(data)
        self.assertEqual(inventory['line_count'],60003)
        self.assertEqual(inventory['numeric_line_count'],60001)
        self.assertEqual(inventory['sha256'],sha256(data))
        self.assertTrue(inventory['preview']['only_preview'])
        self.assertFalse(inventory['preview']['complete_source_shown'])
        self.assertLess(len(preview),bridge.MAX_PREVIEW_BYTES+100)
        self.assertNotIn('1.0300002500000000e+06',preview)
        self.assertEqual(inventory['preview']['lines'][-1]['line'],60003)

    def test_explicit_schema_exposes_both_modes_and_unchanged_adapter_identities(self):
        evidence=dict(role='reference',input_representation=bridge.REPRESENTATION)
        menu=selection_context(evidence)
        self.assertEqual(menu['model_plan_schema']['oneOf'][0]['properties']['version']['const'],1)
        native=menu['model_plan_schema']['oneOf'][1]
        self.assertEqual(native['properties']['mode']['const'],bridge.MODE)
        self.assertEqual(set(native['properties']),{'mode','plan','figures','method_evidence'})
        identity=menu['mixed_native_tool']['adapter_identity']['mixed_analysis']
        self.assertEqual(identity['structural_statistics'],coordination.adapter_identity())
        self.assertFalse(menu['physical_evaluation_performed'])

    def test_all_sources_and_methods_need_explicit_declarations(self):
        files={'source1':scalar_data(5),'source2':geometry.synthetic_bcc_dump()}
        original=mixed_plan(files,5)
        bridge.validate_plan(original,files)
        for change in ('source','operation','evidence','extra','automatic_window','automatic_frame','too_many_files'):
            plan=deepcopy(original);inputs=files
            if change=='source':plan['plan']['tables'].pop()
            if change=='operation':plan['plan']['operations'].pop()
            if change=='evidence':plan['method_evidence'].pop()
            if change=='extra':plan['plan']['tables'][0]['path']='elsewhere'
            if change=='automatic_window':plan['plan']['operations'][0]['window']='all'
            if change=='automatic_frame':plan['plan']['operations'][1]['frames']='last'
            if change=='too_many_files':inputs={f'source{i}':b'1\n' for i in range(33)}
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError,KeyError)):
                bridge.validate_plan(plan,inputs)

    def test_parser_and_operation_evidence_must_use_authenticated_quote_ids(self):
        files={'source1':scalar_data(5)};plan=mixed_plan(files,5)
        expected=bridge.column_units(plan,files)
        value=dict(status='ready',summary='Synthetic only',limitations=[],plan=plan,
            citations=[dict(id='method',source_id='request',quote='Synthetic data')],
            column_evidence=[dict(table=t,column=c,meaning='Synthetic meaning',unit=u,citations=['method'])
                for (t,c),u in expected.items()])
        evidence=dict(input_representation=bridge.REPRESENTATION,
            sources=[dict(id='request',origin='user',locator='request',text='Synthetic data')])
        _validate_output(evidence,value,files)
        value['plan']['method_evidence'][0]['citations']=['unavailable']
        with self.assertRaisesRegex(ValueError,'引用'):_validate_output(evidence,value,files)

    def test_preview_gap_cannot_be_quoted_as_original_source_text(self):
        data=scalar_data(20);_,preview=bridge.describe(data)
        gap='[OMITTED SOURCE LINES: PREVIEW ONLY]'
        self.assertIn(gap,preview)
        evidence=dict(sources=[dict(id='source1',origin='output',locator='synthetic.dat',text=preview)])
        value=dict(status='needs_evidence',summary='Synthetic',limitations=[],plan=None,column_evidence=[],
            citations=[dict(id='gap',source_id='source1',quote=gap)])
        with self.assertRaisesRegex(ValueError,'间隔'):_validate_output(evidence,value,{'source1':data})

    def test_source_limits_are_bounded_and_old_table_tool_constants_are_unchanged(self):
        self.assertEqual(bridge.tables.MAX_TABLES,16)
        self.assertEqual(bridge.tables.MAX_ROWS,100000)
        self.assertEqual(bridge.analysis_v2.MAX_V3_SOURCES,29)
        with patch.object(bridge,'MAX_ROWS',2),self.assertRaises(AnalysisError):bridge.describe(b'1\n2\n3\n')
        with patch.object(bridge,'MAX_BYTES',3),self.assertRaises(AnalysisError):bridge.check_files({'source1':b'1\n2\n'})
        with self.assertRaises(AnalysisError):bridge.describe(b'1\x00\n')

    def test_full_numeric_native_result_uses_interior_rows_and_exact_selection(self):
        data=scalar_data();files={'source1':data};plan=mixed_plan(files)
        result=bridge.execute_plan(plan,files)
        self.assertEqual(result['parsed_numeric_row_count'],60001)
        stats=result['results'][0]
        self.assertEqual(stats['sample_count'],60001)
        self.assertAlmostEqual(stats['values']['mean'],30000.25+1000000/60001)
        self.assertEqual(stats['source_line_ranges'],[[3,60003]])
        self.assertEqual(result['operation_sources'][0]['source_sha256'],sha256(data))
        self.assertEqual(result['derived_tables'][0]['rows'][0][0],60001)
        self.assertEqual(result['figure_report']['results'],[])


def ovito_ready():
    try:return metadata.version('ovito')==coordination.REQUIRED['ovito']
    except metadata.PackageNotFoundError:return False


@unittest.skipUnless(ovito_ready(),'Pinned application OVITO geometry runtime is optional')
class FullMixedBridgeTests(unittest.TestCase):
    def test_nine_full_numeric_and_nine_real_geometry_sources_use_mature_workers(self):
        data=scalar_data();dump=geometry.synthetic_bcc_dump()
        files={**{f'source{i+1}':data for i in range(9)},**{f'source{i+10}':dump for i in range(9)}}
        self.assertGreater(sum(map(len,files.values())),16*1024*1024)
        plan=mixed_plan(files,with_figure=True)
        with patch.object(coordination,'analyze_isolated',wraps=coordination.analyze_isolated) as worker:
            result=bridge.execute_plan(plan,files)
        self.assertEqual(worker.call_count,9)
        self.assertEqual(result['parsed_numeric_row_count'],540009)
        self.assertEqual(len(result['sources']),18)
        self.assertEqual(len(result['structural_results']),9)
        self.assertEqual(len(result['derived_tables']),18)
        self.assertEqual(len(result['operation_sources']),18)
        self.assertTrue(all(r['sample_count']==60001 for r in result['results']))
        for row in result['structural_results']:
            self.assertEqual(row['chart']['values'],[1.,-1.,1.])
            self.assertEqual(row['worker_receipt']['mode'],'fixed_source_subprocess')
            self.assertEqual(row['source']['sha256'],sha256(dump))
        self.assertEqual(result['derived_tables'][9]['rows'][1][-2:],[-1.,-1.])
        self.assertEqual(result['operation_sources'][9]['frames'],[dict(frame=0,timestep=0,atom_count=54)])
        from auto_lammps.postprocess_figures import render_figures
        rendered=render_figures(result['figure_report'],role='reference',job_id='123')
        self.assertEqual(list(rendered['files']),['plot-selected_ordering.png'])
        self.assertTrue(rendered['files']['plot-selected_ordering.png'].startswith(b'\x89PNG'))


class MixedAnalysisHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.OutputAnalysisTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.files={'source1':scalar_data(5)}
        self.rows=[dict(id='f'*64,name='native.dat',size=len(self.files['source1']),
            sha256=sha256(self.files['source1']),request_id=self.fx.source)]
        listing=patch.object(self.fx.raw,'listing',return_value=dict(files=self.rows));listing.start();self.addCleanup(listing.stop)
        download=patch.object(self.fx.raw,'download',side_effect=lambda *a:('native.dat','text/plain',io.BytesIO(self.files['source1'])))
        download.start();self.addCleanup(download.stop)
        plan=mixed_plan(self.files,5)
        self.fx.proposal=dict(status='ready',summary='完整合成统计',limitations=['Synthetic only'],plan=plan,
            citations=[dict(id='method',source_id='method',quote='dimensionless values')],
            column_evidence=[dict(table=t,column=c,meaning='Synthetic only',unit=u,citations=['method'])
                for (t,c),u in bridge.column_units(plan,self.files).items()])

    def test_HTTP_retains_full_numeric_report_CSV_receipt_and_no_implicit_plot(self):
        record=self.fx.post().json()
        self.assertEqual(record['state'],'completed',record)
        self.assertEqual(record['result']['analysis_mode'],bridge.MODE)
        self.assertEqual(record['result']['numeric_results'][0]['sample_count'],5)
        summary=record['result']['numeric_results'][0]
        self.assertEqual((summary['table'],summary['column'],summary['value_unit']),('source1','signal','1'))
        self.assertEqual(summary['values']['count'],5)
        self.assertEqual(summary['values']['mean'],200002.25)
        self.assertEqual(record['result']['figures']['figures'],[])
        self.assertEqual(record['result']['source_identity'][0]['line_count'],7)
        self.assertEqual(record['result']['operation_sources'][0]['source_sha256'],sha256(self.files['source1']))
        output=self.fx.http.get(self.fx.url+'/'+record['request_id']+'/files/result1.csv')
        self.assertEqual(output.status_code,200)
        self.assertIn(b'sample_count,mean,min,max',output.content)
        report=json.loads(self.fx.service.download(self.fx.task,record['request_id'],'numeric-report.json'))
        self.assertEqual(report['plan'],self.fx.proposal['plan'])
        self.assertEqual(record['result']['native_numeric_results'],report['results'])
        self.assertNotIn('count',report['results'][0]['values'])
        self.assertNotIn('table',report['results'][0])
        self.assertEqual(summary['source_line_ranges'],report['results'][0]['source_line_ranges'])
        self.assertEqual(summary['value_units'],report['results'][0]['value_units'])
        self.assertEqual(self.fx.transport.call_count,1)
        self.assertEqual(self.fx.post().json()['state'],'completed')
        self.assertEqual(self.fx.transport.call_count,1)

    def test_technical_failure_recovers_same_answer_and_round_without_new_model(self):
        with patch.object(bridge,'execute_plan',side_effect=OSError('PRIVATE_RUNTIME_CANARY')):
            record=self.fx.post().json()
        self.assertEqual(record['state'],'analysis_execution_failed',record)
        self.assertNotIn('PRIVATE_RUNTIME_CANARY',canonical(record).decode())
        self.assertEqual(self.fx.post('b'*32).status_code,422)
        recovered=self.fx.post().json()
        self.assertEqual(recovered['state'],'completed',recovered)
        self.assertEqual(recovered['round'],1)
        self.assertEqual(self.fx.transport.call_count,1)

    def test_native_parser_failure_is_actionable_model_feedback_not_infinite_technical_recovery(self):
        self.files['source1']=self.files['source1'].replace(b'1.2500000000000000e+00',b'not_a_number',1)
        self.rows[0].update(size=len(self.files['source1']),sha256=sha256(self.files['source1']))
        record=self.fx.post().json()
        self.assertEqual(record['state'],'invalid_output',record)
        self.assertIn('Nonnumeric',record['message'])
        self.assertEqual(self.fx.transport.call_count,1)

    def test_mixed_research_context_has_no_reference_paper_or_method_provider(self):
        snapshot=self.fx.ledger.evaluation_snapshot
        def research_snapshot(*args,**kwargs):
            value=snapshot(*args,**kwargs);value['identity']['role']='agent';return value
        self.fx.proposal['citations']=[dict(id='method',source_id='source1',quote='# Synthetic scalar samples')]
        with patch.object(self.fx.ledger,'evaluation_snapshot',side_effect=research_snapshot):
            record=self.fx.post().json()
        self.assertEqual(record['state'],'completed',record)
        self.fx.reference.assert_not_called()
        payload=json.loads(self.fx.transport.call_args.args[0])
        evidence=json.loads(payload['messages'][-1]['content'])
        self.assertEqual(evidence['role'],'research')
        self.assertIsNone(evidence['paper'])
        self.assertFalse(any(s['origin']=='paper' for s in evidence['sources']))

    def test_missing_derived_column_evidence_is_invalid_and_preserves_three_round_cap(self):
        self.fx.proposal['column_evidence'].pop()
        for prefix in ('a','b','c'):
            record=self.fx.post(prefix*32).json()
            self.assertEqual(record['state'],'invalid_output',record)
        self.assertEqual(self.fx.post('d'*32).status_code,422)
        self.assertEqual(self.fx.transport.call_count,3)
        payload=json.loads(self.fx.transport.call_args.args[0])
        evidence=json.loads(payload['messages'][-1]['content'])
        self.assertEqual(evidence['previous']['complete_previous_answer'],self.fx.proposal)


class RetainedInputCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.OutputAnalysisTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def test_completed_legacy_full_text_context_remains_readable_after_preview_upgrade(self):
        original=self.fx.service._input
        def legacy(identifier,request,question,representation=None):
            return original(identifier,request,question,None)
        with patch.object(self.fx.service,'_input',side_effect=legacy):
            record=self.fx.post().json()
        self.assertEqual(record['state'],'completed',record)
        context=self.fx.service._stored(self.fx.task,'a'*32)[0]
        self.assertNotIn('input_representation',context['input'])
        self.assertEqual(self.fx.service.get(self.fx.task,'a'*32)['result'],record['result'])
        self.assertEqual(self.fx.transport.call_count,1)

    def test_old_style_numeric_results_pass_through_the_display_projection_unchanged(self):
        record=self.fx.post().json()
        self.assertEqual(record['state'],'completed',record)
        report=json.loads(self.fx.service.download(self.fx.task,record['request_id'],'numeric-report.json'))
        self.assertEqual(record['result']['numeric_results'],report['results'])
        summary=record['result']['numeric_results'][0]
        self.assertEqual((summary['table'],summary['column'],summary['value_unit']),('samples','value','1'))
        self.assertEqual(summary['values']['count'],2)
        self.assertEqual(summary['values']['mean'],2)
        self.assertNotIn('native_numeric_results',record['result'])

    def test_eighteen_large_sources_produce_bounded_full_inventory_for_model_selection(self):
        data=scalar_data();dump=geometry.synthetic_bcc_dump()
        values=[data]*9+[dump]*9
        rows=[dict(id=f'{i:064x}',request_id=self.fx.source,name=f'{i:02}.dat' if i<9 else f'{i:02}.dump',
            size=len(content),sha256=sha256(content)) for i,content in enumerate(values)]
        by_id={row['id']:content for row,content in zip(rows,values)}
        def download(identifier,key):return ('synthetic','text/plain',io.BytesIO(by_id[key]))
        with patch.object(self.fx.raw,'listing',return_value=dict(files=rows)),patch.object(self.fx.raw,'download',side_effect=download):
            evidence,files=self.fx.service._input(self.fx.task,self.fx.source,'Synthetic only')
        self.assertEqual(len(files),18)
        self.assertGreater(evidence['full_source_bytes'],16*1024*1024)
        self.assertEqual(evidence['files'][0]['numeric_line_count'],60001)
        self.assertEqual(evidence['files'][9]['frame_count'],1)
        self.assertEqual(evidence['files'][9]['atom_column_sets'],[['id','type','x','y','z']])
        self.assertEqual(evidence['files'][9]['observed_type_count_sets'],[{'1':27,'2':27}])
        self.assertTrue(evidence['files'][9]['type_count_sets_cover_all_frames'])
        self.assertIn('ITEM: ATOMS id type x y z',evidence['sources'][10]['text'])
        self.assertEqual(evidence['files'][0]['sha256'],sha256(data))
        self.assertLess(len(canonical(evidence)),64000)
        self.assertTrue(all(s['preview']['only_preview'] for s in evidence['sources'] if s['origin']=='output'))
        self.fx.transport.assert_not_called()


if __name__=='__main__':unittest.main()
