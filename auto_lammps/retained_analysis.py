"""Postprocess retained LAMMPS dumps with the application OVITO module.

Controller supplies verified raw-output paths and hashes. This tool never starts
a simulation, retrieves a resource, or changes an evaluation/score. Its explicit
diagnostic plan is retained alongside every derived result.
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

from .analysis_runtime import versions


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def analyze(plan, destination):
    if not all(p['ready'] for p in versions().values()):
        raise ValueError('Application analysis runtime is missing or has different versions')
    source = Path(plan['source']['path'])
    if source.is_symlink() or not source.is_file():
        raise ValueError('Source must be a retained regular output')
    if digest(source) != plan['source']['sha256']:
        raise ValueError('Source differs from verified output')
    if plan['kind'] not in {'cna', 'rdf_snapshot'} or plan['length_unit'] != 'angstrom':
        raise ValueError('Unsupported diagnostic plan')
    elements = plan['elements']
    if (not isinstance(elements, dict) or not elements
            or any(not re.fullmatch(r'[1-9][0-9]*', k)
                   or not re.fullmatch(r'[A-Z][a-z]?', v) for k, v in elements.items())):
        raise ValueError('Explicit particle type to element mapping required')
    if plan['kind'] == 'rdf_snapshot':
        cutoff, bins = plan['cutoff'], plan['bins']
        if (not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff) or cutoff <= 0
                or type(bins) is not int or not 2 <= bins <= 10000):
            raise ValueError('Invalid RDF settings')
    # The pinned Linux runtime uses CPU Tachyon rendering without a display.
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('OVITO_THREAD_COUNT', '2')
    import ovito
    import numpy as np
    from ovito.io import import_file
    from ovito.modifiers import CommonNeighborAnalysisModifier, CoordinationAnalysisModifier
    from ovito.vis import Viewport, TachyonRenderer
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib.figure import Figure
    with source.open('rb') as stream:
        if not stream.read(32).startswith(b'ITEM: TIMESTEP\n'):
            raise ValueError('Only explicit LAMMPS dump files are accepted')
    pipeline = import_file(str(source), input_format='lammps/dump')
    frames = plan['frames']
    if frames == 'all' and plan['kind'] == 'cna':
        frames = list(range(pipeline.source.num_frames))
    if (not isinstance(frames, list) or not frames or len(set(frames)) != len(frames)
            or any(type(f) is not int or not 0 <= f < pipeline.source.num_frames for f in frames)
            or (plan['kind'] == 'rdf_snapshot' and len(frames) != 1)):
        raise ValueError('Invalid source frames')
    snapshot_frames = plan.get('snapshot_frames', [])
    if len(snapshot_frames) > 24 or any(type(f) is not int or f not in frames for f in snapshot_frames):
        raise ValueError('Snapshot must be an analyzed frame')
    folder = Path(destination)
    folder.mkdir(mode=0o700)  # Refuse overwrite; no silent replacement of earlier evidence.
    if plan['kind'] == 'cna':
        pipeline.modifiers.append(CommonNeighborAnalysisModifier(
            mode=CommonNeighborAnalysisModifier.Mode.AdaptiveCutoff))
    else:
        pipeline.modifiers.append(CoordinationAnalysisModifier(cutoff=cutoff, number_of_bins=bins,
                                                              partial=True))
    rows, initial_length = [], None
    for frame in frames:
        data = pipeline.compute(frame)
        if data.particles.count < 1 or not np.isfinite(data.particles.positions).all():
            raise ValueError('Invalid particle coordinates')
        present = set(map(int, data.particles['Particle Type']))
        if not present.issubset({int(k) for k in elements}):
            raise ValueError('Unmapped particle type')
        matrix = np.array(data.cell)[:, :3]
        length = float(np.linalg.norm(matrix[:, 0]))
        if initial_length is None:
            initial_length = float(np.linalg.norm(np.array(pipeline.compute(0).cell)[:, 0]))
        step = int(data.attributes['Timestep'])
        if plan['kind'] == 'cna':
            counts = np.bincount(np.asarray(data.particles['Structure Type']), minlength=5)
            rows.append([frame, step, length / initial_length - 1, data.particles.count,
                         *map(int, counts[:5])])
        else:
            # A single periodic box must be large enough for an unambiguous radius.
            heights = 1 / np.linalg.norm(np.linalg.inv(matrix), axis=1)
            if not all(data.cell.pbc) or cutoff >= float(min(heights)) / 2:
                raise ValueError('RDF requires periodic cell with cutoff below half its minimum height')
            table = data.tables['coordination-rdf']
            labels = ['-'.join(elements.get(part, part) for part in label.split('-'))
                      for label in table.y.component_names]
            values = table.xy()
            if not np.isfinite(values).all():
                raise ValueError('Nonfinite RDF')
            rows = values.tolist()
        if frame in snapshot_frames:
            pipeline.add_to_scene()
            try:
                view = Viewport(type=Viewport.Type.Ortho, camera_dir=(-1, -1, -1))
                view.zoom_all(size=(640, 480))
                view.render_image(filename=str(folder / f'frame-{frame}.png'), size=(640, 480),
                                  frame=frame, background=(1, 1, 1), renderer=TachyonRenderer())
            finally:
                pipeline.remove_from_scene()
    fig = Figure(figsize=(7, 4.5), layout='constrained')
    ax = fig.subplots()
    if plan['kind'] == 'cna':
        headers = ['frame', 'step', 'cell_x_strain', 'atoms', 'Other', 'FCC', 'HCP', 'BCC', 'ICO']
        for index, color in zip(range(4, 9), ('#64748b', '#0e9f6e', '#cf4d67', '#2563eb', '#d58b15')):
            ax.plot([r[2] for r in rows], [r[index] / r[3] for r in rows], label=headers[index], color=color)
        ax.set(xlabel='Cell x strain', ylabel='Atom fraction', title='Adaptive CNA · retained trajectory')
    else:
        headers = ['r_angstrom', *labels]
        for index, label in enumerate(labels, 1):
            ax.plot([r[0] for r in rows], [r[index] for r in rows], label=label, linewidth=1)
        ax.set(xlabel='r (Å)', ylabel='Partial g(r)', title='Single-frame RDF · not time averaged')
    ax.legend(fontsize=8, ncol=3)
    ax.grid(alpha=.15)
    fig.savefig(folder / 'analysis.png', dpi=160)
    if snapshot_frames:
        from matplotlib.image import imread
        sheet = Figure(figsize=(12, 3.7 * math.ceil(len(snapshot_frames) / 3)), layout='constrained')
        axes = sheet.subplots(math.ceil(len(snapshot_frames) / 3), 3, squeeze=False).flat
        for index, axis in enumerate(axes):
            axis.axis('off')
            if index < len(snapshot_frames):
                frame = snapshot_frames[index]
                axis.imshow(imread(folder / f'frame-{frame}.png'))
                axis.set_title(f'Frame {frame} · cell x strain {rows[frames.index(frame)][2]:.3f}', fontsize=11)
        sheet.suptitle('Adaptive CNA · blue BCC / green FCC / red HCP / grey Other', fontsize=12)
        sheet.savefig(folder / 'snapshots.png', dpi=140)
    with (folder / 'analysis.csv').open('x', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
    if digest(source) != plan['source']['sha256']:
        raise ValueError('Raw output changed during analysis; no receipt published')
    receipt = dict(version=1, status='posthoc_diagnostic', plan=plan,
                   packages=versions(), ovito_version=ovito.version_string,
                   frames=frames, input_frame_count=pipeline.source.num_frames,
                   files=[dict(name=p.name, size=p.stat().st_size, sha256=digest(p))
                          for p in sorted(folder.iterdir())],
                   physics_simulation=False, scientific_pass=None)
    (folder / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    for file in folder.iterdir():
        file.chmod(0o600)
    return receipt


def main():
    parser = argparse.ArgumentParser(description='Analyze already collected output; no simulation')
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = analyze(json.loads(args.plan.read_text()), args.output)
    print(json.dumps({'status': result['status'], 'frames': len(result['frames'])}))


if __name__ == '__main__':
    main()
