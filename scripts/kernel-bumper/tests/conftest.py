"""Shared pytest fixtures for bump_kernel and post_pr black-box tests.

Scripts are never imported; they are always executed as subprocesses so the
test environment accurately reflects real-world invocation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent
BUMP_SCRIPT = SCRIPTS_DIR / "bump_kernel.py"
POST_PR_SCRIPT = SCRIPTS_DIR / "post_pr.py"
TEMPLATE_FILE = SCRIPTS_DIR / "PR_TEMPLATE.md"


# ---------------------------------------------------------------------------
# Module-level helpers (plain functions, not fixtures)
# ---------------------------------------------------------------------------


def make_build_args(repo: Path, series: str, version: str) -> Path:
    """Create kernel/<series>/build-args with KERNEL_VERSION=<version>."""
    d = repo / "kernel" / series
    d.mkdir(parents=True, exist_ok=True)
    p = d / "build-args"
    p.write_text(f"KERNEL_VERSION={version}\n")
    return p


def git_run(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    """Run a git command in *repo*, raising CalledProcessError on failure."""
    return subprocess.run(["git"] + args, cwd=repo, check=True, capture_output=True, text=True)


def commit_all(repo: Path, message: str = "add files") -> None:
    """Stage everything under *repo* and create a commit."""
    git_run(["add", "-A"], repo)
    git_run(["commit", "-m", message], repo)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kernel_server():
    """Local HTTP server standing in for https://www.kernel.org/releases.json.

    Call ``set_versions(["6.6.85", ...])`` before each test to configure the
    set of releases that bump_kernel.py will see.
    """
    state: dict = {"releases": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # suppress per-request noise in test output
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def set_versions(versions: list[str]) -> None:
        state["releases"] = [{"version": v, "moniker": "stable"} for v in versions]

    yield {"url": f"http://127.0.0.1:{port}/releases.json", "set_versions": set_versions}
    server.shutdown()


@pytest.fixture
def git_repo(tmp_path):
    """Temporary git working-tree with a local bare remote ready for push.

    The repo starts with a single "initial" commit on *main* that is already
    pushed to the bare remote, so subsequent ``git push`` calls succeed
    without credential prompts.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    git_run(["init", "-b", "main"], repo)
    git_run(["config", "user.email", "ci@test.local"], repo)
    git_run(["config", "user.name", "CI Bot"], repo)
    git_run(["remote", "add", "origin", str(remote)], repo)

    # Seed with an initial commit so push to a new branch succeeds
    (repo / ".gitkeep").write_text("")
    commit_all(repo, "initial")
    git_run(["push", "-u", "origin", "main"], repo)
    return repo


@pytest.fixture
def fake_gh(tmp_path):
    """Fake ``gh`` binary that records every invocation without touching GitHub.

    Behaviour:
    - ``gh repo view``  → prints "main"
    - ``gh pr list``    → returns [] unless ``set_existing_pr()`` was called
    - ``gh pr create``  → records the PR as open; saves ``--body-file`` content
    - ``gh pr edit``    → saves ``--body-file`` content

    Introspection helpers returned in the dict:
    - ``calls()``              – list of argument lists recorded so far
    - ``last_pr_body()``       – body text from the most recent create/edit call
    - ``set_existing_pr(n=1)`` – make ``gh pr list`` report an open PR
    - ``bin_dir``              – directory to prepend to PATH
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_file = tmp_path / "calls.ndjson"
    body_file = tmp_path / "last_pr_body.txt"
    pr_state_file = tmp_path / "pr_state.json"
    pr_state_file.write_text("null")  # json null → no existing PR

    # Build the fake gh as an inline Python script so it is portable and
    # requires no shell features beyond a working python3 on PATH.
    script_lines = [
        "#!/usr/bin/env python3",
        "import sys, json",
        "from pathlib import Path",
        f"calls_f   = Path({str(calls_file)!r})",
        f"body_f    = Path({str(body_file)!r})",
        f"pr_st_f   = Path({str(pr_state_file)!r})",
        "args = sys.argv[1:]",
        # Record invocation
        "with open(calls_f, 'a') as fp:",
        "    fp.write(json.dumps(args) + '\\n')",
        # Capture --body-file for pr create / pr edit
        "if args[:2] in (['pr', 'create'], ['pr', 'edit']) and '--body-file' in args:",
        "    idx = args.index('--body-file')",
        "    body_f.write_text(Path(args[idx + 1]).read_text())",
        # Route commands
        "if args[:2] == ['repo', 'view']:",
        "    print('main')",
        "elif args[:2] == ['pr', 'list']:",
        "    pr = json.loads(pr_st_f.read_text())",
        "    print(json.dumps([pr] if pr else []))",
        "elif args[:2] == ['pr', 'create']:",
        "    pr_st_f.write_text(json.dumps({'number': 1, 'url': 'https://github.com/x/y/pull/1'}))",
        "    print('https://github.com/x/y/pull/1')",
        "elif args[:2] == ['pr', 'edit']:",
        "    pass",
        "sys.exit(0)",
    ]
    script = bin_dir / "gh"
    script.write_text("\n".join(script_lines) + "\n")
    script.chmod(0o755)

    def calls() -> list[list[str]]:
        if not calls_file.exists():
            return []
        return [json.loads(ln) for ln in calls_file.read_text().splitlines() if ln.strip()]

    def last_pr_body() -> str | None:
        return body_file.read_text() if body_file.exists() else None

    def set_existing_pr(number: int = 1) -> None:
        pr_state_file.write_text(json.dumps({"number": number, "url": f"https://github.com/x/y/pull/{number}"}))

    return {
        "bin_dir": str(bin_dir),
        "calls": calls,
        "last_pr_body": last_pr_body,
        "set_existing_pr": set_existing_pr,
    }


@pytest.fixture
def run_bump(kernel_server):
    """Return a callable that executes bump_kernel.py as a subprocess.

    ``KERNEL_JSON_URL`` is pointed at the local ``kernel_server`` fixture so
    no real network traffic occurs.

    Signature: ``run_bump(repo, extra_args=[]) -> CompletedProcess``
    """

    def _run(repo: Path, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BUMP_SCRIPT), "--repo-root", str(repo)] + (extra_args or []),
            capture_output=True,
            text=True,
            env={**os.environ, "KERNEL_JSON_URL": kernel_server["url"]},
        )

    return _run


@pytest.fixture
def post_pr_runner(fake_gh):
    """Run post_pr.py and inspect gh CLI interactions via a single fixture.

    Returned dict keys:
    - ``run(repo, extra_args=[]) -> CompletedProcess``
    - ``calls()``              – gh invocations recorded so far
    - ``last_pr_body()``       – body passed via --body-file in most recent call
    - ``set_existing_pr(n=1)`` – configure fake gh to report an open PR
    """

    def _run(repo: Path, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": fake_gh["bin_dir"] + ":" + os.environ.get("PATH", ""),
            "KERNEL_BUMP_BRANCH": "kernel-bump",
        }
        return subprocess.run(
            [sys.executable, str(POST_PR_SCRIPT), "--template-file", str(TEMPLATE_FILE)] + (extra_args or []),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(repo),
        )

    return {
        "run": _run,
        "calls": fake_gh["calls"],
        "last_pr_body": fake_gh["last_pr_body"],
        "set_existing_pr": fake_gh["set_existing_pr"],
    }
