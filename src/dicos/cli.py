"""DiCOS local setup and TOTP commands."""

import sys
import fcntl
import os
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
            raise ValueError("init cancelled: input required") from None
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
    default_partition: Optional[str] = typer.Option(None, "--default-partition", help="Saved as defaults.partition."),
    default_time: Optional[str] = typer.Option(None, "--default-time", help="Saved as defaults.time, e.g. 6h."),
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
        # Probing touches the jump hosts, so skip it while a failed login is latched.
        probe_first = jump is None and not no_jump and not (paths.home() / "login.failed").exists()
        selected_jump = init_net.select_jump(jump=jump, no_jump=no_jump, probe=probe_first)
        cfg = config.Config(user=username, jump=selected_jump)
        creds = credentials.Credentials(password=password, totp_secret=account.secret)
        code_value = otp.current_code(creds.totp_secret)
        credentials.save(creds)
        config.save(cfg)
        (paths.home() / "login.failed").unlink(missing_ok=True)
        if not probe_first:
            init_net.select_jump(jump=selected_jump, no_jump=selected_jump is None)
        typer.echo(f"Current TOTP code: {code_value}")
        workdir = init_net.connect_and_detect(cfg, creds)
        if workdir is not None:
            cfg.workdir = workdir
            config.save(cfg)
            cfg.defaults = _ask_defaults(cfg, default_partition, default_time)
            config.save(cfg)
    except (typer.Abort, EOFError):
        typer.echo("init cancelled: input required", err=True)
        raise typer.Exit(1) from None
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def _interactive() -> bool:
    return sys.stdin.isatty()


def _ask_defaults(cfg, partition, time):
    """Default partition and time limit: flags, else prompts on a terminal, else none."""
    from dicos import slurm

    interactive = _interactive()
    if partition is None and time is None and not interactive:
        return dict(cfg.defaults)
    allowed = [name for name, _, ok in _partitions(cfg) if ok]
    if partition is None and interactive:
        typer.echo("Partitions for your account: " + ", ".join(allowed))
    while True:
        value = partition if partition is not None else (
            typer.prompt("Default partition (empty = none)", default="", show_default=False) if interactive else "")
        if not value or value in allowed:
            break
        if partition is not None:
            raise ValueError(f"Partition {value} is not available to your account. See `dicos status`.")
        typer.echo("Choose a listed partition, or leave it empty.")
    while True:
        limit = time if time is not None else (
            typer.prompt("Default time limit (30m, 6h, 2d)", default="1h") if interactive else "")
        try:
            if limit:
                slurm.parse_time(limit)
            break
        except ValueError:
            if time is not None:
                raise
            typer.echo("Use a duration such as 30m, 6h, 2d, or Slurm format.")
    defaults = dict(cfg.defaults)
    defaults.update({key: v for key, v in (("partition", value), ("time", limit)) if v})
    if defaults:
        typer.echo("Saved defaults: " + ", ".join(f"{k}={v}" for k, v in defaults.items())
                   + f" (edit {paths.home() / 'config.yaml'} to change)")
    return defaults


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
        home = paths.ensure_home()
        fd = os.open(home / "master.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = home / "login.failed"
            failed = marker.exists()
            marker.unlink(missing_ok=True)
            if not (home / "config.yaml").exists():
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
    marker = home / "login.failed"
    if marker.exists():
        row("login latch", False, marker.read_text(encoding="utf-8").strip()
            + "\nFix credentials with `dicos init --force` or run `dicos logout` to allow one new attempt.")
        Console().print(table)
        raise typer.Exit(1)
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
                    detail = result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}"
                    if name == "sinfo" and result.returncode == 0:
                        detail = f"{len(set(result.stdout.split()))} partitions"
                    row(name, ok, detail)
                except OSError as exc:
                    row(name, False, str(exc))
    Console().print(table)
    if not healthy:
        raise typer.Exit(1)


