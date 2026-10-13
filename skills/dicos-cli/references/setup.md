# Setup & Utilities

## Requirements

- SSH from an allow-listed source IP. In practice a lab server alias in `~/.ssh/config` with key auth (e.g. `idlab`), used as ProxyJump. A direct connection works only from allow-listed networks.
- DiCOS account with Google Authenticator 2FA: password plus the TOTP secret.
- Local `ssh`, `rsync`, `git`, and [uv](https://docs.astral.sh/uv).

## Install and update

```bash
uv tool install git+https://github.com/novis10813/dicos
uvx --from git+https://github.com/novis10813/dicos dicos --help   # one-off, no install
dicos update --check     # report installed vs remote HEAD
dicos update             # uv tool install --force --refresh <source>
dicos --version
```

`update` works only for a `uv tool` install from an unpinned git source. Editable checkouts are told to `git pull` instead.

## `dicos init`

Interactive steps, in order:

1. Username.
2. TOTP: base32 secret, `otpauth://totp/...` URI, Google Authenticator export URI (`otpauth-migration://...`), or press Enter and give a QR image path. Export QRs with several accounts: unsupported entries (non-TOTP, non-SHA1, non-6-digit) are skipped; an account whose issuer/name contains "dicos" is chosen automatically, otherwise you pick a number.
3. Password (hidden).
4. Jump host: every `~/.ssh/config` Host alias plus "none (direct)" is probed for TCP reachability of `slurm-ui.twgrid.org:22`, a table is shown, and you choose a reachable one. Probing is skipped while a login failure is latched.
5. Files are written (`credentials.json` first, then `config.yaml`), the latch is cleared, and the current TOTP code is printed so you can compare it with your authenticator app.
6. Real login (opens the master), then remote workdir detection: `/ceph/work/<group>/<user>/dicos` where `<group>` matches your Slurm account case-insensitively, else `$HOME/dicos`. Detection has a 180 s limit; on timeout the saved files stay and you can rerun `dicos init --force`.

`init` is run by the user in their own terminal; agents follow [authentication.md](authentication.md). Non-interactive form, for the user's own scripts with secrets from a secret store (stdin lines are read in this order: password, then TOTP):

```bash
printf '%s\n%s\n' "$PASSWORD" "$TOTP_URI" | \
  dicos init --user alice --jump idlab --password-stdin --totp-secret-stdin
dicos init --user alice --jump idlab --qr ~/Downloads/export.png   # password prompted
```

`init` refuses to overwrite an existing config without `--force`. `--jump` and `--no-jump` are mutually exclusive, as are `--qr` and `--totp-secret-stdin`.

## `dicos doctor`

Rows (exit 1 if any fails):

| Row | Checks |
|---|---|
| login latch | Only shown when latched; doctor stops there without attempting a login. |
| local files and modes | `~/.dicos` is 0700; `config.yaml`, `credentials.json` exist; every file is 0600. |
| jump reachable | The configured jump (or direct) reaches port 22. |
| master login | Master alive or opened now. |
| sinfo | `sinfo` works; detail shows the partition count. |
| account association | `sacctmgr` returns your Slurm account. |
| workdir writable | `test -w <workdir>`. |
| GitHub reachable | `git ls-remote https://github.com/git/git` from the login node. |

## Login master and the latch

- Any remote command first checks the master (`ssh -O check`). If it is dead (8 h ControlPersist expired, network change, reboot), dicos logs in again automatically using the stored credentials. That is one login attempt.
- The latch file `~/.dicos/login.failed` is written before every attempt and removed only on success. If it exists, no command logs in; they print the stored reason.
- Clear it with `dicos logout` (one new attempt allowed) or `dicos init --force` (new credentials). Fix the cause first: wrong password, wrong TOTP secret (compare `dicos code` with the app), clock skew, or an unreachable jump host.

## Utilities

```bash
dicos ssh                     # interactive login-node shell (tty)
dicos exec -- hostname        # single command; stdin/stdout/exit code pass through
dicos exec -- 'sacct -u $USER -S today'
dicos code                    # current 6-digit TOTP (waits if <3 s remain in the window)
dicos logout                  # ssh -O exit on the master; clears the latch
```

`exec` joins several argv words with shell quoting; a single argument is passed verbatim, so quote shell syntax as one string.
