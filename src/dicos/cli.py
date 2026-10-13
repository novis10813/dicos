"""DiCOS local setup and TOTP commands."""

import sys
from pathlib import Path
from typing import Optional

import typer

from dicos import __version__, config, credentials, init_net, otp, paths

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
        index = typer.prompt("Account number", type=int)
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
    """Initialize private configuration and credentials (no remote verification yet)."""
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
        if not username.strip():
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
        selected_jump = jump if jump is not None else (None if no_jump else init_net.select_jump())
        cfg = config.Config(user=username, jump=selected_jump)
        creds = credentials.Credentials(password=password, totp_secret=account.secret)
        code_value = otp.current_code(creds.totp_secret)
        config.save(cfg)
        credentials.save(creds)
        typer.echo(f"Current TOTP code: {code_value}")
        workdir = init_net.connect_and_detect(cfg, creds)
        if workdir is not None:
            cfg.workdir = workdir
            config.save(cfg)
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