def _partitions(cfg):
    """[(name, sinfo details, allowed for the user's account)] from one remote call."""
    from dicos import slurm

    separator = "__DICOS_STATUS_SECTION__"
    command = ("sinfo -h -o '%P|%G|%F|%l' && "
               f"printf '\\n{separator}\\n' && "
               "scontrol show partition -o && "
               f"printf '\\n{separator}\\n' && "
               "sacctmgr -n show assoc user=$USER format=account -P | head -1")
    result = conn.run(cfg, command)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"Remote status failed (exit {result.returncode}).")
    sections = result.stdout.split(separator)
    if len(sections) != 3:
        raise ValueError("Incomplete remote status response.")
    info = slurm.parse_sinfo(sections[0])
    partitions = {p["name"]: p for p in slurm.parse_partitions(sections[1])}
    account = sections[2].strip().split("|", 1)[0].strip().casefold()
    if not account:
        raise ValueError("No Slurm account association found.")
    rows = []
    for name, details in info.items():
        if name not in partitions:
            raise ValueError(f"Missing account permissions for partition {name}.")
        accounts = {a.casefold() for a in partitions[name]["allow_accounts"]}
        rows.append((name, details, "all" in accounts or account in accounts))
    return rows


@app.command()
def status(all_partitions: bool = typer.Option(False, "--all")):
    """Show available partitions, or all partitions with --all."""
    try:
        rows = _partitions(_remote_config())
        table = Table("PARTITION", "GRES", "NODES idle/total", "TIMELIMIT")
        if all_partitions:
            table.add_column("ALLOWED")
        for name, details, allowed in rows:
            if not allowed and not all_partitions:
                continue
            nodes = details["nodes"]
            row = [escape(name), escape(details["gres"]), f"{nodes['idle']}/{nodes['total']}",
                   escape(details["timelimit"])]
            if all_partitions:
                row.append("yes" if allowed else "no")
            table.add_row(*row)
        Console().print(table)
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


_PARTITION = typer.Option(None, "-p", "--partition")
_GPUS = typer.Option(None, "--gpus", min=0)
_CPUS = typer.Option(None, "--cpus", min=1)
_MEM = typer.Option(None, "--mem")
_TIME = typer.Option(None, "-t", "--time", help="30m, 6h, 2d, 1d12h, or Slurm format.")
_SYNC = typer.Option(False, "--sync", help="rsync the working tree instead of a pushed commit.")
_BRANCH = typer.Option(None, "--branch", help="Exact local branch published on origin.")
_COMMIT = typer.Option(None, "--commit", help="Full or unique abbreviated commit SHA.")
_TAG = typer.Option(None, "--tag", help="Exact local tag published on origin.")


def _submit(sync, branch, commit, tag, resources, commands, array):
    """Resolve project and commit locally, then log in, ship code, and submit."""
    from dicos import gitutil, jobs
    from dicos.store import JobStore

    cfg = config.load()
    if not cfg.workdir:
        raise ValueError("Workdir missing: run dicos init.")
    if sync and any(value is not None for value in (branch, commit, tag)):
        raise ValueError("--branch, --commit and --tag select a git commit; drop --sync.")
    request = jobs.resources(cfg, **resources)
    root, name, project = jobs.resolve_project(cfg)
    sha = None if sync else gitutil.resolve_commit(root, branch=branch, commit=commit, tag=tag)
    cfg = _remote_config()
    submitted = jobs.submit(JobStore(), cfg, root, name, project, request, commands,
                            mode="sync" if sync else "git", sha=sha, array=array)
    for job in submitted:
        typer.echo(f"{job['id']} (slurm {job['slurm_id']}) queued on {job['partition']}")


