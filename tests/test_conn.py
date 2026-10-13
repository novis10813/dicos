import json
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
import pexpect

from dicos import conn, paths
from dicos.config import Config
from dicos.credentials import Credentials

SECRET = "JBSWY3DPEHPK3PXP"
CREDS = Credentials("synthetic-password", SECRET)
CFG = Config("alice", jump="lab")


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "ssh"
    executable.parent.mkdir()
    executable.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
home = Path(os.environ['DICOS_HOME'])
a = sys.argv[1:]
if '-O' in a:
    op = a[a.index('-O') + 1]
    alive = (home / 'cm.sock').exists()
    if op == 'exit' and alive:
        (home / 'cm.sock').unlink()
    sys.exit(0 if alive else 1)
(home / 'argv.json').write_text(json.dumps(a))
with (home / 'attempts').open('a') as f:
    f.write('attempt\\n')
mode = os.environ.get('FAKE_MODE', 'success')
def prompt(text):
    print(text, end='', flush=True)
    return sys.stdin.readline().strip()
if mode == 'hostkey':
    prompt('Are you sure you want to continue connecting (yes/no)?')
    sys.exit(1)
prompt('Choose your login method:')
prompt('Username:')
password = prompt('Password:')
if mode == 'repeat':
    prompt('Password:')
    sys.exit(1)
code = prompt('Verification code:')
if mode == 'repeat_otp':
    prompt('Verification code:')
    sys.exit(1)
if mode == 'repeat_password_after_otp':
    prompt('\\n(alice@host) Password:')
    sys.exit(1)
if mode == 'fallback_possessive':
    prompt("\\nalice@host's Password:")
    sys.exit(1)
if mode == 'fallback_for':
    prompt('\\nPassword for alice@host:')
    sys.exit(1)
if mode == 'fail':
    print('diagnostic ' + password + ' ' + code + ' ' + os.environ['FAKE_SECRET'])
    sys.exit(1)
if mode == 'dead':
    sys.exit(0)
if mode == 'hang':
    time.sleep(5)
    sys.exit(1)
