# Authentication Handoff (agents)

How an agent works with the user on DiCOS login. Read this before running any dicos command that can reach the cluster.

## Why it matters

- Login is password + TOTP through a shared lab jump host. More than 5 failed logins ban the jump host's IP, which blocks DiCOS access for everyone who uses that host, not only this user.
- dicos stores the password and TOTP secret in `~/.dicos/credentials.json` and logs in automatically whenever the SSH master is missing. So ordinary commands (`status`, `run`, `list`, `wait`, `doctor`, ...) can trigger a login attempt.
- After a failed attempt dicos writes `~/.dicos/login.failed` (the latch) and refuses further logins until a human clears it.

## Hard rules

1. Never ask for, accept, type, echo, or store the password, the TOTP secret, an `otpauth://` or `otpauth-migration://` URI, or a QR image of one. If the user pastes one into the chat, do not repeat it, and suggest rotating it if the chat is shared or logged.
2. Never read, print, copy, or edit `~/.dicos/credentials.json`.
3. Never construct `dicos init --password-stdin` / `--totp-secret-stdin` pipelines. They exist for the user's own scripts with secrets from a secret store.
4. Never run `dicos code` unless the user asks. Its output is a live login code.
5. Never retry a failed login, delete `login.failed` by hand, or run `dicos logout` / `dicos init --force` to "try again" without the user's go-ahead.

## Who runs what

| Action | Who | Notes |
|---|---|---|
| `dicos init`, `dicos init --force` | User, in their own terminal | Hidden prompts need a real TTY. Agent shells usually have none. |
| First-time host key check (`ssh -J <jump> <user>@slurm-ui.twgrid.org`) | User | They verify the fingerprint, then abort at the login menu. |
| `ssh-add` (private GitHub repos in git mode) | User | May prompt for a key passphrase. |
| `dicos ssh` | User | Interactive shell. |
| `dicos logout` while latched | Agent, only after the user agrees | Allows exactly one new login attempt. |
| Any remote command while the master is down | Agent, after telling the user | It performs one automated login. |
| Remote commands while the master is alive | Agent | `status`, `run`, `sweep`, `list`, `show`, `logs`, `wait`, `stop`, `clean`, `exec`, `doctor`. |
| `chmod 600 ~/.dicos/credentials.json` (when dicos asks for it) | Agent | Changes the mode only, never the content. |

## Pre-flight check (never logs in)

```bash
H="${DICOS_HOME:-$HOME/.dicos}"
test -f "$H/config.yaml" || echo "not configured"
test -e "$H/login.failed" && cat "$H/login.failed"   # timestamp + reason, secrets redacted
ssh -o ControlPath="$H/cm.sock" -O check x 2>/dev/null && echo "master alive"
```

Decision:

1. **Not configured**: ask the user to run `dicos init` in their terminal (template T1). Stop.
2. **Latched**: do not run remote commands. Report the reason and the fix (table below). Wait for the user.
3. **Master alive**: proceed.
4. **Master down, not latched**: tell the user that the next command logs in once with the stored credentials (template T2). If the user already approved remote work in this session, saying so is enough; otherwise wait for a yes.

`dicos doctor` is safe while latched (it stops before logging in) but logs in when the master is down, so it follows rule 4.

## Latch reasons and what to tell the user

| `login.failed` reason contains | Meaning | Ask the user to |
|---|---|---|
| `previous answer rejected`, `Server rejected the login` | Wrong password or TOTP secret | Run `dicos code` in their terminal and compare it with the authenticator app. If the codes differ or the password changed, run `dicos init --force`. |
| `Login timed out`, `Connection closed without an active master` | Network, VPN, or jump host problem | Check that `ssh <jump>` works, then tell you to run `dicos logout` (one new attempt). |
| `Unknown host key` | First connection or changed host key | Run the printed `ssh` command, verify the key, abort at the menu, then approve `dicos logout`. |
| `login attempt in progress or interrupted` | A login was killed midway, or one is running | Confirm no other dicos command is running, then approve `dicos logout`. |
| Anything else | Unknown | Show the reason verbatim and ask how to proceed. |

## Message templates

Keep messages short and give one concrete action.

- **T1 not configured**: "dicos is not set up on this machine. Please run `dicos init` in your own terminal. It asks for your DiCOS username, the TOTP secret or QR export, and your password. I cannot type those for you. Tell me when it finishes."
- **T2 login needed**: "The DiCOS SSH session has expired. My next command will log in once using your saved credentials. OK to go ahead?"
- **T3 latched**: "DiCOS login is blocked after a failed attempt: `<reason>`. Retrying blindly risks the 5-failure IP ban on the shared jump host. Please `<fix from table>`, then tell me and I will continue."
- **T4 host key**: "This is the first connection to slurm-ui.twgrid.org from this machine. Please run `<printed ssh command>` in your terminal, check the host key, and press Ctrl-C at the login menu. Then I can run `dicos logout` to allow one new attempt."
- **T5 ssh-agent**: "The login node could not clone your private repo because no SSH key was forwarded. Please run `ssh-add` in your terminal and check `ssh-add -l`, then I will resubmit."
