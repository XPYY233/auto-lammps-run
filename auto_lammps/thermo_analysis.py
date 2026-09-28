"""Explicit, provenance-retaining thermo-block analysis of completed logs."""
import csv
import json
import math
from pathlib import Path

from .analysis_runtime import versions
from .retained_analysis import digest


def blocks(content):
    """Read completed numeric thermo blocks, preserving original line numbers."""
    result, header, rows = [], None, []
    for line, text in enumerate(content.splitlines(), 1):
        fields = text.split()
        if fields and fields[0] == 'Step' and len(fields) > 2:
            if header is not None:
                raise ValueError('Unfinished thermo block')
            header, rows = fields, []
        elif header is not None:
            if text.startswith('Loop time of'):
                if not rows:
                    raise ValueError('Empty thermo block')
                result.append(rows)
                header, rows = None, []
            elif fields:
                if len(fields) != len(header):
                    raise ValueError('Unexpected thermo row')
                values = list(map(float, fields))
                if not all(map(math.isfinite, values)) or not values[0].is_integer():
                    raise ValueError('Invalid thermo number')
                row = dict(zip(header, values), source_line=line)
                if rows and row['Step'] <= rows[-1]['Step']:
                    raise ValueError('Thermo steps not increasing')
                rows.append(row)
    if header is not None:
        raise ValueError('Incomplete thermo output')
    return result


def series(content, plan):
    if plan['units'] != 'metal' or plan['dt_ps'] <= 0 or not math.isfinite(plan['dt_ps']):
        raise ValueError('Explicit metal units and positive timestep required')
    sections = blocks(content)
    result = []
    for stage in plan['stages']:
        part = sections[stage['block']]
        if (part[0]['Step'], part[-1]['Step']) != (stage['first_step'], stage['last_step']):
            raise ValueError('Stage extent differs from plan')
        for item in part:
            if plan['energy'] == 'logged_total':
                energy, pressure, volume = item['TotEng'], item['Press'] * 1e-4, item['Volume']
            elif plan['energy'] == 'reconstructed_total':
                # Controller must verify temperature DOF and absence of constraints.
                dof = plan['temperature_dof']
                if type(dof) is not int or dof <= 0:
                    raise ValueError('Explicit temperature degrees of freedom required')
                energy = item['PotEng'] + .5 * dof * plan['boltzmann_eV_per_K'] * item['Temp']
                pressure = (item['Pxx'] + item['Pyy'] + item['Pzz']) / 3 * 1e-4
                volume = item['Lx'] * item['Ly'] * item['Lz']
            else:
                raise ValueError('Unknown total energy source')
            result.append(dict(stage=stage['name'], step=int(item['Step']), time_ps=item['Step']*plan['dt_ps'],
                               temperature_K=item['Temp'], total_energy_eV=energy,
                               pressure_GPa=pressure, volume_A3=volume, source_line=item['source_line']))
    return result


def analyze(plan, destination):
    source = Path(plan['source']['path'])
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 32*1024*1024:
        raise ValueError('Invalid retained log')
    if digest(source) != plan['source']['sha256']:
        raise ValueError('Source differs from verified log')
    rows = series(source.read_text(), plan)
    folder = Path(destination)
    folder.mkdir(mode=0o700)
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib.figure import Figure
    fig = Figure(figsize=(10, 6), layout='constrained')
    for ax, field, label, color in zip(fig.subplots(2, 2).flat,
            ('temperature_K', 'total_energy_eV', 'pressure_GPa', 'volume_A3'),
            ('Temperature (K)', 'Total energy (eV)', 'Pressure (GPa)', 'Volume (Å³)'),
            ('#087f8c', '#8056aa', '#ba7524', '#2563eb')):
        ax.plot([r['time_ps'] for r in rows], [r[field] for r in rows], '.-', color=color, markersize=3)
        ax.set(xlabel='Time (ps)', ylabel=label)
        ax.grid(alpha=.15)
        ax.axvline(plan['stages'][0]['last_step']*plan['dt_ps'], color='#94a3b8', linestyle='--', linewidth=.7)
    fig.suptitle('Heating and relaxation · ' + plan['energy'].replace('_', ' '))
    fig.savefig(folder / 'analysis.png', dpi=160)
    with (folder / 'analysis.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    if digest(source) != plan['source']['sha256']:
        raise ValueError('Log changed during analysis')
    receipt = dict(version=1, status='posthoc_diagnostic', plan=plan, packages=versions(),
                   physics_simulation=False, scientific_pass=None, rows=len(rows),
                   files=[dict(name=p.name, size=p.stat().st_size, sha256=digest(p))
                          for p in sorted(folder.iterdir())])
    (folder / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    for file in folder.iterdir():
        file.chmod(0o600)
    return receipt
