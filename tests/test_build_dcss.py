import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_dcss', ROOT / 'scripts/build_dcss.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildPinTests(unittest.TestCase):
    def test_exact_fork_and_upstream_revisions_are_accepted(self):
        lock = json.loads((ROOT / 'dcss.lock.json').read_text())
        for revision in (lock['revision'], lock['upstream_revision']):
            self.assertRegex(revision, r'^[0-9a-f]{40}$')
            with patch.object(build.subprocess, 'check_output', return_value=revision + '\n'):
                build.check_revision(ROOT, lock)

    def test_other_revision_is_rejected_without_mutation(self):
        lock = json.loads((ROOT / 'dcss.lock.json').read_text())
        with patch.object(build.subprocess, 'check_output', return_value='0' * 40), \
                patch.object(build.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'not pinned'):
                build.check_revision(ROOT, lock)
            run.assert_not_called()
