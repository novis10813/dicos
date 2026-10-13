"""One private SSH master, with bounded, non-retrying authentication."""

import fcntl
import os
import re
import shlex
import subprocess
import sys
import time

import pexpect

from dicos import otp, paths
from dicos.config import Config
from dicos.credentials import Credentials

HOSTKEY = re.compile(rb"(?i)(continue connecting|\(yes/no[^)]*\)\?)")
MENU = re.compile(rb"(?i)(method|choice|option)\s*:")
USER = re.compile(rb"(?i)(user ?name|login|account)\s*:")
PASSWORD = re.compile(rb"(?i)password\s*:")
OTP = re.compile(rb"(?i)(code|otp|token)\s*:")
DENIED = re.compile(rb"(?i)(permission denied|authentication fail\w*|access denied|too many)")
LOGIN_TIMEOUT = 90


class LoginError(Exception):
    pass


def _control(cfg: Config, operation: str) -> list[str]:
    return ["ssh", "-o", f"ControlPath={paths.home() / 'cm.sock'}", "-O", operation,
            f"{cfg.user}@{cfg.host}"]


def is_alive(cfg: Config) -> bool:
    return subprocess.run(_control(cfg, "check"), capture_output=True).returncode == 0


def ensure_master(cfg: Config, creds: Credentials) -> None:
    home = paths.ensure_home()
    fd = os.open(home / "master.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if is_alive(cfg):
            return
        argv = ["ssh", "-M", "-N", "-f", "-o", f"ControlPath={home / 'cm.sock'}",
                "-o", "ControlPersist=8h", "-o", "ServerAliveInterval=60",
                "-o", "PreferredAuthentications=keyboard-interactive,password",
                "-o", "NumberOfPasswordPrompts=1", "-A"]
        if cfg.jump:
            argv += ["-J", cfg.jump]
        argv += [f"{cfg.user}@{cfg.host}"]
        child = pexpect.spawn(argv[0], argv[1:], echo=False, timeout=LOGIN_TIMEOUT)
        child.logfile = None
        deadline = time.monotonic() + LOGIN_TIMEOUT
        seen = set()
        output = b""
        secrets = [creds.password, creds.totp_secret]

        def failure(reason: str) -> LoginError:
            text = output.decode(errors="replace")
            for secret in secrets:
                if secret:
                    text = text.replace(secret, "[redacted]")
            lines = [line for line in text.splitlines() if line.strip()][-5:]
            return LoginError(reason + ("\n" + "\n".join(lines) if lines else ""))

        try:
            patterns = [HOSTKEY, DENIED, MENU, USER, PASSWORD, OTP, pexpect.EOF, pexpect.TIMEOUT]
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise failure("Login timed out (90s).")
                index = child.expect(patterns, timeout=remaining)
                output = (output + child.before)[-16384:]
                if index == 0:
                    manual = ["ssh"] + (["-J", cfg.jump] if cfg.jump else []) + [f"{cfg.user}@{cfg.host}"]
                    raise LoginError(f"Unknown host key. Run `{shlex.join(manual)}` once manually to verify it.")
                if index == 1:
                    raise failure("Server rejected the login.")
                if index == 6:
                    child.close()
                    if child.exitstatus != 0 or not is_alive(cfg):
                        raise failure("Connection closed without an active master.")
                    return
                if index == 7:
                    raise failure("Login timed out (90s).")
                if index in seen:
                    raise LoginError("previous answer rejected")
                seen.add(index)
                if index == 2:
                    child.sendline("1")
                elif index == 3:
                    child.sendline(cfg.user)
                elif index == 4:
                    child.sendline(creds.password)
                else:
                    code = otp.current_code(creds.totp_secret)
                    secrets.append(code)
                    child.sendline(code)
                    # ssh -f exits after authentication. Ignore MOTD prose such as
                    # "password:" while still detecting repeated auth prompts.
                    patterns[4] = re.compile(rb"(?im)^(?:\([^\r\n]*\)\s*)?password\s*:")
        finally:
            if child.isalive():
                child.terminate(force=True)
            child.close(force=True)


def ssh_argv(cfg: Config, cmd: str | None, tty: bool, agent: bool = False) -> list[str]:
    argv = ["ssh", "-o", f"ControlPath={paths.home() / 'cm.sock'}", "-o",
            "ControlMaster=no", "-o", "BatchMode=yes"]
    if tty:
        argv.append("-t")
    if agent:
        argv.append("-A")
    argv.append(f"{cfg.user}@{cfg.host}")
    if cmd is not None:
        argv += ["--", cmd]
    return argv


def run(cfg: Config, cmd: str, input: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_argv(cfg, cmd, tty=False), input=input, text=True,
                          capture_output=True, check=check)


def exec_interactive(cfg: Config, cmd: str | None) -> None:
    argv = ssh_argv(cfg, cmd, tty=cmd is None or sys.stdin.isatty())
    os.execvp(argv[0], argv)


def close(cfg: Config) -> bool:
    if not is_alive(cfg):
        return False
    subprocess.run(_control(cfg, "exit"), capture_output=True, check=True)
    return True
