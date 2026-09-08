from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


MANUAL_FILENAME = "crawl_manual.rst"
PROVENANCE_FILENAME = "crawl_manual.provenance.json"


@dataclass(frozen=True)
class ManualProvision:
    """Paths and provenance for a provisioned, read-only manual."""

    manual_path: Path
    provenance_path: Path
    license_path: Path | None
    source: Path
    source_revision: str | None
    sha256: str


def _git_revision(source: Path) -> str | None:
    """Return the containing checkout's revision, if source is in one."""
    try:
        result = subprocess.run(
            ["git", "-C", str(source.parent), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    revision = result.stdout.strip()
    return revision or None


def _readonly(path: Path) -> None:
    """Make a provisioned regular file readable but not writable."""
    path.chmod(0o444)


def prepare_manual(source: Path, destination: Path) -> ManualProvision:
    """Copy *source* into an episode workspace with reproducible provenance.

    ``destination`` is the per-episode workspace directory.  The operation is
    idempotent: an existing copy is replaced only after the source has been
    validated, and all resulting files are read-only.  A small JSON sidecar
    records the source path, source checkout revision, content hash, and the
    license reference; no Crawl source tree is vendored.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Crawl manual source is not a file: {source}")
    destination.mkdir(parents=True, exist_ok=True)

    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    revision = _git_revision(source)
    manual_path = destination / MANUAL_FILENAME
    provenance_path = destination / PROVENANCE_FILENAME

    # Replace only files owned by this provisioner, using a temporary sibling
    # so an interrupted setup cannot leave a truncated manual.
    temp_manual = manual_path.with_name(f".{manual_path.name}.tmp-{os.getpid()}")
    temp_manual.write_bytes(content)
    _readonly(temp_manual)
    temp_manual.replace(manual_path)
    provenance = {
        "source": str(source),
        "source_revision": revision,
        "sha256": digest,
        "manual_filename": MANUAL_FILENAME,
        "license_reference": "See the source manual's Section L (Licence, contact, history).",
    }
    temp_provenance = provenance_path.with_name(
        f".{provenance_path.name}.tmp-{os.getpid()}"
    )
    temp_provenance.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    _readonly(temp_provenance)
    temp_provenance.replace(provenance_path)
    return ManualProvision(manual_path, provenance_path, None, source, revision, digest)


__all__ = ["ManualProvision", "prepare_manual"]
