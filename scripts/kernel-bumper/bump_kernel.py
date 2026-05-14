#!/usr/bin/env python3
"""bump_kernel.py

Scan kernel/*/build-args for KERNEL_VERSION entries, find latest
patch versions from kernel.org and update the files.

Usage:
  python3 scripts/bump_kernel.py [--dry-run]

The script is idempotent and will no-op if no changes are necessary.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Final, List, Optional

import requests
from semver import Version

KERNEL_JSON_URL = "https://www.kernel.org/releases.json"

logger = logging.getLogger(__name__)

DEFAULT_REPO_ROOT: Final = Path(__file__).parent.parent.parent


@dataclass
class Change:
    path: Path
    old: Version
    new: Version

    def to_dict(self) -> Dict[str, str]:
        return {"path": str(self.path), "old": str(self.old), "new": str(self.new)}


@dataclass
class BuildArgsFile:
    path: Path
    prefix: str = "KERNEL_VERSION"
    version: Optional[Version] = None
    kernel_version_line_re: Final = re.compile(rf"^{prefix}\s*=\s*([\d.]+)\s*$", re.MULTILINE)

    def get_current_version(self) -> Optional[Version]:
        # just to stop us reading the file every damn time
        if not self.version:
            for ln in self.path.read_text().splitlines():
                kv = self.parse_line(ln)
                if kv:
                    self.version = kv
                    break
        return self.version

    def parse_line(self, s: str) -> Optional[Version]:
        m = self.kernel_version_line_re.match(s)
        if not m:
            return None
        version_str = m.group(1)
        version = Version.parse(version_str)
        if not version:
            return None
        return version

    def replace_kernel_version(self, new_version: Version) -> Optional[Change]:
        text = self.path.read_text()
        new_text = re.sub(self.kernel_version_line_re, f"{self.prefix}={new_version}", text)
        if new_text != text:
            self.path.write_text(new_text)
            return Change(path=self.path, old=self.version, new=new_version)
        else:
            return None


def fetch_releases_json(timeout: int = 10) -> Dict:
    """Fetch the kernel.org releases JSON.

    Raises requests.HTTPError on non-2xx responses.
    """
    resp = requests.get(KERNEL_JSON_URL, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def parse_versions_from_releases(data: Dict) -> List[Version]:
    """Extract a list of version strings from the releases JSON.

    Returns a list like ["6.1.12", "6.1.13", ...].
    """
    releases = data.get("releases") or []
    versions = []
    for r in releases:
        vstr = r.get("version", "")
        if not Version.is_valid(vstr):
            # kernel release candidates follow the form "6.2-rc1" which semver doesn't like, so just skip invalid versions
            logger.debug(f"Skipping invalid version in releases.json: {vstr}")
            continue
        versions.append(Version.parse(vstr))
    return versions


def latest_patch_for_series(versions: List[Version], major: int, minor: int) -> Optional[Version]:
    """Return the latest patch-level version for a given major.minor series.

    Example: given versions containing "6.1.12" and "6.1.13",
    calling with major=6, minor=1 returns "6.1.13".
    """
    candidates = [v for v in versions if v.major == major and v.minor == minor]
    return max(candidates) if candidates else None


def find_build_args_files(root: Path = DEFAULT_REPO_ROOT) -> List[BuildArgsFile]:
    """Returns a list of kernel/*/build-args files in the workspace."""
    return [BuildArgsFile(path=p) for p in root.glob("kernel/*/build-args")]


def process_build_args_file(build_args: BuildArgsFile, versions: List[str], dry_run: bool) -> Optional[Change]:
    """Check a single `build-args` file and update it if a newer patch exists.

    Returns a `Change` if the file was modified, otherwise None.
    """
    current_version = build_args.get_current_version()
    if not current_version:
        # No KERNEL_VERSION line found
        return None

    latest = latest_patch_for_series(versions, current_version.major, current_version.minor)
    if not latest:
        logger.info(f"No matching release found for series {current_version.major}.{current_version.minor}.x")
        return None

    if latest == current_version:
        logger.info(f"{build_args.path} is up-to-date ({current_version})")
        return None

    logger.info(f"Updating {build_args.path}: {current_version} -> {latest}")
    if dry_run:
        logger.debug(f"Dry run: would update {build_args.path} from {current_version} to {latest}")
        return Change(path=build_args.path, old=current_version, new=latest)

    return build_args.replace_kernel_version(latest)


def main(argv: Optional[List[str]] = None) -> int:

    p = argparse.ArgumentParser(description="Bump kernel KERNEL_VERSION in kernel/*/build-args")
    p.add_argument("--dry-run", action="store_true", help="Show what would be changed but do not modify files")
    p.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT, help="Path to the root of the repository")
    p.add_argument("--log-level", default="INFO", help="Logging level (e.g. DEBUG, INFO, WARNING)")
    p.add_argument("--verbose", "-v", action="store_const", const="DEBUG", dest="log_level", help="Set log level to DEBUG")
    p.add_argument("--changes-to-file", type=Path, help="Dump a JSON list of changes to a file")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper())

    files = find_build_args_files(args.repo_root)
    if not files:
        logger.info("No build-args files found.")
        return 0

    try:
        releases = fetch_releases_json()
    except Exception as e:
        logger.error(f"Failed to fetch releases.json: {e}")
        return 1

    versions = parse_versions_from_releases(releases)
    changes: List[Change] = []

    # Process each build-args file and collect changes
    for f in files:
        ch = process_build_args_file(f, versions, args.dry_run)
        if ch and (not args.dry_run):
            changes.append(ch)

    if not changes:
        logger.info("No changes necessary.")
    else:
        logger.info(f"Changed {len(changes)} build-args files.")
        if args.changes_to_file:
            fpath = Path(args.changes_to_file)
            logger.info(f"Writing changes to {fpath}")
            content = json.dumps([c.to_dict() for c in changes], indent=2)
            fpath.write_text(content)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
