# dicos CLI — Implementation Spec

`dicos` is a CLI for running jobs on ASGC DiCOS (Slurm) from a laptop. Its verbs mirror `lab`
(`~/.local/share/uv/tools/lab/lib/python3.13/site-packages/lab/`, see `cli.py`, `store.py`, `gitutil.py`,
`remote.py`). Distributed with `uvx --from git+<url> dicos` / `uv tool install`.

Reference prototype (login automation, QR/TOTP decode, tested and working): `~/research/dicos-login/src/dicos_login/`.

## Environment facts (verified 2026-10-13)

- Login node `slurm-ui.twgrid.org` (round robin slurm-ui01..03, AlmaLinux 9.5, 4 cores / 7 GiB). Accepts SSH only from
  allow-listed source IPs; a lab server (`idlab`, `idlab1`, `idlab2` in `~/.ssh/config`) must be used as ProxyJump.
  Jump hosts use key auth.
- Auth is keyboard-interactive, exact prompts in order:
  1. Banner lines, then `Choose your login method:` → answer `1` (Google Authenticator)
  2. `(user@slurm-ui.twgrid.org) Password:`
  3. `(user@slurm-ui.twgrid.org) Verification code:` → 6-digit TOTP
- More than 5 failed logins bans the source IP (the shared jump host). Never retry automatically. Stop on the first
  repeated prompt.
- The MOTD printed after login contains the text `password:` — output after auth must never be matched against prompts.
- Slurm: GPU request `--gres=gpu:N` (≤8/node). Partition names contain `al9` with inconsistent suffix order
  (e.g. `a100-al9`, `a100_short-al9`, `a100_long-al9`, `a100_devel-al9` (20 min), `v100-al9*`, `b6000-al9*`,
  `edr1-al9_short_serial` CPU). Some partitions have
  `AllowAccounts` that exclude the user's account (`idlab`). Always read partitions live from `sinfo`/`scontrol`.
- Account association: `sacctmgr -n show assoc user=$USER format=account -P`.
- Storage: `$HOME=/dicos_ui_home/<user>` (NFS, temporary, no integrity guarantee). Persistent: `/ceph/work/<group>/`.
- Login node has git, rsync, curl, python3 3.9; no uv/conda/tmux on PATH. `module` available
  (`cuda/12.x`, `anaconda3/2024.10-1`, `singularity/4.1.2`).
- Login node AND compute nodes have outbound internet (GitHub HTTPS+SSH, PyPI). No proxy.
- Usage is free for users; no cost display.

## Decisions

- D1 Typer + rich CLI, verbs mirror `lab`. No `--json` in v1.
- D2 All local state in `$DICOS_HOME` (default `~/.dicos`), dir mode 0700, every file 0600, created with
  `os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)` + atomic rename. Credentials refuse to load if the file mode is
  looser than 0600 (error message tells user to `chmod 600`). No OS keyring.
- D3 `dicos init` is an interactive wizard; also fully scriptable via flags for tests.
- D4 One SSH ControlMaster connection per user (ControlPersist 8h). Only the master performs 2FA; every other command
  multiplexes through it with plain `subprocess`/`os.execvp` of `ssh -S <sock>`, so stdin/tty work natively.
- D5 Jump host is chosen in `init` from `~/.ssh/config` Host aliases by TCP-probing `slurm-ui.twgrid.org:22` through
  each (`ssh -o BatchMode=yes <alias> 'timeout 6 bash -c "echo > /dev/tcp/slurm-ui.twgrid.org/22"'`), or `--jump`.
  A direct connection (no jump) is also a candidate.
- D6 Code shipping: default git mode (exact commit, cloned on the login node with SSH agent forwarding `-A`);
  `--sync` mode rsyncs the local working tree. Environment setup (e.g. `uv sync`) runs inside the job.

## Local layout (`$DICOS_HOME`)

| File | Content |
|---|---|
| `config.yaml` | `user`, `host` (default `slurm-ui.twgrid.org`), `jump` (alias or null), `workdir` (remote base dir), `defaults: {partition, time, gpus, cpus, mem}`, `projects: {<name>: {repo, prefix, setup}}` |
| `credentials.json` | `{"password": ..., "totp_secret": <base32>}` |
| `dicos.db` | SQLite jobs table |
| `cm.sock` | ControlMaster socket (ssh creates it; `ControlPath` = this path) |

