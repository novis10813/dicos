"""DiCOS local setup and TOTP commands."""

import sys
import shlex
import stat
from pathlib import Path
from typing import Optional

import pexpect
import typer

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from dicos import __version__, config, conn, credentials, init_net, otp, paths

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True)):
    """Manage DiCOS local state."""


def _stdin_line(label: str) -> str:
    line = sys.stdin.readline()
    if not line:
        raise ValueError(f"Missing {label} line on stdin.")
    return line.rstrip("\r\n")


def _choose_account(accounts: list[otp.OtpAccount]) -> otp.OtpAccount:
    if not accounts:
        raise ValueError("No supported TOTP accounts found.")
    if len(accounts) == 1:
        return accounts[0]
    for account in accounts:
        if "dicos" in (account.issuer + " " + account.name).lower():
            return account
    for i, account in enumerate(accounts, 1):
        typer.echo(f"{i}: {account.issuer} {account.name}")
    while True:
        try:
            index = typer.prompt("Account number", type=int)
        except (typer.Abort, EOFError):
            raise ValueError("Account selection requires input. Run init interactively to choose an account.") from None
        if 1 <= index <= len(accounts):
            return accounts[index - 1]
        typer.echo("Choose a listed account number.")


@app.command()
def init(
    user: Optional[str] = typer.Option(None, "--user"),
    jump: Optional[str] = typer.Option(None, "--jump", metavar="ALIAS"),
    no_jump: bool = typer.Option(False, "--no-jump"),
    qr: Optional[Path] = typer.Option(None, "--qr", metavar="PATH"),
    totp_secret_stdin: bool = typer.Option(False, "--totp-secret-stdin"),
    password_stdin: bool = typer.Option(False, "--password-stdin"),
    force: bool = typer.Option(False, "--force"),
):
    """Initialize private configuration, credentials, and connection."""
    if (paths.home() / "config.yaml").exists() and not force:
        typer.echo("Configuration already exists. Use --force to overwrite.", err=True)
        raise typer.Exit(1)
    if jump is not None and no_jump:
        raise typer.BadParameter("--jump and --no-jump are mutually exclusive")
    if qr is not None and totp_secret_stdin:
        raise typer.BadParameter("--qr and --totp-secret-stdin are mutually exclusive")
    try:
        # Read flagged stdin values before prompts, in the documented line order.
        password = _stdin_line("password") if password_stdin else None
        secret_input = _stdin_line("TOTP") if totp_secret_stdin else None
        username = user if user is not None else typer.prompt("Username")
        username = username.strip()
        if not username:
            raise ValueError("Username must not be empty.")
        if qr is not None:
            secret_input = otp.decode_qr(str(qr))
        elif secret_input is None:
            secret_input = typer.prompt("TOTP secret or URI (empty = QR image)", hide_input=True, default="", show_default=False)
            if not secret_input:
                secret_input = otp.decode_qr(typer.prompt("QR image path"))
        account = _choose_account(otp.parse_otpauth(secret_input))
        if password is None:
            password = typer.prompt("Password", hide_input=True)
        if not password:
            raise ValueError("Password must not be empty.")
        selected_jump = init_net.select_jump(jump=jump, no_jump=no_jump) if jump is not None or no_jump else init_net.select_jump()
        cfg = config.Config(user=username, jump=selected_jump)
        creds = credentials.Credentials(password=password, totp_secret=account.secret)
        code_value = otp.current_code(creds.totp_secret)
        credentials.save(creds)
        config.save(cfg)
        (paths.home() / "login.failed").unlink(missing_ok=True)
        typer.echo(f"Current TOTP code: {code_value}")
        workdir = init_net.connect_and_detect(cfg, creds)
        if workdir is not None:
            cfg.workdir = workdir
            config.save(cfg)
    except (typer.Abort, EOFError):
        typer.echo("Initialization requires input. Run init interactively to choose a jump host.", err=True)
        raise typer.Exit(1) from None
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def code():
    """Print the current six-digit TOTP code."""
    try:
        typer.echo(otp.current_code(credentials.load().totp_secret))
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def _remote_config() -> config.Config:
    try:
        cfg = config.load()
        conn.ensure_master(cfg, credentials.load())
        return cfg
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("ssh")
def ssh_shell():
    """Open an interactive shell through the master."""
    conn.exec_interactive(_remote_config(), None)


@app.command("exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_command(ctx: typer.Context):
    """Execute CMD after -- with native stdin, stdout, and exit status."""
    if not ctx.args:
        raise typer.BadParameter("Provide a command after --.")
    cmd = ctx.args[0] if len(ctx.args) == 1 else shlex.join(ctx.args)
    conn.exec_interactive(_remote_config(), cmd)


@app.command()
def logout():
    """Close the existing master without opening a new connection."""
    try:
        marker = paths.home() / "login.failed"
        failed = marker.exists()
        marker.unlink(missing_ok=True)
        if not (paths.home() / "config.yaml").exists():
            running = False
        else:
            running = conn.close(config.load())
        if running:
            typer.echo("Logged out.")
        elif failed:
            typer.echo("Login failure cleared. No master closed.")
        else:
            typer.echo("No master is running or the master could not be closed.")
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def doctor():
    """Check local permissions, login, and remote services."""
    table = Table("CHECK", "STATUS", "DETAIL")
    healthy = True

    def row(name, ok, detail):
        nonlocal healthy
        healthy &= ok
        table.add_row(name, "[green]ok[/]" if ok else "[red]FAIL[/]", escape(detail))

    home = paths.home()
    local_ok = home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700
    details = []
    if not local_ok:
        details.append("directory must exist with mode 0700")
    for name in ("config.yaml", "credentials.json"):
        if not (home / name).is_file():
            local_ok = False
            details.append(f"{name} missing")
    if home.is_dir():
        for path in home.iterdir():
            if not path.is_socket() and (not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600):
                local_ok = False
                details.append(f"{path.name} must have mode 0600")
    row("local files and modes", local_ok, ", ".join(details) or str(home))
    try:
        cfg = config.load()
        creds = credentials.load()
    except (ValueError, OSError) as exc:
        row("master login", False, str(exc))
    else:
        reachable = init_net.probe_jumps([cfg.jump], host=cfg.host)[cfg.jump]
        row("jump reachable", reachable, cfg.jump or "direct")
        try:
            conn.ensure_master(cfg, creds)
        except (conn.LoginError, ValueError, pexpect.ExceptionPexpect, OSError) as exc:
            row("master login", False, str(exc))
            typer.echo(init_net.LOGIN_WARNING, err=True)
        else:
            row("master login", True, "ControlMaster active")
            checks = [
                ("sinfo", "sinfo -h -o %P", False),
                ("account association", "sacctmgr -n show assoc user=$USER format=account -P", True),
                ("workdir writable", "test -w " + shlex.quote(cfg.workdir or ""), False),
                ("GitHub reachable", "timeout 15 git ls-remote https://github.com/git/git HEAD", True),
            ]
            for name, command, needs_output in checks:
                try:
                    result = conn.run(cfg, "timeout 20 bash -c " + shlex.quote(command))
                    ok = result.returncode == 0 and (not needs_output or bool(result.stdout.strip().strip("|")))
                    row(name, ok, result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}")
                except OSError as exc:
                    row(name, False, str(exc))
    Console().print(table)
    if not healthy:
        raise typer.Exit(1)
