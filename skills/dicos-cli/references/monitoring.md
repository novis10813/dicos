# Monitoring & Control

## Job states

| dicos status | Slurm states |
|---|---|
| `queued` | `PENDING`, `REQUEUED`, `CONFIGURING` |
| `running` | `RUNNING`, `COMPLETING` |
| `succeeded` | `COMPLETED` |
| `failed` | `FAILED`, `TIMEOUT`, `OUT_OF_MEMORY`, `NODE_FAIL`, `BOOT_FAIL`, `DEADLINE`, `PREEMPTED` (raw state in `error`); also submission errors (error text in `error`) |
| `cancelled` | `CANCELLED*`, or `dicos stop` |
| `lost` | Found in neither `squeue` nor `sacct` |

`succeeded`, `failed`, `cancelled`, `lost` are terminal and never updated again.

Refresh: `list`, `show`, and `wait` run one `squeue` + one `sacct` for all queued/running jobs per invocation. When nothing is active, `list` and `show` do not contact the cluster at all.

## `dicos status [--all]`

Table of partitions your Slurm account may use: PARTITION, GRES (e.g. `gpu:8`), NODES idle/total, TIMELIMIT. `--all` also lists partitions whose `AllowAccounts` excludes you, with an ALLOWED column. Idle nodes 0 means new jobs will queue.

## `dicos list [-s STATUS] [--all]`

Columns: ID, STATUS, PARTITION, GPUS, SLURM, CREATED, COMMAND (truncated). Latest 20 by default; `--all` for everything; `-s queued|running|succeeded|failed|cancelled|lost` filters.

## `dicos show ID`

Every stored field: project, seq, slurm_id, mode, commit_sha, command, partition, gpus, cpus, mem, time_limit, status, slurm_state, exit_code, error, remote_dir, created/started/finished timestamps. For a queued job it adds Slurm's `Reason` (`Resources`, `Priority`, `QOSMaxGRESPerUser`, `ReqNodeNotAvail`, ...).

## `dicos logs ID [-n 200] [-f]`

`tail -n N` (or `tail -F` with `-f`) of `<jobdir>/output.log` (array element: `output_<i>.log`) through the master. stdout and stderr are combined. The file appears when the job starts, so a queued job has no log yet. Ctrl-C ends following, not the job.

## `dicos wait ID [--timeout S] [--interval 15]`

Refreshes every `interval` seconds until the job is terminal, then prints `<id> <status>` (plus the raw state for failures).

| Exit | Meaning |
|---|---|
| 0 | succeeded |
| 1 | failed (also: unknown ID, remote or login error) |
| 2 | cancelled |
| 3 | lost |
| 124 | timeout reached (job keeps running) |

The exit code is the job outcome, not your script's exit code (see `exit_code` in `show`). Each round re-checks the master and logs in again if it expired, so long waits survive the 8 h master lifetime, but a latched failure stops the wait with exit 1.

```bash
dicos run --sync -p edr1-al9_short_serial -t 10m -- python3 job.py
dicos wait job-3 --timeout 900; case $? in 0) echo ok;; 124) echo still running;; *) dicos logs job-3 -n 50;; esac
```

## `dicos stop ID`

Queued or running: `scancel <slurm_id>` (array element: `<arrayid>_<i>`), then the job is marked `cancelled`. Already terminal: prints `already finished` and exits 0.

## `dicos clean ID [-y]`

- Only terminal jobs; queued/running jobs exit 1.
- Asks for confirmation unless `-y`.
- Removes `<workdir>/jobs/<id>/` (logs included). Git mode then runs `git worktree prune` in the project's base clone.
- Array jobs share one directory: refused until every element is terminal.
- Refuses any path that is not directly under `<workdir>/jobs/`.
- The local record stays; `logs` will fail afterwards.

## Resource release

Slurm frees CPUs, memory and GPUs when the job ends (completed, failed, timeout, or `scancel`). Nothing else holds compute resources: the SSH master only connects to the login node. To verify:

```bash
dicos exec -- 'squeue -u $USER'                         # nothing of yours queued/running
dicos exec -- 'sacct -j <slurm_id> -o JobID,State,AllocTRES,NodeList -P'
```
