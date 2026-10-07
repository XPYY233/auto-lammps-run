"""Synthetic periodic geometry/statistics only: no potential or simulation."""
from copy import deepcopy
from importlib import metadata
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_lammps import analysis_v2
from auto_lammps import coordination_analysis as structural
from auto_lammps.analysis import AnalysisError
from auto_lammps.manifest import canonical, sha256

TABLE = dict(file='geometry.dump', format='lammps_dump', length_unit='angstrom',
             elements={'1': 'Fe', '2': 'Ni'}, expected_counts={'1': 27, '2': 27},
             pbc=[True, True, True])
OPERATION = dict(id='ordering', method='warren_cowley_first_shell', file=TABLE['file'],
                 neighbors=8, neighbor_selection='nearest_k',
                 frames=dict(first=0, last=0, stride=1),
                 aggregation='equal_frame_mean', pair_mode='directed_and_symmetric')


def synthetic_bcc_dump(steps=(0,), *, reverse=False, field_change=None):
    rows=[]
    for x in range(3):
        for y in range(3):
            for z in range(3):
                for kind, offset in ((1, 0), (2, .5)):
                    rows.append([len(rows)+1, kind, 3*(x+offset), 3*(y+offset), 3*(z+offset)])
    if field_change:
        field_change(rows)
    frames=[]
    for index, step in enumerate(steps):
        current=list(reversed(rows)) if reverse and index else rows
        frames.append('ITEM: TIMESTEP\n'+str(step)+'\nITEM: NUMBER OF ATOMS\n54\n'
                      'ITEM: BOX BOUNDS pp pp pp\n0 9\n0 9\n0 9\n'
                      'ITEM: ATOMS id type x y z\n'+
                      '\n'.join(' '.join(map(str, row)) for row in current)+'\n')
    return ''.join(frames).encode('ascii')


class StructuralContractTests(unittest.TestCase):
    def test_dump_only_and_mixed_analysis_share_normal_versioned_contract(self):
        plan=dict(tables=[deepcopy(TABLE)], operations=[deepcopy(OPERATION)])
        analysis_v2.validate_plan(plan, [TABLE['file']])
        self.assertEqual(analysis_v2.plan_adapter(plan)[0], 'numeric_tables_v3')
        numeric=dict(file='values.dat', columns=[dict(name='step', unit='step'),dict(name='energy',unit='eV')])
        plan['tables'].append(numeric)
        plan['operations'].append(dict(id='energy', method='summary', file='values.dat',
                                       x='step',y='energy',window=[0,10]))
        analysis_v2.validate_plan(plan, [TABLE['file'], 'values.dat'])

    def test_nine_numeric_and_nine_trajectory_sources_do_not_recreate_legacy_cap(self):
        tables=[];operations=[]
        for index in range(9):
            table=deepcopy(TABLE);table['file']=f'frame_{index}.dump'
            operation=deepcopy(OPERATION);operation.update(id=f'order_{index}', file=table['file'])
            tables.append(table);operations.append(operation)
            name=f'energy_{index}.dat'
            tables.append(dict(file=name,columns=[dict(name='step',unit='step'),dict(name='energy',unit='eV')]))
            operations.append(dict(id=f'energy_{index}',method='summary',file=name,x='step',y='energy',window=[0,10]))
        analysis_v2.validate_plan(dict(tables=tables,operations=operations), [t['file'] for t in tables])
        self.assertEqual(analysis_v2.MAX_TABLES,16)

    def test_source_and_sampling_conditions_are_never_inferred(self):
        for change in ('unknown_type', 'missing_count', 'zero_count', 'nonperiodic', 'integer_pbc',
                       'unit', 'duplicate_element', 'path'):
            table=deepcopy(TABLE)
            if change=='unknown_type':table['elements']['3']='Cr'
            if change=='missing_count':del table['expected_counts']['2']
            if change=='zero_count':table['expected_counts']['2']=0
            if change=='nonperiodic':table['pbc'][1]=False
            if change=='integer_pbc':table['pbc']=[1,1,1]
            if change=='unit':table['length_unit']='nm'
            if change=='duplicate_element':table['elements']['2']='Fe'
            if change=='path':table['file']='../answer.dump'
            with self.subTest(change=change), self.assertRaises(AnalysisError):
                structural.validate_table(table)
        for change in ('no_frames','automatic_final','too_many_frames','stride','neighbors','mean','symmetry','bounds'):
            operation=deepcopy(OPERATION)
            if change=='no_frames':del operation['frames']
            if change=='automatic_final':operation['frames']='last'
            if change=='too_many_frames':operation['frames']['last']=8
            if change=='stride':operation['frames']['stride']=0
            if change=='neighbors':operation['neighbors']=True
            if change=='mean':operation['aggregation']='time_average'
            if change=='symmetry':operation['pair_mode']='silent_average'
            if change=='bounds':operation['max_distance']=3
            with self.subTest(change=change), self.assertRaises(AnalysisError):
                structural.validate_operation(operation,TABLE)

    def test_numeric_operations_cannot_interpret_dump_as_labeled_table(self):
        plan=dict(tables=[deepcopy(TABLE)],operations=[dict(id='bad',method='summary',file=TABLE['file'],
                     x='x',y='y',window=[0,1])])
        with self.assertRaisesRegex(AnalysisError,'trajectory'):
            analysis_v2.validate_plan(plan,[TABLE['file']])

    def test_bounded_shell_is_explicit_and_nearest_k_has_no_invented_gap(self):
        structural.validate_operation(OPERATION,TABLE)
        bounded=dict(OPERATION,neighbor_selection='bounded_shell',max_distance=2.8,shell_gap=.3)
        structural.validate_operation(bounded,TABLE)
        for value in (0, float('nan'), True, -1):
            bounded['shell_gap']=value
            with self.assertRaises(AnalysisError):structural.validate_operation(bounded,TABLE)


