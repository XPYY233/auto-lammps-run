"""Synthetic PNG/provenance checks; no model, HPC, papers or scientific targets."""
import copy
import io
import json
from importlib.util import find_spec
import unittest
from unittest.mock import patch

from auto_lammps import postprocess_figures as renderer
from auto_lammps.manifest import canonical, sha256
from auto_lammps.postprocess_tables import execute_plan


def report_for(method='line', *, count=4, operations=None):
    data = ''.join(f'{index} {index * 2 + 1}\n' for index in range(count)).encode('ascii')
    declaration = dict(id='samples', file='original.dat', sha256=sha256(data), format='whitespace',
                       header_lines=0, columns=[dict(name='step', unit='step'), dict(name='energy', unit='eV')])
    operation = dict(id='curve', method=method, table='samples', x='step', y='energy')
    if method in {'summary', 'histogram'}:
        operation = dict(id='distribution', method=method, table='samples', column='energy')
        if method == 'histogram':
            operation.update(bins=3, range=[2, 8])
    plan = dict(version=1, tables=[declaration], operations=operations or [operation])
    return execute_plan(plan, {'original.dat': data})


class FigureValidationTests(unittest.TestCase):
    def test_role_and_original_job_are_required_without_executing_a_renderer(self):
        report = report_for()
        for role, job in [('agent', '123'), ('reference', 123), ('reference', '../job'),
                          ('reference', '123;id'), ([], '123')]:
            with self.subTest(role=role, job=job), patch.object(renderer, '_png') as draw:
                with self.assertRaises(renderer.FigureError):
                    renderer.render_figures(report, role=role, job_id=job)
                draw.assert_not_called()

    def test_source_operation_and_plan_identity_are_checked_before_rendering(self):
        original = report_for()
        changes = [lambda r: r.update(version=True),
                   lambda r: r.update(scientific_status='passed'),
                   lambda r: r.update(plan_sha256='a' * 64),
                   lambda r: r['results'][0].update(id='../../escape'),
                   lambda r: r['results'][0].update(table='foreign'),
                   lambda r: r['results'][0].update(value_units={'x': 'step', 'y': 'GPa'}),
                   lambda r: r['results'][0]['points'].pop(),
                   lambda r: r['results'][0]['points'][0].__setitem__(1, 999),
                   lambda r: r['results'][0].update(row_selection={'start': 0, 'stop': 2, 'step': 1}),
                   lambda r: r['results'].clear(),
                   lambda r: r['results'].append(copy.deepcopy(r['results'][0]))]
        for change in changes:
            report = copy.deepcopy(original); change(report)
            with self.subTest(change=change), patch.object(renderer, '_png') as draw:
                with self.assertRaises(renderer.FigureError):
                    renderer.render_figures(report, role='reference', job_id='123')
                draw.assert_not_called()

    def test_nonfinite_boolean_and_wrong_histogram_counts_are_rejected(self):
        for change in (lambda r: r['results'][0]['values']['counts'].__setitem__(0, -1),
                       lambda r: r['results'][0]['values']['counts'].__setitem__(0, True),
                       lambda r: r['results'][0]['values']['edges'].__setitem__(0, float('nan')),
                       lambda r: r['results'][0]['values']['edges'].__setitem__(0, 1),
                       lambda r: r['results'][0]['values'].update(in_range=999)):
            report = report_for('histogram'); change(report)
            with self.subTest(change=change), patch.object(renderer, '_png') as draw:
                with self.assertRaises(renderer.FigureError):
                    renderer.render_figures(report, role='reference', job_id='123')
                draw.assert_not_called()

    def test_chart_count_and_report_bytes_are_bounded_without_truncation(self):
        operations = [dict(id=f'plot{i}', method='line', table='samples', x='step', y='energy')
                      for i in range(renderer.MAX_FIGURES + 1)]
        report = report_for(operations=operations)
        with patch.object(renderer, '_png') as draw:
            with self.assertRaisesRegex(renderer.FigureError, 'figure_count_limit'):
                renderer.render_figures(report, role='reference', job_id='123')
            draw.assert_not_called()
        with patch.object(renderer, 'MAX_OUTPUT_BYTES', 20), patch.object(renderer, '_png') as draw:
            with self.assertRaises(renderer.FigureError):
                renderer.render_figures(report_for(), role='reference', job_id='123')
            draw.assert_not_called()

    def test_summary_only_reports_do_not_invent_a_plot(self):
        result = renderer.render_figures(report_for('summary'), role='research', job_id='123')
        self.assertEqual(result['files'], {})
        self.assertEqual(result['figures'], [])
        self.assertFalse(result['physics_simulation'])
        self.assertEqual(result['scientific_status'], 'not_evaluated')

    def test_point_and_bin_limits_reject_instead_of_sampling_or_combining(self):
        report = report_for()
        with patch.object(renderer, 'MAX_ROWS', 3), patch.object(renderer, '_png') as draw:
            with self.assertRaises(renderer.FigureError):
                renderer.render_figures(report, role='reference', job_id='123')
            draw.assert_not_called()
        report = report_for('histogram')
        with patch.object(renderer, 'MAX_BINS', 2), patch.object(renderer, '_png') as draw:
            with self.assertRaisesRegex(renderer.FigureError, 'invalid_histogram'):
                renderer.render_figures(report, role='reference', job_id='123')
            draw.assert_not_called()

    def test_tool_menu_uses_actual_limits(self):
        menu = renderer.tool_context()
        self.assertEqual(menu['limits']['figures'], renderer.MAX_FIGURES)
        self.assertEqual(menu['limits']['png_bytes'], renderer.MAX_PNG_BYTES)
        self.assertEqual(menu['roles'], ['reference', 'research'])
        self.assertIn('no_mathtext', menu['adapter_identity']['text_interpretation'])

    def test_a_later_caller_edit_cannot_change_the_validated_plot_snapshot(self):
        report = report_for()
        digest = sha256(canonical(report))
        actual_points = []
        def draw(result, description, **kwargs):
            report['results'][0]['points'][0][1] = 999
            actual_points.extend(result['points'])
            return b'synthetic PNG transport placeholder'
        with patch.object(renderer, '_png', side_effect=draw):
            rendered = renderer.render_figures(report, role='reference', job_id='123')
        self.assertEqual(actual_points[0], [0.0, 1.0])
        self.assertEqual(rendered['report_sha256'], digest)


