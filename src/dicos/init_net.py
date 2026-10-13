"""Init connection boundary. S1 does not perform any network operations."""

import shlex
from pathlib import Path

import typer

from dicos.config import Config
from dicos.credentials import Credentials


def select_jump() -> str | None:
    aliases = []
    path = Path.home() / ".ssh" / "config"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = shlex.split(line, comments=True)
            if parts and parts[0].lower() == "host":
                for alias in parts[1:]:
                    if not any(char in alias for char in "*?![") and alias not in aliases:
                        aliases.append(alias)
    typer.echo("SSH Host aliases: " + (", ".join(aliases) or "(none)"))
    while True:
        choice = typer.prompt("Jump host (none = direct)", default="none")
        if choice.lower() == "none":
            return None
        if choice in aliases:
            return choice
        typer.echo("Choose a listed alias or none.")


def connect_and_detect(cfg: Config, creds: Credentials) -> str | None:
    typer.echo("Remote verification is not yet implemented.")
    return None
