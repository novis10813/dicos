"""Private local state paths and atomic writes."""

import os
from pathlib import Path
from uuid import uuid4


def home() -> Path:
    return Path(os.environ.get("DICOS_HOME", "~/.dicos")).expanduser()


def ensure_home() -> Path:
    path = home()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
