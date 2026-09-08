import json
import tempfile
import unittest
from pathlib import Path

from ai_dungeon_crawl.manual import (
    MANUAL_FILENAME,
    PROVENANCE_FILENAME,
    prepare_manual,
)


class ManualProvisionTests(unittest.TestCase):
    def test_prepare_manual_copies_read_only_manual_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source_root = tmp_path / "crawl" / "docs"
            source_root.mkdir(parents=True)
            source = source_root / MANUAL_FILENAME
            source.write_text("Manual revision\n")
            destination = tmp_path / "episode" / "shell"

            provision = prepare_manual(source, destination)

            self.assertEqual(provision.manual_path.read_text(), "Manual revision\n")
            self.assertIsNone(provision.license_path)
            metadata = json.loads((destination / PROVENANCE_FILENAME).read_text())
            self.assertEqual(metadata["source"], str(source.resolve()))
            self.assertIsNone(metadata["source_revision"])
            self.assertEqual(metadata["sha256"], provision.sha256)
            self.assertIn("Section L", metadata["license_reference"])
            for path in (provision.manual_path, provision.provenance_path):
                self.assertEqual(path.stat().st_mode & 0o222, 0)

    def test_prepare_manual_rejects_missing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                prepare_manual(tmp_path / "missing.rst", tmp_path / "episode")

    def test_prepare_manual_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source = tmp_path / MANUAL_FILENAME
            source.write_text("same\n")
            destination = tmp_path / "episode"
            first = prepare_manual(source, destination)
            second = prepare_manual(source, destination)
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(second.manual_path.read_text(), "same\n")


if __name__ == "__main__":
    unittest.main()
