"""`factory` command: the human's only touch points. Conversation goes through the Product Manager."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from factory import gates, git
from factory.agents import chat
from factory.config import PRD_PATH, RULES_FILE, Project
from factory.pipeline import Factory, HaltError, ask_human, ensure_gitignore

if TYPE_CHECKING:
    from collections.abc import Callable


def cmd_new(p: Project, autonomy: int) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", p.name):
        sys.exit("project name: lowercase letters, digits and dashes")
    if p.dir.exists():
        sys.exit(f"{p.dir} already exists")
    (p.dir / "rules").mkdir(parents=True)
    git.git(p.dir, "init", "-b", "main")
    shutil.copy(RULES_FILE, p.dir / "rules" / "rules.json")
    ensure_gitignore(p.dir)
    (p.dir / "README.md").write_text(f"# {p.name}\n\nBuilt by the software factory.\n", encoding="utf-8")
    git.stage_all(p.dir)
    git.commit(p.dir, "factory: initialise project with rulebook")
    p.state.mkdir(parents=True, exist_ok=True)
    (p.state / "config.json").write_text(json.dumps({"autonomy_level": autonomy}, indent=2), encoding="utf-8")
    print(f"Created {p.dir} (autonomy L{autonomy}). Next: factory chat {p.name}")
    return 0


def cmd_chat(p: Project) -> int:
    f = Factory(p)
    git.discard(p.dir)
    git.checkout(p.dir, "main")
    fingerprint = git.control_fingerprint(p.dir)
    if f.state["phase"] == "prd":
        opening = (
            "Start discovery with me: ask what I want built, research competitors on the web, propose "
            "features, and write docs/prd.md. Tell me when the PRD is ready for my approval."
        )
    else:
        inbox = p.inbox.read_text(encoding="utf-8") if p.inbox.exists() else "(empty)"
        opening = (
            "Here is the factory inbox. Explain it to me in product terms, give me options with your "
            f"recommendation, and update docs/prd.md if I change scope.\n\n{inbox}"
        )
    chat(p, "product-manager", opening)

    if git.control_fingerprint(p.dir) != fingerprint:
        sys.exit("A1: git control files changed during the chat - refusing to continue")
    git.stage_all(p.dir)
    diff = git.staged_diff(p.dir)
    v = gates.path_guard(
        diff,
        allowed=[PRD_PATH, "docs/research/**"],
        protected=f.protected(owner="product-manager"),
        test_globs=f.test_globs(),
        mode="any",
    )
    if v:
        git.discard(p.dir)
        print("Product Manager changes rejected:\n" + "\n".join(v))
        return 1
    if diff.files:
        git.commit(p.dir, "prd: product requirements update\n\nRole: product-manager")
    if p.inbox.exists():
        p.inbox.rename(p.state / f"inbox-read-{datetime.now(UTC):%Y%m%dT%H%M%S}.md")

    if f.state["phase"] == "prd" and (p.dir / PRD_PATH).exists():
        answer = ask_human("Approve this PRD as the scope and let the factory build it?")
        if answer:
            f.ev.append("human_gate", {"gate": "gate1", "approved": True, "commit": git.head(p.dir)})
            f.state["phase"] = "architecture"
            f.save()
            print(f"Scope approved. Start the build: factory run {p.name}")
    return 0


def cmd_approve(p: Project) -> int:
    f = Factory(p)
    pending = [g for g in ("gate2", "gate3") if f.state.get(g) == "pending"]
    if not pending:
        print("Nothing is waiting for your approval.")
        return 0
    gate = pending[0]
    if p.inbox.exists():
        print(p.inbox.read_text(encoding="utf-8"))
    answer = ask_human(f"Approve {gate} ({'architecture' if gate == 'gate2' else 'release'})?")
    if answer is None:
        sys.exit("approval needs an interactive terminal")
    feedback = "" if answer else input("What should change? ")
    f.ev.append("human_gate", {"gate": gate, "approved": answer, "feedback": feedback, "commit": git.head(p.dir)})
    if answer:
        f.state[gate] = "approved"
    elif gate == "gate2":
        f.state.update({gate: None, "phase": "architecture", "architecture_feedback": feedback})
    else:
        f.state[gate] = None
        try:
            f.state["phase"] = f.replan("The human rejected the release", feedback)
        except HaltError as e:
            f.notify_halt(str(e))
    f.save()
    print(f"Recorded. Continue with: factory run {p.name}")
    return 0


def cmd_status(p: Project) -> int:
    f = Factory(p)
    print(f"phase: {f.state['phase']}   cost: ${f.state['cost_usd']:.2f}   autonomy: L{p.config['autonomy_level']}")
    for gate in ("gate2", "gate3"):
        if f.state.get(gate) == "pending":
            print(f"WAITING FOR YOU: {gate} -> factory approve {p.name}")
    if p.stop_file.exists():
        print("KILL SWITCH ENGAGED (factory stop --clear to release)")
    for tid, t in f.board.tasks.items():
        print(f"  {tid:<10} {t['status']:<12} {t['role']:<14} ${t['cost_usd']:.2f}  {t['title']}")
    if p.inbox.exists():
        print(f"Inbox has news: factory chat {p.name}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="factory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [
        ("chat", "talk to the Product Manager"),
        ("run", "run the factory until done or blocked"),
        ("approve", "answer a pending approval gate"),
        ("status", "show progress"),
        ("verify", "verify the evidence chain"),
        ("stop", "kill switch"),
    ]:
        sp = sub.add_parser(name, help=text)
        sp.add_argument("project")
        if name == "stop":
            sp.add_argument("--clear", action="store_true", help="release the kill switch")
    new = sub.add_parser("new", help="create a project")
    new.add_argument("project")
    new.add_argument("--autonomy", type=int, choices=range(4), default=1)
    args = ap.parse_args()
    p = Project(args.project)
    if args.cmd != "new" and not p.dir.exists():
        sys.exit(f"unknown project {p.name}")

    commands: dict[str, Callable[[], int]] = {
        "new": lambda: cmd_new(p, args.autonomy),
        "chat": lambda: cmd_chat(p),
        "run": lambda: Factory(p).run(),
        "approve": lambda: cmd_approve(p),
        "status": lambda: cmd_status(p),
        "verify": lambda: cmd_verify(p),
        "stop": lambda: cmd_stop(p, clear=args.clear),
    }
    sys.exit(commands[args.cmd]())


def cmd_verify(p: Project) -> int:
    problem = Factory(p).ev.verify()
    print(problem or "evidence chain intact")
    return 1 if problem else 0


def cmd_stop(p: Project, *, clear: bool) -> int:
    """J1: the human's kill switch. Deliberately bypasses every agent."""
    p.state.mkdir(parents=True, exist_ok=True)
    if clear:
        p.stop_file.unlink(missing_ok=True)
        print("kill switch released")
    else:
        p.stop_file.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        print("kill switch engaged: agents stop before their next step")
    return 0
