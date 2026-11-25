"""Stack-agnostic gates the factory enforces itself. Pure functions: diff in, violations out."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from factory.sandbox import GENERIC_INTERPRETERS

if TYPE_CHECKING:
    from factory.git import Diff

# C2: suppression comments and skipped/focused tests, across common languages.
SUPPRESSION = re.compile(
    r"""(?ix)
      \bnoqa\b | type:\s*ignore | pyright:\s*ignore | pylint:\s*disable | \bnosec\b | pragma:\s*no\s*cover
    | eslint-disable | stylelint-disable | @ts-(?:ignore|nocheck|expect-error) | (?:istanbul|c8|v8)\s+ignore
    | prettier-ignore | //\s*nolint | \bNOSONAR\b | @SuppressWarnings | \#!?\[allow\( | \#\[ignore\]
    | \b(?:it|test|describe|context|suite)\.(?:only|skip|todo)\b | (?<![.\w])[xf](?:it|describe|test)\s*\(
    | pytest\.mark\.(?:skip|skipif|xfail) | pytest\.skip\( | unittest\.skip | @Disabled\b | @Ignore\b
    | \bt\.Skip\w*\(
    """
)
# C4: lines that carry test weight.
ASSERTION = re.compile(r"\b(?:assert\w*|expect|should|verify)\b|\bdef test_|(?<![.\w])(?:it|test)\s*\(")
TEXT_SUFFIXES_EXEMPT = {".md", ".txt", ".rst"}  # prose may legitimately mention these markers

Mode = Literal["any", "no_tests", "tests_only", "none"]


def matches(path: str, globs: list[str]) -> bool:
    p = PurePosixPath(path)
    return any(p.full_match(g) for g in globs)


def path_guard(
    diff: Diff, *, allowed: list[str] | None, protected: list[str], test_globs: list[str], mode: Mode
) -> list[str]:
    """A4 / B3 / C5: who may touch which files."""
    violations = []
    for _, path in diff.files:
        if matches(path, protected):
            violations.append(f"A4: {path} is protected")
        elif mode == "none":
            violations.append(f"A1: this role may not change files ({path})")
        elif mode == "tests_only" and not matches(path, test_globs):
            violations.append(f"C5: test author may only change test files ({path})")
        elif mode == "no_tests" and matches(path, test_globs):
            violations.append(f"C5: only the test author may change tests ({path})")
        elif mode != "tests_only" and allowed is not None and not matches(path, allowed):
            violations.append(f"B3: {path} is outside this task's paths")
    return violations


def suppressions(diff: Diff) -> list[str]:
    return [
        f"C2: suppression/skip marker in {path}: {line.strip()[:120]}"
        for path, line in diff.added
        if PurePosixPath(path).suffix not in TEXT_SUFFIXES_EXEMPT and SUPPRESSION.search(line)
    ]


def test_weakening(diff: Diff, test_globs: list[str]) -> list[str]:
    violations = [f"C4: test file deleted: {p}" for s, p in diff.files if s == "D" and matches(p, test_globs)]
    removed = sum(1 for p, line in diff.removed if matches(p, test_globs) and ASSERTION.search(line))
    added = sum(1 for p, line in diff.added if matches(p, test_globs) and ASSERTION.search(line))
    if removed > added:
        violations.append(f"C4: net removal of {removed - added} assertion/test lines")
    return violations


def image_pinned(image: str) -> bool:
    """F5: explicit tag or digest, never latest."""
    if "@sha256:" in image:
        return True
    name = image.rsplit("/", 1)[-1]
    return ":" in name and not name.endswith(":latest")


def map_violations(emap: dict[str, Any], rules: list[dict[str, Any]], layers: set[str]) -> list[str]:
    """M1 + F5: every applicable project rule is mapped to a check with a canary, or justified as N/A."""
    known = {r["id"] for r in rules}
    applicable = {r["id"] for r in rules if r["enforced_by"] == "project" and r["layer"] in {"all", *layers}}
    mapped: set[str] = set()
    violations = []
    if not image_pinned(emap["sandbox_image"]):
        violations.append(f"F5: sandbox image {emap['sandbox_image']!r} is not pinned to an explicit tag")
    violations += [
        f"F3: network command {p!r} must be a specific install command, not an interpreter"
        for p in emap["network_commands"]
        if not p.split() or p.split()[0] in GENERIC_INTERPRETERS
    ]
    for check in emap["checks"]:
        canaried = {c["rule"] for c in check["canaries"]}
        for rid in check["rules"]:
            if rid not in known:
                violations.append(f"M1: check {check['name']!r} cites unknown rule {rid}")
            if rid not in canaried:
                violations.append(f"M2: rule {rid} in check {check['name']!r} has no canary")
            mapped.add(rid)
    na = {n["rule"] for n in emap["not_applicable"]}
    if "I1" in na:
        violations.append("M1: I1 (integration) can never be not-applicable")
    violations += [f"M1: applicable rule {rid} has no check" for rid in sorted(applicable - mapped - na)]
    return violations


REQ_ID = re.compile(r"\b(?:FR|NFR|SEC)-\d+\b")


def untested_requirements(req_docs: list[str], test_texts: list[str]) -> list[str]:
    """H4: every FR/NFR/SEC id defined in the spec docs appears in at least one test file."""
    defined = {m for doc in req_docs for m in REQ_ID.findall(doc)}
    referenced = {m for text in test_texts for m in REQ_ID.findall(text)}
    return sorted(defined - referenced)


def parse_layers(spec: str) -> set[str]:
    """Read the '## Layers' bullet list from docs/spec.md."""
    match = re.search(r"^#+\s*Layers\s*$(.*?)(?=^#|\Z)", spec, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not match:
        return set()
    found = re.findall(r"^\s*[-*]\s*(frontend|backend|data)\b", match.group(1), re.MULTILINE | re.IGNORECASE)
    return {f.lower() for f in found}