@app.command("run", context_settings={"allow_extra_args": True})
def run_job(
    ctx: typer.Context,
    sync: bool = _SYNC,
    partition: Optional[str] = _PARTITION,
    gpus: Optional[int] = _GPUS,
    cpus: Optional[int] = _CPUS,
    mem: Optional[str] = _MEM,
    time: Optional[str] = _TIME,
    branch: Optional[str] = _BRANCH,
    commit: Optional[str] = _COMMIT,
    tag: Optional[str] = _TAG,
):
    """Submit CMD after -- from a pushed commit (default) or the working tree (--sync)."""
    if not ctx.args:
        raise typer.BadParameter("Provide a command after --.")
    command = ctx.args[0] if len(ctx.args) == 1 else shlex.join(ctx.args)
    try:
        _submit(sync, branch, commit, tag, dict(partition=partition, gpus=gpus, cpus=cpus, mem=mem, time=time),
                [command], array=False)
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def sweep(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="Sweep YAML file."),
    sync: bool = _SYNC,
    partition: Optional[str] = _PARTITION,
    gpus: Optional[int] = _GPUS,
    cpus: Optional[int] = _CPUS,
    mem: Optional[str] = _MEM,
    time: Optional[str] = _TIME,
    branch: Optional[str] = _BRANCH,
    commit: Optional[str] = _COMMIT,
    tag: Optional[str] = _TAG,
):
    """Submit one Slurm array job with one element per grid point of FILE."""
    import yaml
    from dicos import sweep as sweeps

    try:
        try:
            commands, resources = sweeps.load_sweep(yaml.safe_load(file.read_text(encoding="utf-8")))
        except (sweeps.SweepError, yaml.YAMLError) as exc:
            raise ValueError(f"{file}: {exc}") from None
        flags = dict(partition=partition, gpus=gpus, cpus=cpus, mem=mem, time=time)
        resources.update({key: value for key, value in flags.items() if value is not None})
        _submit(sync, branch, commit, tag, resources, commands, array=True)
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def _job(store, id):
    job = store.get(id)
    if job is None:
        raise ValueError(f"Unknown job: {id}")
    return job


@app.command("list")
def list_jobs(
    status: Optional[str] = typer.Option(None, "-s", "--status"),
    show_all: bool = typer.Option(False, "--all"),
):
    """Refresh and list the latest 20 jobs, or all jobs with --all."""
    from dicos.jobs import refresh
    from dicos.store import JobStore

    try:
        store = JobStore()
        cfg = _remote_config() if store.active() else config.load()
        refresh(store, cfg)
        table = Table("ID", "STATUS", "PARTITION", "GPUS", "SLURM", "CREATED", "COMMAND")
        records = store.list(status, limit=None if show_all else 20)
        table.columns[0].min_width = max((len(job["id"]) for job in records), default=2)
        table.columns[0].no_wrap = True
        for job in records:
            command = job["command"] or ""
            if len(command) > 60:
                command = command[:57] + "..."
            table.add_row(*[escape(str(value)) for value in (
                job["id"], job["status"], job["partition"] or "-", job["gpus"],
                job["slurm_id"] or "-", job["created_at"], command)])
        Console().print(table)
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def show(id: str):
    """Refresh and show every field of a job."""
    import re
    from dicos.jobs import checked_run, refresh
    from dicos.store import JobStore

    try:
        store = JobStore()
        _job(store, id)
        cfg = _remote_config() if store.active() else config.load()
        refresh(store, cfg)
        job = _job(store, id)
        table = Table.grid(padding=(0, 2))
        for key, value in job.items():
            table.add_row(key, escape(str(value)) if value is not None else "-")
        if job["status"] == "queued" and job["slurm_id"]:
            output = checked_run(cfg, "scontrol show job " + shlex.quote(job["slurm_id"]))
            reason = re.search(r"\bReason=(\S+)", output)
            table.add_row("Reason", escape(reason.group(1)) if reason else "-")
        Console().print(table)
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def logs(
    id: str,
    lines: int = typer.Option(200, "-n", "--lines", min=0),
    follow: bool = typer.Option(False, "-f", "--follow"),
):
    """Read the job's output log through the SSH master."""
    from dicos.store import JobStore

    from dicos.jobs import log_path

    try:
        job = _job(JobStore(), id)
        command = f"tail -n {lines} " + ("-F " if follow else "") + shlex.quote(log_path(job))
        conn.exec_interactive(_remote_config(), command)
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def fetch(
    id: str,
    paths: list[str] = typer.Argument(..., help="Paths relative to the job's src/ directory, or absolute."),
    dest: Optional[Path] = typer.Option(None, "-o", "--output", help="Local directory (default ./<ID>)."),
):
    """Copy job outputs from the cluster to a local directory."""
    import subprocess
    from dicos.jobs import fetch_argv
    from dicos.store import JobStore

    try:
        job = _job(JobStore(), id)
        target = Path(id) if dest is None else dest
        cfg = _remote_config()
        argvs = [fetch_argv(cfg, job, path, target) for path in paths]
        target.mkdir(parents=True, exist_ok=True)
        for path, argv in zip(paths, argvs):
            result = subprocess.run(argv, capture_output=True, text=True)
            if result.returncode:
                raise ValueError(f"fetch {path} failed: " + (result.stderr.strip() or f"rsync exit {result.returncode}"))
            typer.echo(f"{path} -> {target}/{Path(path.rstrip('/')).name}")
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def stop(id: str):
    """Cancel a queued or running job."""
    from dicos.jobs import checked_run, now
    from dicos.store import JobStore

    try:
        store = JobStore()
        job = _job(store, id)
        if job["status"] not in {"queued", "running"}:
            typer.echo(f"{id} already finished: {job['status']}")
            return
        if not job["slurm_id"]:
            raise ValueError(f"Job {id} has no Slurm ID.")
        checked_run(_remote_config(), "scancel " + shlex.quote(job["slurm_id"]))
        store.update(id, status="cancelled", slurm_state="CANCELLED", finished_at=now())
        typer.echo(f"{id} cancelled")
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