def _ovito_ready():
    try:return metadata.version('ovito')==structural.REQUIRED['ovito']
    except metadata.PackageNotFoundError:return False


@unittest.skipUnless(_ovito_ready(), 'Application OVITO geometry runtime is optional')
class RealGeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'geometry.dump'

    def analyze(self,data=None,table=None,operation=None):
        data=data or synthetic_bcc_dump()
        self.path.write_bytes(data)
        source=dict(size=len(data),sha256=sha256(data))
        with patch('subprocess.Popen',side_effect=AssertionError('No physics or model subprocess')):
            return structural.analyze_trajectory(self.path,table or TABLE,operation or OPERATION,source)

    def test_actual_ovito_periodic_neighbors_match_hand_calculated_binary_order(self):
        value=self.analyze()
        frame=value['frames'][0]
        self.assertEqual(frame['timestep'],0)
        self.assertEqual(frame['directed'],[[1,1,0,27,27,0.,.5,1.],
            [1,2,216,27,27,1.,.5,-1.],[2,1,216,27,27,1.,.5,-1.],[2,2,0,27,27,0.,.5,1.]])
        self.assertEqual(frame['symmetric'],[[1,1,1.],[1,2,-1.],[2,2,1.]])
        self.assertTrue(frame['neighbor_diagnostics']['reciprocal'])
        self.assertGreater(frame['neighbor_diagnostics']['minimum_gap'],.4)
        self.assertEqual(value['chart']['values'],[1.,-1.,1.])
        self.assertFalse(value['physics_simulation'])
        self.assertEqual(value['scientific_status'],'not_evaluated')
        proof=value.pop('derived_sha256')
        self.assertEqual(proof,sha256(canonical(value)))

    def test_selected_frames_are_exact_and_particle_reordering_is_allowed(self):
        operation=deepcopy(OPERATION);operation['frames']=dict(first=1,last=2,stride=1)
        result=self.analyze(synthetic_bcc_dump((0,100,200),reverse=True),operation=operation)
        self.assertEqual([v['frame'] for v in result['frames']],[1,2])
        self.assertEqual([v['timestep'] for v in result['frames']],[100,200])
        self.assertEqual(result['aggregates']['symmetric'][1],[1,2,-1.,0.,-1.,-1.])

    def test_missing_duplicate_and_reversed_sampling_fails_without_partial_tables(self):
        operation=deepcopy(OPERATION);operation['frames']=dict(first=0,last=1,stride=1)
        for data in (synthetic_bcc_dump(),synthetic_bcc_dump((100,100)),synthetic_bcc_dump((100,0))):
            with self.subTest(data=data[:40]),self.assertRaises(AnalysisError):
                self.analyze(data,operation=operation)

    def test_type_count_unique_ids_nonfinite_and_overlap_are_checked_on_actual_dump(self):
        changes=[lambda rows:rows[0].__setitem__(1,3), lambda rows:rows[0].__setitem__(1,2),
                 lambda rows:rows[1].__setitem__(0,rows[0][0]),
                 lambda rows:rows[0].__setitem__(2,float('nan')),
                 lambda rows:rows[1].__setitem__(slice(2,5),rows[0][2:5])]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(AnalysisError):
                self.analyze(synthetic_bcc_dump(field_change=change))

    def test_bound_violation_is_not_replaced_by_nearest_k(self):
        operation=dict(OPERATION,neighbor_selection='bounded_shell',max_distance=2.5,shell_gap=.3)
        with self.assertRaisesRegex(AnalysisError,'bounded-shell'):
            self.analyze(operation=operation)

    def test_changed_sha_symlink_and_periodic_self_image_are_rejected(self):
        self.path.write_bytes(synthetic_bcc_dump())
        with self.assertRaisesRegex(AnalysisError,'changed'):
            structural.analyze_trajectory(self.path,TABLE,OPERATION,dict(size=self.path.stat().st_size,sha256='0'*64))
        link=self.path.with_name('linked.dump');link.symlink_to(self.path)
        with self.assertRaises(OSError):
            structural.analyze_trajectory(link,TABLE,OPERATION,dict(size=self.path.stat().st_size,sha256=sha256(self.path.read_bytes())))
        operation=deepcopy(OPERATION);operation['neighbors']=32
        # Many periodic images would be required in a tiny cell. This must not
        # silently count multiple images as different atoms.
        tiny=synthetic_bcc_dump().replace(b'0 9\n0 9\n0 9',b'0 3\n0 3\n0 3')
        with self.assertRaises(AnalysisError):self.analyze(tiny,operation=operation)

    def test_same_type_terms_and_unequal_composition_keep_directed_denominators(self):
        data=b'ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n4\nITEM: BOX BOUNDS pp pp pp\n0 100\n0 100\n0 100\nITEM: ATOMS id type x y z\n1 1 1 1 1\n2 2 2 1 1\n3 2 1 2 1\n4 2 1 1 2\n'
        table=dict(TABLE,expected_counts={'1':1,'2':3})
        operation=dict(OPERATION,neighbors=3)
        result=self.analyze(data,table=table,operation=operation)
        rows=result['frames'][0]['directed']
        self.assertAlmostEqual(rows[1][-1],-1/3)
        self.assertAlmostEqual(rows[2][-1],-1/3)
        self.assertAlmostEqual(rows[3][-1],1/9)
        self.assertEqual(rows[1][2],3)
        self.assertEqual(rows[2][2],3)

    def test_real_fcc_three_element_neighbor_table_retains_all_nine_directed_six_symmetric_pairs(self):
        rows=[]
        basis=[(0,0,0),(.5,.5,0),(.5,0,.5),(0,.5,.5)]
        for x in range(3):
            for y in range(3):
                for z in range(3):
                    for index,(a,b,c) in enumerate(basis):
                        rows.append([len(rows)+1,min(index+1,3),4*(x+a),4*(y+b),4*(z+c)])
        data=('ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n108\nITEM: BOX BOUNDS pp pp pp\n'
              '0 12\n0 12\n0 12\nITEM: ATOMS id type x y z\n'+
              '\n'.join(' '.join(map(str,row)) for row in rows)+'\n').encode('ascii')
        table=dict(TABLE,elements={'1':'Fe','2':'Ni','3':'Cr'},expected_counts={'1':27,'2':27,'3':54})
        result=self.analyze(data,table=table,operation=dict(OPERATION,neighbors=12))
        directed=result['frames'][0]['directed'];symmetric=result['frames'][0]['symmetric']
        self.assertEqual(len(directed),9);self.assertEqual(len(symmetric),6)
        self.assertEqual(directed[1][:5],[1,2,108,27,27])
        self.assertEqual(directed[2][:5],[1,3,216,27,54])
        self.assertEqual(directed[6][:5],[3,1,216,54,27])
        for actual,expected in zip(result['chart']['values'],[1,-1/3,-1/3,1,-1/3,1/3]):
            self.assertAlmostEqual(actual,expected)

    def test_nonreciprocal_nearest_neighborhood_retains_directed_values_and_diagnostics(self):
        data=b'ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n4\nITEM: BOX BOUNDS pp pp pp\n0 100\n0 100\n0 100\nITEM: ATOMS id type x y z\n1 1 0 0 0\n2 1 1 0 0\n3 2 3 0 0\n4 2 8 0 0\n'
        table=dict(TABLE,expected_counts={'1':2,'2':2})
        result=self.analyze(data,table=table,operation=dict(OPERATION,neighbors=1))
        frame=result['frames'][0]
        self.assertFalse(frame['neighbor_diagnostics']['reciprocal'])
        self.assertEqual(frame['neighbor_diagnostics']['missing_reverse_edges'],2)
        self.assertEqual(frame['directed'][1][2],0)
        self.assertEqual(frame['directed'][2][2],1)
        self.assertEqual(frame['directed'][1][-1],1.)
        self.assertEqual(frame['directed'][2][-1],0.)
        self.assertEqual(frame['symmetric'][1][-1],.5)

    def test_fixed_source_worker_reuses_pinned_runtime_and_retains_bound_receipt(self):
        data=synthetic_bcc_dump();self.path.write_bytes(data)
        result=structural.analyze_isolated(self.path,TABLE,OPERATION,dict(size=len(data),sha256=sha256(data)))
        self.assertEqual(result['chart']['values'],[1.,-1.,1.])
        self.assertEqual(result['worker_receipt']['mode'],'fixed_source_subprocess')
        self.assertEqual(result['worker_receipt']['timeout_seconds'],180)
        self.assertNotIn(str(self.path),json.dumps(result))
        proof=result.pop('derived_sha256')
        self.assertEqual(proof,sha256(canonical(result)))

    def test_worker_native_crash_timeout_and_output_overflow_return_no_partial_statistics(self):
        data=synthetic_bcc_dump();self.path.write_bytes(data)
        source=dict(size=len(data),sha256=sha256(data))
        for failure,code in (('command_failed',-11),('timeout',None),('output_limit',None)):
            captured=dict(failure=failure,returncode=code,stdout='',stderr='')
            with self.subTest(failure=failure),patch('auto_lammps.slurm_read._capture',return_value=captured),self.assertRaises(AnalysisError):
                structural.analyze_isolated(self.path,TABLE,OPERATION,source)
