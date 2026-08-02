"""JSON Schema and semantic validation for TRACE contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "trace-gate.schema.json"
TEMPLATE_DIR = ROOT / "templates"

TEMPLATE_SCHEMAS = {
    "request-receipt.yaml": "requestReceipt",
    "ticket.yaml": "ticket",
    "evidence-manifest.yaml": "evidenceManifest",
    "gate-decision.yaml": "gateDecision",
    "document-access-intent.yaml": "documentAccessIntent",
    "document-manifest.yaml": "documentManifest",
    "document-read-profile.yaml": "documentReadProfile",
    "document-gate-receipt.yaml": "documentGateReceipt",
    "agent-run.yaml": "agentRun",
    "tool-definition.yaml": "toolDefinition",
    "subagent-task.yaml": "subagentTask",
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that rejects duplicate mapping keys."""


# JSON Schema validates timestamps as strings. Keep YAML timestamps lexical so
# canonical digests are independent of the host timezone implementation.
UniqueKeyLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != "tag:yaml.org,2002:timestamp"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_unique_mapping(
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
    _construct_unique_mapping,
)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.load(stream, Loader=UniqueKeyLoader)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return value


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def semantic_errors(name: str, value: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if name == "document-access-intent.yaml":
        purpose = value["purpose"]["purpose_class"]
        constraints = value["constraints"]
        if constraints.get("raw_access_requested") and purpose not in {
            "incident_response",
            "audit_reconstruction",
        }:
            errors.append("raw access requires incident_response or audit_reconstruction")
        if constraints.get("full_history_requested") and purpose != "audit_reconstruction":
            errors.append("full history requires audit_reconstruction")

    if name == "document-read-profile.yaml":
        selection = value["selection"]
        purposes = set(value["applies_to"].get("purpose_classes", []))
        if selection.get("raw_access_allowed") and not purposes <= {
            "incident_response",
            "audit_reconstruction",
        }:
            errors.append("raw-enabled profiles may only serve incident or audit purposes")

    if name == "document-manifest.yaml":
        subjects = value["integrity"].get("subjects", [])
        locations = [subject.get("location") for subject in subjects]
        if len(locations) != len(set(locations)):
            errors.append("integrity subjects must have unique locations")
        if value["access"].get("contains_secrets") and value["tiers"].get("summary"):
            errors.append("secret-bearing manifests cannot publish a normal summary")

    if name == "tool-definition.yaml":
        effect = value["effect"]
        risk = value["risk"]
        if effect == "destructive" and risk != "red":
            errors.append("destructive tools must be red risk")
        if effect == "write" and risk == "green":
            errors.append("write tools cannot be green risk")
        if effect != "read" and not value.get("idempotency", {}).get("strategy"):
            errors.append("mutating tools require an idempotency strategy")

    if name == "agent-run.yaml":
        limits = value["limits"]
        for key in ("max_steps", "max_tool_calls", "deadline_seconds"):
            if limits[key] <= 0:
                errors.append(f"{key} must be positive")

    if name == "subagent-task.yaml":
        scope = value["scope"]
        overlap = set(scope["write_roots"]) & set(scope.get("forbidden_write_roots", []))
        if overlap:
            errors.append(f"write roots overlap forbidden roots: {sorted(overlap)}")
        if scope["write_roots"] and not value["lease"].get("writer_key"):
            errors.append("subagents with write scope require a writer lease")

    return errors


def validate_value(name: str, value: dict[str, Any]) -> None:
    schema_name = TEMPLATE_SCHEMAS.get(name)
    if schema_name is None:
        raise ValueError(f"no schema mapping for {name}")
    root_schema = load_schema()
    schema = {"$ref": f"#/$defs/{schema_name}", **root_schema}
    validator = jsonschema.Draft202012Validator(schema)
    schema_errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    messages = [error.message for error in schema_errors]
    messages.extend(semantic_errors(name, value))
    if messages:
        raise ValueError(f"{name}: " + "; ".join(messages))


def validate_path(path: Path) -> None:
    validate_value(path.name, load_yaml(path))


def validate_templates(directory: Path = TEMPLATE_DIR) -> list[Path]:
    paths = sorted(path for path in directory.glob("*.yaml") if path.name in TEMPLATE_SCHEMAS)
    missing = sorted(set(TEMPLATE_SCHEMAS) - {path.name for path in paths})
    if missing:
        raise ValueError(f"missing templates: {', '.join(missing)}")
    for path in paths:
        validate_path(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    paths = args.paths or validate_templates()
    if args.paths:
        for path in paths:
            validate_path(path)
    for path in paths:
        print(f"PASS {path}")
    print(f"TRACE contract validation passed: {len(paths)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
