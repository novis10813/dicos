"""Jump reachability and init's remote work directory detection."""

import shlex
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from dicos import conn
from dicos.config import Config
from dicos.credentials import Credentials

HOST = "slurm-ui.twgrid.org"
LOGIN_WARNING = "Do not retry repeatedly: 5 failed logins ban the jump host IP."


def probe_jumps(candidates: list[str | None], host: str = HOST) -> dict[str | None, bool]:
    def probe(candidate):
        try:
            if candidate is None:
                with socket.create_connection((host, 22), 6):
                    return True
            command = f'timeout 6 bash -c {shlex.quote("echo > /dev/tcp/" + host + "/22")}'
            return subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", candidate, command],
                capture_output=True, timeout=16,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    if not candidates:
        return {}
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        return dict(zip(candidates, pool.map(probe, candidates)))


def select_jump(jump: str | None = None, no_jump: bool = False) -> str | None:
    if jump is not None or no_jump:
        candidate = jump if not no_jump else None
        if not probe_jumps([candidate])[candidate]:
            raise ValueError(f"{'Jump host ' + candidate if candidate else 'Direct connection'} cannot reach {HOST}:22.")
        return candidate
    aliases = []
    path = Path.home() / ".ssh" / "config"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = shlex.split(line, comments=True)
            if parts and parts[0].lower() == "host":
                for alias in parts[1:]:
                    if not any(char in alias for char in "*?![") and alias not in aliases:
                        aliases.append(alias)
    results = probe_jumps([None, *aliases])
    table = Table("JUMP", "STATUS")
    for candidate, reachable in results.items():
        table.add_row(candidate or "none (direct)", "[green]ok[/]" if reachable else "[red]FAIL[/]")
    Console().print(table)
    reachable = [alias for alias in aliases if results[alias]]
    if results[None]:
        reachable.append(None)
    if not reachable:
        raise ValueError(f"No candidate can reach {HOST}:22.")
    while True:
        choice = typer.prompt("Jump host (none = direct)", default=reachable[0] or "none")
        candidate = None if choice.lower() == "none" else choice
        if candidate in reachable:
            return candidate
        typer.echo("Choose a reachable candidate.")


DETECT_WORKDIR = r'''set -e
account=$(sacctmgr -n show assoc user=$USER format=account -P | head -1)
account=${account%%|*}
account=$(printf '%s' "$account" | tr -d '[:space:]')
workdir="$HOME/dicos"
if [ -n "$account" ] && [ -d /ceph/work ]; then
    while IFS= read -r group; do
        if [ "${group,,}" = "${account,,}" ] && [ -w "/ceph/work/$group" ]; then
            workdir="/ceph/work/$group/$USER/dicos"
            break
        fi
    done < <(ls /ceph/work)
fi
mkdir -p "$workdir"
printf '%s\n' "$workdir"
'''


def connect_and_detect(cfg: Config, creds: Credentials) -> str:
    try:
        conn.ensure_master(cfg, creds)
    except conn.LoginError as exc:
        typer.echo(str(exc), err=True)
        typer.echo(LOGIN_WARNING, err=True)
        raise typer.Exit(1) from None
    result = conn.run(cfg, DETECT_WORKDIR)
    if result.returncode or not result.stdout.strip():
        raise ValueError("Could not create the remote work directory.")
    return result.stdout.strip().splitlines()[-1]
