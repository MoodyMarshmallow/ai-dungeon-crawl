import argparse
import os
from pathlib import Path
import subprocess


def main():
    """Apply the opt-in input marker and build a separate local WebTiles binary."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build DCSS for the agent harness")
    parser.add_argument("crawl", nargs="?", type=Path, default=root.parent / "crawl",
                        help="DCSS checkout; defaults to the sibling crawl directory")
    args = parser.parse_args()
    checkout = args.crawl.expanduser().resolve()
    source = checkout / "crawl-ref/source"
    if not (source / "tileweb.cc").is_file():
        parser.error("Expected a DCSS checkout containing crawl-ref/source/tileweb.cc")
    patch = root / "patches/dcss-input-boundary.patch"
    applied = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)],
                             cwd=checkout, capture_output=True).returncode == 0
    if not applied:
        check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout,
                               capture_output=True, text=True)
        if check.returncode:
            parser.error("Readiness patch does not match this checkout; no files changed.\n" + check.stderr)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    subprocess.run([
        "make", f"-j{min(os.cpu_count() or 2, 8)}", "WEBTILES=1", "DEBUG=1",
        "NO_PKGCONFIG=1", "GAME=crawl-web-harness", "crawl-web-harness", "webserver",
    ], cwd=source, check=True)
    print(f"Built {source / 'crawl-web-harness'}")


if __name__ == "__main__":
    main()
