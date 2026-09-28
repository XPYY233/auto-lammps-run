"""Application-owned analysis dependencies; never discover desktop applications.

The probe exercises geometry, structural classification and offscreen rendering,
not an energy/force calculation. Invoke in a subprocess so native failures cannot
terminate the web service.
"""
import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import tempfile

REQUIRED = {'ovito': '3.16.1.post1', 'matplotlib': '3.11.2', 'ase': '3.29.0'}


def versions():
    result = {}
    for name, expected in REQUIRED.items():
        try:
            found = metadata.version(name)
        except metadata.PackageNotFoundError:
            found = None
        result[name] = {'expected': expected, 'installed': found, 'ready': found == expected}
    return result


def smoke_check():
    """Real synthetic BCC classification, RDF and PNG export in a clean process."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('OVITO_THREAD_COUNT', '2')
    import ovito  # Must precede Qt imports; loads platform runtime libraries.
    import numpy as np
    from ovito.data import DataCollection, Particles, SimulationCell
    from ovito.pipeline import Pipeline, StaticSource
    from ovito.modifiers import CommonNeighborAnalysisModifier, CoordinationAnalysisModifier
    from ovito.vis import Viewport, TachyonRenderer
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib.figure import Figure

    points = [(3*(x+b), 3*(y+b), 3*(z+b))
              for x in range(4) for y in range(4) for z in range(4) for b in (0, .5)]
    data = DataCollection()
    particles = Particles()
    particles.create_property('Position', data=points)
    data.objects.append(particles)
    cell = SimulationCell(pbc=(True, True, True))
    cell[...] = [[12, 0, 0, 0], [0, 12, 0, 0], [0, 0, 12, 0]]
    data.objects.append(cell)
    pipeline = Pipeline(source=StaticSource(data=data))
    pipeline.modifiers.append(CommonNeighborAnalysisModifier(
        mode=CommonNeighborAnalysisModifier.Mode.AdaptiveCutoff))
    pipeline.modifiers.append(CoordinationAnalysisModifier(cutoff=4, number_of_bins=40))
    out = pipeline.compute()
    if not np.all(out.particles['Structure Type'] == 3):
        raise RuntimeError('Synthetic BCC classification failed')
    rdf = out.tables['coordination-rdf'].xy()
    if not np.isfinite(rdf).all() or not np.any(rdf[:, 1] > 0):
        raise RuntimeError('Synthetic RDF failed')
    with tempfile.TemporaryDirectory(prefix='auto-lammps-analysis-check-') as folder:
        pipeline.add_to_scene()
        try:
            view = Viewport(type=Viewport.Type.Ortho, camera_dir=(-1, -1, -1))
            view.zoom_all(size=(160, 160))
            image = Path(folder) / 'structure.png'
            view.render_image(filename=str(image), size=(160, 160),
                              renderer=TachyonRenderer(antialiasing=False))
            if not image.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
                raise RuntimeError('Structure PNG export failed')
        finally:
            pipeline.remove_from_scene()
        fig = Figure()
        fig.subplots().plot(rdf[:, 0], rdf[:, 1])
        fig.savefig(Path(folder) / 'rdf.png')
    return {'bcc_atoms': len(points), 'rdf_bins': len(rdf), 'structure_png': True,
            'plot_png': True, 'engine_version': ovito.version_string,
            'physics_simulation': False}


def main():
    parser = argparse.ArgumentParser(description='Check the application analysis runtime')
    parser.add_argument('--smoke', action='store_true', help='Exercise synthetic geometry and rendering')
    args = parser.parse_args()
    packages = versions()
    report = {'packages': packages, 'ready': all(p['ready'] for p in packages.values())}
    if args.smoke and report['ready']:
        try:
            report['checks'] = smoke_check()
        except Exception as exc:
            report.update(ready=False, error=type(exc).__name__,
                          action='Check analysis runtime installation and platform graphics libraries')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
