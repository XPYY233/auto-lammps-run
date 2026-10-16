"""Bounded PNG rendering of already executed declarative postprocessing.

Only in-memory report data are accepted. No file selection, expression evaluation,
histogram recomputation, model request, physics execution or score is available.
The caller remains responsible for original run identity and storage accounting.
"""
import hashlib
import io
import json
import math
from importlib import metadata
from pathlib import Path
import re
import threading
import unicodedata

from .manifest import canonical, sha256
from .postprocess_tables import MAX_BINS, MAX_OUTPUT_BYTES, MAX_ROWS


VERSION = 1
MAX_FIGURES = 16
MAX_PNG_BYTES = 2 * 1024 * 1024
MAX_TOTAL_PNG_BYTES = 16 * 1024 * 1024
WIDTH, HEIGHT, DPI = 960, 600, 120
METHODS = ('line', 'scatter', 'histogram')
ROLES = {'reference': '作者 A / Author A', 'research': '本次科研计算 / Research'}
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')
_HASH = re.compile(r'[a-f0-9]{64}')
_LOCK = threading.RLock()


class FigureError(ValueError):
    """Fixed public error codes; no path or upstream exception is reflected."""


def _package(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def adapter_identity():
    return dict(adapter_version=VERSION, source_sha256=sha256(Path(__file__).read_bytes()),
                matplotlib_version=_package('matplotlib'), pillow_version=_package('Pillow'),
                backend='FigureCanvasAgg', width=WIDTH, height=HEIGHT, dpi=DPI,
                text_interpretation='literal_no_TeX_no_mathtext')


def tool_context():
    return dict(version=VERSION, adapter_identity=adapter_identity(), methods=list(METHODS),
                roles=list(ROLES), input='complete_executed_postprocess_report_only',
                histogram='render_saved_counts_and_edges_without_rebinning',
                limits=dict(figures=MAX_FIGURES, points_per_figure=MAX_ROWS,
                            histogram_bins=MAX_BINS, png_bytes=MAX_PNG_BYTES,
                            total_png_bytes=MAX_TOTAL_PNG_BYTES, report_bytes=MAX_OUTPUT_BYTES),
                physics_simulation=False, scientific_status='not_evaluated')


def _label(value):
    if (type(value) is not str or not 1 <= len(value) <= 160
            or any(unicodedata.category(character).startswith('C') for character in value)):
        raise FigureError('postprocess_figure_invalid_label')
    return value


def _name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise FigureError('postprocess_figure_invalid_identifier')
    return value


def _number(value):
    if type(value) not in (int, float):
        raise FigureError('postprocess_figure_nonfinite_number')
    try:
        if not math.isfinite(value):
            raise FigureError('postprocess_figure_nonfinite_number')
    except OverflowError:
        raise FigureError('postprocess_figure_nonfinite_number') from None
    return value


def _tables(report):
    tables = {}
    for key, kind in (('sources', 'raw'), ('derived_tables', 'derived')):
        rows = report.get(key)
        if type(rows) is not list:
            raise FigureError('postprocess_figure_source_missing')
        for table in rows:
            if not isinstance(table, dict):
                raise FigureError('postprocess_figure_source_missing')
            identifier = _name(table.get('id'))
            digest = table.get('sha256' if kind == 'raw' else 'csv_sha256')
            if identifier in tables or type(digest) is not str or not _HASH.fullmatch(digest):
                raise FigureError('postprocess_figure_source_identity')
            if (type(table.get('rows')) is not list or not 1 <= len(table['rows']) <= MAX_ROWS
                    or type(table.get('columns')) is not list):
                raise FigureError('postprocess_figure_source_shape')
            columns = {}
            for index, column in enumerate(table['columns']):
                if not isinstance(column, dict):
                    raise FigureError('postprocess_figure_source_shape')
                name = _name(column.get('name'))
                if name in columns:
                    raise FigureError('postprocess_figure_source_shape')
                columns[name] = (index, _label(column.get('unit')))
            tables[identifier] = (table, kind, columns)
    return tables


def _operation(result, plan_operations, tables):
    identifier = _name(result.get('id'))
    operation = plan_operations.get(identifier)
    if (operation is None or any(result.get(key) != value for key, value in operation.items())
            or result.get('method') not in (*METHODS, 'summary')):
        raise FigureError('postprocess_figure_operation_identity')
    if result['method'] == 'summary':
        return None
    source_id = result.get('table')
    if source_id not in tables:
        raise FigureError('postprocess_figure_source_missing')
    table, kind, columns = tables[source_id]
    count = result.get('sample_count')
    if (type(count) is not int or count != len(table['rows'])
            or result.get('row_selection') != dict(start=0, stop=count, step=1)
            or result.get('units_origin') != 'declared_not_inferred'):
        raise FigureError('postprocess_figure_incomplete_selection')
    source = dict(table=source_id, kind=kind,
                  sha256=table['sha256' if kind == 'raw' else 'csv_sha256'],
                  record_sha256=sha256(canonical(table)))
    if kind == 'raw':
        source['file'] = table['file']
    else:
        source['references_sha256'] = sha256(canonical(table['references']))
        source['source_tables'] = list(table['source_tables'])
    description = dict(operation_id=identifier, method=result['method'], source=source,
                       operation_sha256=sha256(canonical(operation)),
                       result_sha256=sha256(canonical(result)), sample_count=count,
                       row_selection=dict(result['row_selection']), units_origin=result['units_origin'])
    if result['method'] in {'line', 'scatter'}:
        x, y = result.get('x'), result.get('y')
        if x == y or x not in columns or y not in columns:
            raise FigureError('postprocess_figure_invalid_axes')
        xi, xu = columns[x]; yi, yu = columns[y]
        if result.get('value_units') != dict(x=xu, y=yu):
            raise FigureError('postprocess_figure_units_changed')
        points = result.get('points')
        if type(points) is not list or len(points) != count:
            raise FigureError('postprocess_figure_incomplete_selection')
        for point, row in zip(points, table['rows']):
            if (type(point) is not list or len(point) != 2
                    or type(row) is not list or max(xi, yi) >= len(row)
                    or point != [row[xi], row[yi]]):
                raise FigureError('postprocess_figure_points_changed')
            _number(point[0]); _number(point[1])
        description.update(x=x, y=y, x_unit=xu, y_unit=yu, rendered_points=count,
                           data_extent=dict(x=[min(p[0] for p in points), max(p[0] for p in points)],
                                            y=[min(p[1] for p in points), max(p[1] for p in points)]))
    else:
        column = result.get('column')
        if column not in columns or result.get('value_unit') != columns[column][1]:
            raise FigureError('postprocess_figure_units_changed')
        values = result.get('values')
        if not isinstance(values, dict):
            raise FigureError('postprocess_figure_invalid_histogram')
        bins, counts, edges = values.get('bins'), values.get('counts'), values.get('edges')
        bounds = values.get('range')
        if (type(bins) is not int or not 1 <= bins <= MAX_BINS or bins != result.get('bins')
                or type(counts) is not list or len(counts) != bins
                or any(type(n) is not int or n < 0 for n in counts)
                or type(edges) is not list or len(edges) != bins + 1
                or type(bounds) is not list or len(bounds) != 2 or bounds != result.get('range')):
            raise FigureError('postprocess_figure_invalid_histogram')
        for value in edges + bounds:
            _number(value)
        if (edges[0] != bounds[0] or edges[-1] != bounds[1]
                or any(a >= b or not math.isfinite(b - a) for a, b in zip(edges, edges[1:]))
                or not math.isfinite(bounds[1] - bounds[0])):
            raise FigureError('postprocess_figure_invalid_histogram')
        for key in ('below_range', 'above_range', 'in_range', 'total'):
            if type(values.get(key)) is not int or values[key] < 0:
                raise FigureError('postprocess_figure_invalid_histogram')
        if (sum(counts) != values['in_range'] or values['total'] != count
                or values['in_range'] + values['below_range'] + values['above_range'] != count):
            raise FigureError('postprocess_figure_invalid_histogram')
        description.update(x=column, y='Count', x_unit=columns[column][1], y_unit='1',
                           rendered_bins=bins, histogram=dict(values))
    return description


class _PNGBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_PNG_BYTES:
            raise FigureError('postprocess_figure_png_limit')
        return super().write(data)


def _png(result, description, *, role, job_id, plan_sha256, renderer):
    try:
        import matplotlib
        from matplotlib import font_manager
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError:
        raise FigureError('postprocess_matplotlib_required') from None
    available = {font.name for font in font_manager.fontManager.ttflist}
    fonts = [name for name in ('PingFang SC', 'Noto Sans CJK SC', 'WenQuanYi Zen Hei',
                              'Arial Unicode MS', 'Heiti TC', 'STHeiti', 'DejaVu Sans') if name in available]
    rc = {'text.usetex': False, 'text.parse_math': False, 'axes.formatter.use_mathtext': False,
          'path.simplify': False, 'agg.path.chunksize': 0, 'font.family': fonts or ['sans-serif'],
          'font.size': 10, 'axes.unicode_minus': False}
    color = '#1e4c9a' if role == 'reference' else '#0b7780'
    with matplotlib.rc_context(rc):
        figure = Figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, layout='constrained')
        try:
            canvas = FigureCanvasAgg(figure)
            axes = figure.subplots()
            figure.suptitle(ROLES[role] + ' · ' + description['operation_id'], fontsize=13,
                            usetex=False, parse_math=False)
            axes.set_title('Job ' + job_id + ' · ' + description['source']['table'], fontsize=10,
                           usetex=False, parse_math=False)
            if result['method'] == 'histogram':
                edges = result['values']['edges']
                axes.bar(edges[:-1], result['values']['counts'], align='edge',
                         width=[b-a for a, b in zip(edges, edges[1:])], color=color,
                         edgecolor='white', linewidth=.4)
                axes.set_xlim(*result['range'])
                axes.set_ylim(bottom=0)
            else:
                x = [point[0] for point in result['points']]
                y = [point[1] for point in result['points']]
                if result['method'] == 'line':
                    axes.plot(x, y, color=color, linewidth=1.4)
                else:
                    axes.scatter(x, y, color=color, s=12, linewidths=0)
            axes.set_xlabel(description['x'] + ' (' + description['x_unit'] + ')', usetex=False, parse_math=False)
            axes.set_ylabel(description['y'] + ' (' + description['y_unit'] + ')', usetex=False, parse_math=False)
            axes.grid(alpha=.18, axis='y' if result['method'] == 'histogram' else 'both')
            description['font_families'] = fonts
            png_metadata = dict(role=role, job_id=job_id, plan_sha256=plan_sha256,
                                figure=description, renderer=renderer,
                                physics_simulation=False, scientific_status='not_evaluated')
            buffer = _PNGBuffer()
            canvas.print_png(buffer, metadata={'Title': ROLES[role] + ' · ' + description['operation_id'],
                                              'Description': canonical(png_metadata).decode('ascii')})
            return buffer.getvalue()
        finally:
            figure.clear()


