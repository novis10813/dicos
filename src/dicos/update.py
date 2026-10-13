"""Self-update: compare the installed git commit against the remote and reinstall."""

import json
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SCP_RE = re.compile(r"^([^@/]+)@([^@/]+):(.+)$")  # scp-like user@host:path


class UpdateError(Exception):
    pass


def is_uv_tool_install(prefix: Path | None = None) -> bool:
    """Only uv tool environments should be replaced with `uv tool install --force`."""
    return ((prefix or Path(sys.prefix)) / "uv-receipt.toml").is_file()


def installed_source() -> tuple[str, str | None] | None:
    """(git url, installed commit) from PEP 610 direct_url.json, or None if not installed from git."""
    try:
        raw = distribution("dicos").read_text("direct_url.json")
    except (PackageNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    vcs = data.get("vcs_info") or {}
    url = data.get("url")
    if vcs.get("vcs") != "git" or not url:
        return None
    if vcs.get("requested_revision"):
        raise UpdateError("git source is pinned to a revision; update it with your installer")
    return url, vcs.get("commit_id")


def uv_git_requirement(origin: str) -> str:
    """Turn git+..., ssh://, http(s):// or scp-like user@host:path into a uv requirement."""
    if origin.startswith("git+"):
        return origin
    if origin.startswith(("ssh://", "http://", "https://")):
        return f"git+{origin}"
    match = _SCP_RE.match(origin)
    if match:
        user, host, path = match.groups()
        return f"git+ssh://{user}@{host}/{path}"
    return f"git+{origin}"


def remote_head(url: str, timeout: float = 15.0) -> str:
    """Full SHA of the remote's HEAD via the system git and the user's ssh config."""
    git_url = url.removeprefix("git+")
    try:
        result = subprocess.run(["git", "ls-remote", git_url, "HEAD"], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"could not check {git_url}: {exc}") from exc
    if result.returncode:
        raise UpdateError(f"git ls-remote {git_url} failed: {result.stderr.strip() or f'exit {result.returncode}'}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise UpdateError(f"no commits found on {git_url}")
    sha = lines[0].split()[0]
    if not _SHA_RE.fullmatch(sha):
        raise UpdateError(f"unexpected git ls-remote output: {lines[0]!r}")
    return sha


def run_install(argv: list[str]) -> int:
    """Run the installer and return its exit code. Seam for tests."""
    return subprocess.run(argv).returncode
