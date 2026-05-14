#!/usr/bin/env python3
"""Create or update a single PR for kernel build-args version bumps.

Behaviour:
  - Detects local git changes to files - is is assumed this script runs after a make -C kernel update-kernel-yamls.
  - Creates or switches to the `kernel-bump` branch, commits, and force-pushes.
  - Opens a PR if none exists for the branch; otherwise the push updates the
    existing one and the PR body is always refreshed to reflect the latest set
    of changes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

BRANCH_NAME = os.environ.get("KERNEL_BUMP_BRANCH", "kernel-bump")
TEMPLATE_PATH = Path(__file__).parent / "PR_TEMPLATE.md"


# ---------------------------------------------------------------------------
# PR body rendering
# ---------------------------------------------------------------------------


class PullRequestTemplate:
    """Loads a Markdown template and fills in the {{HEADLINES}} and
    {{CHANGED_FILES}} placeholders."""

    def __init__(self, path: Path = TEMPLATE_PATH) -> None:
        self.path = path

    def render(self, changes_data: Optional[list], changed_files: List[str]) -> str:
        """Return the template with placeholders replaced by change details."""
        template = self.path.read_text()

        # Build the {{HEADLINES}} block — a summary of each version bump.
        headline_lines: List[str] = []
        if changes_data:
            headline_lines.append("Updated kernel versions in build-args files:")
            for change in changes_data:
                path, old, new = change.get("path"), change.get("old"), change.get("new")
                if path and old and new:
                    headline_lines.append(f" - Updated {path}: {old} → {new}")

        # Build the {{CHANGED_FILES}} block — a plain list of modified paths.
        file_lines = ["The following files were modified:"] + [f"Modified {f}" for f in changed_files]

        return template.replace("{{HEADLINES}}", "\n".join(headline_lines)).replace("{{CHANGED_FILES}}", "\n".join(file_lines))


# ---------------------------------------------------------------------------
# Shell / git helpers
# ---------------------------------------------------------------------------


def run(cmd: List[str], capture: bool = True, check: bool = True, cwd: Optional[str] = None) -> str:
    """Run a subprocess command and return its stdout as a string.

    Raises subprocess.CalledProcessError on non-zero exit when check=True.
    Pass capture=False to let the command write directly to the terminal
    (useful for commands like git commit whose output is meant to be visible).
    """
    logger.debug("run: %s (cwd=%s)", shlex.join(cmd), cwd)
    result = subprocess.run(cmd, capture_output=capture, text=True, cwd=cwd)
    if check and result.returncode != 0:
        logger.error("Command failed (%d): %s", result.returncode, result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result.stdout.strip() if capture else ""


def get_repo_root() -> str:
    """Return the absolute path to the git repository root."""
    return run(["git", "rev-parse", "--show-toplevel"])


def get_modified_files() -> List[str]:
    """Return paths of all files with uncommitted modifications.

    Parses `git status --porcelain` output; each line is "<XY> <path>" where
    XY encodes the index/worktree status.  We collect paths whose worktree
    status includes 'M' (modified).
    """
    changed = []
    for line in run(["git", "status", "--porcelain"]).splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and "M" in parts[0]:
            changed.append(parts[1])
    return changed


def get_default_branch() -> str:
    """Return the repository's default branch name via the gh CLI."""
    return run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
        cwd=get_repo_root(),
    )


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------


def git_commit_and_push(paths: List[str]) -> None:
    """Stage `paths`, commit to `BRANCH_NAME`, and force-push to origin.

    The commit author is set to the GitHub Actions actor (or a generic bot
    identity) so the resulting commit is attributed correctly in the UI.
    Force-push ensures repeated runs update the same branch without conflicts.
    """
    actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    repo_root = get_repo_root()
    run(["git", "config", "user.name", actor], cwd=repo_root)
    run(["git", "config", "user.email", f"{actor}@users.noreply.github.com"], cwd=repo_root)
    run(["git", "checkout", "-B", BRANCH_NAME], cwd=repo_root)
    run(["git", "add", *paths], cwd=repo_root)
    run(["git", "commit", "-m", "kernel-bump: bump kernel versions"], capture=False, cwd=repo_root)
    run(["git", "push", "--force", "--set-upstream", "origin", BRANCH_NAME], cwd=repo_root)


# ---------------------------------------------------------------------------
# GitHub PR management (via gh CLI)
# ---------------------------------------------------------------------------


def find_existing_pr() -> Optional[dict]:
    """Return the open PR dict for `BRANCH_NAME`, or None if no PR exists."""
    try:
        out = run(
            ["gh", "pr", "list", "--head", BRANCH_NAME, "--state", "open", "--json", "number,url"],
            cwd=get_repo_root(),
        )
        data = json.loads(out)
        return data[0] if data else None
    except Exception as e:
        logger.error("gh pr list failed: %s", e)
        return None


def _write_body_file(body: str) -> str:
    """Write `body` to a named temp file and return its path.

    gh does not accept PR body via stdin, so we use --body-file to avoid
    any shell-quoting issues with the body content.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tf:
        tf.write(body)
        tf.flush()
        return tf.name


def create_pr(title: str, body: str, base: str) -> None:
    """Open a new pull request using the gh CLI."""
    run(
        ["gh", "pr", "create", "--title", title, "--body-file", _write_body_file(body), "--head", BRANCH_NAME, "--base", base],
        cwd=get_repo_root(),
    )


def update_pr_body(pr_number: int, body: str) -> None:
    """Overwrite the PR body for `pr_number` using the gh CLI."""
    run(["gh", "pr", "edit", str(pr_number), "--body-file", _write_body_file(body)], cwd=get_repo_root())


def ensure_pr(title: str, body: str, default_branch: str) -> int:
    """Create a PR if none exists for `BRANCH_NAME`, then return its number."""
    existing = find_existing_pr()
    if existing:
        logger.info("PR already open: %s (will be updated by push)", existing.get("url"))
        return existing["number"]

    create_pr(title, body, default_branch)

    # Re-query to confirm creation and retrieve the PR number.
    created = find_existing_pr()
    if not created:
        raise RuntimeError("Could not find PR after creation — gh may have reported an error above.")
    return created["number"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create or update a PR for kernel build-args bumps")
    p.add_argument("--changes-data-file", type=Path, help="JSON file produced by bump_kernel.py")
    p.add_argument("--template-file", type=Path, default=TEMPLATE_PATH)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--verbose", "-v", action="store_const", const="DEBUG", dest="log_level")
    p.add_argument("--quiet", "-q", action="store_const", const="ERROR", dest="log_level")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s: %(message)s")

    changed_files = get_modified_files()
    if not changed_files:
        logger.info("No modified files detected — nothing to do.")
        return 0

    # Load the structured change data produced by bump_kernel.py.  This is
    # optional — it's used only to produce a richer PR body with per-file
    # version details.
    build_args_changes = []
    if args.changes_data_file:
        try:
            build_args_changes = json.loads(args.changes_data_file.read_text())
        except Exception as e:
            logger.error("Failed to read --changes-data-file: %s", e)
            return 1

    pr_body = PullRequestTemplate(path=args.template_file).render(build_args_changes, changed_files)

    try:
        git_commit_and_push(changed_files)
    except subprocess.CalledProcessError as e:
        logger.error("Git operation failed: %s", e)
        return 3

    default_branch = get_default_branch()
    pr_number = ensure_pr("kernel: bump kernel versions", pr_body, default_branch)

    # Always refresh the PR body so it reflects the latest change set, even
    # if the PR was already open from a previous run.
    update_pr_body(pr_number, pr_body)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
