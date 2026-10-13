"""Job submission, cleanup, and batched Slurm reconciliation."""

from datetime import datetime, timezone
from pathlib import Path
import re
import shlex
import subprocess

from dicos import config, conn, slurm

REFRESH_MARKER = "__DICOS_JOBS_SECTION__"
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


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


def git_ship_script(workdir, name, repo, jobdir, sha):
    """Login-node script: keep one base clone per project, add a detached worktree per job."""
    repos = workdir.rstrip("/") + "/repos"
    base = shlex.quote(f"{repos}/{name}")
    return f"""set -e
export GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
mkdir -p {shlex.quote(repos)} {shlex.quote(jobdir)}
exec 9>{shlex.quote(f"{repos}/.{name}.lock")}
flock 9
if [ -d {base}/.git ]; then
    git -C {base} fetch --quiet --tags --force origin
else
    git clone --quiet --no-checkout {shlex.quote(repo)} {base}
fi
git -C {base} worktree add --quiet --detach {shlex.quote(jobdir + "/src")} {shlex.quote(sha)}
"""


def submit(store, cfg, root, name, project, request, commands, mode, sha=None, array=False):
    """Ship code once, submit one job (or one array job), and record one row per command."""
    if not cfg.workdir:
        raise ValueError("Workdir missing: run dicos init.")
    if mode == "git" and not project.get("repo"):
        raise ValueError("git mode requires an origin remote; use --sync")
    ids = store.add_many(name, project["prefix"], [dict(
        mode=mode, commit_sha=sha, command=command, partition=request["partition"],
        gpus=request["gpus"], cpus=request["cpus"], mem=request["mem"],
        time_limit=request["time"]) for command in commands])
    jobdir = cfg.workdir.rstrip("/") + "/jobs/" + ids[0]
    for id in ids:
        store.update(id, remote_dir=jobdir)
    try:
        if mode == "git":
            result = conn.run(cfg, "bash -s", input=git_ship_script(cfg.workdir, name, project["repo"], jobdir, sha),
                              agent=True)
            if result.returncode:
                raise ValueError("git checkout on the login node failed: " + (
                    result.stderr.strip() or f"exit {result.returncode}"))
        else:
            checked_run(cfg, "mkdir -p " + shlex.quote(jobdir))
            result = subprocess.run(rsync_argv(cfg, root, jobdir), capture_output=True, text=True)
            if result.returncode:
                raise ValueError(result.stderr.strip() or f"rsync failed (exit {result.returncode}).")
        common = dict(jobdir=jobdir, src_dir=jobdir + "/src", setup=project.get("setup"), **request)
        if array:
            script = slurm.render_array_script(ids=ids, commands=commands, **common)
        else:
            script = slurm.render_job_script(id=ids[0], command=commands[0], **common)
        checked_run(cfg, "cat > " + shlex.quote(jobdir + "/job.sh"), input=script)
        slurm_id = slurm.parse_sbatch_parsable(checked_run(
            cfg, "sbatch --parsable " + shlex.quote(jobdir + "/job.sh")))
        for i, id in enumerate(ids):
            store.update(id, slurm_id=f"{slurm_id}_{i}" if array else slurm_id)
    except (ValueError, OSError) as exc:
        for id in ids:
            store.update(id, status="failed", error=str(exc), finished_at=now())
        raise
    return [store.get(id) for id in ids]


def log_path(job):
    """output.log, or output_<i>.log for array element <arrayid>_<i>."""
    if not job["remote_dir"]:
        raise ValueError(f"Job {job['id']} has no remote directory.")
    _, _, index = (job["slurm_id"] or "").partition("_")
    return job["remote_dir"] + (f"/output_{index}.log" if index else "/output.log")


def clean_command(store, cfg, job):
    """Remote command that removes a finished job's directory (shared by array elements)."""
    if job["status"] not in TERMINAL:
        raise ValueError(f"{job['id']} is {job['status']}; stop it or wait before cleaning.")
    jobdir = job["remote_dir"]
    if not jobdir:
        raise ValueError(f"Job {job['id']} has no remote directory.")
    active = [j["id"] for j in store.by_remote_dir(jobdir) if j["status"] not in TERMINAL]
    if active:
        raise ValueError(f"{jobdir} is shared with unfinished jobs: {', '.join(active)}")
    jobs_root = (cfg.workdir or "").rstrip("/") + "/jobs/"
    leaf = jobdir[len(jobs_root):]
    if not cfg.workdir or not jobdir.startswith(jobs_root) or not re.fullmatch(r"[A-Za-z0-9_-]+", leaf):
        raise ValueError(f"Refusing to remove {jobdir}: not a job directory under {jobs_root}")
    command = "rm -rf " + shlex.quote(jobdir)
    if job["mode"] == "git":
        base = shlex.quote(cfg.workdir.rstrip("/") + "/repos/" + job["project"])
        command += f" && if [ -d {base} ]; then git -C {base} worktree prune; fi"
    return command


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
        command = (f"squeue -h -j {quoted} -r -o '%i|%T' 2>/dev/null || true; "
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
        if status in TERMINAL and not fields.get("finished_at"):
            fields["finished_at"] = now()
        store.update(job["id"], **fields)
