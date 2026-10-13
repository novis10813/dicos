# Troubleshooting

Start with `dicos doctor`. It never logs in while the latch is set, but it does log in when the master is down. Agents handle every login problem with the user as described in [authentication.md](authentication.md).

## Login and connection

| Message / symptom | Cause | Action |
|---|---|---|
| `The previous login failed: <reason>` | Latch from an earlier failure | Read the reason, fix it, then `dicos logout` once (or `dicos init --force`). Do not loop: 5 failures ban the jump host IP. |
| `previous answer rejected` | Server repeated the password or code prompt | Wrong password or TOTP. Compare `dicos code` with your app; fix with `dicos init --force`. |
| `Server rejected the login.` | Authentication denied | Same as above; also check the account is active. |
| `Login timed out (90s).` | Jump host or login node unreachable or slow | `dicos doctor` (jump reachable row). Check VPN/network, then `dicos logout` once. |
| `Unknown host key. Run ... once manually` | First connection to the login node | The user runs the printed ssh command, verifies and accepts the key, and aborts at the login menu. Then `dicos logout` once, with their agreement. |
| `Credentials permissions are unsafe. Run chmod 600 ...` | File mode too open | Run the printed `chmod`. |
| `Configuration missing. Run dicos init first.` / `Credentials missing...` | Not initialized or wrong `DICOS_HOME` | `dicos init`. |
| `Workdir missing: run dicos init.` | Workdir detection never finished | `dicos init --force`, or set `workdir:` in `config.yaml`. |
| `Work directory detection timed out (180s)` | Slow `sacctmgr` or `/ceph` | Retry `dicos init --force` later; saved config stays. |
| `jump reachable` FAIL in doctor | Jump host down or not allow-listed | Pick another alias: `dicos init --force --jump <alias>`. |

## Submission

| Message | Action |
|---|---|
| `Partition required: use --partition or set defaults.partition.` | Add `-p`, a sweep `partition:`, or `defaults.partition`. |
| `Invalid time: '...'` | Use `30m`, `6h`, `2d`, `1d12h`, or Slurm format. |
| `... is not a git repository; use --sync` | Run inside a repo or use `--sync`. |
| `git mode requires an origin remote; use --sync` | `git remote add origin ...` or `--sync`. |
| `working tree is dirty; commit first or use --sync` | Commit (untracked files count, e.g. a new `sweep.yaml`), stash, use `--sync`, or pass an explicit target. |
| `HEAD is not on any remote branch; push first` | `git push`. |
| `selected commit is not published on origin; push its branch or tag` | Push the branch/tag that contains it. |
| `--branch, --commit and --tag select a git commit; drop --sync.` | Choose one shipping mode. |
| `git checkout on the login node failed: ... Permission denied (publickey)` | Load a key with repo access into your local agent: `ssh-add`, check `ssh-add -l`. |
| `git checkout on the login node failed: ... not our ref / reference is not a tree` | The commit is not on origin as seen from the login node; push and retry. |
| `rsync ...` errors | Check local rsync and disk; `dicos exec -- df -h <workdir>`. |
| sbatch `Invalid account or account/partition combination specified` | Partition excludes your account; choose one from `dicos status`. |
| sbatch `Requested time limit is invalid` | `-t` exceeds the partition limit (see TIMELIMIT in `dicos status`). |
| sbatch `Requested node configuration is not available` | Too many GPUs/CPUs/memory for one node; at most 8 GPUs per node. |
| `Project name 'x' already registered for another repository.` | Two repos share a basename; rename one entry in `config.yaml` projects (keep its `repo`). |

Failed submissions are kept as `failed` records with the error in `dicos show ID`; `dicos clean ID -y` removes any partial remote directory.

## Running jobs

| Symptom | Diagnosis | Action |
|---|---|---|
| Stays `queued` | `dicos show ID` → Reason | `Resources`/`Priority`: wait or switch partition (`dicos status` idle counts). `QOSMax...`: per-user limit, wait for your other jobs. `ReqNodeNotAvail`: maintenance. |
| `failed` quickly | `dicos logs ID -n 100` | Usually setup: `uv sync` errors, missing `pyproject.toml` dependencies, or `set -eo pipefail` stopping at the first failing command. |
| `failed` with error `TIMEOUT` | Hit `-t` | Raise `-t` (within the partition limit) or checkpoint. |
| `failed` with error `OUT_OF_MEMORY` | Exceeded `--mem` (or the default) | Raise `--mem`. |
| `lost` | Not in squeue or sacct (accounting purged or never registered) | Check `dicos logs ID`; resubmit. |
| `logs` shows nothing | Job not started, or output buffered | Wait for `running`; Python is unbuffered via `PYTHONUNBUFFERED=1`, other tools may need line buffering. |
| `logs`: `No such file` | Not started yet, or cleaned | `dicos show ID`. |
| GPU not visible | `nvidia-smi` missing in job | Confirm `--gpus N` was set (`show` → gpus) and a GPU partition was used. |

## Clean

| Message | Action |
|---|---|
| `<id> is queued/running; stop it or wait before cleaning.` | `dicos stop ID` or `dicos wait ID`. |
| `... is shared with unfinished jobs: ...` | Array siblings still active; wait or stop them. |
| `Refusing to remove ...` | Record points outside `<workdir>/jobs/` (e.g. workdir changed). Remove manually with `dicos exec` after checking the path. |

## Manual inspection

```bash
dicos exec -- 'squeue -u $USER -o "%i %T %P %b %R"'
dicos exec -- 'sacct -u $USER -S today -o JobID,State,ExitCode,Elapsed,AllocTRES -P'
dicos exec -- 'scontrol show job <slurm_id>'
dicos exec -- 'cat <workdir>/jobs/<id>/job.sh'
```
