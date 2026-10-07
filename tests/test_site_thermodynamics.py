"""Complete synthetic arrays and analytically solvable independent-site models.

No engine, API, target paper answers or scientific acceptance is exercised.
"""
from copy import deepcopy
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps import analysis_v2 as mixed
from auto_lammps import site_thermodynamics as thermo
from auto_lammps.manifest import canonical, sha256


TABLE = dict(file='sites.dat', format=thermo.FORMAT,
    columns=[dict(name=n, unit=u) for n, u in thermo.BASE_COLUMNS+[('n_Fe', '1'), ('n_Al', '1')]])
OPERATION = dict(id='thermodynamics', method=thermo.METHOD, file=TABLE['file'],
    equations_version=thermo.EQUATIONS_VERSION, states=dict(first=0,last=1,stride=1),
    sites=dict(first=1,last=4,stride=1), elements={'0':'Fe','1':'Al'},
    expected_counts={'0':2,'1':2}, vacancy_variant=2, pressure_GPa=0,
    convergence=dict(force_metric='fnorm',force_max_eV_per_A=0.01,
        pressure_tolerance_GPa=0.001,max_iterations=1000,max_evaluations=10000),
    baseline_tolerance=dict(energy_eV=1e-8,volume_A3=1e-8),
    reservoir_anchor=dict(method='baseline_euler_enthalpy_v1', source='Synthetic independent-site enthalpy anchor'),
    temperatures_K=[200,400,600,800,1000], beta_grid=dict(first=10,last=30,count=11),
    solver=dict(method='safeguarded_newton_bisection_v1',chemical_potential_bounds_eV=[-10,10],
        composition_tolerance=1e-11,chemical_potential_tolerance_eV=1e-12,max_iterations=128),
    models=['two_state_host_vacancy','three_state_competing_species'], aggregation='equal_state_site_weight')


def synthetic_rows(op=OPERATION):
    """Uniform substitution 0.2 eV, vacancy formation 0.1 eV/1 A3 at p=0.

    All baseline atoms have -4 eV and 10 A3. Species substitution changes
    single-site H by 0.2 eV and V by 2 A3. At equal composition the frozen
    Euler anchor gives mu=(-4.1,-3.9) eV, partial volumes=(9,11) A3.
    """
    count = sum(op['expected_counts'].values())
    data = []
    for state in thermo._domain(op['states'], 'state'):
        for index, site in enumerate(thermo._domain(op['sites'], 'site')):
            host = 0 if index < op['expected_counts']['0'] else 1
            for variant in (-1,0,1,2):
                counts = [op['expected_counts']['0'],op['expected_counts']['1']]
                de,dv=0.,0.
                if variant!=-1:
                    counts[host]-=1
                    if variant<2:
                        counts[variant]+=1
                        de,dv=.2*(variant-host),2.*(variant-host)
                    else:
                        de,dv=4.2-.2*host,-8.-2*host
                data.append([state,site,variant,sum(counts),-4.*count+de,10.*count+dv,
                    host,int(variant==-1),1,0.,0.,op['pressure_GPa']*1e4,12,1000,10000,*counts])
    return data


def encoded(rows, table=TABLE):
    header='# columns: '+' '.join(c['name'] for c in table['columns'])+'\n'
    header+='# units: '+' '.join(c['unit'] for c in table['columns'])+'\n'
    return (header+'\n'.join(' '.join(map(str,row)) for row in rows)+'\n').encode()


def arrays(op=OPERATION):
    data=encoded(synthetic_rows(op))
    values,lines=thermo.parse_array(data,TABLE)
    return thermo.validate_array(values,lines,TABLE,op)


