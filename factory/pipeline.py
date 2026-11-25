"""The orchestrator: deterministic phase machine. It makes no judgement calls; agents decide, code enforces."""

from __future__ import annotations

import json
import re
import shutil
import sys
from typing import TYPE_CHECKING, Any, cast

from factory import gates, git, sandbox
from factory.agents import AgentResult, load_role, run_agent
from factory.board import Board
from factory.config import (
    FORBIDDEN_FILES,
    MAP_PATH,
    PRD_PATH,
    PROTECTED_PATHS,
    SEC_REQ_PATH,
    SPEC_PATH,
    TASKS_PATH,
    Project,
    load_rules,
    threshold,
)
from factory.evidence import Evidence
from factory.schemas import (
    ACCEPTANCE,
    CHANGE_REQUEST,
    CR_DECISION,
    DECISIONS,
    ENFORCEMENT_MAP,
    REVIEW,
    TASKS,
    validate,
)

if TYPE_CHECKING:
    from pathlib import Path

PHASES = [
    "prd",
    "architecture",
    "threat_model",
    "gate2",
    "canary",
    "build",
    "integration",
    "pentest",
    "acceptance",
    "release",
    "gate3",
    "done",
]
DEFAULT_TEST_GLOBS = [
    "**/test/**",
    "**/tests/**",
    "**/__tests__/**",
    "**/*.test.*",
    "**/*.spec.*",
    "**/test_*.*",
    "**/*_test.*",
]
TASK_UPDATABLE = {"description", "paths", "acceptance", "req_ids", "needs_tests", "allow_test_changes"}
REF = re.compile(r"(?:FR|NFR|SEC)-\d+|AC-[\d.]+|ADR[- ]?\d+|CWE-\d+|contracts/\S+|docs/adr/\S+")
MAX_REPLANS = 3
FEEDBACK_LIMIT = 12_000


class HaltError(Exception):
    """Stop the whole factory (J2). The reason goes to the PM inbox."""


class TaskHaltError(Exception):
    """Stop one task; the factory keeps going with the rest."""


def ask_human(question: str) -> bool | None:
    """Human gates are answered at a real terminal only; None means nobody can answer right now."""
    if not sys.stdin.isatty():
        return None
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


