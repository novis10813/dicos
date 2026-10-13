"""Pin a submission to a commit that origin already has."""

import re
import subprocess
import uuid
from pathlib import Path


def _git(cwd: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True, text=True)


def _git_out(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    if result.returncode:
        raise ValueError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_commit(cwd: Path, branch: str | None = None, commit: str | None = None,
                   tag: str | None = None) -> str:
    """Return HEAD (clean and pushed) or an explicit target published on origin."""
    if sum(value is not None for value in (branch, commit, tag)) > 1:
        raise ValueError("--branch, --commit and --tag are mutually exclusive")
    if _git(cwd, "rev-parse", "--is-inside-work-tree").returncode:
        raise ValueError(f"{cwd} is not a git repository; use --sync")
    if _git(cwd, "remote", "get-url", "origin").returncode:
        raise ValueError("git mode requires an origin remote; use --sync")
    if all(value is None for value in (branch, commit, tag)):
        if _git_out(cwd, "status", "--porcelain"):
            raise ValueError("working tree is dirty; commit first or use --sync")
        if not _git_out(cwd, "branch", "-r", "--contains", "HEAD"):
            raise ValueError("HEAD is not on any remote branch; push first")
        return _git_out(cwd, "rev-parse", "HEAD")
    if commit is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", commit):
            raise ValueError("--commit must be a full or unique abbreviated hex commit SHA")
        sha = _git_out(cwd, "rev-parse", "--verify", f"{commit}^{{object}}")
        if _git_out(cwd, "cat-file", "-t", sha) != "commit":
            raise ValueError("--commit must identify a commit object")
    else:
        ref = f"refs/heads/{branch}" if branch is not None else f"refs/tags/{tag}"
        if _git(cwd, "check-ref-format", ref).returncode:
            raise ValueError("invalid branch or tag ref name")
        sha = _git_out(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}")
    namespace = f"refs/dicos-submit/{uuid.uuid4().hex}"
    try:
        _git_out(cwd, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--refmap=", "origin",
                 f"+refs/heads/*:{namespace}/heads/*", f"+refs/tags/*:{namespace}/tags/*")
        # for-each-ref peels annotated tags when testing --contains.
        if not _git_out(cwd, "for-each-ref", f"--contains={sha}", "--format=%(refname)", namespace):
            raise ValueError("selected commit is not published on origin; push its branch or tag")
        return sha
    finally:
        refs = _git_out(cwd, "for-each-ref", "--format=%(refname)", namespace)
        if refs:
            result = _git(cwd, "update-ref", "--stdin",
                          input="".join(f"delete {ref}\n" for ref in refs.splitlines()))
            if result.returncode:
                raise ValueError(f"git update-ref cleanup failed: {result.stderr.strip()}")