class ThermodynamicsTests(unittest.TestCase):
    def test_known_uniform_binary_models_and_analytic_partial_volumes(self):
        a=arrays()
        beta=20.
        for model, denominator in zip(OPERATION['models'],(1.,2.)):
            summary,p,hv,vv,_=thermo.evaluate(a,OPERATION,beta,model)
            with self.subTest(model=model):
                self.assertAlmostEqual(summary['mu_0_eV'],-4.1,places=9)
                self.assertAlmostEqual(summary['mu_1_eV'],-3.9,places=9)
                self.assertAlmostEqual(summary['partial_volume_0_A3'],9.,places=8)
                self.assertAlmostEqual(summary['partial_volume_1_A3'],11.,places=8)
                self.assertAlmostEqual(summary['vacancy_fraction'],math.exp(-2)/(denominator+math.exp(-2)),places=10)
                self.assertLess(max(abs(hv-.1)),1e-10)
                self.assertLess(max(abs(vv-1.)),1e-8)
                self.assertLess(max(abs(p.sum(axis=1)-1.)),1e-14)

    def test_pressure_conversion_and_constrained_chemical_volume(self):
        op=deepcopy(OPERATION);op['pressure_GPa']=2.5
        for model in op['models']:
            summary,_,hv,vv,_=thermo.evaluate(arrays(op),op,10.,model)
            self.assertAlmostEqual(summary['mu_0_eV'],-4.1+9*2.5*thermo.GPA_A3_EV,places=9)
            self.assertAlmostEqual(float(hv[0]),.1+2.5*thermo.GPA_A3_EV,places=9)
            self.assertAlmostEqual(float(vv[0]),1.,places=8)

    def test_nonuniform_three_state_volume_matches_finite_pressure_derivative(self):
        np=thermo._numpy();a=arrays();op=deepcopy(OPERATION)
        # Complete heterogeneous costs exercise the implicit derivative rather
        # than verifying only the uniform closed form.
        a['dh'][:,1]+=np.array([0,.15,-.1,.05]*2)
        a['dv'][:,1]+=np.array([0,1.,-.2,.3]*2)
        model=op['models'][1];s,_,_,_,_=thermo.evaluate(a,op,17.,model)
        eps=1e-6
        plus={**a,'dh':a['dh']+eps*a['dv'],'anchor_enthalpy':a['anchor_enthalpy']+eps*a['anchor_volume']}
        minus={**a,'dh':a['dh']-eps*a['dv'],'anchor_enthalpy':a['anchor_enthalpy']-eps*a['anchor_volume']}
        sp,*_=thermo.evaluate(plus,op,17.,model);sm,*_=thermo.evaluate(minus,op,17.,model)
        for species in (0,1):
            numeric=(sp[f'mu_{species}_eV']-sm[f'mu_{species}_eV'])/(2*eps)
            self.assertAlmostEqual(s[f'partial_volume_{species}_A3'],numeric,places=5)

    def test_explicit_reference_anchor_is_required_and_preserved(self):
        op=deepcopy(OPERATION)
        op['reservoir_anchor']=dict(method='explicit_reference_v1',chemical_potential_eV=-3.,
                                   partial_volume_A3=12.,source='Synthetic known reservoir')
        s,*_=thermo.evaluate(arrays(op),op,20.,op['models'][0])
        self.assertAlmostEqual((s['mu_0_eV']+s['mu_1_eV'])/2,-3.)
        self.assertAlmostEqual((s['partial_volume_0_A3']+s['partial_volume_1_A3'])/2,12.)
        for key in ('reservoir_anchor','solver','convergence','expected_counts'):
            bad=deepcopy(OPERATION);bad.pop(key)
            with self.subTest(key=key),self.assertRaises(thermo.AnalysisError):
                thermo.validate_operation(bad,TABLE)

    def test_extreme_reservoir_and_beta_stay_finite_without_false_underflow_root(self):
        op=deepcopy(OPERATION)
        op['reservoir_anchor']=dict(method='explicit_reference_v1',chemical_potential_eV=-1000.,
                                   partial_volume_A3=10.,source='Synthetic underflow stress case')
        s,p,*_=thermo.evaluate(arrays(op),op,1e4,op['models'][1])
        self.assertTrue(math.isfinite(s['mu_0_eV']))
        self.assertAlmostEqual(s['mu_1_eV']-s['mu_0_eV'],.2,places=7)
        self.assertEqual(s['vacancy_fraction'],1.)

    def test_missing_duplicate_foreign_and_unconverged_array_is_rejected(self):
        for kind in ('missing','duplicate','state','site','variant','baseline','host','N','composition',
                     'converged','force','pressure','iteration','bounds','baseline_energy','host_energy'):
            rows=synthetic_rows()
            if kind=='missing':rows.pop()
            if kind=='duplicate':rows[-1]=deepcopy(rows[-2])
            if kind=='state':rows[-1][0]=99
            if kind=='site':rows[-1][1]=99
            if kind=='variant':rows[-1][2]=99
            if kind=='baseline':rows[-1][7]=1
            if kind=='host':rows[-1][6]=0
            if kind=='N':rows[-1][3]+=1
            if kind=='composition':rows[-1][-1]+=1
            if kind=='converged':rows[-1][8]=0
            if kind=='force':rows[-1][10]=.1
            if kind=='pressure':rows[-1][11]=1000
            if kind=='iteration':rows[-1][12]=1001
            if kind=='bounds':rows[-1][13]=999
            if kind=='baseline_energy':rows[-4][4]+=1.
            if kind=='host_energy':rows[-2][4]+=1.
            with self.subTest(kind=kind),self.assertRaises(thermo.AnalysisError):
                values,lines=thermo.parse_array(encoded(rows),TABLE)
                thermo.validate_array(values,lines,TABLE,OPERATION)

    def test_parser_rejects_units_truncation_nan_overflow_and_foreign_tokens(self):
        data=encoded(synthetic_rows())
        for bad in (data[:-1],data.replace(b'angstrom^3',b'nm^3'),data+b'0 nan\n',
                    data.replace(b'-16.0',b'1e999',1),data.replace(b'-16.0',b'12garbage',1),data+b'\xff'):
            with self.subTest(tail=bad[-12:]),self.assertRaises(thermo.AnalysisError):
                thermo.parse_array(bad,TABLE)

    def test_solver_rejects_out_of_frozen_bounds_and_unresolved_tolerance(self):
        for bounds,iterations in (([-.1,.1],128),([-10,10],1)):
            op=deepcopy(OPERATION);op['solver']['chemical_potential_bounds_eV']=bounds
            op['solver']['max_iterations']=iterations
            with self.subTest(bounds=bounds),self.assertRaises(thermo.AnalysisError):
                thermo.evaluate(arrays(op),op,20.,op['models'][0])

    def test_force_pressure_diagnostic_at_iteration_bound_is_not_replaced_by_new_criterion(self):
        rows=synthetic_rows();rows[-1][12]=1000
        values,lines=thermo.parse_array(encoded(rows),TABLE)
        self.assertTrue(thermo.validate_array(values,lines,TABLE,OPERATION)['coverage']['complete'])

    def test_energy_gauge_does_not_change_vacancy_physics(self):
        np=thermo._numpy();original=arrays();shifted=deepcopy(original)
        offset=123.25
        shifted['dh']+=offset*shifted['delta_counts'].sum(axis=2)
        shifted['anchor_enthalpy']+=offset
        for model in OPERATION['models']:
            s,p,h,v,_=thermo.evaluate(original,OPERATION,20.,model)
            t,pp,hh,vv,_=thermo.evaluate(shifted,OPERATION,20.,model)
            np.testing.assert_allclose(p,pp,rtol=1e-10,atol=1e-11)
            np.testing.assert_allclose(h,hh,rtol=1e-10,atol=1e-11)
            self.assertAlmostEqual(t['mu_0_eV']-s['mu_0_eV'],offset,places=9)
            self.assertAlmostEqual(t['mu_1_eV']-s['mu_1_eV'],offset,places=9)

    def test_full_ten_thousand_beta_grid_is_retained_as_download(self):
        op=deepcopy(OPERATION);op['beta_grid']['count']=10000;op['temperatures_K']=[400]
        data=encoded(synthetic_rows(op));source=dict(TABLE,sha256=sha256(data),size=len(data))
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder).resolve()/'derived'
            result=thermo.analyze(data,TABLE,op,source,folder)
            curve=next(r for r in result['derived_files'] if r['name'].endswith('-curves.csv'))
            rows=list(csv.DictReader(io.StringIO(thermo.read_derived(folder,curve).decode())))
            beta_rows=[r for r in rows if r['grid']=='beta']
            self.assertEqual(len(beta_rows),20000)
            for model in op['models']:
                selected=[r for r in beta_rows if r['model']==model]
                self.assertEqual([int(r['index']) for r in selected],list(range(10000)))
                self.assertEqual(float(selected[0]['beta']),10.)
                self.assertEqual(float(selected[-1]['beta']),30.)

    def test_shuffled_source_rows_are_complete_and_source_lines_stay_exact(self):
        data=encoded(synthetic_rows()[::-1]);values,lines=thermo.parse_array(data,TABLE)
        a=thermo.validate_array(values,lines,TABLE,OPERATION)
        self.assertEqual(a['coverage']['rows'],32)
        self.assertEqual(int(a['baseline_lines'][0]),34)
        self.assertEqual(int(a['variant_lines'][0,2]),31)

    def test_full_50_state_1024_site_scope_is_not_limited_by_legacy_cells(self):
        op=deepcopy(OPERATION);op['states']['last']=49;op['sites']['last']=1024
        op['expected_counts']={'0':512,'1':512}
        thermo.validate_operation(op,TABLE)
        np=thermo._numpy()
        data=np.array(synthetic_rows(op),dtype=np.float64)
        self.assertEqual(data.size,3_481_600)
        a=thermo.validate_array(data,np.arange(3,len(data)+3),TABLE,op)
        self.assertEqual(a['coverage'],dict(states=50,sites=1024,variants=3,rows=204800,
                                          baseline_rows=51200,complete=True))

    def test_complete_downloads_are_bound_and_report_is_bounded(self):
        data=encoded(synthetic_rows());source=dict(TABLE,sha256=sha256(data),size=len(data))
        with tempfile.TemporaryDirectory() as root:
            root=Path(root).resolve()
            result=thermo.analyze(data,TABLE,OPERATION,source,Path(root)/'derived')
            self.assertLess(len(canonical(result)),60000)
            self.assertEqual(result['scientific_status'],'not_evaluated')
            self.assertFalse(result['physics_simulation'])
            receipts={r['name'].split('-')[-1]:r for r in result['derived_files']}
            self.assertEqual(receipts['differences.csv']['rows'],24)
            self.assertEqual(receipts['sites.csv']['rows'],80)
            self.assertEqual(receipts['curves.csv']['rows'],32)
            for receipt in result['derived_files']:
                raw=thermo.read_derived(Path(root)/'derived',receipt)
                self.assertEqual(receipt['source_sha256'],source['sha256'])
                self.assertEqual(receipt['parameters_sha256'],sha256(canonical(OPERATION)))
                self.assertEqual(len(list(csv.reader(io.StringIO(raw.decode()))))-1,receipt['rows'])
            reservation=thermo.reservation_bytes(dict(operations=[OPERATION]))
            self.assertLess(sum(r['size'] for r in result['derived_files'])+65536,reservation)
            # Resume only exact bytes, not silently replaced derivatives.
            self.assertEqual(thermo.analyze(data,TABLE,OPERATION,source,Path(root)/'derived'),result)
            receipt=result['derived_files'][0];(Path(root)/'derived'/receipt['name']).write_bytes(b'corrupted')
            with self.assertRaises(thermo.AnalysisError):thermo.read_derived(Path(root)/'derived',receipt)

    def test_version_four_selected_only_for_complete_site_arrays(self):
        plan=dict(tables=[TABLE],operations=[OPERATION])
        self.assertIs(mixed.validate_plan(plan,[TABLE['file']]),plan)
        status,identity=mixed.plan_adapter(plan)
        self.assertEqual(status,'numeric_tables_v4')
        self.assertEqual(identity['adapter_version'],4)
        self.assertEqual(identity['site_thermodynamics'],thermo.adapter_identity())
        self.assertGreater(thermo.reservation_bytes(plan),65536)
        self.assertEqual(mixed.plan_adapter(dict(tables=[dict(file='legacy.dat',columns=[
            dict(name='x',unit='1'),dict(name='y',unit='eV')])]))[0],'numeric_tables_v1')

    def test_generic_binary_elements_and_nonnative_ids_are_not_paper_special_cases(self):
        table=deepcopy(TABLE);table['columns'][-2]['name']='n_Cu';table['columns'][-1]['name']='n_Ni'
        op=deepcopy(OPERATION);op['elements']={'4':'Cu','7':'Ni'};op['expected_counts']={'4':2,'7':2}
        op['vacancy_variant']=9;op['states']=dict(first=3,last=4,stride=1)
        op['sites']=dict(first=11,last=14,stride=1);op['models']=['three_state_competing_species']
        mapping={-1:-1,0:4,1:7,2:9}
        rows=synthetic_rows()
        for row in rows:
            row[0]+=3;row[1]+=10;row[2]=mapping[row[2]];row[6]=mapping[row[6]]
        values,lines=thermo.parse_array(encoded(rows,table),table)
        a=thermo.validate_array(values,lines,table,op)
        summary,*_=thermo.evaluate(a,op,20.,op['models'][0])
        self.assertEqual(a['species'],[4,7])
        self.assertAlmostEqual(summary['mu_1_eV']-summary['mu_0_eV'],.2,places=9)


