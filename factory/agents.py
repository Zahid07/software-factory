"""Launch one agent role as a headless Claude Code session with role-scoped tools (A1, D3, H3)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory.config import AGENTS_DIR, Project, load_rules

# Defence in depth on top of the path guard: the CLI refuses these edits outright.
DENY = ",".join(f"Edit({p})" for p in [".git/**", ".claude/**", "**/CLAUDE.md", ".mcp.json", "rules/**", ".github/**"])


@dataclass(frozen=True)
class Role:
    name: str
    model_kind: str  # "author" | "reviewer"
    tools: list[str]
    writes: list[str]
    body: str


@dataclass
class AgentResult:
    ok: bool
    cost_usd: float
    text: str
    outputs: dict[str, Any] = field(default_factory=dict)  # parsed .factory-out/*.json by stem
    bad_outputs: list[str] = field(default_factory=list)


def load_role(name: str) -> Role:
    raw = (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    _, front, body = raw.split("---", 2)
    meta = dict(line.split(":", 1) for line in front.strip().splitlines())
    meta = {k.strip(): v.strip() for k, v in meta.items()}

    def items(key: str) -> list[str]:
        return [x.strip() for x in meta.get(key, "").split(",") if x.strip()]

    return Role(meta["name"], meta["model"], items("tools"), items("writes"), body.strip())


def system_prompt(role: Role) -> str:
    protocol = (AGENTS_DIR / "_protocol.md").read_text(encoding="utf-8")
    rules = "\n".join(
        f"- {r['id']} [{r['layer']}, {r['enforced_by']}] {r['rule']}"
        + (f" (threshold {r['threshold']['env']}={r['threshold']['value']})" if "threshold" in r else "")
        for r in load_rules()
    )
    return f"{role.body}\n\n{protocol}\n\n# Factory rules (rules/rules.json)\n{rules}\n"


def _claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    return exe


def _env() -> dict[str, str]:
    """Agents find `sbx` next to the interpreter running the factory (the venv)."""
    scripts = str(Path(sys.executable).parent)
    return {**os.environ, "PATH": scripts + os.pathsep + os.environ.get("PATH", "")}


def _read_outputs(project: Project) -> tuple[dict[str, Any], list[str]]:
    outputs: dict[str, Any] = {}
    bad: list[str] = []
    if project.out.exists():
        for f in sorted(project.out.glob("*.json")):  # top level only; inputs/ is ours
            try:
                outputs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                bad.append(f.name)
    return outputs, bad


def run_agent(
    project: Project, role_name: str, prompt: str, *, label: str, inputs: dict[str, str] | None = None
) -> AgentResult:
    role = load_role(role_name)
    cfg = project.config
    model = cfg["reviewer_model"] if role.model_kind == "reviewer" else cfg["author_model"]
    shutil.rmtree(project.out, ignore_errors=True)
    (project.out / "inputs").mkdir(parents=True)
    for name, content in (inputs or {}).items():
        (project.out / "inputs" / name).write_text(content, encoding="utf-8")

    runs = project.state / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    base = runs / f"{stamp}-{label}-{role.name}"
    sys_file = base.with_suffix(".system.md")
    sys_file.write_text(system_prompt(role), encoding="utf-8")
    base.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")

    cmd = [
        _claude(),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--permission-mode",
        "dontAsk",  # anything not allowlisted is denied, never asked
        "--allowedTools",
        ",".join(role.tools),
        "--disallowedTools",
        DENY,
        "--append-system-prompt-file",
        str(sys_file),
        "--max-budget-usd",
        str(cfg["budget_usd_per_call"]),
        "--setting-sources",
        "project",  # no user hooks/plugins leak into agents
        "--strict-mcp-config",  # no MCP servers
        "--no-session-persistence",
    ]
    print(f"  -> {role.name} [{model}] {label}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=project.dir,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg["agent_timeout_s"],
            env=_env(),
            check=False,
        )
        transcript, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        transcript, err, code = str(e.stdout or ""), "timeout", 124
    base.with_suffix(".transcript.jsonl").write_text(transcript + "\n" + err, encoding="utf-8")

    result: dict[str, Any] = {}
    for line in reversed(transcript.splitlines()):
        if line.startswith("{") and '"type":"result"' in line.replace(" ", ""):
            result = json.loads(line)
            break
    outputs, bad = _read_outputs(project)
    ok = code == 0 and bool(result) and not result.get("is_error", False)
    return AgentResult(ok, float(result.get("total_cost_usd", 0.0)), str(result.get("result", err)), outputs, bad)


def chat(project: Project, role_name: str, opening: str) -> None:
    """Interactive session for the human with the Product Manager (the human's only contact)."""
    role = load_role(role_name)
    sys_file = project.state / "runs" / "chat.system.md"
    sys_file.parent.mkdir(parents=True, exist_ok=True)
    sys_file.write_text(system_prompt(role), encoding="utf-8")
    subprocess.run(
        [
            _claude(),
            "--model",
            project.config["author_model"],
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            ",".join(role.tools),
            "--disallowedTools",
            DENY,
            "--append-system-prompt-file",
            str(sys_file),
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            opening,
        ],
        cwd=project.dir,
        env=_env(),
        check=False,
    )
