#!/usr/bin/env python3
"""Validate TRACE YAML templates and TRACE-DM required fields."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"

TRACE_DM_REQUIRED: dict[str, tuple[str, ...]] = {
    "document-access-intent.yaml": (
        "schema_version",
        "subframework_id",
        "access_intent_id",
        "trace_id",
        "actor",
        "purpose",
        "targets",
        "constraints",
    ),
    "document-read-profile.yaml": (
        "schema_version",
        "subframework_id",
        "profile_id",
        "applies_to",
        "selection",
        "limits",
        "validation",
        "fallback",
        "escalation",
    ),
    "document-manifest.yaml": (
        "schema_version",
        "subframework_id",
        "manifest_id",
        "identity",
        "governance",
        "integrity",
        "lineage",
        "storage",
        "freshness",
        "access",
        "tiers",
        "validation",
    ),
    "document-gate-receipt.yaml": (
        "schema_version",
        "subframework_id",
        "receipt_id",
        "trace_id",
        "access_intent_id",
        "review",
        "gate_results",
        "access_plan",
        "evidence_refs",
    ),
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def load_template(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.load(stream, Loader=UniqueKeyLoader)
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT)}: top level must be a mapping")
    return value


def validate_trace_dm(path: Path, value: dict[str, Any]) -> None:
    required = TRACE_DM_REQUIRED.get(path.name)
    if required is None:
        return
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(
            f"{path.relative_to(ROOT)}: missing required keys: {', '.join(missing)}"
        )
    if value.get("subframework_id") != "TRACE-DM":
        raise ValueError(
            f"{path.relative_to(ROOT)}: subframework_id must be TRACE-DM"
        )


def main() -> int:
    paths = sorted(TEMPLATE_DIR.glob("*.yaml"))
    if not paths:
        raise ValueError("no YAML templates found")
    for path in paths:
        value = load_template(path)
        validate_trace_dm(path, value)
        print(f"PASS {path.relative_to(ROOT)}")
    print(f"Template validation passed: {len(paths)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
