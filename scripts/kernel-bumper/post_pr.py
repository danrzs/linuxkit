#!/usr/bin/env python3
"""post_pr.py

Create or update a single PR for kernel build-args changes.

Behavior:
- Detect any local git changes to `kernel/*/build-args`.
- Create or switch to branch `kernel-bump`, commit changes, and push.
- If a PR from that branch exists, pushing updates it; otherwise create a PR.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)
BRANCH_NAME = "kernel-bump"
MATCH_RE = re.compile(r"^kernel/[^/]+/build-args$")
KV_RE = re.compile(r"KERNEL_VERSION\s*=\s*([\d.]+)")
TEMPLATE_PATH = Path(__file__).parent / "PR_TEMPLATE.md"


class PullRequestTemplate:
    def __init__(self, path: Optional[Path] = TEMPLATE_PATH):
        self.path = path
        self._content = None

    @property
    def content(self) -> str:
        if not self._content:
            self._content = self.load_template()

        return self._content

    def load_template(self) -> str:
        if self.path and self.path.exists():
            return self.path.read_text()
        raise FileNotFoundError(f"PR template not found at {self.path}")

    def populate_headlines(self, changes_data: Optional[dict] = None) -> List[str]:
        headlines = []
        if changes_data:
            headlines.append("Updated kernel versions in build-args files:")
            for change in changes_data:
                try:
                    path = change.get("path")
                    old = change.get("old")
                    new = change.get("new")
                    if path and old and new:
                        headlines.append(f" - Updated {path}: {old} → {new}")
                except Exception as e:
                    logger.error("Error processing change data: %s", e)
                    continue

        return headlines

    def populate_changed_files(self, change_files: List[str]) -> List[str]:
        changed_files = []
        changed_files.append("The following files were modified:")

        for cf in change_files:
            changed_files.append(f"Modified {cf}")
        return changed_files

    def render(self, changed_data, changed_files) -> str:
        headlines_content = "\n".join(self.populate_headlines(changed_data))
        changed_files_content = "\n".join(self.populate_changed_files(changed_files))
        new_content = self.content.replace("{{HEADLINES}}", headlines_content)
        new_content = new_content.replace("{{CHANGED_FILES}}", changed_files_content)
        return new_content


def _configure_logging(level: str = "INFO") -> None:
    """Configure module-level logging from `LOG_LEVEL` env var or provided level."""
    logging.basicConfig(level=level.upper(), format="%(levelname)s: %(message)s")


def run(cmd: List[str], capture: bool = True, check: bool = True, cwd: Optional[str] = None) -> str:
    """Run a subprocess command and return stdout (text).

    Args:
        cmd: command list to run
        capture: capture stdout/stderr
        check: raise on non-zero exit
        cwd: working directory to run the command in (optional)
    """
    logger.debug("Running command: %s (cwd=%s)", shlex.join(cmd), cwd)
    completed = subprocess.run(cmd, capture_output=capture, text=True, cwd=cwd)
    if check and completed.returncode != 0:
        logger.error("Command failed (%s): %s", completed.returncode, completed.stderr)
        raise subprocess.CalledProcessError(completed.returncode, cmd, output=completed.stdout, stderr=completed.stderr)
    return completed.stdout.strip() if capture else ""


def get_repo_root() -> str:
    """Return the root directory of the git repository."""
    try:
        return run(["git", "rev-parse", "--show-toplevel"])
    except subprocess.CalledProcessError as e:
        raise RuntimeError("Failed to determine git repository root.") from e


def changed_build_args() -> List[str]:
    """Return list of changed paths from git status."""
    stdout = run(["git", "status", "--porcelain"])  # lines like ' M path'
    outs = []
    for line in stdout.splitlines():
        line = line.strip()
        componets = line.split(maxsplit=1)
        if line and len(componets) == 2 and "M" in componets[0]:
            outs.append(componets[1])
    return outs


def read_head_file(path: str) -> Optional[str]:
    """Return file contents from HEAD for `path`, or None if not present."""
    try:
        return run(["git", "show", f"HEAD:{path}"])
    except subprocess.CalledProcessError:
        logger.debug("No HEAD version for %s", path)
        return None


def read_worktree_file(path: str) -> Optional[str]:
    """Read a file from the working tree, returning contents or None."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        logger.debug("Worktree file not found: %s", path)
        return None


def parse_kernel_version(contents: Optional[str]) -> Optional[str]:
    """Parse and return the first KERNEL_VERSION value found in `contents`."""
    if not contents:
        return None
    for ln in contents.splitlines():
        m = KV_RE.search(ln)
        if m:
            return m.group(1)
    return None


def git_commit_and_push(paths: List[str]) -> None:
    """Commit provided `paths` to `BRANCH_NAME` and force-push to origin."""
    actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    repo_root = run(["git", "rev-parse", "--show-toplevel"]) or os.getcwd()
    run(["git", "config", "user.name", actor], cwd=repo_root)
    run(["git", "config", "user.email", f"{actor}@users.noreply.github.com"], cwd=repo_root)  # type: ignore[arg-type]
    run(["git", "checkout", "-B", BRANCH_NAME], cwd=repo_root)
    run(["git", "add", *paths], cwd=repo_root)
    run(["git", "commit", "-m", "kernel-bump: bump kernel versions"], capture=False, cwd=repo_root)
    run(["git", "push", "--force", "--set-upstream", "origin", BRANCH_NAME], cwd=repo_root)


