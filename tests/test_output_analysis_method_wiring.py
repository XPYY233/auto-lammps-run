"""Web composition connects cached methods without reading or invoking AI."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_lammps.tasks import TaskStore
from auto_lammps.web import create_app


class OutputAnalysisMethodWiringTests(unittest.TestCase):
    def test_composition_defers_reference_cache_to_the_current_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            tasks=TaskStore(Path(root)/'tasks.sqlite')
            workspace=Mock(tasks=tasks,execution=None)
            expected=dict(sources=[],source_binding=None,missing=['not read'],available=False)
            workspace.cached_analysis_sources.return_value=expected
            analysis=Mock(tasks=tasks)
            with patch('auto_lammps.output_analysis.OutputAnalysis',return_value=analysis) as factory:
                create_app(tasks,author_workspace_service=workspace)
            workspace.cached_analysis_sources.assert_not_called()
            provider=factory.call_args.kwargs['reference_method_sources']
            self.assertEqual(provider('a'*32),expected)
            workspace.cached_analysis_sources.assert_called_once_with('a'*32)

