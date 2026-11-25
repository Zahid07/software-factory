"""Schemas for every machine-readable agent output, plus a tiny validator (stdlib only)."""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


def validate(value: object, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of problems; empty means valid. Supports the JSON-Schema subset used below."""
    expected = schema.get("type")
    if expected and (not isinstance(value, _TYPES[expected]) or (expected != "boolean" and isinstance(value, bool))):
        return [f"{path}: expected {expected}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.fullmatch(schema["pattern"], value):
        errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        errors += [f"{path}.{key}: required" for key in schema.get("required", []) if key not in value]
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        for i, item in enumerate(value):
            errors += validate(item, schema.get("items", {}), f"{path}[{i}]")
    return errors


_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
BUILD_ROLES = ["designer", "frontend-dev", "backend-dev", "data-modeler", "data-engineer", "bi-developer", "devops"]
LAYERS = ["design", "frontend", "backend", "data-model", "data-engineering", "bi", "infra"]

TASK = {
    "type": "object",
    "required": ["id", "title", "role", "layer", "description", "req_ids", "paths", "depends_on", "needs_tests"],
    "properties": {
        "id": {"type": "string", "pattern": r"[A-Z]+-\d+"},
        "title": _STR,
        "description": _STR,
        "role": {"type": "string", "enum": BUILD_ROLES},
        "layer": {"type": "string", "enum": LAYERS},
        "req_ids": {"type": "array", "items": _STR, "minItems": 1},
        "paths": {"type": "array", "items": _STR, "minItems": 1},
        "depends_on": _STRS,
        "needs_tests": {"type": "boolean"},
        "acceptance": _STRS,
    },
}
TASKS = {
    "type": "object",
    "required": ["tasks"],
    "properties": {"tasks": {"type": "array", "items": TASK, "minItems": 1}},
}

ENFORCEMENT_MAP = {
    "type": "object",
    "required": ["sandbox_image", "network_commands", "test_globs", "checks", "not_applicable"],
    "properties": {
        "sandbox_image": _STR,
        "network_commands": _STRS,
        "setup": _STR,
        "test_globs": {"type": "array", "items": _STR, "minItems": 1},
        "checks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "command", "rules", "canaries"],
                "properties": {
                    "name": _STR,
                    "command": _STR,
                    "rules": _STRS,
                    "config_files": _STRS,
                    "canaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["rule", "file", "content"],
                            "properties": {"rule": _STR, "file": _STR, "content": _STR},
                        },
                    },
                },
            },
        },
        "not_applicable": {
            "type": "array",
            "items": {"type": "object", "required": ["rule", "reason"], "properties": {"rule": _STR, "reason": _STR}},
        },
    },
}

FINDING = {
    "type": "object",
    "required": ["ref", "severity", "issue"],
    "properties": {
        "ref": _STR,  # D2: rule / requirement / ADR / contract / CWE
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "nit"]},
        "file": _STR,
        "line": {"type": "integer"},
        "issue": _STR,
        "fix": _STR,
    },
}
REVIEW = {
    "type": "object",
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approved", "changes_requested"]},
        "summary": _STR,
        "findings": {"type": "array", "items": FINDING},
    },
}

DECISIONS = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["decision", "type", "reason"],
        "properties": {
            "decision": _STR,
            "type": {"type": "string", "enum": ["reversible", "cross-boundary"]},
            "alternatives": _STRS,
            "reason": _STR,
            "confidence": {"type": "number"},
            "assumptions": _STRS,
        },
    },
}

CHANGE_REQUEST = {
    "type": "object",
    "required": ["kind", "target", "proposal", "reason"],
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "task_rejection",
                "contract_change",
                "dependency",
                "adr_amendment",
                "test_change",
                "review_dispute",
            ],
        },
        "target": _STR,
        "proposal": _STR,
        "reason": _STR,
    },
}
CR_DECISION = {
    "type": "object",
    "required": ["decision", "reason"],
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reject", "counter"]},
        "reason": _STR,
        "task_updates": {"type": "object"},
    },
}

ACCEPTANCE = {
    "type": "object",
    "required": ["accepted", "criteria"],
    "properties": {
        "accepted": {"type": "boolean"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "met", "evidence"],
                "properties": {"id": _STR, "met": {"type": "boolean"}, "evidence": _STR},
            },
        },
        "new_tasks": {"type": "array", "items": TASK},
    },
}
