"""YAML configuration."""

from dataclasses import asdict, dataclass, field

import yaml

from dicos.paths import ensure_home, home, write_private


@dataclass
class Config:
    user: str
    host: str = "slurm-ui.twgrid.org"
    jump: str | None = None
    workdir: str | None = None
    defaults: dict = field(default_factory=dict)
    projects: dict = field(default_factory=dict)


def load() -> Config:
    path = home() / "config.yaml"
    if not path.exists():
        raise ValueError("Configuration missing. Run `dicos init` first.")
    try:
        return Config(**yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, TypeError):
        raise ValueError("Invalid config.yaml.") from None


def save(cfg: Config) -> None:
    write_private(ensure_home() / "config.yaml", yaml.safe_dump(asdict(cfg), sort_keys=False))
