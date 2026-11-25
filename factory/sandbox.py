"""Disposable, locked-down containers for every command (F1-F5). Also the `sbx` CLI agents are allowed to call."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from factory.config import MAP_PATH, thresholds_env

# F3: these may never be declared as network-enabled command prefixes.
GENERIC_INTERPRETERS = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "python",
    "python3",
    "node",
    "deno",
    "bun",
    "curl",
    "wget",
    "nc",
    "perl",
    "ruby",
    "php",
    "env",
    "exec",
    "eval",
    "docker",
}
SHELL_META = re.compile(r"[;&|`$<>\n\\]")
OUTPUT_LIMIT = 20_000


class SandboxError(Exception):
    pass


def load_map(project_dir: Path) -> dict[str, Any]:
    path = project_dir / MAP_PATH
    if not path.exists():
        raise SandboxError(f"{MAP_PATH} missing: the Tech Lead must declare sandbox_image first")
    emap: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return emap


def network_allowed(command: str, network_commands: list[str]) -> bool:
    """F3: network only for declared install commands, never chained with anything else."""
    cmd = command.strip()
    for prefix in network_commands:
        first = prefix.split()[0] if prefix.split() else ""
        if first in GENERIC_INTERPRETERS or not prefix.strip():
            continue
        if (cmd == prefix or cmd.startswith(prefix + " ")) and not SHELL_META.search(cmd):
            return True
    return False


def docker_args(project_dir: Path, image: str, command: str, *, network: bool, name: str) -> list[str]:
    env = {
        **thresholds_env(),
        "HOME": "/tmp",
        "CI": "true",
        "XDG_CACHE_HOME": "/cache/xdg",
        "npm_config_cache": "/cache/npm",
        "PIP_CACHE_DIR": "/cache/pip",
        "UV_CACHE_DIR": "/cache/uv",
    }
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "bridge" if network else "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",  # F2
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,size=2g",
        "--pids-limit",
        "1024",
        "--cpus",
        "2",
        "--memory",
        "6g",  # F4
        "--mount",
        f"type=bind,source={project_dir.resolve()},target=/work",
        "--mount",
        f"type=volume,source=factory-cache-{project_dir.name.lower()},target=/cache",
        "--workdir",
        "/work",
        *[a for k, v in env.items() for a in ("--env", f"{k}={v}")],
        image,
        "sh",
        "-c",
        command,
    ]


def run(project_dir: Path, command: str, *, timeout: int, force_network: bool = False) -> tuple[int, str]:
    """Run one command in a fresh container. Returns (exit code, combined output). Fails closed."""
    emap = load_map(project_dir)
    network = force_network or network_allowed(command, emap.get("network_commands", []))
    name = f"sbx-{uuid.uuid4().hex[:12]}"
    try:
        proc = subprocess.run(
            docker_args(project_dir, emap["sandbox_image"], command, network=network, name=name),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        return 124, f"TIMEOUT after {timeout}s: {command}"
    except FileNotFoundError:
        return 127, "docker not found: sandbox unavailable (fail closed)"
    output = (proc.stdout + proc.stderr)[-OUTPUT_LIMIT:]
    return proc.returncode, output


def main() -> None:
    """`sbx <command...>`: the only shell agents get. Runs from the agent's cwd (the project)."""
    if len(sys.argv) < 2:
        sys.exit("usage: sbx <command>")
    command = " ".join(sys.argv[1:])
    try:
        code, output = run(Path.cwd(), command, timeout=1800)
    except SandboxError as e:
        sys.exit(f"sbx: {e}")
    print(output)
    sys.exit(code)