class Factory:
    def __init__(self, project: Project) -> None:
        self.p = project
        self.repo = project.dir
        self.ev = Evidence(project.state / "evidence.jsonl")
        self.board = Board(project.state / "board.json")
        self.state_file = project.state / "state.json"
        self.state: dict[str, Any] = (
            json.loads(self.state_file.read_text(encoding="utf-8"))
            if self.state_file.exists()
            else {"phase": "prd", "cost_usd": 0.0, "locked": [], "replans": 0}
        )
        self.rule_ids = {r["id"] for r in load_rules()}

    # ---------------------------------------------------------------- plumbing
    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def emap(self) -> dict[str, Any]:
        path = self.repo / MAP_PATH
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_globs(self) -> list[str]:
        globs: list[str] = self.emap().get("test_globs") or DEFAULT_TEST_GLOBS
        return globs

    def protected(self, *, owner: str = "") -> list[str]:
        prot = [*PROTECTED_PATHS, PRD_PATH, *self.state["locked"]]
        if owner == "product-manager":
            prot.remove(PRD_PATH)
        if owner == "tech-lead-architecture":
            prot.remove(".gitignore")
        if owner == "tech-lead-cr":
            prot = [p for p in prot if p not in self.state["locked"]]
        return prot

    def guard(self) -> None:
        """J1/J2/G2/H2: checked before every agent step."""
        if self.p.stop_file.exists():
            raise HaltError("J1: kill switch engaged (factory stop)")
        if self.state["cost_usd"] > self.p.config["budget_usd_per_run"]:
            raise HaltError(f"G2: run budget exceeded (${self.state['cost_usd']:.2f})")
        if problem := self.ev.verify():
            raise HaltError(f"H2: {problem}")
        if self.board.tasks and self.board.halted_ratio() > threshold("FACTORY_MAX_HALTED_RATIO"):
            raise HaltError(f"J2: {self.board.halted_ratio():.0%} of tasks halted")

    def step(
        self,
        role: str,
        prompt: str,
        label: str,
        *,
        mode: gates.Mode,
        allowed: list[str] | None,
        protected: list[str],
        task: dict[str, Any] | None = None,
        inputs: dict[str, str] | None = None,
    ) -> tuple[AgentResult, list[str], git.Diff]:
        """Run one agent, then judge everything it did. Never trusts the agent's own account."""
        self.guard()
        fingerprint = git.control_fingerprint(self.repo)
        res = run_agent(self.p, role, prompt, label=label, inputs=inputs)
        self.state["cost_usd"] += res.cost_usd
        self.save()
        if task is not None:
            task["cost_usd"] += res.cost_usd
            self.board.save()
        if git.control_fingerprint(self.repo) != fingerprint:
            raise HaltError(f"A1: {role} modified git control files (hooks/config) - possible escape attempt")
        planted = [f for f in FORBIDDEN_FILES if (self.repo / f).exists()]
        for f in planted:
            target = self.repo / f
            shutil.rmtree(target) if target.is_dir() else target.unlink()

        git.stage_all(self.repo)
        diff = git.staged_diff(self.repo)
        v = [f"A4: {role} planted agent-runtime file {f}" for f in planted]
        v += gates.path_guard(diff, allowed=allowed, protected=protected, test_globs=self.test_globs(), mode=mode)
        v += gates.suppressions(diff)
        if not (task or {}).get("allow_test_changes"):
            v += gates.test_weakening(diff, self.test_globs())
        v += [f"E1: {b} is not valid JSON" for b in res.bad_outputs]
        if not res.ok:
            v.append(f"agent run failed: {res.text[:800]}")
        decisions = res.outputs.get("decisions")
        if decisions is not None:
            if errs := validate(decisions, DECISIONS):
                v.append(f"E1: decisions.json invalid: {errs[:5]}")
            else:
                self.ev.append("decision", {"role": role, "label": label, "decisions": decisions})
        self.ev.append(
            "agent_run",
            {
                "role": role,
                "label": label,
                "task": (task or {}).get("id"),
                "cost_usd": res.cost_usd,
                "ok": res.ok,
                "violations": v,
            },
        )
        return res, v, diff

    def checks(self) -> tuple[bool, str, list[dict[str, Any]]]:
        """Run setup + every mapped check in the sandbox. Fail closed."""
        emap = self.emap()
        timeout = int(self.p.config["check_timeout_s"])
        results: list[dict[str, Any]] = []
        if emap.get("setup"):
            code, out = sandbox.run(self.repo, emap["setup"], timeout=timeout)
            results.append({"name": "setup", "rules": [], "exit": code, "tail": out[-3000:]})
        if not results or results[0]["exit"] == 0:
            for check in emap.get("checks", []):
                code, out = sandbox.run(self.repo, check["command"], timeout=timeout)
                results.append({"name": check["name"], "rules": check["rules"], "exit": code, "tail": out[-3000:]})
        ok = bool(results) and all(r["exit"] == 0 for r in results)
        report = "\n\n".join(
            f"### {r['name']} (rules {', '.join(r['rules'])}) exit={r['exit']}\n{r['tail']}"
            for r in results
            if r["exit"] != 0
        )
        return ok, report[-FEEDBACK_LIMIT:], results

    def notify_halt(self, reason: str) -> None:
        self.ev.append("halt", {"reason": reason, "phase": self.state["phase"]})
        self.p.notify(
            "Factory halted",
            f"Phase `{self.state['phase']}` stopped: {reason}\n\n"
            "Open `factory chat` so the Product Manager can explain options.",
        )

    # ---------------------------------------------------------------- driver
    def run(self) -> int:
        try:
            while self.state["phase"] != "done":
                nxt = getattr(self, f"phase_{self.state['phase']}")()
                if nxt is None:
                    return 0  # waiting for a human
                self.ev.append("phase", {"from": self.state["phase"], "to": nxt})
                self.state["phase"] = nxt
                self.save()
        except HaltError as e:
            git.discard(self.repo)
            self.notify_halt(str(e))
            return 2
        print("Factory run complete.")
        return 0

    def on_branch(self, name: str) -> None:
        git.discard(self.repo)
        git.checkout(self.repo, "main")
        git.git(self.repo, "checkout", "-B", name, "main")

    def finish_branch(self, name: str, message: str) -> str:
        sha = git.merge(self.repo, name, message)
        self.ev.append("merge", {"branch": name, "commit": sha, "message": message.splitlines()[0]})
        return sha

    # ---------------------------------------------------------------- phases
    def phase_prd(self) -> str | None:
        print("Waiting for an approved PRD: run `factory chat <project>` to work with the Product Manager.")
        return None

    def phase_architecture(self) -> str | None:
        self.on_branch("phase/architecture")
        feedback = self.state.pop("architecture_feedback", "")
        for attempt in range(int(threshold("FACTORY_MAX_CI_RETRIES")) + 1):
            prompt = (
                "Architecture phase. Read docs/prd.md (approved by the human) and rules/rules.json, then produce "
                "ALL deliverables listed in your role. Write docs/enforcement-map.json early: `sbx` needs its "
                "sandbox_image. Make the scaffold and reference feature pass every check in the map."
                + (f"\n\nFix these problems from the previous attempt / reviewers:\n{feedback}" if feedback else "")
            )
            _, v, _ = self.step(
                "tech-lead",
                prompt,
                f"architecture-{attempt}",
                mode="any",
                allowed=None,
                protected=self.protected(owner="tech-lead-architecture"),
            )
            if not v:
                v = self.validate_architecture()
            if not v:
                git.commit(self.repo, "architecture: spec, HLD, ADRs, contracts, scaffold, task DAG\n\nRole: tech-lead")
                self.finish_branch("phase/architecture", "Merge architecture phase")
                return "threat_model"
            feedback = "\n".join(v)[:FEEDBACK_LIMIT]
            if any(x.startswith(("A4", "A1", "C2", "C4", "C5")) for x in v):
                git.discard(self.repo)  # rule violations are thrown away, not fixed in place
        raise HaltError(f"Tech Lead could not produce a valid architecture: {feedback[:1500]}")

    def validate_architecture(self) -> list[str]:
        ensure_gitignore(self.repo)
        git.stage_all(self.repo)
        required = (SPEC_PATH, "docs/hld.md", MAP_PATH, TASKS_PATH)
        v = [f"missing deliverable {path}" for path in required if not (self.repo / path).exists()]
        if v:
            return v
        try:
            emap = json.loads((self.repo / MAP_PATH).read_text(encoding="utf-8"))
            tasks = json.loads((self.repo / TASKS_PATH).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return [f"invalid JSON: {e}"]
        v += [f"{MAP_PATH}: {e}" for e in validate(emap, ENFORCEMENT_MAP)]
        v += [f"{TASKS_PATH}: {e}" for e in validate(tasks, TASKS)]
        if v:
            return v
        layers = gates.parse_layers((self.repo / SPEC_PATH).read_text(encoding="utf-8"))
        if not layers:
            v.append("docs/spec.md has no '## Layers' list (frontend/backend/data)")
        v += gates.map_violations(emap, load_rules(), layers)
        try:
            Board(self.p.state / "dry-run.json").add_all(tasks["tasks"])
        except ValueError as e:
            v.append(f"task DAG: {e}")
        finally:
            (self.p.state / "dry-run.json").unlink(missing_ok=True)
        return v

    def phase_threat_model(self) -> str | None:
        self.on_branch("phase/security")
        for attempt in range(int(threshold("FACTORY_MAX_CI_RETRIES")) + 1):
            prompt = (
                "Threat-modeling phase. Read docs/prd.md, docs/spec.md, docs/hld.md, docs/adr/, contracts/ and "
                "docs/enforcement-map.json. Write docs/security/threat-model.md and docs/security/requirements.md "
                "(SEC-<n>). Then review the enforcement map (M4) and write .factory-out/review.json "
                '({"verdict": "approved"|"changes_requested", "summary": str, "findings": [{"ref", "severity", '
                '"issue", "fix", "file"}]}). Request changes for any weak check, unjustified not_applicable entry, '
                "unpinned image or over-broad network command."
            )
            res, v, _ = self.step(
                "cybersecurity-expert",
                prompt,
                f"threat-model-{attempt}",
                mode="any",
                allowed=["docs/security/**"],
                protected=self.protected(),
            )
            review = res.outputs.get("review")
            v += [f"review.json: {e}" for e in validate(review, REVIEW)] if review is not None else ["missing review"]
            if not (self.repo / SEC_REQ_PATH).exists():
                v.append(f"missing {SEC_REQ_PATH}")
            if v:
                git.discard(self.repo)
                continue
            review = cast("dict[str, Any]", review)
            git.commit(self.repo, "security: threat model and SEC requirements\n\nRole: cybersecurity-expert")
            self.ev.append("map_review", {"verdict": review["verdict"], "findings": review["findings"]})
            if review["verdict"] == "approved":
                self.finish_branch("phase/security", "Merge threat model")
                return "gate2"
            # M4 rejected: the Tech Lead fixes the map on the same branch, then Security looks again.
            fb = json.dumps(review["findings"], indent=1)[:FEEDBACK_LIMIT]
            _, tv, _ = self.step(
                "tech-lead",
                f"The Cybersecurity Expert rejected the enforcement map. Fix every finding:\n{fb}",
                f"map-fix-{attempt}",
                mode="any",
                allowed=None,
                protected=self.protected(),
            )
            tv += self.validate_architecture()
            if tv:
                git.discard(self.repo)
            else:
                git.commit(self.repo, "architecture: address security review of enforcement map\n\nRole: tech-lead")
        raise HaltError("M4: Cybersecurity Expert never approved the enforcement map")

    def phase_gate2(self) -> str | None:
        return self.human_gate("gate2", "architecture", "canary", level_needed=1)

    def phase_canary(self) -> str | None:
        git.checkout(self.repo, "main")
        for attempt in range(int(threshold("FACTORY_MAX_CI_RETRIES")) + 1):
            v = self.canary()
            self.ev.append("canary", {"attempt": attempt, "violations": v, "commit": git.head(self.repo)})
            if not v:
                emap = self.emap()
                self.state["locked"] = sorted(
                    {MAP_PATH, *[f for c in emap["checks"] for f in c.get("config_files", [])]}
                )
                self.save()
                return "build"
            self.on_branch("phase/canary-fix")
            _, tv, _ = self.step(
                "tech-lead",
                "The factory's canary run (rule M2) found checks that do not work:\n"
                + "\n".join(v)[:FEEDBACK_LIMIT]
                + "\nFix the checks or canaries.",
                f"canary-fix-{attempt}",
                mode="any",
                allowed=None,
                protected=self.protected(),
            )
            tv += self.validate_architecture()
            if tv:
                git.discard(self.repo)
                git.checkout(self.repo, "main")
                continue
            git.commit(self.repo, "architecture: fix checks found ineffective by canary run\n\nRole: tech-lead")
            self.finish_branch("phase/canary-fix", "Merge canary fixes")
        raise HaltError("M2: checks still do not catch their canary violations")

    def canary(self) -> list[str]:
        """M2: each check passes on the clean tree and fails with its planted violation."""
        ok, report, results = self.checks()
        if not ok:
            return [f"M2: check {r['name']} fails on the clean tree" for r in results if r["exit"] != 0] + [report]
        v = []
        protected = self.protected()
        for check in self.emap()["checks"]:
            for c in check["canaries"]:
                target = (self.repo / c["file"]).resolve()
                if (
                    not target.is_relative_to(self.repo.resolve())
                    or target.exists()
                    or gates.matches(c["file"], protected)
                ):
                    v.append(f"M2: canary {c['file']} must be a new, unprotected file inside the project")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(c["content"], encoding="utf-8")
                try:
                    code, _ = sandbox.run(self.repo, check["command"], timeout=int(self.p.config["check_timeout_s"]))
                finally:
                    git.discard(self.repo)
                if code == 0:
                    v.append(f"M2: check {check['name']!r} did not catch the canary for rule {c['rule']}")
        return v

    def phase_build(self) -> str | None:
        if not self.board.tasks:
            tasks = json.loads((self.repo / TASKS_PATH).read_text(encoding="utf-8"))["tasks"]
            self.board.add_all(tasks)
        self.recover()
        # ponytail: tasks run one at a time; per-task git worktrees would allow true parallelism.
        while ids := self.board.claimable():
            self.guard()
            try:
                self.run_task(ids[0])
            except TaskHaltError as e:
                self.halt_task(ids[0], str(e))
        if not self.board.done() and not self.board.blocked():
            raise HaltError("build stalled with unfinished tasks")
        for tid in self.board.blocked():
            self.board.move(tid, "halted", "dependency halted")
        return "integration"

    def recover(self) -> None:
        """Resume after a crash: unfinished tasks restart from main; approved ones get merged."""
        for tid, t in self.board.tasks.items():
            if t["status"] in {"ci", "review"}:
                self.board.move(tid, "in_progress", "recovered after restart")
            if t["status"] in {"claimed", "in_progress"}:
                self.board.move(tid, "open", "recovered after restart")
            if t["status"] == "approved":
                self.board.move(tid, "merged", "recovered: " + self.finish_branch(f"task/{tid}", f"Merge {tid}"))

    def halt_task(self, tid: str, reason: str) -> None:
        git.discard(self.repo)
        git.checkout(self.repo, "main")
        self.board.move(tid, "halted", reason)
        self.ev.append("task_halt", {"task": tid, "reason": reason})
        self.p.notify(f"Task {tid} halted", reason[:2000])

    def bump_retries(self, t: dict[str, Any], why: str) -> str:
        """G3: count a failed attempt; halt the task past the limit. Returns feedback for the next attempt."""
        t["ci_retries"] += 1
        self.board.save()
        if t["ci_retries"] > threshold("FACTORY_MAX_CI_RETRIES"):
            raise TaskHaltError(f"G3: retry limit reached. Last problem:\n{why[:3000]}")
        return why

    def write_tests(self, t: dict[str, Any], feedback: str, refs: str) -> str:
        """C5: the Test Author writes the tests first. Sets tests_done on success."""
        _, v, diff = self.step(
            "test-author",
            task_prompt(t, "Write the tests for this task.", feedback),
            f"{t['id']}-tests",
            mode="tests_only",
            allowed=None,
            protected=self.protected(),
            task=t,
        )
        if v or not diff.files:
            git.discard(self.repo)
            return self.bump_retries(t, "\n".join(v) or "no test files were written")
        git.commit(self.repo, f"{t['id']}: tests for {t['title']}\n\nRole: test-author\n{refs}")
        t["tests_done"] = True
        self.board.save()
        return ""

    def review_rejected(self, t: dict[str, Any], feedback: str) -> str:
        """D5: count the round; at the limit the Tech Lead arbitrates once, after that the task halts."""
        self.board.move(t["id"], "in_progress", "changes requested")
        t["review_rounds"] += 1
        self.board.save()
        limit = threshold("FACTORY_MAX_REVIEW_ROUNDS")
        if t["review_rounds"] > limit + 1:
            raise TaskHaltError(f"D5: review rounds exhausted after escalation:\n{feedback[:3000]}")
        if t["review_rounds"] > limit:
            escalation = {
                "kind": "review_dispute",
                "target": t["id"],
                "reason": "D5 escalation: review rounds exhausted",
                "proposal": "Tech Lead: resolve the deadlock between author and reviewers (waive findings or "
                "clarify the task).",
            }
            return self.change_request(t, escalation) + "\n" + feedback
        return feedback

    def approve(self, t: dict[str, Any], sha: str, results: list[dict[str, Any]], review_hashes: list[str]) -> None:
        """D4: approval is only valid with complete evidence. L0 adds a human check per merge."""
        self.ev.append(
            "approval",
            {
                "task": t["id"],
                "commit": sha,
                "req_ids": t["req_ids"],
                "checks": [{k: r[k] for k in ("name", "exit")} for r in results],
                "reviews": review_hashes,
                "cost_usd": t["cost_usd"],
            },
        )
        self.board.move(t["id"], "approved")
        if self.p.config["autonomy_level"] == 0 and ask_human(f"Merge {t['id']} ({t['title']})?") is not True:
            raise TaskHaltError("L0: human did not approve the merge")

    def run_task(self, tid: str) -> None:
        t = self.board.tasks[tid]
        self.on_branch(f"task/{tid}")
        self.board.move(tid, "claimed")
        self.board.move(tid, "in_progress")
        refs = f"Refs: {', '.join(t['req_ids'])}\nTask: {tid}"
        feedback = ""
        while True:
            if t["cost_usd"] > self.p.config["budget_usd_per_task"]:
                raise TaskHaltError(f"G1: task budget exceeded (${t['cost_usd']:.2f})")
            if t["needs_tests"] and not t.get("tests_done"):
                feedback = self.write_tests(t, feedback, refs)
                if not t.get("tests_done"):
                    continue

            res, v, diff = self.step(
                t["role"],
                task_prompt(t, "Implement this task.", feedback),
                tid,
                mode="no_tests",
                allowed=t["paths"],
                protected=self.protected(),
                task=t,
            )
            if (cr := res.outputs.get("change-request")) is not None:
                git.discard(self.repo)
                feedback = self.change_request(t, cr)
                continue
            if v or not diff.files:
                git.discard(self.repo)
                feedback = self.bump_retries(t, "\n".join(v) or "no changes were made")
                continue
            sha = git.commit(self.repo, f"{tid}: {t['title']}\n\nRole: {t['role']}\n{refs}")

            self.board.move(tid, "ci")
            ok, report, results = self.checks()
            self.ev.append(
                "checks",
                {
                    "task": tid,
                    "commit": sha,
                    "ok": ok,
                    "results": [{k: r[k] for k in ("name", "rules", "exit")} for r in results],
                },
            )
            if not ok:
                self.board.move(tid, "in_progress", "checks failed")
                feedback = self.bump_retries(
                    t, f"Your commit failed the checks (D1). Fix the code, not the checks:\n{report}"
                )
                continue

            self.board.move(tid, "review")
            approved, feedback, review_hashes = self.review(t, sha)
            if not approved:
                feedback = self.review_rejected(t, feedback)
                continue

            self.approve(t, sha, results, review_hashes)
            merged = self.finish_branch(f"task/{tid}", f"Merge {tid}: {t['title']}\n\n{refs}")
            self.board.move(tid, "merged", merged)
            return

    def review(self, t: dict[str, Any], sha: str) -> tuple[bool, str, list[str]]:
        """D1-D3, S1: layer reviewer + Cybersecurity Expert, fresh context, reviewer model."""
        diff = git.range_diff(self.repo, "main", "HEAD")
        inputs = {"change.diff": diff.text[-300_000:], "task.json": json.dumps(t, indent=2)}
        waivers = "\n".join(t.get("waivers", [])) or "none"
        feedback, hashes, approved = [], [], True
        for role, layer in (("reviewer", t["layer"]), ("cybersecurity-expert", "security")):
            prompt = (
                f"Review task {t['id']} for layer '{layer}'. The change is in .factory-out/inputs/change.diff and "
                "the task in .factory-out/inputs/task.json; read any repository file you need. Do not modify "
                "files. Write .factory-out/review.json: "
                '{"verdict": "approved"|"changes_requested", "summary": str, "findings": [{"ref": "<rule/FR/NFR/'
                'SEC/AC/ADR/CWE/contract>", "severity": "critical|high|medium|low|nit", "file": str, "line": int, '
                f'"issue": str, "fix": str}}]}}.\nFindings waived by the Tech Lead (do not raise again): {waivers}'
            )
            review: Any = None
            for attempt in range(2):
                res, v, _ = self.step(
                    role,
                    prompt,
                    f"{t['id']}-review-{layer}-{attempt}",
                    mode="none",
                    allowed=None,
                    protected=self.protected(),
                    task=t,
                    inputs=inputs,
                )
                git.discard(self.repo)
                review = res.outputs.get("review")
                if not v and review is not None and not validate(review, REVIEW):
                    break
            else:
                raise TaskHaltError(f"{role} failed to produce a valid review twice (fail closed)")
            kept = [f for f in review["findings"] if f["ref"] in self.rule_ids or REF.fullmatch(f["ref"])]
            severe = [f for f in kept if f["severity"] in {"critical", "high"}]
            blocking = (review["verdict"] == "changes_requested" and bool(kept)) or (
                role == "cybersecurity-expert" and bool(severe)
            )  # S1 veto
            record = self.ev.append(
                "review",
                {
                    "task": t["id"],
                    "commit": sha,
                    "reviewer": role,
                    "layer": layer,
                    "model": self.p.config[f"{load_role(role).model_kind}_model"],
                    "verdict": review["verdict"],
                    "blocking": blocking,
                    "summary": review["summary"],
                    "findings": kept,
                    "dropped_uncited": len(review["findings"]) - len(kept),
                    "round": t["review_rounds"],
                },
            )
            hashes.append(record["hash"])
            if blocking:
                approved = False
                feedback.append(f"## {role} ({layer}) requested changes\n" + json.dumps(kept, indent=1))
        return approved, "\n".join(feedback)[:FEEDBACK_LIMIT], hashes

    def change_request(self, t: dict[str, Any], cr: dict[str, Any]) -> str:
        """E2/G4: the Tech Lead decides every cross-boundary change; the decision is final."""
        if errs := validate(cr, CHANGE_REQUEST):
            return f"Your change request was invalid: {errs}. Fix it or proceed without one."
        t["change_requests"] += 1
        self.board.save()
        if t["change_requests"] > threshold("FACTORY_MAX_CHANGE_REQUESTS"):
            raise TaskHaltError(f"G4: too many change requests; last: {cr['proposal'][:500]}")
        prompt = (
            f"Decide this change request for task {t['id']} (task in .factory-out/inputs/task.json):\n"
            f"{json.dumps(cr, indent=1)}\nIf you approve a contract/ADR/dependency/check change, make it now. "
            'Write .factory-out/cr-decision.json: {"decision": "approve"|"reject"|"counter", "reason": str, '
            '"task_updates": {optional: description, paths, acceptance, req_ids, needs_tests, '
            "allow_test_changes}}. Your decision is final."
        )
        res, v, diff = self.step(
            "tech-lead",
            prompt,
            f"{t['id']}-cr-{t['change_requests']}",
            mode="any",
            allowed=None,
            protected=self.protected(owner="tech-lead-cr"),
            task=t,
            inputs={"task.json": json.dumps(t, indent=2)},
        )
        decision = res.outputs.get("cr-decision")
        if v or decision is None or validate(decision, CR_DECISION):
            git.discard(self.repo)
            return "Your change request could not be processed (Tech Lead error); proceed without it."
        touched_locked = any(gates.matches(p, self.state["locked"]) for _, p in diff.files)
        if decision["decision"] == "approve" and diff.files:
            git.commit(self.repo, f"{t['id']}: change request ({cr['kind']}) approved\n\nRole: tech-lead")
            if touched_locked and (cv := self.canary()):  # M2 must still hold after changing checks
                git.git(self.repo, "reset", "--hard", "HEAD~1")
                decision = {"decision": "reject", "reason": "change broke the canary checks: " + "; ".join(cv)[:800]}
        else:
            git.discard(self.repo)
        updates = {k: val for k, val in decision.get("task_updates", {}).items() if k in TASK_UPDATABLE}
        if decision["decision"] == "approve":
            t.update(updates)
            if cr["kind"] == "test_change":
                t["allow_test_changes"], t["tests_done"] = True, False
            if cr["kind"] == "review_dispute":
                t.setdefault("waivers", []).append(f"{cr['target']}: {decision['reason']}")
        self.board.save()
        self.ev.append("change_request", {"task": t["id"], "request": cr, "decision": decision})
        return f"Tech Lead {decision['decision']}d your {cr['kind']} change request: {decision['reason']}"

    def replan(self, why: str, report: str, *, security: bool = False) -> str:
        """Integration/pentest/acceptance failures become new tasks planned by the Tech Lead."""
        self.state["replans"] += 1
        self.save()
        if self.state["replans"] > MAX_REPLANS:
            raise HaltError(f"{why}: still failing after {MAX_REPLANS} replans")
        git.checkout(self.repo, "main")
        existing = ", ".join(self.board.tasks)
        prompt = (
            f"{why}. Plan remediation tasks. Report is in .factory-out/inputs/report.md. Existing task ids "
            f"(do not reuse): {existing}. Write .factory-out/new-tasks.json in the docs/tasks.json format. "
            "Do not change any files."
        )
        res, v, _ = self.step(
            "tech-lead",
            prompt,
            f"replan-{self.state['replans']}",
            mode="none",
            allowed=None,
            protected=self.protected(),
            inputs={"report.md": report},
        )
        git.discard(self.repo)
        new = res.outputs.get("new-tasks")
        if v or new is None or validate(new, TASKS):
            raise HaltError(f"{why}: Tech Lead could not produce a valid remediation plan")
        for task in new["tasks"]:
            task["security"] = security
        try:
            self.board.add_all(new["tasks"])
        except ValueError as e:
            raise HaltError(f"{why}: invalid remediation plan: {e}") from e
        self.ev.append("replan", {"why": why, "tasks": [x["id"] for x in new["tasks"]]})
        return "build"

    def phase_integration(self) -> str | None:
        git.checkout(self.repo, "main")
        ok, report, results = self.checks()
        req_docs = [
            (self.repo / p).read_text(encoding="utf-8") for p in (SPEC_PATH, SEC_REQ_PATH) if (self.repo / p).exists()
        ]
        tests = [
            f.read_text(encoding="utf-8", errors="replace")
            for rel in git.git(self.repo, "ls-files").splitlines()
            if gates.matches(rel, self.test_globs()) and (f := self.repo / rel).is_file()
        ]
        untested = gates.untested_requirements(req_docs, tests)
        self.ev.append(
            "integration",
            {
                "commit": git.head(self.repo),
                "ok": ok,
                "untested": untested,
                "results": [{k: r[k] for k in ("name", "exit")} for r in results],
            },
        )
        if ok and not untested:
            return "pentest"
        report += f"\n\nH4: requirements with no test referencing them: {untested}" if untested else ""
        return self.replan("Integration checks on main failed (J2/H4)", report)

    def phase_pentest(self) -> str | None:
        git.checkout(self.repo, "main")
        prompt = (
            "Penetration-test phase (staging). Start the system inside the sandbox with `sbx` and attack it as "
            "described in your role. Write .factory-out/review.json with every verified finding."
        )
        res, v, _ = self.step(
            "cybersecurity-expert", prompt, "pentest", mode="none", allowed=None, protected=self.protected()
        )
        git.discard(self.repo)
        review = res.outputs.get("review")
        if v or review is None or validate(review, REVIEW):
            raise HaltError("Pentest produced no valid report (fail closed)")
        severe = [f for f in review["findings"] if f["severity"] in {"critical", "high"}]
        self.ev.append("pentest", {"commit": git.head(self.repo), "findings": review["findings"]})
        if not severe:
            return "acceptance"
        return self.replan("S2: pentest found critical/high issues", json.dumps(severe, indent=1), security=True)

    def phase_acceptance(self) -> str | None:
        git.checkout(self.repo, "main")
        prompt = (
            "Acceptance phase. Verify the built product against EVERY acceptance criterion in docs/prd.md. "
            'Write .factory-out/acceptance.json: {"accepted": bool, "criteria": [{"id": "AC-x.y", "met": '
            'bool, "evidence": "<test name, file or observed behaviour>"}]}.'
        )
        res, v, _ = self.step(
            "product-manager", prompt, "acceptance", mode="none", allowed=None, protected=self.protected()
        )
        git.discard(self.repo)
        acc = res.outputs.get("acceptance")
        if v or acc is None or validate(acc, ACCEPTANCE):
            raise HaltError("Acceptance produced no valid verdict (fail closed)")
        unmet = [c for c in acc["criteria"] if not c["met"]]
        self.ev.append("acceptance", {"commit": git.head(self.repo), "accepted": acc["accepted"], "unmet": unmet})
        if acc["accepted"] and not unmet:
            return "release"
        return self.replan("Product acceptance found unmet criteria", json.dumps(unmet, indent=1))

    def phase_release(self) -> str | None:
        version = f"v0.{len(git.git(self.repo, 'tag', '--list', 'v*').split()) + 1}.0"
        self.state["version"] = version
        self.on_branch("phase/release")
        role = load_role("devops")
        _, v, diff = self.step(
            "devops",
            f"Release phase for {version}. Write CHANGELOG.md and "
            f"docs/release/{version}.md from the merged work (git history is in the repo).",
            "release",
            mode="any",
            allowed=role.writes,
            protected=self.protected(),
        )
        if v or not diff.files:
            raise HaltError(f"release notes rejected: {v or 'no changes'}")
        git.commit(self.repo, f"release: {version} notes\n\nRole: devops")
        self.finish_branch("phase/release", f"Merge release notes {version}")
        return "gate3"

    def phase_gate3(self) -> str | None:
        nxt = self.human_gate("gate3", "release", "done", level_needed=2)
        if nxt == "done":
            git.git(self.repo, "tag", "-a", self.state["version"], "-m", f"Release {self.state['version']}")
            self.ev.append("release", {"version": self.state["version"], "commit": git.head(self.repo)})
            self.p.notify(f"Released {self.state['version']}", self.digest())
        return nxt

    def human_gate(self, gate: str, back_to: str, forward: str, *, level_needed: int) -> str | None:
        if self.p.config["autonomy_level"] > level_needed:
            return forward
        status = self.state.get(gate)
        if status == "approved":
            return forward
        if status != "pending":
            git.checkout(self.repo, "main")
            res, _, _ = self.step(
                "product-manager",
                f"Write the human a plain-language summary for {gate} "
                f"({'architecture' if gate == 'gate2' else 'release'} approval) as described in "
                'your role. Write .factory-out/summary.json: {"summary": "<markdown>"}.',
                f"{gate}-summary",
                mode="none",
                allowed=None,
                protected=self.protected(),
            )
            git.discard(self.repo)
            summary = (res.outputs.get("summary") or {}).get("summary", "(the PM could not write a summary)")
            self.p.notify(f"Approval needed: {gate}", f"{summary}\n\nApprove with `factory approve {self.p.name}`.")
            self.state[gate], self.state[f"{gate}_back"] = "pending", back_to
            self.save()
        print(f"Waiting for human approval ({gate}): run `factory approve {self.p.name}`.")
        return None

    def digest(self) -> str:
        tasks = self.board.tasks.values()
        merged = [t["id"] for t in tasks if t["status"] == "merged"]
        halted = [t["id"] for t in tasks if t["status"] == "halted"]
        return (
            f"Merged tasks: {', '.join(merged) or 'none'}\nHalted tasks: {', '.join(halted) or 'none'}\n"
            f"Total cost: ${self.state['cost_usd']:.2f}\nEvidence records: {len(self.ev.records())}"
        )


def task_prompt(t: dict[str, Any], instruction: str, feedback: str) -> str:
    body = json.dumps(
        {k: t[k] for k in ("id", "title", "role", "layer", "description", "req_ids", "paths", "acceptance") if k in t},
        indent=1,
    )
    waivers = "\n".join(t.get("waivers", []))
    return (
        f"{instruction}\n\nTask:\n{body}\n\nRead docs/spec.md, docs/adr/, docs/boundaries.md, contracts/ and "
        "the reference feature first. Run the relevant checks from docs/enforcement-map.json with `sbx` before "
        "you finish."
        + (f"\n\nWaived review findings: {waivers}" if waivers else "")
        + (f"\n\nFeedback from the previous attempt:\n{feedback}" if feedback else "")
    )


def ensure_gitignore(repo: Path) -> None:
    gi = repo / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if ".factory-out/" not in text.splitlines():
        gi.write_text(text + ("\n" if text and not text.endswith("\n") else "") + ".factory-out/\n", encoding="utf-8")
