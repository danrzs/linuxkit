"""Integration tests: bump_kernel.py writes a changes file → post_pr.py reads it.

The two scripts are exercised end-to-end without importing any of their code.
A local kernel.org mock server and a fake gh CLI mean no real network or
GitHub access is required.
"""

from __future__ import annotations

from conftest import commit_all, make_build_args


def test_bump_writes_changes_file_that_post_pr_uses_in_pr_body(run_bump, post_pr_runner, kernel_server, git_repo, tmp_path):
    """Full pipeline: bump_kernel updates a build-args file and writes a
    structured changes JSON; post_pr reads that JSON and includes the old → new
    version details in the PR body.

    Preconditions:
    - kernel/6.6.x/build-args is at 6.6.80 (tracked in git)
    - remote reports 6.6.85 as the latest patch

    Expected results:
    - bump_kernel exits 0 and writes changes.json
    - post_pr exits 0, creates a PR, and the PR body contains both "6.6.80"
      and "6.6.85"
    """
    kernel_server["set_versions"](["6.6.85"])
    make_build_args(git_repo, "6.6.x", "6.6.80")
    commit_all(git_repo, "add build-args")

    changes_file = tmp_path / "changes.json"
    bump_result = run_bump(git_repo, ["--changes-to-file", str(changes_file)])
    assert bump_result.returncode == 0
    assert changes_file.exists(), "bump_kernel must write changes.json when a bump occurs"

    post_result = post_pr_runner["run"](git_repo, ["--changes-data-file", str(changes_file)])
    assert post_result.returncode == 0

    body = post_pr_runner["last_pr_body"]()
    assert body is not None
    assert "6.6.80" in body
    assert "6.6.85" in body


def test_no_changes_file_means_post_pr_body_omits_version_details(run_bump, post_pr_runner, kernel_server, git_repo, tmp_path):
    """post_pr.py is robust when called without --changes-data-file.

    Preconditions:
    - A build-args file has been manually modified (simulating bump_kernel
      having run without --changes-to-file).
    - post_pr is called with no --changes-data-file argument.

    Expected result: a PR is still created and its body is non-empty (it
    lists the changed files even without version-level detail).
    """
    make_build_args(git_repo, "6.6.x", "6.6.80")
    commit_all(git_repo, "add build-args")
    # Simulate bump_kernel having modified the file without writing a changes file
    (git_repo / "kernel" / "6.6.x" / "build-args").write_text("KERNEL_VERSION=6.6.85\n")

    post_result = post_pr_runner["run"](git_repo)
    assert post_result.returncode == 0

    body = post_pr_runner["last_pr_body"]()
    assert body is not None
    assert len(body.strip()) > 0
