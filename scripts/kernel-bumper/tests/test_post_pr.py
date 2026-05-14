"""Black-box contract tests for post_pr.py.

Scripts are invoked as subprocesses; no internal code is imported.
Each test describes its preconditions and expected outcome in its docstring.

All git operations target a local bare repository created by the ``git_repo``
fixture so no real GitHub access or push credentials are required.  The
``post_pr_runner`` fixture wraps a fake ``gh`` CLI that records every
invocation.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import commit_all


def _tracked_then_modified(repo: Path, rel_path: str, v1: str, v2: str) -> None:
    """Commit a file with content *v1*, then overwrite it with *v2*.

    After this call ``git status`` reports the file as modified ('M') which
    is the normal state post_pr.py is designed to operate on.
    """
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(v1)
    commit_all(repo, f"add {rel_path}")
    p.write_text(v2)  # unstaged modification → shows as ' M' in porcelain output


# ---------------------------------------------------------------------------
# No-op path
# ---------------------------------------------------------------------------


def test_exits_zero_with_no_modified_files(post_pr_runner, git_repo):
    """Precondition:  the working tree has no uncommitted modifications.
    Expected result: the script exits 0 and neither git nor gh are driven to
    create or update a PR.
    """
    result = post_pr_runner["run"](git_repo)

    assert result.returncode == 0
    assert not any(c[:2] == ["pr", "create"] for c in post_pr_runner["calls"]())
    assert not any(c[:2] == ["pr", "edit"] for c in post_pr_runner["calls"]())


# ---------------------------------------------------------------------------
# PR creation
# ---------------------------------------------------------------------------


def test_creates_pr_when_none_exists(post_pr_runner, git_repo):
    """Precondition:  a tracked file has been modified; no PR is open for the
    kernel-bump branch.
    Expected result: ``gh pr create`` is called exactly once.
    """
    _tracked_then_modified(
        git_repo,
        "kernel/6.6.x/build-args",
        "KERNEL_VERSION=6.6.80\n",
        "KERNEL_VERSION=6.6.85\n",
    )

    result = post_pr_runner["run"](git_repo)

    assert result.returncode == 0
    assert any(c[:2] == ["pr", "create"] for c in post_pr_runner["calls"]())


def test_pr_title_is_correct(post_pr_runner, git_repo):
    """Precondition:  a modified file exists and no open PR is present.
    Expected result: the PR is opened with the title
    'kernel: bump kernel versions'.
    """
    _tracked_then_modified(
        git_repo,
        "kernel/6.6.x/build-args",
        "KERNEL_VERSION=6.6.80\n",
        "KERNEL_VERSION=6.6.85\n",
    )

    post_pr_runner["run"](git_repo)

    create_calls = [c for c in post_pr_runner["calls"]() if c[:2] == ["pr", "create"]]
    assert any("kernel: bump kernel versions" in arg for c in create_calls for arg in c)


def test_pr_body_lists_modified_files(post_pr_runner, git_repo):
    """Precondition:  a modified file exists.
    Expected result: the PR body contains the relative path of the modified
    file so reviewers know which files will be changed.
    """
    _tracked_then_modified(
        git_repo,
        "kernel/6.6.x/build-args",
        "KERNEL_VERSION=6.6.80\n",
        "KERNEL_VERSION=6.6.85\n",
    )

    post_pr_runner["run"](git_repo)

    body = post_pr_runner["last_pr_body"]()
    assert body is not None
    assert "build-args" in body


def test_pr_body_contains_version_details_from_changes_file(post_pr_runner, git_repo, tmp_path):
    """Precondition:  a modified file exists and a --changes-data-file JSON is
    provided (as produced by bump_kernel.py).
    Expected result: the PR body includes the old and new version strings so
    reviewers see what changed.
    """
    _tracked_then_modified(
        git_repo,
        "kernel/6.6.x/build-args",
        "KERNEL_VERSION=6.6.80\n",
        "KERNEL_VERSION=6.6.85\n",
    )
    changes_file = tmp_path / "changes.json"
    changes_file.write_text(json.dumps([{"path": "kernel/6.6.x/build-args", "old": "6.6.80", "new": "6.6.85"}]))

    post_pr_runner["run"](git_repo, ["--changes-data-file", str(changes_file)])

    body = post_pr_runner["last_pr_body"]()
    assert "6.6.80" in body
    assert "6.6.85" in body


# ---------------------------------------------------------------------------
# Existing PR — update rather than re-create
# ---------------------------------------------------------------------------


def test_does_not_open_second_pr_when_one_already_exists(post_pr_runner, git_repo):
    """Precondition:  a modified file exists and an open PR is already present
    for the kernel-bump branch.
    Expected result: ``gh pr create`` is NOT called (no duplicate PRs).
    """
    post_pr_runner["set_existing_pr"](1)
    _tracked_then_modified(
        git_repo,
        "kernel/6.6.x/build-args",
        "KERNEL_VERSION=6.6.80\n",
        "KERNEL_VERSION=6.6.85\n",
    )

    result = post_pr_runner["run"](git_repo)

    assert result.returncode == 0
    assert not any(c[:2] == ["pr", "create"] for c in post_pr_runner["calls"]())


def test_updates_existing_pr_body(post_pr_runner, git_repo):
    """Precondition:  an open PR already exists for the kernel-bump branch.
    Expected result: ``gh pr edit`` is called to refresh the PR body, and the
    body content is non-empty.
    """
    post_pr_runner["set_existing_pr"](1)
    _tracked_then_modified(
        git_repo,
        "kernel/6.6.x/build-args",
        "KERNEL_VERSION=6.6.80\n",
        "KERNEL_VERSION=6.6.85\n",
    )

    post_pr_runner["run"](git_repo)

    assert any(c[:2] == ["pr", "edit"] for c in post_pr_runner["calls"]())
    assert post_pr_runner["last_pr_body"]() is not None