def find_existing_pr() -> Optional[dict]:
    """Return the open PR dict for `BRANCH_NAME` if one exists, else None.

    Uses the `gh` CLI; assumes it is already authenticated.
    """
    repo_root = get_repo_root()

    try:
        out = run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                BRANCH_NAME,
                "--state",
                "open",
                "--json",
                "number,url",
            ],
            cwd=repo_root,
        )
        data = json.loads(out)
        logger.debug("find_existing_pr (gh) response count: %d", len(data))
        return data[0] if data else None
    except Exception as e:
        logger.error("gh pr list failed: %s", e)
        return None


def create_pr(title: str, body: str, base: str) -> dict:
    """Create a pull request using the `gh` CLI and return a dict with `number` and `url`.

    Assumes the `gh` CLI is already authenticated.
    """
    repo_root = get_repo_root()

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
        tf.write(body)
        tf.flush()  # Ensure content is written to disk before gh reads it
        logger.debug("Created temporary file for PR body: %s", tf.name)
        out = run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                title,
                "--body-file",
                tf.name,
                "--head",
                BRANCH_NAME,
                "--base",
                base,
                "--json",
                "number,url",
            ],
            cwd=repo_root,
        )
    data = json.loads(out)
    logger.info("Created PR via gh: %s", data.get("url"))
    return data


def get_default_branch() -> str:
    """Return the repository's default branch name (fallback to 'master') using `gh` CLI."""
    try:
        return run(["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"], cwd=get_repo_root())
    except Exception as e:
        raise RuntimeError("Failed to get default branch via gh.") from e


def update_pr_body_with_gh(pr_number: int, body: str) -> None:
    """Update PR body using the `gh` CLI (called in-repo)."""
    try:
        logger.debug("Updating PR body for #%d via gh", pr_number)
        # Use --body with stdin is not straightforward; use --body argument safely
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            tf.write(body)
            tf.flush()  # Ensure content is written to disk before gh reads it
            logger.debug("Created temporary file for PR body: %s", tf.name)
            run(["gh", "pr", "edit", str(pr_number), "--body-file", tf.name], cwd=get_repo_root())
        logger.info("Updated PR body for #%d", pr_number)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to update PR body via gh: %s", e)


def load_pr_template() -> str:
    """Load PR template from local `PR_TEMPLATE.md` or repo template, return empty string if none."""
    local_template = Path(__file__).parent / "PR_TEMPLATE.md"
    if local_template.exists():
        try:
            return local_template.read_text()
        except Exception as e:
            logger.debug("Failed to read local template: %s", e)
            return ""
    try:
        return Path(".github/PULL_REQUEST_TEMPLATE.md").read_text()
    except Exception:
        return ""


def build_pr_body(headlines: List[str], template: str) -> str:
    """Render the PR body by inserting `headlines` into `template` when applicable."""
    headlines_md = "\n".join([f"- {h}" for h in headlines])
    if "{{HEADLINES}}" in template:
        return template.replace("{{HEADLINES}}", headlines_md)
    return headlines_md + "\n\n" + template


def ensure_pr(title: str, body: str, default_branch: str) -> int:
    """Ensure a PR exists for the bump branch; create if missing. Return PR number."""
    existing = find_existing_pr()
    if existing:
        logger.info("PR already exists: %s (updated by push)", existing.get("url"))
        return existing["number"]
    pr = create_pr(title, body, default_branch)
    return pr["number"]


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the post-PR script."""
    parser = argparse.ArgumentParser(description="Create or update a PR for kernel build-args changes")
    parser.add_argument("--changes-data-file", type=Path, help="Path to JSON file containing list of changed files")
    parser.add_argument("--log-level", default="INFO", help="Logging level (e.g. DEBUG, INFO, WARNING)")
    parser.add_argument("--verbose", "-v", action="store_const", const="DEBUG", dest="log_level", help="Set log level to DEBUG")
    parser.add_argument("--template-file", default=TEMPLATE_PATH, type=Path, help="Path to PR template file (optional)")
    parser.add_argument("--quiet", "-q", action="store_const", const="ERROR", dest="log_level", help="Suppress non-error output")
    return parser.parse_args()


def get_repo() -> Optional[str]:
    try:
        return run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], capture=False, cwd=get_repo_root())
    except Exception as e:
        raise RuntimeError("Failed to get repository via gh.") from e


def main() -> int:
    args = parse_args()
    _configure_logging(args.log_level)

    repo_full = get_repo()
    logger.info("Using repository: %s", repo_full)

    changed_files = changed_build_args()
    if not changed_files:
        logger.info("No changes detected; nothing to do.")
        return 0

    build_args_changes = []
    if args.changes_data_file:
        logger.debug("Reading changed build-args from file: %s", args.changes_data_file)
        try:
            build_args_changes = json.loads(args.changes_data_file.read_text())
            logger.debug("Loaded changed build-args: %s", build_args_changes)
        except Exception as e:
            logger.error("Failed to read changes data file: %s", e)
            return 1

    pr_template = PullRequestTemplate(path=args.template_file)
    pr_body = pr_template.render(build_args_changes, changed_files)  # Preload template content

    try:
        git_commit_and_push(changed_files)
    except subprocess.CalledProcessError as e:
        logger.error("Git operation failed: %s", e)
        return 3

    default_branch = get_default_branch()
    title = "kernel: bump kernel versions"

    pr_number = ensure_pr(title, pr_body, default_branch)

    # always update the PR Body with the latest changes, even if the PR already existed
    update_pr_body_with_gh(pr_number, pr_body)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
