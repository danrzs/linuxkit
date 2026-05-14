#!/usr/bin/env python3
"""Scan kernel/*/build-args for KERNEL_VERSION entries, query kernel.org for
the latest patch release in each tracked series, and update the files.

Usage:
  uv run bump_kernel.py [--dry-run] [--changes-to-file PATH]

The script is idempotent: if a file is already at the latest patch it is
left unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Final, List, Optional

import requests
from semver import Version

KERNEL_JSON_URL: str = os.environ.get("KERNEL_JSON_URL", "https://www.kernel.org/releases.json")

# Matches "KERNEL_VERSION = 6.1.88" or "KERNEL_VERSION=6.1.88" (anchored to
# the start of a line so it cannot partially match other keys).
KERNEL_VERSION_RE: Final = re.compile(r"^KERNEL_VERSION\s*=\s*([\d.]+)\s*$", re.MULTILINE)

DEFAULT_REPO_ROOT: Final = Path(__file__).parent.parent.parent

logger = logging.getLogger(__name__)


@dataclass
class Change:
    """Records a single version bump applied to one build-args file."""

    path: Path
    old: Version
    new: Version

    def to_dict(self) -> Dict[str, str]:
        return {"path": str(self.path), "old": str(self.old), "new": str(self.new)}


@dataclass
class BuildArgsFile:
    """Wraps a kernel/*/build-args file and provides version read/write helpers."""

    path: Path
    # Lazily populated on first read; avoids re-reading the file on repeated calls.
    _version: Optional[Version] = field(default=None, init=False, repr=False)

    def get_current_version(self) -> Optional[Version]:
        """Return the KERNEL_VERSION from this file, reading and caching on first call."""
        if self._version is None:
            m = KERNEL_VERSION_RE.search(self.path.read_text())
            if m and Version.is_valid(m.group(1)):
                self._version = Version.parse(m.group(1))
        return self._version

    def bump_to(self, new_version: Version) -> Optional[Change]:
        """Rewrite the KERNEL_VERSION line to `new_version` and return a Change record."""
        text = self.path.read_text()
        new_text = KERNEL_VERSION_RE.sub(f"KERNEL_VERSION={new_version}", text)
        if new_text == text:
            # No line matched or the value was already equal — nothing to do.
            return None
        self.path.write_text(new_text)
        return Change(path=self.path, old=self._version, new=new_version)


def fetch_kernel_versions(timeout: int = 10) -> List[Version]:
    """Fetch all stable release versions from kernel.org.

    Release candidates (e.g. "6.2-rc1") are skipped because we should be
    bumping to them (and it breaks semver)
    """
    resp = requests.get(KERNEL_JSON_URL, timeout=timeout)
    resp.raise_for_status()
    versions: List[Version] = []
    for release in resp.json().get("releases", []):
        vstr = release.get("version", "")
        if not Version.is_valid(vstr):
            logger.debug("Skipping non-stable version: %s", vstr)
            continue
        versions.append(Version.parse(vstr))
    return versions


def latest_patch(versions: List[Version], major: int, minor: int) -> Optional[Version]:
    """Return the highest patch-level version for the given major.minor series."""
    candidates = [v for v in versions if v.major == major and v.minor == minor]
    return max(candidates) if candidates else None


def find_build_args_files(root: Path = DEFAULT_REPO_ROOT) -> List[BuildArgsFile]:
    """Glob for all kernel/*/build-args files under `root`."""
    return [BuildArgsFile(path=p) for p in sorted(root.glob("kernel/*/build-args"))]


def check_and_bump(f: BuildArgsFile, versions: List[Version], dry_run: bool) -> Optional[Change]:
    """Compare `f`'s current version against upstream `versions`.

    If a newer patch exists for the same major.minor series, bumps the file
    (unless `dry_run` is True) and returns a Change.  Returns None when no
    update is needed.
    """
    current = f.get_current_version()
    if current is None:
        logger.warning("No KERNEL_VERSION found in %s, skipping", f.path)
        return None

    best = latest_patch(versions, current.major, current.minor)
    if best is None:
        logger.info("No known releases for %d.%d.x, skipping %s", current.major, current.minor, f.path)
        return None

    if best <= current:
        logger.info("%s is up-to-date at %s", f.path, current)
        return None

    logger.info("Bumping %s: %s → %s%s", f.path, current, best, " (dry run)" if dry_run else "")
    if dry_run:
        return Change(path=f.path, old=current, new=best)
    return f.bump_to(best)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bump KERNEL_VERSION in kernel/*/build-args")
    p.add_argument("--dry-run", action="store_true", help="Report changes without writing files")
    p.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    p.add_argument(
        "--changes-to-file",
        type=Path,
        metavar="PATH",
        help="Write a JSON list of changes here (skipped in --dry-run)",
    )
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--verbose", "-v", action="store_const", const="DEBUG", dest="log_level")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s: %(message)s")

    files = find_build_args_files(args.repo_root)
    if not files:
        logger.info("No build-args files found.")
        return 0

    try:
        versions = fetch_kernel_versions()
    except Exception as e:
        logger.error("Failed to fetch releases.json: %s", e)
        return 1

    changes: List[Change] = []
    for f in files:
        ch = check_and_bump(f, versions, args.dry_run)
        if ch:
            changes.append(ch)

    if not changes:
        logger.info("No changes necessary.")
        return 0

    logger.info("%d file(s) %s.", len(changes), "would be updated" if args.dry_run else "updated")

    # Write the change list only when actually modifying files — the GHA
    # workflow checks for the existence of this file to decide whether to
    # open a PR, so it must not be created on a no-op run.
    if args.changes_to_file and not args.dry_run:
        args.changes_to_file.write_text(json.dumps([c.to_dict() for c in changes], indent=2))
        logger.info("Wrote change list to %s", args.changes_to_file)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
