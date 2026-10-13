"""Sync submission and batched Slurm reconciliation."""

from datetime import datetime, timezone
from pathlib import Path
import re
import shlex
import subprocess

from dicos import config, conn, slurm

REFRESH_MARKER = "__DICOS_JOBS_SECTION__"


def resolve_project(cfg: config.Config, cwd: Path | None = None):
    cwd = Path.cwd() if cwd is None else cwd
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                            capture_output=True, text=True)
    repo = None
    root = cwd
    if result.returncode == 0:
        root = Path(result.stdout.strip())
        origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
                                capture_output=True, text=True)
        if origin.returncode == 0:
            repo = origin.stdout.strip() or None
        if repo:
            for name, project in cfg.projects.items():
                if project.get("repo") == repo:
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", project["prefix"]):
                        raise ValueError("Invalid job prefix")
                    return root, name, project
    name = re.split(r"[/:]", repo.rstrip("/"))[-1].removesuffix(".git") if repo else root.name
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", name) or "project"
    project = {"repo": repo, "prefix": prefix, "setup": None}
    if name in cfg.projects and cfg.projects[name] != project:
        raise ValueError(f"Project name {name!r} already registered for another repository.")
    cfg.projects[name] = project
    config.save(cfg)
    return root, name, project


def resources(cfg, partition=None, gpus=None, cpus=None, mem=None, time=None):
    values = {"partition": partition, "gpus": gpus, "cpus": cpus, "mem": mem, "time": time}
    fallback = {"gpus": 0, "cpus": 1, "time": "1h"}
    result = {k: v if v is not None else cfg.defaults.get(k, fallback.get(k))
              for k, v in values.items()}
    if not result["partition"]:
        raise ValueError("Partition required: use --partition or set defaults.partition.")
    if result["gpus"] < 0 or result["cpus"] < 1:
        raise ValueError("GPUs must be nonnegative and CPUs must be positive.")
    result["time"] = slurm.parse_time(result["time"])
    return result


def checked_run(cfg, command, input=None):
    result = conn.run(cfg, command, input=input)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or
                         f"Remote command failed (exit {result.returncode}).")
    return result.stdout


def rsync_argv(cfg, root, jobdir):
    return ["rsync", "-az", "--delete", "-e", shlex.join(conn.ssh_argv(cfg, None, tty=False)[:-1]),
            "--exclude=.git/", "--exclude=.venv/", "--exclude=__pycache__/",
            "--filter=:- .gitignore", str(root) + "/",
            f"{cfg.user}@{cfg.host}:{shlex.quote(jobdir + '/src/')}" ]


def submit_sync(store, cfg, root, name, project, request, command):
    if not cfg.workdir:
        raise ValueError("Workdir missing: run dicos init.")
    id = store.add(name, project["prefix"], mode="sync", command=command,
                   partition=request["partition"], gpus=request["gpus"], cpus=request["cpus"],
                   mem=request["mem"], time_limit=request["time"])
    jobdir = cfg.workdir.rstrip("/") + "/jobs/" + id
    store.update(id, remote_dir=jobdir)
    try:
        checked_run(cfg, "mkdir -p " + shlex.quote(jobdir))
        result = subprocess.run(rsync_argv(cfg, root, jobdir), capture_output=True, text=True)
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"rsync failed (exit {result.returncode}).")
        script = slurm.render_job_script(id=id, jobdir=jobdir, src_dir=jobdir + "/src",
                                         setup=project.get("setup"), command=command, **request)
        checked_run(cfg, "cat > " + shlex.quote(jobdir + "/job.sh"), input=script)
        slurm_id = slurm.parse_sbatch_parsable(checked_run(
            cfg, "sbatch --parsable " + shlex.quote(jobdir + "/job.sh")))
        store.update(id, slurm_id=slurm_id)
    except (ValueError, OSError) as exc:
        store.update(id, status="failed", error=str(exc), finished_at=now())
        raise
    return store.get(id)


def now():
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value):
    return value if value and value not in {"Unknown", "None", "N/A"} else None


def refresh(store, cfg):
    active = store.active()
    if not active:
        return
    ids = ",".join(job["slurm_id"] for job in active if job["slurm_id"])
    queue, accounting = {}, {}
    if ids:
        quoted = shlex.quote(ids)
        command = (f"squeue -h -j {quoted} -o '%i|%T' 2>/dev/null || true; "
                   f"printf '\\n{REFRESH_MARKER}\\n'; "
                   f"sacct -j {quoted} -P -n -o JobID,State,ExitCode,Start,End")
        output = checked_run(cfg, command)
        sections = output.split(REFRESH_MARKER)
        if len(sections) != 2:
            raise ValueError("Incomplete job refresh response.")
        queue, accounting = slurm.parse_squeue(sections[0]), slurm.parse_sacct(sections[1])
    for job in active:
        id = job["slurm_id"]
        raw = queue.get(id)
        fields = {}
        if id in accounting:
            state, code, start, end = accounting[id]
            raw = raw or state
            fields.update(exit_code=code, started_at=_timestamp(start), finished_at=_timestamp(end))
        status = slurm.map_state(raw) if raw else "lost"
        fields.update(status=status, slurm_state=raw, error=raw if status == "failed" else None)
        if status == "running" and not fields.get("started_at"):
            fields["started_at"] = job["started_at"] or now()
        if status in {"succeeded", "failed", "cancelled", "lost"} and not fields.get("finished_at"):
            fields["finished_at"] = now()
        store.update(job["id"], **fields)
