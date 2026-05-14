"""Black-box contract tests for bump_kernel.py.

Scripts are invoked as subprocesses; no internal code is imported.
Each test describes its preconditions and expected outcome in its docstring.
"""

from __future__ import annotations

import json

from conftest import make_build_args

# ---------------------------------------------------------------------------
# Already up-to-date
# ---------------------------------------------------------------------------


def test_no_file_change_when_already_latest(run_bump, kernel_server, tmp_path):
    """Precondition:  build-args is at the same version as the latest remote patch.
    Expected result: the file is not modified and the script exits 0.
    """
    kernel_server["set_versions"](["6.6.85"])
    make_build_args(tmp_path, "6.6.x", "6.6.85")

    result = run_bump(tmp_path)

    assert result.returncode == 0
    assert "KERNEL_VERSION=6.6.85" in (tmp_path / "kernel" / "6.6.x" / "build-args").read_text()


def test_changes_file_not_created_when_up_to_date(run_bump, kernel_server, tmp_path):
    """Precondition:  build-args is already at the latest patch version.
    Expected result: --changes-to-file is NOT written (no-op run must not
    trigger a downstream PR).
    """
    kernel_server["set_versions"](["6.6.85"])
    make_build_args(tmp_path, "6.6.x", "6.6.85")
    changes_file = tmp_path / "changes.json"

    run_bump(tmp_path, ["--changes-to-file", str(changes_file)])

    assert not changes_file.exists()


# ---------------------------------------------------------------------------
# Outdated — bump required
# ---------------------------------------------------------------------------


def test_outdated_file_is_bumped_to_latest_patch(run_bump, kernel_server, tmp_path):
    """Precondition:  build-args is at an older patch for a tracked series.
    Expected result: the file is rewritten with the latest patch version.
    """
    kernel_server["set_versions"](["6.6.85", "6.6.80"])
    make_build_args(tmp_path, "6.6.x", "6.6.80")

    result = run_bump(tmp_path)

    assert result.returncode == 0
    assert "KERNEL_VERSION=6.6.85" in (tmp_path / "kernel" / "6.6.x" / "build-args").read_text()


def test_changes_file_written_with_correct_shape(run_bump, kernel_server, tmp_path):
    """Precondition:  an outdated build-args file exists and --changes-to-file
    is requested.
    Expected result: the file is a JSON list whose entries have 'path', 'old',
    and 'new' keys with the correct values.
    """
    kernel_server["set_versions"](["6.6.85"])
    make_build_args(tmp_path, "6.6.x", "6.6.80")
    changes_file = tmp_path / "changes.json"

    run_bump(tmp_path, ["--changes-to-file", str(changes_file)])

    data = json.loads(changes_file.read_text())
    assert len(data) == 1
    assert data[0]["old"] == "6.6.80"
    assert data[0]["new"] == "6.6.85"
    assert "6.6.x" in data[0]["path"]


def test_multiple_series_bumped_independently(run_bump, kernel_server, tmp_path):
    """Precondition:  two different kernel series are tracked, both outdated.
    Expected result: each is bumped to its own latest patch independently.
    """
    kernel_server["set_versions"](["6.6.85", "6.1.100"])
    make_build_args(tmp_path, "6.6.x", "6.6.80")
    make_build_args(tmp_path, "6.1.x", "6.1.90")

    result = run_bump(tmp_path)

    assert result.returncode == 0
    assert "KERNEL_VERSION=6.6.85" in (tmp_path / "kernel" / "6.6.x" / "build-args").read_text()
    assert "KERNEL_VERSION=6.1.100" in (tmp_path / "kernel" / "6.1.x" / "build-args").read_text()


def test_only_outdated_series_is_bumped(run_bump, kernel_server, tmp_path):
    """Precondition:  one series is outdated, another is already at latest.
    Expected result: only the outdated file is modified; the current one is
    left unchanged.
    """
    kernel_server["set_versions"](["6.6.85", "6.1.100"])
    make_build_args(tmp_path, "6.6.x", "6.6.80")
    make_build_args(tmp_path, "6.1.x", "6.1.100")

    run_bump(tmp_path)

    assert "KERNEL_VERSION=6.6.85" in (tmp_path / "kernel" / "6.6.x" / "build-args").read_text()
    assert "KERNEL_VERSION=6.1.100" in (tmp_path / "kernel" / "6.1.x" / "build-args").read_text()


# ---------------------------------------------------------------------------
# Local version is newer than remote (edge case — no downgrade)
# ---------------------------------------------------------------------------


def test_no_downgrade_when_local_version_is_ahead(run_bump, kernel_server, tmp_path):
    """Precondition:  local build-args has a higher patch than anything on the
    remote (e.g. a pre-release was already tracked locally).
    Expected result: the file is left unchanged — no downgrade occurs.
    """
    kernel_server["set_versions"](["6.6.80"])
    make_build_args(tmp_path, "6.6.x", "6.6.99")

    result = run_bump(tmp_path)

    assert result.returncode == 0
    assert "KERNEL_VERSION=6.6.99" in (tmp_path / "kernel" / "6.6.x" / "build-args").read_text()


# ---------------------------------------------------------------------------
# Series not present on remote
# ---------------------------------------------------------------------------


def test_unknown_series_is_skipped_without_error(run_bump, kernel_server, tmp_path):
    """Precondition:  the remote only publishes 6.6.x but a 5.15.x build-args
    file also exists locally.
    Expected result: the 5.15.x file is left untouched and the script exits 0.
    """
    kernel_server["set_versions"](["6.6.85"])
    make_build_args(tmp_path, "5.15.x", "5.15.10")

    result = run_bump(tmp_path)

    assert result.returncode == 0
    assert "KERNEL_VERSION=5.15.10" in (tmp_path / "kernel" / "5.15.x" / "build-args").read_text()


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_files(run_bump, kernel_server, tmp_path):
    """Precondition:  an outdated build-args file exists; --dry-run is passed.
    Expected result: no file is modified on disk even though a bump would be
    needed.
    """
    kernel_server["set_versions"](["6.6.85"])
    make_build_args(tmp_path, "6.6.x", "6.6.80")

    result = run_bump(tmp_path, ["--dry-run"])

    assert result.returncode == 0
    assert "KERNEL_VERSION=6.6.80" in (tmp_path / "kernel" / "6.6.x" / "build-args").read_text()


def test_changes_file_not_written_on_dry_run(run_bump, kernel_server, tmp_path):
    """Precondition:  an outdated file exists; both --dry-run and
    --changes-to-file are requested.
    Expected result: the changes file is NOT written because --dry-run
    suppresses all file I/O (the downstream PR workflow must not fire).
    """
    kernel_server["set_versions"](["6.6.85"])
    make_build_args(tmp_path, "6.6.x", "6.6.80")
    changes_file = tmp_path / "changes.json"

    run_bump(tmp_path, ["--dry-run", "--changes-to-file", str(changes_file)])

    assert not changes_file.exists()
