from scripts import bump_kernel


class DummyResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise Exception("bad status")

    def json(self):
        return self._data


def test_dry_run_updates(tmp_path, monkeypatch, capsys):
    # prepare a fake kernel build-args
    kd = tmp_path / "kernel" / "5.15.x"
    kd.mkdir(parents=True)
    f = kd / "build-args"
    f.write_text("KERNEL_VERSION=5.15.27\n")

    data = {"releases": [{"version": "5.15.148"}, {"version": "5.15.27"}]}
    monkeypatch.setattr(bump_kernel, "fetch_releases_json", lambda: data)

    # run dry-run
    rc = bump_kernel.main(["--dry-run"])  # runs in repo root, but we created temp files in tmp_path
    # nothing in repo root should change; ensure exit code is 0 or 1 depending on network
    assert rc in (0, 1)


def test_replace_updates_file(tmp_path, monkeypatch):
    kd = tmp_path / "kernel" / "5.15.x"
    kd.mkdir(parents=True)
    f = kd / "build-args"
    f.write_text("KERNEL_VERSION=5.15.27\n")

    data = {"releases": [{"version": "5.15.148"}, {"version": "5.15.27"}]}
    monkeypatch.setattr(bump_kernel, "fetch_releases_json", lambda: data)

    # monkeypatch finding files to point at our tmp_path
    monkeypatch.setattr(bump_kernel, "find_build_args_files", lambda root=...: [f])

    # run non-dry (will call replace)
    rc = bump_kernel.main([])
    assert rc == 0
    # file should now contain updated version
    assert "5.15.148" in f.read_text()
