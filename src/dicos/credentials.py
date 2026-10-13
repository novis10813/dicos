"""Private credentials. Secret values are never represented in diagnostics."""

import json
import stat
from dataclasses import asdict, dataclass, field

from dicos.paths import ensure_home, home, write_private


@dataclass
class Credentials:
    password: str = field(repr=False)
    totp_secret: str = field(repr=False)


def load() -> Credentials:
    path = home() / "credentials.json"
    if not path.exists():
        raise ValueError("Credentials missing. Run `dicos init` first.")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"Credentials permissions are unsafe. Run `chmod 600 {path}`.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        creds = Credentials(**data)
        if not isinstance(creds.password, str) or not isinstance(creds.totp_secret, str):
            raise ValueError
        return creds
    except (ValueError, TypeError):
        raise ValueError("Invalid credentials.json. Run `dicos init --force`.") from None


def save(creds: Credentials) -> None:
    write_private(ensure_home() / "credentials.json", json.dumps(asdict(creds)) + "\n")
