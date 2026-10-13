# Configuration

## Local state (`$DICOS_HOME`, default `~/.dicos`)

Directory mode 0700, every file 0600, written atomically.

| File | Content |
|---|---|
| `config.yaml` | Settings below. Safe to edit by hand. |
| `credentials.json` | `{"password": ..., "totp_secret": <base32>}`. Refuses to load if group/other can read it (`chmod 600`). Never share or print. |
| `dicos.db` | SQLite `jobs` table (local job records). |
| `cm.sock` | ControlMaster socket. |
| `master.lock` | Serializes logins across concurrent commands. |
| `login.failed` | Present only after a failed or interrupted login (the latch). |

Use `DICOS_HOME=/path dicos ...` for an isolated profile (e.g. tests or a second account).

## `config.yaml`

```yaml
user: alice                    # DiCOS username
host: slurm-ui.twgrid.org      # login node (round robin slurm-ui01..03)
jump: idlab                    # ~/.ssh/config alias used as ProxyJump, or null for direct
workdir: /ceph/work/IDLAB/alice/dicos   # remote base directory, detected by init
defaults:                      # optional; used when flags and sweep keys are absent
  partition: a100-al9
  time: 6h
  gpus: 1
  cpus: 8
  mem: 64G
projects:
  my-project:
    repo: git@github.com:alice/my-project.git   # must equal `git remote get-url origin`
    prefix: exp                                 # job IDs exp-1, exp-2, ... ; [A-Za-z0-9_-]+
    setup: null                                 # null = default uv setup, "" = none, or shell code
```

Project resolution on `run`/`sweep`:

1. Inside a git repo, `origin` is matched against `projects.*.repo`.
2. No match: the project is auto-registered as `name = repo basename` (without `.git`), `prefix = name` with other characters replaced by `_`, `setup: null`, and saved.
3. Outside git (only with `--sync`): the current directory's basename, `repo: null`.

Rename a prefix or set `setup` by editing the entry. Sequence numbers are per project (`MAX(seq)+1`).

## Remote layout (`workdir`)

```
<workdir>/
  repos/<project>/          base clone (git mode), plus .<project>.lock
  jobs/<id>/
    src/                    git worktree at the commit, or the rsync target; job cwd
    job.sh                  generated sbatch script
    output.log              stdout+stderr (array jobs: output_<i>.log)
```

Storage notes: `/ceph/work/<group>/` is persistent; `$HOME` (`/dicos_ui_home/<user>`) is temporary NFS without integrity guarantees. The login node has git, rsync, curl, python3.9, `module`; uv is installed per job into `~/.local/bin` when needed.

## `sweep.yaml`

| Key | Type | Required |
|---|---|---|
| `command` | string with `{name}` placeholders | yes |
| `grid` | mapping name -> non-empty list | when placeholders are used |
| `partition`, `mem`, `time` | string | no |
| `gpus` | integer >= 0 | no |
| `cpus` | integer >= 1 | no |

Unknown keys are errors.
