import argparse
import json
import os
from pathlib import Path
import subprocess


def check_revision(checkout, lock):
    """Accept the pinned fork or its upstream base plus our bundled patches."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if revision not in (lock["revision"], lock["upstream_revision"]):
        raise ValueError(
            f"DCSS revision {revision} is not pinned. Use {lock['repository']} "
            f"at {lock['revision']}, or upstream at {lock['upstream_revision']}. "
            "No files changed; the build helper never switches your checkout.")


def main():
    """Apply opt-in harness patches and build a separate local WebTiles binary."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build DCSS for the agent harness")
    parser.add_argument("crawl", nargs="?", type=Path, default=root.parent / "crawl",
                        help="DCSS checkout; defaults to the sibling crawl directory")
    args = parser.parse_args()
    checkout = args.crawl.expanduser().resolve()
    source = checkout / "crawl-ref/source"
    if not (source / "tileweb.cc").is_file():
        parser.error("Expected a DCSS checkout containing crawl-ref/source/tileweb.cc")
    lock = json.loads((root / "dcss.lock.json").read_text())
    try:
        check_revision(checkout, lock)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    patches = [root / "patches" / name for name in (
        "dcss-input-boundary.patch", "dcss-session-guard.patch", "dcss-score.patch",
    )]
    pending = []
    for patch in patches:
        applied = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)],
                                 cwd=checkout, capture_output=True).returncode == 0
        if applied:
            continue
        check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout,
                               capture_output=True, text=True)
        if check.returncode:
            parser.error(f"{patch.name} does not match this checkout; no files changed.\n" + check.stderr)
        pending.append(patch)
    for patch in pending:
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    subprocess.run([
        "make", f"-j{min(os.cpu_count() or 2, 8)}", "WEBTILES=1", "DEBUG=1",
        "NO_PKGCONFIG=1", "GAME=crawl-web-harness", "crawl-web-harness", "webserver",
    ], cwd=source, check=True)
    print(f"Built {source / 'crawl-web-harness'}")


if __name__ == "__main__":
    main()
