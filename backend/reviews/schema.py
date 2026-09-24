"""JSON schema for the reviewer's structured output, plus a thin validator.

The reviewer must return machine-checkable JSON so the harness — not the model —
decides what becomes a stored finding. Keeping the schema here (rather than the
prompt) means an invalid model response is rejected in code and retried once.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as _JSONSchemaError

SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = (
    "bug",
    "security",
    "concurrency",
    "data",
    "api",
    "perf",
    "test_gap",
    "doc_defect",
    "style",
    "speculative",
)

# Shared by every stage so severities mean the same thing everywhere; the
# verdict is computed from them, so an inflated severity is a wrong verdict.
SEVERITY_RUBRIC = """Severity rubric (calibrate honestly; the merge verdict is computed from it):
- critical: exploitable security hole, data loss/corruption, or a crash/outage on a primary path.
- high: incorrect behavior users will hit in normal use, or an authentication/authorization weakness.
- medium: a real bug confined to an edge case, error path, or uncommon input.
- low: a concrete but minor defect (misleading docstring/comment/message, naming mismatch, a test that does not test what it claims)."""

REVIEWER_SCHEMA: dict = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "path", "line", "severity", "category"],
                "properties": {
                    "text": {"type": "string"},
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "suggestion": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}


ADVERSARY_SCHEMA: dict = {
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["BLOCK", "CONCERNS", "CLEAN"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["persona", "severity", "text"],
                "properties": {
                    "persona": {"type": "string"},
                    "severity": {"type": "string", "enum": ["BLOCKER", "WARNING", "NIT"]},
                    "text": {"type": "string"},
                    "citation": {"type": "string"},
                },
            },
        },
    },
}


VERIFY_SCHEMA: dict = {
    "type": "object",
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "keep"],
                "properties": {
                    "index": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}


class SchemaError(ValueError):
    """Raised when model output does not match a schema."""


def validate(data: dict, schema: dict = REVIEWER_SCHEMA) -> None:
    try:
        Draft202012Validator(schema).validate(data)
    except _JSONSchemaError as exc:
        # Compress the jsonschema message to a single, log-safe line.
        raise SchemaError(exc.message) from exc