class CollectedSitePipelineTests(unittest.TestCase):
    """Actual trusted freeze, collection, ledger and persistent CSVs; synthetic physics data."""
    def prepare(self, data=None):
        import test_outputs as collected
        import test_runtime_launcher as runtime
        from auto_lammps.manifest import freeze
        f=collected.OutputTests();f.setUp();self.addCleanup(f.doCleanups)
        self.f=f;self.root=f.root
        plan=dict(tables=[deepcopy(TABLE)],operations=[deepcopy(OPERATION)])
        outputs=runtime.OUTPUTS+[TABLE['file']]
        spec=dict(proposal=dict(quantity='Synthetic binary site thermodynamics',method='Frozen independent sites',
                               files=[TABLE['file']],plan=plan),outputs=outputs,
                  implementation_status='numeric_tables_v4',adapter_identity=mixed.site_adapter_identity())
        folder=self.root/'site-input';folder.mkdir()
        (folder/'input.in').write_bytes(b'# synthetic package; no engine execution\n')
        (folder/'analysis.json').write_bytes(canonical(spec))
        self.snapshot=freeze(folder,self.root/'site-snapshots',
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
        f.private(f.case/'output'/TABLE['file'],data or encoded(synthetic_rows()))
        self.service=mixed.VersionedAnalysisService(f.collector,self.root/'reports')

    def analyze(self):
        with self.f.local_transfer():return self.service.run(self.f.request_id,self.snapshot)

    def test_complete_collection_uses_accounted_derived_storage_and_resumes(self):
        self.prepare();saved=self.analyze();report=saved['report']
        self.assertEqual(report['status'],'analyzed')
        self.assertEqual(report['adapter_version'],4)
        self.assertEqual(saved['context']['storage_bytes'],thermo.reservation_bytes(dict(operations=[OPERATION])))
        item=report['site_thermodynamic_results'][0]
        self.assertEqual(item['coverage']['rows'],32)
        folder=self.root/'reports'/saved['context']['analysis_id']
        self.assertEqual(len(list(folder.glob('*.csv'))),3)
        before=self.f.ledger.get(self.f.request_id)['charge_storage_bytes']
        self.service=mixed.VersionedAnalysisService(self.f.collector,self.root/'reports')
        self.assertEqual(self.analyze(),saved)
        self.assertEqual(self.f.ledger.get(self.f.request_id)['charge_storage_bytes'],before)
        kinds=[e['kind'] for e in self.f.ledger.events(self.f.request_id)]
        self.assertEqual(kinds.count('analysis_reserved'),1)
        self.assertEqual(kinds.count('analysis_saved'),1)
        self.assertEqual(self.f.ledger.evaluation_snapshot(self.f.evaluation)['dispatch_claims'],1)

    def test_csv_tampering_is_rejected_on_saved_report_reuse(self):
        self.prepare();saved=self.analyze();receipt=saved['report']['site_thermodynamic_results'][0]['derived_files'][0]
        path=self.root/'reports'/saved['context']['analysis_id']/receipt['name']
        path.write_bytes(b'changed\n')
        with self.assertRaises(thermo.AnalysisError):self.analyze()

    def test_invalid_full_array_retains_failure_without_partial_properties(self):
        rows=synthetic_rows();rows[-1][3]=999
        self.prepare(encoded(rows));saved=self.analyze()
        self.assertEqual(saved['report']['status'],'analysis_failed')
        self.assertNotIn('site_thermodynamic_results',saved['report'])
        self.assertIn('composition',saved['report']['reason'])
        self.assertEqual(self.analyze(),saved)

    def test_changed_v4_identity_does_not_relabel_or_migrate_frozen_plan(self):
        self.prepare()
        changed={**mixed.site_adapter_identity(),'site_thermodynamics':{'source_sha256':'f'*64}}
        with patch.object(mixed,'site_adapter_identity',return_value=changed):saved=self.analyze()
        self.assertEqual(saved['report']['status'],'analysis_failed')
        self.assertIn('frozen plan',saved['report']['reason'])


if __name__=='__main__':
    unittest.main()
