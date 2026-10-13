import subprocess

import pytest
import typer

from dicos import config, conn, credentials, init_net


def test_probes(monkeypatch):
    calls = []
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append("closed")
    monkeypatch.setattr(init_net.socket, "create_connection", lambda address, timeout: calls.append((address, timeout)) or Socket())
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0 if "good" in argv else 1)
    monkeypatch.setattr(init_net.subprocess, "run", run)
    assert init_net.probe_jumps([None, "good", "bad"]) == {None: True, "good": True, "bad": False}
    assert ((init_net.HOST, 22), 6) in calls and "closed" in calls
    argv, kwargs = next(c for c in calls if isinstance(c, tuple) and isinstance(c[0], list))
    assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    assert "/dev/tcp/slurm-ui.twgrid.org/22" in argv[-1]
    assert kwargs["timeout"] == 16


def test_failed_probes(monkeypatch):
    def fail(*a, **kw):
        raise OSError("unreachable")
    monkeypatch.setattr(init_net.socket, "create_connection", fail)
    monkeypatch.setattr(init_net.subprocess, "run", fail)
    assert init_net.probe_jumps([None, "bad"]) == {None: False, "bad": False}
    assert init_net.probe_jumps([]) == {}


def test_explicit_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(init_net, "probe_jumps", lambda candidates: calls.append(candidates) or dict.fromkeys(candidates, True))
    assert init_net.select_jump(jump="lab") == "lab"
    assert init_net.select_jump(no_jump=True) is None
    assert calls == [["lab"], [None]]
    monkeypatch.setattr(init_net, "probe_jumps", lambda c: dict.fromkeys(c, False))
    with pytest.raises(ValueError, match="cannot reach"):
        init_net.select_jump(jump="lab")
    with pytest.raises(ValueError, match="No candidate"):
        init_net.select_jump()


def test_default_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(init_net.Path, "home", lambda: tmp_path)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/config").write_text("Host first second * !excluded\n")
    monkeypatch.setattr(init_net, "probe_jumps", lambda c: {None: True, "first": False, "second": True})
    defaults = []
    monkeypatch.setattr(init_net.typer, "prompt", lambda *a, **kw: defaults.append(kw["default"]) or kw["default"])
    assert init_net.select_jump() == "second"
    assert defaults == ["second"]


def test_detect(monkeypatch):
    cfg = config.Config("alice")
    creds = credentials.Credentials("synthetic", "JBSWY3DPEHPK3PXP")
    calls = []
    monkeypatch.setattr(conn, "ensure_master", lambda *a: calls.append("master"))
    monkeypatch.setattr(conn, "run", lambda cfg, cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess([], 0, "/home/alice/dicos\n", ""))
    assert init_net.connect_and_detect(cfg, creds) == "/home/alice/dicos"
    assert calls == ["master", ("bash -s", {"input": init_net.DETECT_WORKDIR})]


def test_detect_login_failure(monkeypatch, capsys):
    def fail(*a):
        raise conn.LoginError("previous answer rejected")
    monkeypatch.setattr(conn, "ensure_master", fail)
    with pytest.raises(typer.Exit) as error:
        init_net.connect_and_detect(config.Config("alice"), credentials.Credentials("x", "x"))
    assert error.value.exit_code == 1
    assert "5 failed logins" in capsys.readouterr().err


@pytest.mark.parametrize("account,groups,expected_group", [("idlab", ["IDLab", "other"], "IDLab"),
    ("missing", ["IDLab"], None), ("", ["IDLab"], None)])
def test_detect_script(tmp_path, account, groups, expected_group):
    """Execute only a local bash script with a fake sacctmgr and temp storage."""
    import os
    work = tmp_path / "ceph" / "work"
    work.mkdir(parents=True)
    for group in groups:
        (work / group).mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sacctmgr = bindir / "sacctmgr"
    sacctmgr.write_text("#!/bin/sh\nprintf '%s\\n' \"$TEST_ACCOUNT\"\n")
    sacctmgr.chmod(0o755)
    env = dict(os.environ, HOME=str(home), USER="alice", TEST_ACCOUNT=account,
               PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    script = init_net.DETECT_WORKDIR.replace("/ceph/work", str(work))
    result = subprocess.run(["bash", "-s"], input=script, env=env, text=True, capture_output=True, check=True)
    expected = work / expected_group / "alice" / "dicos" if expected_group else home / "dicos"
    assert result.stdout.strip() == str(expected)
    assert expected.is_dir()