print('\\nMOTD: change your password: use the portal')
time.sleep(0.05)
(home / 'cm.sock').touch(mode=0o600)
sys.exit(0)
''')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_SECRET", SECRET)
    return executable


def attempts():
    return (paths.home() / "attempts").read_text().splitlines()


def test_master_success(fake_ssh):
    conn.ensure_master(CFG, CREDS)
    assert conn.is_alive(CFG)
    argv = json.loads((paths.home() / "argv.json").read_text())
    assert argv == ["-M", "-N", "-f", "-o", f"ControlPath={paths.home() / 'cm.sock'}",
                    "-o", "ControlPersist=8h", "-o", "ServerAliveInterval=60",
                    "-o", "PreferredAuthentications=keyboard-interactive,password",
                    "-o", "NumberOfPasswordPrompts=1", "-A", "-J", "lab",
                    "alice@slurm-ui.twgrid.org"]
    assert CREDS.password not in argv and SECRET not in argv
    conn.ensure_master(CFG, CREDS)
    assert len(attempts()) == 1
    assert stat.S_IMODE((paths.home() / "master.lock").stat().st_mode) == 0o600
    assert conn.close(CFG) is True
    assert conn.close(CFG) is False


@pytest.mark.parametrize("mode,message", [("repeat", "previous answer rejected"),
    ("repeat_otp", "previous answer rejected"), ("repeat_password_after_otp", "previous answer rejected"),
    ("fallback_possessive", "previous answer rejected"), ("fallback_for", "previous answer rejected"),
    ("hostkey", "ssh -J lab alice@slurm-ui.twgrid.org"), ("fail", "Connection closed"),
    ("dead", "Connection closed"), ("hang", "timed out")])
def test_login_failure(fake_ssh, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_MODE", mode)
    if mode == "hang":
        monkeypatch.setattr(conn, "LOGIN_TIMEOUT", 0.5)
    with pytest.raises(conn.LoginError) as error:
        conn.ensure_master(CFG, CREDS)
    assert message in str(error.value)
    assert CREDS.password not in str(error.value)
    assert SECRET not in str(error.value)
    assert conn.otp.current_code(SECRET) not in str(error.value)
    assert len(attempts()) == 1
    assert not conn.is_alive(CFG)
    marker = paths.home() / "login.failed"
    assert marker.exists()
    assert CREDS.password not in marker.read_text() and SECRET not in marker.read_text()
    with pytest.raises(conn.LoginError, match="previous login failed"):
        conn.ensure_master(CFG, CREDS)
    assert len(attempts()) == 1


def test_lock_single_login(fake_ssh):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: conn.ensure_master(CFG, CREDS), range(2)))
    assert len(attempts()) == 1


def test_argv():
    base = ["ssh", "-o", f"ControlPath={paths.home() / 'cm.sock'}", "-o", "ControlMaster=no",
            "-o", "BatchMode=yes", "-o", "ProxyCommand=false"]
    assert conn.ssh_argv(CFG, None, True) == base + ["-t", "alice@slurm-ui.twgrid.org"]
    assert conn.ssh_argv(CFG, "echo hi", False, True) == base + ["-A", "alice@slurm-ui.twgrid.org", "--", "echo hi"]


def test_run(monkeypatch):
    calls = []
    monkeypatch.setattr(conn.subprocess, "run", lambda *a, **kw: calls.append((a, kw)) or "result")
    assert conn.run(CFG, "cat", input="data", check=True) == "result"
    assert calls == [((conn.ssh_argv(CFG, "cat", False),), dict(input="data", text=True, capture_output=True, check=True))]


@pytest.mark.parametrize("cmd,stdin_tty,expected", [(None, False, True), ("pwd", False, False), ("pwd", True, True)])
def test_exec(monkeypatch, cmd, stdin_tty, expected):
    calls = []
    monkeypatch.setattr(conn.sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(conn.os, "execvp", lambda *a: calls.append(a))
    conn.exec_interactive(CFG, cmd)
    assert calls == [("ssh", conn.ssh_argv(CFG, cmd, expected))]


def test_failed_login_latch_parallel(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")

    def login(_):
        with pytest.raises(conn.LoginError) as error:
            conn.ensure_master(CFG, CREDS)
        return str(error.value)

    with ThreadPoolExecutor(max_workers=5) as pool:
        errors = list(pool.map(login, range(5)))
    assert len(errors) == 5 and len(attempts()) == 1
    marker = paths.home() / "login.failed"
    text = marker.read_text()
    assert "Connection closed" in text
    assert CREDS.password not in text and SECRET not in text
    assert conn.otp.current_code(SECRET) not in text
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert conn.datetime.fromisoformat(text.splitlines()[0]).tzinfo is not None
    monkeypatch.setenv("FAKE_MODE", "success")
    error = login(None)
    assert "previous login failed" in error
    assert "dicos init --force" in error and "dicos logout" in error
    assert len(attempts()) == 1


def test_success_removes_marker(fake_ssh, monkeypatch):
    login = conn._login

    def successful_login(*args):
        login(*args)
        paths.write_private(paths.home() / "login.failed", "stale")

    monkeypatch.setattr(conn, "_login", successful_login)
    conn.ensure_master(CFG, CREDS)
    assert not (paths.home() / "login.failed").exists()


def test_live_master_ignores_latch(fake_ssh):
    conn.ensure_master(CFG, CREDS)
    paths.write_private(paths.home() / "login.failed", "stale")
    conn.ensure_master(CFG, CREDS)
    assert len(attempts()) == 1


def test_missing_ssh_latched(monkeypatch):
    monkeypatch.setattr(conn, "is_alive", lambda cfg: False)
    calls = []

    def spawn(*args, **kwargs):
        calls.append(True)
        raise pexpect.ExceptionPexpect("ssh not found")

    monkeypatch.setattr(conn.pexpect, "spawn", spawn)
    for _ in range(2):
        with pytest.raises(conn.LoginError):
            conn.ensure_master(CFG, CREDS)
    assert calls == [True]
    assert "Could not start ssh" in (paths.home() / "login.failed").read_text()


@pytest.mark.parametrize("missing", [False, True])
def test_close_failure(monkeypatch, missing):
    monkeypatch.setattr(conn, "is_alive", lambda cfg: True)

    def run(*args, **kwargs):
        assert not kwargs.get("check")
        if missing:
            raise FileNotFoundError("ssh missing")
        return subprocess.CompletedProcess([], 1)

    monkeypatch.setattr(conn.subprocess, "run", run)
    assert conn.close(CFG) is False


def test_latch_write_failure_no_spawn(monkeypatch):
    monkeypatch.setattr(conn, "is_alive", lambda cfg: False)
    calls = []
    monkeypatch.setattr(conn.pexpect, "spawn", lambda *a, **kw: calls.append(True))

    def fail(*args):
        raise OSError("cannot write")

    monkeypatch.setattr(paths, "write_private", fail)
    with pytest.raises(conn.LoginError, match="No login attempted"):
        conn.ensure_master(CFG, CREDS)
    assert calls == []


@pytest.mark.parametrize("rewrite_fails", [False, True])
def test_interrupted_login_latched(monkeypatch, rewrite_fails):
    monkeypatch.setattr(conn, "is_alive", lambda cfg: False)
    calls = []
    write = paths.write_private

    def record(marker, text):
        if calls and rewrite_fails:
            raise OSError("cannot rewrite")
        write(marker, text)

    def interrupted(*args):
        assert "login attempt in progress or interrupted" in (paths.home() / "login.failed").read_text()
        calls.append(True)
        raise KeyboardInterrupt()

    monkeypatch.setattr(paths, "write_private", record)
    monkeypatch.setattr(conn, "_login", interrupted)
    with pytest.raises(KeyboardInterrupt):
        conn.ensure_master(CFG, CREDS)
    text = (paths.home() / "login.failed").read_text()
    assert ("interrupted" if rewrite_fails else "KeyboardInterrupt") in text
    with pytest.raises(conn.LoginError, match="previous login failed"):
        conn.ensure_master(CFG, CREDS)
    assert calls == [True]


def test_generated_codes_redacted_on_login_error(fake_ssh, monkeypatch):
    codes = ["123456", "654321"]
    monkeypatch.setattr(conn.otp, "current_code", lambda secret: codes[0])
    spawn = conn.pexpect.spawn

    def failing_spawn(*args, **kwargs):
        child = spawn(*args, **kwargs)
        send = child.sendline

        def sendline(value):
            if value == codes[0]:
                # Simulate a transport LoginError after code generation.
                raise conn.LoginError("transport " + " ".join([CREDS.password, SECRET, *codes]))
            return send(value)

        child.sendline = sendline
        return child

    # Track another generated code at the login boundary as well.
    login = conn._login

    def tracked_login(cfg, creds, home, secrets):
        secrets.append(codes[1])
        login(cfg, creds, home, secrets)

    monkeypatch.setattr(conn.pexpect, "spawn", failing_spawn)
    monkeypatch.setattr(conn, "_login", tracked_login)
    with pytest.raises(conn.LoginError) as error:
        conn.ensure_master(CFG, CREDS)
    text = str(error.value) + (paths.home() / "login.failed").read_text()
    for secret in [CREDS.password, SECRET, *codes]:
        assert secret not in text
    assert "transport" in text
