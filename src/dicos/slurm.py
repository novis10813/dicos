"""Pure Slurm script rendering and output parsers."""

import re
import shlex


def parse_time(value: str) -> str:
    """Convert unit durations, preserving valid raw Slurm time formats."""
    if re.fullmatch(r"(?:\d+-)?\d+(?::\d{1,2}(?::\d{1,2})?)?", value):
        return value
    match = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if not match or not any(match.groups()):
        raise ValueError(f"Invalid time: {value!r}")
    days, hours, minutes, seconds = (int(v or 0) for v in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days}-{clock}" if days else clock


_DEFAULT_SETUP = """if [ -f pyproject.toml ]; then
    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    uv sync
fi"""


def _render(name: str, partition: str, time: str, cpus: int, mem: str | None, gpus: int,
            output: str, header: list[str], exports: list[str], src_dir: str,
            setup: list[str], body: list[str]) -> str:
    lines = ["#!/bin/bash", f"#SBATCH --job-name={name}",
             f"#SBATCH --partition={partition}", f"#SBATCH --time={parse_time(time)}",
             f"#SBATCH --cpus-per-task={cpus}"]
    if mem is not None:
        lines.append(f"#SBATCH --mem={mem}")
    if gpus:
        lines.append(f"#SBATCH --gres=gpu:{gpus}")
    output = shlex.quote(output)
    lines.extend([*header, f"#SBATCH --output={output}", f"#SBATCH --error={output}",
                  "set -eo pipefail", *exports,
                  'export PATH="$HOME/.local/bin:$PATH"', f"cd {shlex.quote(src_dir)}",
                  *setup, *body])
    return "\n".join(lines) + "\n"


def _setup(setup: str | None) -> list[str]:
    setup = _DEFAULT_SETUP if setup is None else setup
    return [setup] if setup else []


def render_job_script(id: str, partition: str, time: str, cpus: int,
                      mem: str | None, gpus: int, jobdir: str,
                      setup: str | None, command: str, src_dir: str) -> str:
    return _render(id, partition, time, cpus, mem, gpus, f"{jobdir}/output.log", [],
                   [f"export DICOS_JOB_ID={shlex.quote(id)} PYTHONUNBUFFERED=1"],
                   src_dir, _setup(setup), [command])


def render_array_script(ids: list[str], partition: str, time: str, cpus: int,
                        mem: str | None, gpus: int, jobdir: str,
                        setup: str | None, commands: list[str], src_dir: str) -> str:
    """One Slurm array job; element i runs commands[i] as job ids[i]."""
    if not ids or len(ids) != len(commands):
        raise ValueError("Array jobs need one ID per command.")
    exports = ["ids=(" + " ".join(shlex.quote(id) for id in ids) + ")",
               "commands=(", *(shlex.quote(command) for command in commands), ")",
               'export DICOS_JOB_ID="${ids[$SLURM_ARRAY_TASK_ID]}" PYTHONUNBUFFERED=1']
    setup = _setup(setup)
    if setup:
        # Elements share src/, so serialize setup such as `uv sync`.
        setup = [f"exec 9>{shlex.quote(jobdir + '/.setup.lock')}", "flock 9", *setup,
                 "flock -u 9", "exec 9>&-"]
    return _render(ids[0], partition, time, cpus, mem, gpus, f"{jobdir}/output_%a.log",
                   [f"#SBATCH --array=0-{len(ids) - 1}"], exports, src_dir, setup,
                   ['eval "${commands[$SLURM_ARRAY_TASK_ID]}"'])


def expand_array_ids(job_id: str) -> list[str]:
    """Expand Slurm's compact pending-array form, e.g. 12_[0-3,5%2]."""
    match = re.fullmatch(r"(\d+)_\[([\d,\-]+)(?:%\d+)?\]", job_id)
    if not match:
        return [job_id]
    ids = []
    for part in match.group(2).split(","):
        start, _, end = part.partition("-")
        ids.extend(f"{match.group(1)}_{i}" for i in range(int(start), int(end or start) + 1))
    return ids


def map_state(raw: str) -> str:
    state = re.sub(r" by \d+$", "", raw.strip()).rstrip("+")
    if state.startswith("CANCELLED"):
        return "cancelled"
    if state in {"PENDING", "REQUEUED", "CONFIGURING"}:
        return "queued"
    if state in {"RUNNING", "COMPLETING"}:
        return "running"
    if state == "COMPLETED":
        return "succeeded"
    if state in {"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED"}:
        return "failed"
    return "lost"


def parse_sbatch_parsable(text: str) -> str:
    job_id = text.strip().split(";", 1)[0]
    if not re.fullmatch(r"\d+", job_id):
        raise ValueError("Invalid sbatch job ID")
    return job_id


def parse_squeue(text: str) -> dict[str, str]:
    jobs = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        job_id, state = line.strip().split("|", 1)
        for id in expand_array_ids(job_id.strip()):
            jobs[id] = state.strip()
    return jobs


def parse_sacct(text: str) -> dict[str, tuple[str, int, str, str]]:
    jobs = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        job_id, state, code, start, end = line.strip().split("|")[:5]
        if "." in job_id:
            continue
        for id in expand_array_ids(job_id):
            jobs[id] = (state, int(code.split(":", 1)[0]), start, end)
    return jobs


def parse_partitions(text: str) -> list[dict]:
    partitions = []
    for line in text.splitlines():
        fields = dict(re.findall(r"(\w+)=([^\s]*)", line))
        if "PartitionName" not in fields:
            continue
        entry = {"name": fields["PartitionName"],
                 "allow_accounts": fields.get("AllowAccounts", "ALL").split(","),
                 "max_time": fields.get("MaxTime", ""), "state": fields.get("State", "")}
        for source, key in (("TRES", "tres"), ("Gres", "gres"), ("GRES", "gres")):
            if source in fields:
                entry[key] = fields[source]
        partitions.append(entry)
    return partitions


def parse_sinfo(text: str) -> dict[str, dict]:
    partitions = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        name, gres, counts, timelimit = (v.strip() for v in line.split("|"))
        allocated, idle, other, total = (int(v) for v in counts.split("/"))
        partitions[name.rstrip("*")] = {
            "gres": gres, "nodes": {"allocated": allocated, "idle": idle, "other": other, "total": total},
            "timelimit": timelimit,
        }
    return partitions
