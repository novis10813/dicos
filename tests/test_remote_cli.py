import subprocess
import shlex

import pexpect

import pytest
from typer.testing import CliRunner

from dicos import config, conn, credentials, init_net, paths
from dicos.cli import app
from dicos.conn import close as close_master

runner = CliRunner()


@pytest.fixture
def remote(monkeypatch):
    config.save(config.Config("alice", jump="lab", workdir="/work/alice/dicos"))
    credentials.save(credentials.Credentials("synthetic-password", "JBSWY3DPEHPK3PXP"))
    calls = []
    monkeypatch.setattr(conn, "ensure_master", lambda *args: calls.append("master"))
    monkeypatch.setattr(conn, "exec_interactive", lambda cfg, cmd: calls.append(cmd))
    monkeypatch.setattr(conn, "close", lambda cfg: calls.append("close") or True)
    monkeypatch.setattr(init_net, "probe_jumps", lambda candidates, **kwargs: dict.fromkeys(candidates, True))
    monkeypatch.setattr(conn, "run", lambda cfg, cmd: calls.append(cmd) or subprocess.CompletedProcess([], 0, "ok\n", ""))
    return calls


@pytest.mark.parametrize("args,command", [(["ssh"], None), (["exec", "--", "echo hello"], "echo hello"),
    (["exec", "--", "printf", "%s", "two words"], "printf %s 'two words'"),
    (["exec", "--", "ls", "-l"], "ls -l")])
def test_interactive(remote, args, command):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert remote == ["master", command]


def test_exec_missing(remote):
    result = runner.invoke(app, ["exec"])
    assert result.exit_code != 0
    assert not remote


def test_logout(remote):
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0 and "Logged out" in result.output
    assert remote == ["close"]


def test_logout_absent(remote, monkeypatch):
    monkeypatch.setattr(conn, "close", lambda cfg: False)
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0 and "No master" in result.output
    assert not remote


@pytest.mark.parametrize("args", [["ssh"], ["exec", "--", "pwd"]])
def test_login_failure(remote, monkeypatch, args):
    attempts = []
    def fail(*a):
        attempts.append(True)
        raise conn.LoginError("previous answer rejected")
    monkeypatch.setattr(conn, "ensure_master", fail)
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "previous answer rejected" in result.output
    assert len(attempts) == 1 and not remote


def test_doctor(remote):
    result = runner.invoke(app, ["doctor"], terminal_width=140)
    assert result.exit_code == 0, result.output
    for name in ["CHECK", "STATUS", "DETAIL", "local files and modes", "jump reachable", "master login",
                 "sinfo", "account association", "workdir writable", "GitHub reachable"]:
        assert name in result.output
    assert len(remote) == 5 and remote[0] == "master"
    assert "test -w /work/alice/dicos" in [shlex.split(cmd)[-1] for cmd in remote[1:]]
    assert all(shlex.split(cmd)[:4] == ["timeout", "20", "bash", "-c"] for cmd in remote[1:])
    assert "timeout 15 git ls-remote https://github.com/git/git HEAD" in [shlex.split(cmd)[-1] for cmd in remote[1:]]


def test_doctor_login_failure(remote, monkeypatch):
    attempts = []
    def fail(*a):
        attempts.append(True)
        raise conn.LoginError("previous answer rejected")
    monkeypatch.setattr(conn, "ensure_master", fail)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.output and "previous answer rejected" in result.output
    assert not remote and len(attempts) == 1


@pytest.mark.parametrize("failure", ["sinfo", "account", "workdir", "github", "jump", "mode", "missing"])
def test_doctor_failures(remote, monkeypatch, failure):
    def run(cfg, cmd):
        cmd = shlex.split(cmd)[-1]
        bad = ((failure == "sinfo" and cmd.startswith("sinfo")) or
               (failure == "account" and cmd.startswith("sacctmgr")) or
               (failure == "workdir" and cmd.startswith("test")) or
               (failure == "github" and cmd.startswith("timeout")))
        # An empty account association is a failure even with exit status zero.
        return subprocess.CompletedProcess([], int(bad and failure != "account"), "" if bad else "ok", "")
    monkeypatch.setattr(conn, "run", run)
    if failure == "jump":
        monkeypatch.setattr(init_net, "probe_jumps", lambda c, **kw: dict.fromkeys(c, False))
    if failure == "mode":
        (paths.home() / "config.yaml").chmod(0o644)
    if failure == "missing":
        (paths.home() / "credentials.json").unlink()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1 and "FAIL" in result.output


def test_init_failed_login_preserves_files(monkeypatch):
    monkeypatch.setattr(init_net, "probe_jumps", lambda c, **kw: dict.fromkeys(c, True))
    def fail(*a):
        raise conn.LoginError("previous answer rejected")
    monkeypatch.setattr(conn, "ensure_master", fail)
    result = runner.invoke(app, ["init", "--user", "alice", "--no-jump", "--password-stdin", "--totp-secret-stdin"],
                           input="synthetic-password\nJBSWY3DPEHPK3PXP\n")
    assert result.exit_code == 1
    assert "5 failed logins" in result.output
    assert config.load().user == "alice"
    assert credentials.load().password == "synthetic-password"


@pytest.mark.parametrize("error", [ValueError("Invalid TOTP secret"), pexpect.ExceptionPexpect("ssh missing")])
def test_doctor_login_errors(remote, monkeypatch, error):
    def fail(*args):
        raise error
    monkeypatch.setattr(conn, "ensure_master", fail)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.output and str(error) in result.output
    assert init_net.LOGIN_WARNING in result.output
    assert not remote


def test_logout_clears_latch_and_closes(remote):
    paths.write_private(paths.home() / "login.failed", "stale")
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0 and "Logged out" in result.output
    assert remote == ["close"]
    assert not (paths.home() / "login.failed").exists()


@pytest.mark.parametrize("marker", [False, True])
def test_logout_without_config(marker, monkeypatch):
    if marker:
        paths.write_private(paths.ensure_home() / "login.failed", "stale")
    def forbidden(*args):
        pytest.fail("logout must not connect without configuration")
    monkeypatch.setattr(conn, "close", forbidden)
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert ("Login failure cleared" if marker else "No master") in result.output
    assert not (paths.home() / "login.failed").exists()


def test_logout_failed_exit(remote, monkeypatch):
    monkeypatch.setattr(conn, "is_alive", lambda cfg: True)
    monkeypatch.setattr(conn.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(conn, "close", close_master)
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0 and "could not be closed" in result.output