@unittest.skipUnless(find_spec('matplotlib') is not None, 'application plotting extra unavailable')
class PNGFigureTests(unittest.TestCase):
    def png(self, report=None, *, role='reference'):
        return renderer.render_figures(report_for() if report is None else report, role=role, job_id='123')

    def test_valid_png_with_exact_role_units_job_and_digest_metadata(self):
        from PIL import Image
        report = report_for()
        original = canonical(report)
        result = self.png(report)
        self.assertEqual(canonical(report), original)
        self.assertEqual(set(result['files']), {'plot-curve.png'})
        data = result['files']['plot-curve.png']
        self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
        image = Image.open(io.BytesIO(data))
        self.assertEqual((image.format, image.size), ('PNG', (renderer.WIDTH, renderer.HEIGHT)))
        self.assertIn('作者 A', image.info['Title'])
        embedded = json.loads(image.info['Description'])
        self.assertEqual((embedded['role'], embedded['job_id']), ('reference', '123'))
        self.assertEqual(embedded['figure']['x_unit'], 'step')
        self.assertEqual(embedded['figure']['y_unit'], 'eV')
        metadata = result['figures'][0]
        self.assertEqual(metadata['sha256'], sha256(data))
        self.assertEqual(metadata['size'], len(data))
        self.assertEqual(metadata['source']['sha256'], report['sources'][0]['sha256'])
        self.assertEqual(metadata['operation_sha256'], sha256(canonical(report['plan']['operations'][0])))
        self.assertEqual(result['report_sha256'], sha256(original))
        self.assertIsNotNone(result['renderer']['matplotlib_version'])
        self.assertEqual(result['renderer']['backend'], 'FigureCanvasAgg')
        image.verify()

    def test_line_and_scatter_receive_every_original_point_in_original_order(self):
        from matplotlib.axes import Axes
        for method in ('line', 'scatter'):
            report = report_for(method, count=513)
            original = getattr(Axes, 'plot' if method == 'line' else 'scatter')
            calls = []
            def spy(axes, x, y, *args, **kwargs):
                calls.append((list(x), list(y)))
                return original(axes, x, y, *args, **kwargs)
            with self.subTest(method=method), patch.object(Axes, 'plot' if method == 'line' else 'scatter', new=spy):
                result = self.png(report)
            points = report['results'][0]['points']
            self.assertEqual(calls, [([p[0] for p in points], [p[1] for p in points])])
            self.assertEqual(result['figures'][0]['rendered_points'], 513)
            self.assertEqual(result['figures'][0]['data_extent'], {'x': [0, 512], 'y': [1, 1025]})

    def test_histogram_draws_saved_bins_as_bars_without_rebinning_or_lines(self):
        from matplotlib.axes import Axes
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        report = report_for('histogram', count=6)
        before = copy.deepcopy(report)
        values = report['results'][0]['values']
        original_bar, original_png = Axes.bar, FigureCanvasAgg.print_png
        bars, bounds = [], []
        def bar(axes, x, heights, *args, **kwargs):
            bars.append((list(x), list(heights), list(kwargs['width']), kwargs['align']))
            return original_bar(axes, x, heights, *args, **kwargs)
        def png(canvas, *args, **kwargs):
            bounds.append(tuple(canvas.figure.axes[0].get_xlim()))
            return original_png(canvas, *args, **kwargs)
        with patch.object(Axes, 'bar', new=bar), patch.object(FigureCanvasAgg, 'print_png', new=png), \
                patch.object(Axes, 'hist', side_effect=AssertionError('must not recalculate bins')), \
                patch.object(Axes, 'plot', side_effect=AssertionError('histogram is not a line')):
            result = self.png(report)
        self.assertEqual(report, before)
        self.assertEqual(bars, [(values['edges'][:-1], values['counts'],
                                [b-a for a, b in zip(values['edges'], values['edges'][1:])], 'edge')])
        self.assertEqual(bounds, [tuple(report['results'][0]['range'])])
        self.assertEqual(result['figures'][0]['histogram'], values)
        self.assertEqual(result['figures'][0]['rendered_bins'], 3)
        self.assertEqual(values['total'], 6)
        self.assertEqual(values['below_range'], 1)
        self.assertEqual(values['above_range'], 2)

    def test_derived_table_and_literal_scientific_column_names_remain_traceable(self):
        reference = dict(table='samples', column='energy')
        operations = [dict(id='differences', method='derive', alignment='row_index', columns=[
            dict(name='site_index', unit='1', expression=dict(table='samples', column='step')),
            dict(name='delta_E_alpha', unit='eV', expression=reference)]),
            dict(id='derived_curve', method='scatter', table='differences', x='site_index', y='delta_E_alpha')]
        report = report_for(operations=operations)
        result = self.png(report)
        figure = result['figures'][0]
        self.assertEqual((figure['x'], figure['y']), ('site_index', 'delta_E_alpha'))
        self.assertEqual(figure['source']['kind'], 'derived')
        derived = report['derived_tables'][0]
        self.assertEqual(figure['source']['sha256'], derived['csv_sha256'])
        self.assertEqual(figure['source']['references_sha256'], sha256(canonical(derived['references'])))

    def test_research_is_labeled_as_research_without_calling_it_formal_B(self):
        from PIL import Image
        result = self.png(role='research')
        image = Image.open(io.BytesIO(result['files']['plot-curve.png']))
        self.assertIn('本次科研计算', image.info['Title'])
        self.assertNotIn('Author A', image.info['Title'])
        self.assertNotIn('B', image.info['Title'])
        self.assertEqual(result['figures'][0]['role'], 'research')

    def test_TeX_and_mathtext_are_disabled_even_when_ambient_settings_enable_them(self):
        import matplotlib
        from matplotlib.mathtext import MathTextParser
        from matplotlib.texmanager import TexManager
        from PIL import Image
        report = report_for()
        unit = r'$\input{never-read-this-file}$'
        report['sources'][0]['columns'][1]['unit'] = unit
        report['plan']['tables'][0]['columns'][1]['unit'] = unit
        report['plan_sha256'] = sha256(canonical(report['plan']))
        report['results'][0]['value_units']['y'] = unit
        with matplotlib.rc_context({'text.usetex': True, 'text.parse_math': True}), \
                patch.object(TexManager, 'get_text_width_height_descent', side_effect=AssertionError('TeX executed')), \
                patch.object(MathTextParser, 'parse', side_effect=AssertionError('mathtext executed')):
            result = self.png(report)
            self.assertTrue(matplotlib.rcParams['text.usetex'])
        image = Image.open(io.BytesIO(result['files']['plot-curve.png']))
        self.assertEqual(json.loads(image.info['Description'])['figure']['y_unit'], unit)

    def test_PNG_byte_limits_fail_instead_of_returning_partial_figures(self):
        with patch.object(renderer, 'MAX_PNG_BYTES', 64):
            with self.assertRaisesRegex(renderer.FigureError, 'png_limit'):
                self.png()
        with patch.object(renderer, 'MAX_TOTAL_PNG_BYTES', 64):
            with self.assertRaisesRegex(renderer.FigureError, 'total_png_limit'):
                self.png()


if __name__ == '__main__':
    unittest.main()
