# Job Submission

## `dicos run`

```
dicos run [-p PARTITION] [--gpus N] [--cpus N] [--mem 32G] [-t TIME]
          [--sync | --branch NAME | --tag NAME | --commit SHA] -- CMD...
```

- `--` is required before the command.
- One argument after `--` is used verbatim as shell code: `-- 'a && b | c'`. Several arguments are joined with shell quoting: `-- python train.py --name "exp 1"` stays three words plus one quoted word.
- Prints `<id> (slurm <n>) queued on <partition>` and exits 0 once `sbatch` accepted the job.
- On a submission error (checkout, rsync, upload, sbatch) the job is recorded as `failed` with the error text and the command exits 1.

### Resources

| Flag | Default | Slurm |
|---|---|---|
| `-p/--partition` | `defaults.partition`; required | `--partition` |
| `--gpus N` | `defaults.gpus` or 0 | `--gres=gpu:N` (omitted for 0; max 8 per node) |
| `--cpus N` | `defaults.cpus` or 1 | `--cpus-per-task` |
| `--mem 32G` | `defaults.mem` or none | `--mem` (omitted if unset) |
| `-t/--time` | `defaults.time` or `1h` | `--time` |

Time: `90s`, `30m`, `6h`, `2d`, `1d12h`, or raw Slurm (`06:00:00`, `1-00:00:00`, `30` minutes). The partition's own limit still applies (`a100_devel-al9` = 20 min).

Choosing a partition: run `dicos status`. GPU families: `a100*`, `v100*`, `b6000*`; suffix variants `_short` (6 h), `_long` (7 d), `_devel` (a100: 20 min). CPU: `edr1-al9_*`, `edr2-al9_*`, `intel-g4-al9_*`, `global`, `bigmemory_hdr1`.

## Code shipping

### Git mode (default)

1. dicos resolves the commit locally, before any login:
   - No target flag: the working tree must be clean (untracked files count) and HEAD must be on a remote-tracking branch of `origin`.
   - `--branch NAME` / `--tag NAME` (exact local ref) / `--commit SHA` (full or unique prefix): the tree may be dirty; dicos fetches origin into a temporary namespace and checks the commit is contained in some origin branch or tag, then deletes the temporary refs. Only one target flag at a time.
   - An `origin` remote is required.
2. On the login node, with your local ssh-agent forwarded: `<workdir>/repos/<project>` is cloned once (later: `git fetch --tags --force origin`), then `git worktree add --detach <workdir>/jobs/<id>/src <sha>`. GitHub's SSH host key is accepted on first use. Concurrent submissions of one project are serialized with a lock.
3. Private repos need a key loaded in your local agent (`ssh-add -l`).

### Sync mode (`--sync`)

- `rsync -az --delete` of the project root (git toplevel, or the current directory outside git) to `<workdir>/jobs/<id>/src/` through the master.
- Excludes `.git/`, `.venv/`, `__pycache__/`, and anything matched by `.gitignore` files.
- Uncommitted and untracked files are included. `--branch/--tag/--commit` are rejected with `--sync`.
- Large data directories get copied unless ignored. Keep datasets on `/ceph/work` and read them by path.

## Job environment

The generated `<workdir>/jobs/<id>/job.sh`:

```bash
#!/bin/bash
#SBATCH --job-name=<id>
#SBATCH --partition=... --time=... --cpus-per-task=... [--mem=...] [--gres=gpu:N]
#SBATCH --output=<jobdir>/output.log   (stderr goes to the same file)
set -eo pipefail
export DICOS_JOB_ID=<id> PYTHONUNBUFFERED=1
export PATH="$HOME/.local/bin:$PATH"
cd <jobdir>/src
<setup>
<command>
```

Setup (per project, `projects.<name>.setup` in `config.yaml`):

- `null` (default): if `pyproject.toml` exists, install uv to `~/.local/bin` when missing, then `uv sync`.
- `""`: no setup.
- Any string: inserted verbatim, e.g. `module load cuda/12.4 && uv sync`.

Compute nodes have internet access (GitHub, PyPI). `module` provides `cuda/12.x`, `anaconda3/2024.10-1`, `singularity/4.1.2`.

## `dicos sweep`

```
dicos sweep FILE.yaml [-p ...] [--gpus N] [--cpus N] [--mem ...] [-t ...] [--sync | --branch | --tag | --commit]
```

File format:

```yaml
command: uv run python train.py --lr {lr} --seed {seed}   # required; {name} placeholders
grid:                                                     # name -> non-empty list
  lr: ["1e-4", "5e-5"]
  seed: [1, 2, 3]
partition: a100-al9      # optional resource keys: partition, gpus, cpus, mem, time
gpus: 1
time: 6h
```

Rules:

- Commands are the cartesian product of `grid` in YAML key order (last key varies fastest).
- Every placeholder must be in `grid` and every grid key must be used. Positional `{0}` is rejected. Other keys are rejected.
- Values are inserted as text: quote them in the template if they may contain spaces or shell characters.
- Precedence: CLI flag > sweep file key > `config.yaml` defaults > built-in defaults.

Behaviour:

- Code is shipped once. One `sbatch --array=0-(N-1)` job; element `i` runs command `i` with `DICOS_JOB_ID` set to its own ID.
- Local records get consecutive IDs (`proj-7`, `proj-8`, ...); `slurm_id` is `<arrayid>_<i>`. All share the directory `<workdir>/jobs/<first id>/`; logs are `output_<i>.log`.
- Setup runs in every element but is serialized with `flock`, since elements share `src/`.
- `list/show/logs/wait/stop` work per element. `clean` removes the shared directory only once every element is finished.