WAIT_CODES = {"succeeded": 0, "failed": 1, "cancelled": 2, "lost": 3}


@app.command()
def wait(
    id: str,
    timeout: Optional[float] = typer.Option(None, "--timeout", min=0, help="Seconds; exit 124 when reached."),
    interval: float = typer.Option(15, "--interval", min=1, help="Seconds between refreshes."),
):
    """Wait until a job finishes: exit 0 succeeded, 1 failed, 2 cancelled, 3 lost, 124 timeout."""
    import time as clock
    from dicos.jobs import refresh
    from dicos.store import JobStore

    try:
        store = JobStore()
        job = _job(store, id)
        deadline = None if timeout is None else clock.monotonic() + timeout
        cfg = None
        while job["status"] not in WAIT_CODES:
            if cfg is None:
                cfg = config.load()
            conn.ensure_master(cfg, credentials.load())
            refresh(store, cfg)
            job = _job(store, id)
            if job["status"] in WAIT_CODES:
                break
            remaining = None if deadline is None else deadline - clock.monotonic()
            if remaining is not None and remaining <= 0:
                typer.echo(f"{id} still {job['status']} after {timeout:g}s")
                raise typer.Exit(124)
            clock.sleep(interval if remaining is None else min(interval, remaining))
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    detail = f" ({job['error']})" if job["status"] == "failed" and job["error"] else ""
    typer.echo(f"{id} {job['status']}{detail}")
    raise typer.Exit(WAIT_CODES[job["status"]])


