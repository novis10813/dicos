"""Sweep files: one command template x a parameter grid -> one Slurm array job."""

import itertools
import string

RESOURCE_KEYS = ("partition", "gpus", "cpus", "mem", "time")
SWEEP_KEYS = frozenset({"command", "grid", *RESOURCE_KEYS})


class SweepError(ValueError):
    pass


def load_sweep(data) -> tuple[list[str], dict]:
    """Return the expanded commands and the resource keys present in the file."""
    if not isinstance(data, dict):
        raise SweepError("sweep file must be a mapping")
    unknown = sorted(set(data) - SWEEP_KEYS)
    if unknown:
        raise SweepError(f"unknown key(s): {', '.join(map(str, unknown))}")
    resources = {}
    for key in ("gpus", "cpus"):
        if key in data:
            value = data[key]
            if type(value) is not int or value < (0 if key == "gpus" else 1):
                raise SweepError(f"{key} must be an integer >= {0 if key == 'gpus' else 1}, got {value!r}")
            resources[key] = value
    for key in ("partition", "mem", "time"):
        if key in data:
            value = data[key]
            if type(value) not in (str, int) or not str(value).strip():
                raise SweepError(f"{key} must be a nonempty string, got {value!r}")
            resources[key] = str(value)
    return expand_sweep(data), resources


def expand_sweep(spec: dict) -> list[str]:
    """Cartesian product of `grid`, in YAML key order, formatted into `command`."""
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        raise SweepError("command is required")
    grid = spec.get("grid") or {}
    if not isinstance(grid, dict):
        raise SweepError("grid must be a mapping of name -> list of values")
    for key, values in grid.items():
        if not isinstance(values, list) or not values:
            raise SweepError(f"grid.{key} must be a non-empty list")
    used = _placeholders(command)
    missing = used - set(grid)
    if missing:
        raise SweepError(f"command uses keys missing from grid: {', '.join(sorted(missing))}")
    unused = set(grid) - used
    if unused:
        # Would only produce duplicate jobs.
        raise SweepError(f"grid keys not used in command: {', '.join(sorted(unused))}")
    keys = list(grid)
    return [command.format(**dict(zip(keys, combo))) for combo in itertools.product(*grid.values())]


def _placeholders(template: str) -> set[str]:
    names = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise SweepError(f"invalid command template: {exc}") from None
    for _, field, _, _ in parsed:
        if field is None:
            continue
        root = field.split(".", 1)[0].split("[", 1)[0]
        if not root or root.isdigit():
            raise SweepError(f"positional placeholder {{{field}}} is not supported; use a name")
        names.add(root)
    return names