Project resolution: current git repo's `origin` URL matched against `projects[*].repo`; if absent, auto-register
`name = repo basename`, `prefix = name`, `setup = null` (default setup, see below) and save. Prefix must match
`^[A-Za-z0-9_-]+$`.

## Remote layout (`workdir`, default `/ceph/work/<group>/<user>/dicos`, fallback `$HOME/dicos`)

- `repos/<project>/` — base clone (git mode).
- `jobs/<id>/src/` — git worktree (detached at commit) or rsync target.
- `jobs/<id>/job.sh` — generated sbatch script. `jobs/<id>/output.log` — combined stdout/stderr (`--output`, `--error` same file).

`<group>`: first existing+writable of `/ceph/work/<account>` (case-insensitive match against `ls /ceph/work`) for the
account from sacctmgr; else fallback.

## Job script (`job.sh`)

```bash
#!/bin/bash
#SBATCH --job-name=<id>
#SBATCH --partition=<partition>
#SBATCH --time=<HH:MM:SS or D-HH:MM:SS>
#SBATCH --cpus-per-task=<cpus>
#SBATCH --mem=<mem>              # omitted if not set
#SBATCH --gres=gpu:<gpus>        # omitted if gpus == 0
#SBATCH --output=<jobdir>/output.log
#SBATCH --error=<jobdir>/output.log
set -eo pipefail
export DICOS_JOB_ID=<id> PYTHONUNBUFFERED=1
export PATH="$HOME/.local/bin:$PATH"
cd <jobdir>/src
<setup>
<command>
```

Default setup when `setup` is null: if `pyproject.toml` exists, install uv to `$HOME/.local/bin` if `command -v uv`
fails (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then `uv sync`. Otherwise no setup. User command is inserted
verbatim (it was passed after `--`, joined with `shlex.join` when multiple argv, kept as-is when a single string).

## Job states

Local status ← Slurm state: `PENDING`/`REQUEUED`/`CONFIGURING` → `queued`; `RUNNING`/`COMPLETING` → `running`;
`COMPLETED` → `succeeded`; `FAILED`, `TIMEOUT`, `OUT_OF_MEMORY`, `NODE_FAIL`, `BOOT_FAIL`, `DEADLINE`, `PREEMPTED` →
`failed` (keep raw state in `error`); `CANCELLED*` → `cancelled`; job not found in squeue nor sacct → `lost`.
Terminal states are never updated again. Refresh: one `squeue` + one `sacct -j <ids> -P -n -o JobID,State,ExitCode,Start,End` per command invocation for all non-terminal jobs.

## SQLite `jobs` table

`id TEXT PK, project, seq INT, slurm_id TEXT, mode ('git'|'sync'), commit_sha, command, partition, gpus INT, cpus INT,
mem, time_limit, status, slurm_state, exit_code INT, error, remote_dir, created_at, started_at, finished_at`.
ID = `f"{prefix}-{seq}"`, seq = per-project `MAX(seq)+1`.

## Commands

| Command | Behaviour |
|---|---|
| `init [--user] [--jump ALIAS\|--no-jump] [--qr PATH] [--totp-secret-stdin] [--password-stdin] [--force] [--default-partition P] [--default-time T]` | Wizard: username → TOTP (secret / otpauth URI / Google Authenticator export URI, or QR image path; reuse `dicos_login.otp`) → password (getpass) → jump selection (D5) → write config + credentials → print current TOTP for comparison → open master (real login) → detect workdir → save → default partition (validated against partitions the account may use) and time limit from flags, or prompts on a TTY, saved to `defaults` → save. Refuses to overwrite existing config without `--force`. |
| `doctor` | Rows: local files & modes, jump reachable, master login, `sinfo` works, account association, workdir writable, GitHub reachable from login node. Exit 1 if any fail. |
| `status` | Table per partition the user's account may use (exclude partitions whose `AllowAccounts` doesn't contain the account, unless `ALL`): PARTITION, GRES, NODES idle/total, TIMELIMIT. `--all` shows excluded too. |
| `run [-p PART] [--gpus N] [--cpus N] [--mem 32G] [-t 6h] [--sync] [--branch/--commit/--tag] -- CMD` | Resolve project, ship code, write job.sh, `sbatch`, record job, print `<id> (slurm <n>) queued on <partition>`. Time accepts `30m`, `6h`, `2d`, `1d12h`, or raw Slurm format. |
| `sweep FILE.yaml [same resource flags]` | lab sweep format (`command` template with `{key}`, `grid`, optional resource keys). One Slurm array job (`--array=0-(N-1)`), job.sh picks the command by `$SLURM_ARRAY_TASK_ID`; one local record per element, `slurm_id = <arrayid>_<i>`, output `output_%a.log`. |
| `list [-s STATUS] [--all]` | Refresh, table: ID, STATUS, PARTITION, GPUS, SLURM, CREATED, COMMAND (latest 20 unless `--all`). |
| `show ID` | Refresh, all fields + `scontrol show job` reason when queued. |
| `logs ID [-n 200] [-f]` | `tail -n` / `tail -F` of output.log through the master. |
| `fetch ID PATH... [-o DEST]` | rsync (`--protect-args`, through the master) each PATH (relative to `jobs/<id>/src/`, or absolute) into DEST (default `./<id>/`). |
| `wait ID [--timeout S] [--interval 15]` | Exit 0 succeeded, 1 failed, 2 cancelled, 3 lost, 124 timeout. |
| `stop ID` | `scancel`; mark cancelled. |
| `clean ID [-y]` | Terminal jobs only: remove remote `jobs/<id>`, `git worktree prune` for git mode. |
| `ssh` | Interactive shell via master (`os.execvp`, `-t`). |
| `exec -- CMD` | Run command via master; stdin/stdout/exit code passthrough. |
| `code` | Print current TOTP. |
| `logout` | `ssh -O exit` on the master. |
| `update [--check]` | Same pattern as `lab update`. |
| `feedback [TITLE] [--body TEXT]` | Same pattern as `lab feedback`: `gh issue create --repo novis10813/dicos` with the dicos version appended; no TITLE opens gh's editor (TTY only). |

