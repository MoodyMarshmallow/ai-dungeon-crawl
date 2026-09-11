"""Parent-owned snapshots crossing the review/action capability boundary.

Only regular files are accepted. No reviewer code is imported or executed here.
"""
import os
from pathlib import Path
import stat
import tempfile

from ..agent.prompts import base_prompts, load_prompts, write_read_only_prompts

MAX_ARTIFACT_FILES = 256
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_LOG_BYTES = 256 * 1024 * 1024


def _copy_file(source, target, limit):
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f'Not a regular file or size limit exceeded: {source.name}')
        with target.open('xb') as writer:
            remaining = limit
            while chunk := reader.read(min(65536, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError(f'File size limit exceeded: {source.name}')
                writer.write(chunk)
        target.chmod(0o600 | (info.st_mode & 0o100))
        return limit - remaining


def _copy_artifacts(source, target):
    budget = [MAX_ARTIFACT_FILES, MAX_ARTIFACT_BYTES, 512]

    def copy_directory(src, dst, depth=0):
        if src.is_symlink() or not src.is_dir() or depth > 16:
            raise ValueError('Artifact directories must be real directories with depth <= 16')
        budget[2] -= 1
        if budget[2] < 0:
            raise ValueError('Too many artifact directories')
        dst.mkdir()
        for entry in src.iterdir():
            if entry.is_symlink():
                raise ValueError('Artifact symlinks are not allowed')
            if entry.is_dir():
                copy_directory(entry, dst / entry.name, depth + 1)
            else:
                budget[0] -= 1
                if budget[0] < 0:
                    raise ValueError('Too many artifact files')
                budget[1] -= _copy_file(entry, dst / entry.name, min(MAX_FILE_BYTES, budget[1]))

    for name in ('tools', 'skills'):
        if (source / name).exists() or (source / name).is_symlink():
            copy_directory(source / name, target / name)
        else:
            (target / name).mkdir()
    if (source / 'prompts').exists() or (source / 'prompts').is_symlink():
        load_prompts(source / 'prompts' / 'agent_editable' / 'action').write(target / 'prompts' / 'agent_editable' / 'action')
    else:
        base_prompts().write(target / 'prompts' / 'agent_editable' / 'action')


def prepare_files(root, workspace, *, profile, artifacts_path, review_log_path):
    # A submission's working directory persists. Provision only once.
    marker = root / 'files-ready'
    if marker.exists():
        return
    if profile == 'review':
        logs = root / 'logs'
        logs.mkdir()
        for name in ('model.jsonl', 'game.jsonl'):
            _copy_file(review_log_path / name, logs / name, MAX_LOG_BYTES)
        (workspace / 'logs').symlink_to(logs, target_is_directory=True)
        reference = root / 'reference'
        reference.mkdir()
        base_prompts().write(reference / 'action_originals')
        write_read_only_prompts(reference)
        if (review_log_path / 'action-prompts.json').exists():
            _copy_file(review_log_path / 'action-prompts.json', reference / 'action-prompts.json', MAX_FILE_BYTES)
    snapshot = artifacts_path / 'snapshot' if artifacts_path else None
    if profile == 'review':
        if snapshot and snapshot.exists():
            _copy_artifacts(snapshot, workspace)
        else:
            for name in ('tools', 'skills'):
                (workspace / name).mkdir()
            base_prompts().write(workspace / 'prompts' / 'agent_editable' / 'action')
        (workspace / 'prompts' / 'read_only').symlink_to(reference, target_is_directory=True)
    else:
        copies = root / 'artifacts'
        copies.mkdir()
        if snapshot and snapshot.exists():
            _copy_artifacts(snapshot, copies)
        else:
            for name in ('tools', 'skills'):
                (copies / name).mkdir()
            base_prompts().write(copies / 'prompts' / 'agent_editable' / 'action')
        for name in ('tools', 'skills', 'prompts'):
            (workspace / name).symlink_to(copies / name, target_is_directory=True)
    marker.touch()


def publish_artifacts(workspace, store):
    """Validate fully before replacing the previous session snapshot.

    Call only after all shell children have been terminated; the reviewer cannot
    read or write this store. The lifecycle serializes publication and loading.
    """
    load_prompts(workspace / 'prompts' / 'agent_editable' / 'action')
    store.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='publish-', dir=store) as staging:
        staged = Path(staging) / 'snapshot'
        staged.mkdir()
        _copy_artifacts(workspace, staged)
        snapshot = store / 'snapshot'
        previous = Path(staging) / 'previous'
        if snapshot.exists():
            snapshot.rename(previous)
        try:
            staged.rename(snapshot)
        except BaseException:
            if previous.exists():
                previous.rename(snapshot)
            raise
