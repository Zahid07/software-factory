"""Thin git wrapper. Only the orchestrator runs git; agents have no git access (A3)."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

BOT = [
    "-c",
    "user.name=software-factory",
    "-c",
    "user.email=factory@localhost",
    "-c",
    "core.quotepath=off",
    # never execute anything an agent may have planted in the repo
    "-c",
    "core.hooksPath=/dev/null/no-hooks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgsign=false",
]


def control_fingerprint(repo: Path) -> str:
    """Hash of git's control files. If an agent changes these, it is trying to escape (A1)."""
    gitdir = repo / ".git"
    parts = [gitdir / "config", gitdir / "info" / "exclude"]
    hooks = sorted(p.name for p in (gitdir / "hooks").glob("*") if not p.name.endswith(".sample"))
    blob = "|".join(p.read_text(encoding="utf-8", errors="replace") if p.exists() else "" for p in parts)
    return hashlib.sha256((blob + "|" + ",".join(hooks)).encode()).hexdigest()


class GitError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *BOT, *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


@dataclass(frozen=True)
class Diff:
    files: list[tuple[str, str]]  # (status letter, path)
    added: list[tuple[str, str]]  # (path, line)
    removed: list[tuple[str, str]]
    text: str


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def stage_all(repo: Path) -> None:
    git(repo, "add", "-A")


def staged_diff(repo: Path) -> Diff:
    """Diff of everything the agent changed (staged) against HEAD."""
    names = git(repo, "diff", "--cached", "--name-status", "--no-renames")
    files = [(line.split("\t", 1)[0][0], line.split("\t", 1)[1]) for line in names.splitlines() if line]
    text = git(repo, "diff", "--cached", "--unified=0", "--no-color", "--no-renames")
    return Diff(files, *_split_lines(text), text)


def range_diff(repo: Path, base: str, tip: str) -> Diff:
    names = git(repo, "diff", "--name-status", "--no-renames", f"{base}...{tip}")
    files = [(line.split("\t", 1)[0][0], line.split("\t", 1)[1]) for line in names.splitlines() if line]
    text = git(repo, "diff", "--unified=3", "--no-color", "--no-renames", f"{base}...{tip}")
    return Diff(files, *_split_lines(text), text)


def _split_lines(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    path = ""
    for line in text.splitlines():
        if line.startswith("--- "):  # comes first; /dev/null for new files
            path = line[6:] if line.startswith("--- a/") else ""
        elif line.startswith("+++ "):  # /dev/null for deleted files: keep the a/ path
            path = line[6:] if line.startswith("+++ b/") else path
        elif line.startswith("+") and path:
            added.append((path, line[1:]))
        elif line.startswith("-") and path:
            removed.append((path, line[1:]))
    return added, removed


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "--allow-empty", "-m", message)
    return head(repo)


def discard(repo: Path) -> None:
    """Throw away an agent's uncommitted work (used when a gate rejects it)."""
    git(repo, "reset", "--hard", "HEAD")
    git(repo, "clean", "-fd")


def checkout(repo: Path, branch: str, *, create: bool = False) -> None:
    git(repo, "checkout", *(["-b"] if create else []), branch)


def merge(repo: Path, branch: str, message: str) -> str:
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "-m", message, branch)
    return head(repo)