Any command needing the remote calls `ensure_master()` first: `ssh -S sock -O check` → if dead, open master
(prompts automated; requires credentials). Master command:
`ssh -M -N -f -o ControlPath=<sock> -o ControlPersist=8h -o ServerAliveInterval=60 -o PreferredAuthentications=keyboard-interactive,password -o NumberOfPasswordPrompts=1 -A [-J jump] user@host`.
Drive with pexpect until EOF (ssh forks to background after auth); success iff exit status 0 and `-O check` passes.
Prompt regexes as in the prototype (`dicos_login/ssh.py`), repeat-prompt abort kept, HOSTKEY prompt → error telling the
user to run `ssh user@host` once manually. Mux client commands: `ssh -S <sock> -o ControlMaster=no -o BatchMode=yes [-A] user@host -- <cmd>`.

## Not doing (v1)

`--json` output, Windows native, DiCOSApps/Jupyter, legacy PanDA `dicos job`, file browser commands beyond
`logs` and `fetch`, multi-cluster, automatic retries of failed logins, OS keyring.

## Slices (each: implement → spec-review → code-review)

- S1 Scaffold + local state: package `dicos` (src layout, Typer app, `dicos` entry point), `paths/config/credentials`
  with D2 permissions, `otp.py` ported with its tests, `init` steps that do not touch the network (flags + wizard,
  jump candidates may be passed explicitly; network parts stubbed behind a function to be filled by S2), `code`.
- S2 Connection: `conn.py` (`ensure_master`, `run`, `exec_interactive`, `close`), jump probing, `init` network steps
  (master + workdir detection), `ssh`, `exec`, `logout`, `doctor`.
- S3 Slurm core: pure `slurm.py` (time parsing, job.sh rendering, state mapping, sbatch/squeue/sacct/sinfo parsing) and
  `store.py`, with unit tests; `status` command.
- S4 Run (sync mode) + `list`, `show`, `logs`, `stop`.
- S5 Git mode shipping (+ `--branch/--commit/--tag`), `wait`, `clean`.
- S6 `sweep`, `update`, README (install, init, examples, safety notes).
