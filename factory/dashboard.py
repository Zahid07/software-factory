"""Read-only live dashboard: `factory dashboard <project>` serves one page on 127.0.0.1 that polls /api."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from factory import git
from factory.board import Board
from factory.evidence import Evidence
from factory.pipeline import PHASES

if TYPE_CHECKING:
    from factory.config import Project

PAGE = Path(__file__).with_name("dashboard.html")

EVENT_KEYS = (
    "task",
    "role",
    "label",
    "reviewer",
    "layer",
    "verdict",
    "blocking",
    "ok",
    "gate",
    "approved",
    "reason",
    "from",
    "to",
    "version",
    "why",
    "cost_usd",
)


def _event(r: dict[str, Any]) -> dict[str, Any]:
    detail = {k: r[k] for k in EVENT_KEYS if k in r}
    if r.get("violations"):
        detail["violations"] = r["violations"][:5]
    if r.get("findings"):
        detail["findings"] = [f"{f['severity']} {f['ref']}: {f['issue']}"[:200] for f in r["findings"][:5]]
    if r.get("untested"):
        detail["untested"] = r["untested"]
    return {"seq": r["seq"], "at": r["at"], "kind": r["kind"], "detail": detail}


def _running(p: Project) -> dict[str, str] | None:
    """The newest agent run that has a prompt but no transcript yet is still running."""
    runs = p.state / "runs"
    prompts = sorted(runs.glob("*.prompt.md")) if runs.exists() else []
    if not prompts:
        return None
    latest = prompts[-1]
    if latest.with_name(latest.name.replace(".prompt.md", ".transcript.jsonl")).exists():
        return None
    stamp, _, rest = latest.name.removesuffix(".prompt.md").partition("-")
    started = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    return {"name": rest, "started": started.isoformat()}


def snapshot(p: Project) -> dict[str, Any]:
    state_file = p.state / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"phase": "prd"}
    ev = Evidence(p.state / "evidence.jsonl")
    records = ev.records()
    board = Board(p.state / "board.json")
    try:
        branch = git.git(p.dir, "branch", "--show-current").strip()
    except git.GitError:
        branch = "?"
    return {
        "project": p.name,
        "now": datetime.now(UTC).isoformat(),
        "phase": state.get("phase", "prd"),
        "phases": PHASES,
        "cost": state.get("cost_usd", 0.0),
        "budget": p.config["budget_usd_per_run"],
        "autonomy": p.config["autonomy_level"],
        "models": {"author": p.config["author_model"], "reviewer": p.config["reviewer_model"]},
        "pending_gates": [g for g in ("gate2", "gate3") if state.get(g) == "pending"],
        "stopped": p.stop_file.exists(),
        "branch": branch,
        "running": _running(p),
        "chain": ev.verify() or "intact",
        "tasks": [
            {
                k: t.get(k)
                for k in (
                    "id",
                    "title",
                    "role",
                    "layer",
                    "status",
                    "cost_usd",
                    "ci_retries",
                    "review_rounds",
                    "change_requests",
                    "security",
                )
            }
            for t in board.tasks.values()
        ],
        "events": [_event(r) for r in records[-80:]][::-1],
        "inbox": p.inbox.read_text(encoding="utf-8") if p.inbox.exists() else "",
    }


def serve(p: Project, port: int) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api":
                body, ctype = json.dumps(snapshot(p)).encode(), "application/json"
            elif self.path in {"/", "/index.html"}:
                body, ctype = PAGE.read_bytes(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:  # keep the terminal quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # local only, read-only
    print(f"Dashboard for {p.name}: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0