def render_figures(report, *, role, job_id):
    """Return PNG bytes plus provenance; never write paths or recompute data.

    ``files`` maps safe PNG basenames to bytes. ``figures`` contains matching
    operation/source/units/range receipts. A rejected report publishes nothing.
    """
    if type(role) is not str or role not in ROLES or type(job_id) is not str or not re.fullmatch(r'[0-9]{1,32}', job_id):
        raise FigureError('postprocess_figure_run_identity')
    try:
        if type(report) is not dict:
            raise FigureError('postprocess_figure_invalid_report')
        raw = canonical(report)
        if len(raw) > MAX_OUTPUT_BYTES:
            raise FigureError('postprocess_figure_invalid_report')
        # Validate and draw the same primitive snapshot, even if a caller later
        # edits its own report while another request waits on the renderer lock.
        report = json.loads(raw)
        if (type(report.get('version')) is not int or report.get('version') != 1
                or report.get('scientific_status') != 'not_evaluated'):
            raise FigureError('postprocess_figure_invalid_report')
        plan = report['plan']
        if report['plan_sha256'] != sha256(canonical(plan)):
            raise FigureError('postprocess_figure_plan_identity')
        operations = {}
        for operation in plan['operations']:
            identifier = _name(operation['id'])
            if identifier in operations:
                raise FigureError('postprocess_figure_operation_identity')
            operations[identifier] = operation
        tables = _tables(report)
        results = report['results']
        if type(results) is not list:
            raise FigureError('postprocess_figure_invalid_report')
        descriptions, selected, seen = [], [], set()
        for result in results:
            if not isinstance(result, dict) or result.get('id') in seen:
                raise FigureError('postprocess_figure_operation_identity')
            seen.add(result.get('id'))
            description = _operation(result, operations, tables)
            if description is not None:
                descriptions.append(description); selected.append(result)
        if seen != {key for key, value in operations.items() if value['method'] != 'derive'}:
            raise FigureError('postprocess_figure_incomplete_results')
        if len(selected) > MAX_FIGURES:
            raise FigureError('postprocess_figure_count_limit')
    except FigureError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, RuntimeError):
        raise FigureError('postprocess_figure_invalid_report') from None
    renderer = adapter_identity()
    files, total = {}, 0
    with _LOCK:
        for result, description in zip(selected, descriptions):
            data = _png(result, description, role=role, job_id=job_id,
                        plan_sha256=report['plan_sha256'], renderer=renderer)
            total += len(data)
            if total > MAX_TOTAL_PNG_BYTES:
                raise FigureError('postprocess_figure_total_png_limit')
            name = 'plot-' + description['operation_id'] + '.png'
            files[name] = data
            description.update(name=name, mime_type='image/png', size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                               role=role, job_id=job_id, width=WIDTH, height=HEIGHT,
                               plan_sha256=report['plan_sha256'])
    return dict(version=VERSION, files=files, figures=descriptions, renderer=renderer,
                role=role, job_id=job_id, report_sha256=sha256(canonical(report)),
                plan_sha256=report['plan_sha256'], physics_simulation=False,
                scientific_status='not_evaluated')