@app.command()
def clean(id: str, yes: bool = typer.Option(False, "-y", "--yes", help="Do not ask for confirmation.")):
    """Remove a finished job's remote directory."""
    from dicos.jobs import checked_run, clean_command
    from dicos.store import JobStore

    try:
        store = JobStore()
        job = _job(store, id)
        cfg = config.load()
        command = clean_command(store, cfg, job)
        shared = [j["id"] for j in store.by_remote_dir(job["remote_dir"])]
        target = job["remote_dir"] + (f" (shared by {', '.join(shared)})" if len(shared) > 1 else "")
        if not yes and not typer.confirm(f"Remove {target}?"):
            typer.echo("Cancelled.")
            raise typer.Exit(1)
        checked_run(_remote_config(), command)
        typer.echo(f"Removed {job['remote_dir']}")
    except (typer.Abort, EOFError):
        typer.echo("Cancelled.", err=True)
        raise typer.Exit(1) from None
    except (conn.LoginError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command()
def update(check: bool = typer.Option(False, "--check", help="Only report whether an update is available.")):
    """Update dicos to the latest commit of its git source (reinstalls via uv)."""
    import shutil
    from dicos import update as updates

    def fail(message):
        typer.echo(f"error: {message}", err=True)
        raise typer.Exit(1)

    try:
        source = updates.installed_source()
    except updates.UpdateError as exc:
        fail(str(exc))
    if source is None:
        fail("dicos has no installed git source (e.g. an editable checkout); "
             "update its checkout with git pull --ff-only, or reinstall using its original installer")
    if not updates.is_uv_tool_install():
        fail("dicos is not managed by uv tool; update it with the installer you used")
    url, current = source
    try:
        remote = updates.remote_head(url)
    except updates.UpdateError as exc:
        fail(str(exc))
    if current == remote:
        typer.echo(f"dicos is up to date ({remote[:7]})")
        return
    if current:
        typer.echo(f"update available: {current[:7]} -> {remote[:7]} ({url})")
    else:
        typer.echo(f"installed commit unknown; remote HEAD is {remote[:7]} ({url})")
    if check:
        return
    uv = shutil.which("uv")
    if uv is None:
        fail("'uv' not found in PATH; install uv (https://docs.astral.sh/uv) and retry")
    requirement = updates.uv_git_requirement(url)
    typer.echo(f"updating: uv tool install --force --refresh {requirement}")
    try:
        code = updates.run_install([uv, "tool", "install", "--force", "--refresh", requirement])
    except OSError as exc:
        fail(f"could not run uv: {exc}")
    if code:
        fail(f"`uv tool install --force --refresh` exited with code {code}")
    typer.echo(f"dicos updated to {remote[:7]}")
    # The installer may have replaced this environment; exit without further imports.
    raise typer.Exit(0)


FEEDBACK_REPO = "novis10813/dicos"


@app.command()
def feedback(
    title: Optional[str] = typer.Argument(None, help="Issue title; omit to write it in gh's editor (needs a terminal)."),
    body: str = typer.Option("", "--body", help="Issue content."),
):
    """Open a GitHub issue in novis10813/dicos using the system gh."""
    import subprocess

    def fail(message):
        typer.echo(f"error: {message}", err=True)
        raise typer.Exit(1)

    if title is not None and not title.strip():
        fail("Issue title must not be empty or whitespace.")
    if title is None and not _interactive():
        fail('Omitting the title requires an interactive terminal.\nUse: dicos feedback "Bug title" --body "What happened"')
    metadata = f"dicos version: {__version__}"
    argv = ["gh", "issue", "create", "--repo", FEEDBACK_REPO, "--body", f"{body}\n\n{metadata}" if body else metadata]
    argv += ["--editor"] if title is None else ["--title", title]
    try:
        result = subprocess.run(argv)
    except FileNotFoundError:
        fail("GitHub CLI (gh) is required but was not found on PATH.\n"
             "Install gh (https://cli.github.com/), run `gh auth login`, and retry,\n"
             f"or open https://github.com/{FEEDBACK_REPO}/issues/new")
    if result.returncode:
        fail(f"gh issue create failed (exit {result.returncode}). See gh output above.\n"
             "Check `gh auth status`, and check "
             f"https://github.com/{FEEDBACK_REPO}/issues for an issue already created before retrying.")
