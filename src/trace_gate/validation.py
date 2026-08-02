"""JSON Schema and semantic validation for TRACE contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "trace-gate.schema.json"
TEMPLATE_DIR = ROOT / "templates"
MANAGED_DOCUMENT_MANIFEST = ROOT / "docs" / "managed-document-manifest.yaml"

TEMPLATE_SCHEMAS = {
    "request-receipt.yaml": "requestReceipt",
    "ticket.yaml": "ticket",
    "evidence-manifest.yaml": "evidenceManifest",
    "gate-decision.yaml": "gateDecision",
    "document-access-intent.yaml": "documentAccessIntent",
    "document-embedding-profile.yaml": "documentEmbeddingProfile",
    "document-embedding-manifest.yaml": "documentEmbeddingManifest",
    "document-manifest.yaml": "documentManifest",
    "document-read-profile.yaml": "documentReadProfile",
    "document-gate-receipt.yaml": "documentGateReceipt",
    "agent-run.yaml": "agentRun",
    "tool-definition.yaml": "toolDefinition",
    "subagent-task.yaml": "subagentTask",
}
SCHEMA_MAPPINGS = {
    **TEMPLATE_SCHEMAS,
    "managed-document-manifest.yaml": "managedDocumentManifest",
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
        retrieval = value.get("retrieval", {"mode": "exact", "embedding_group_ids": []})
        if constraints.get("raw_access_requested") and purpose not in {
            "incident_response",
            "audit_reconstruction",
        }:
            errors.append("raw access requires incident_response or audit_reconstruction")
        if constraints.get("full_history_requested") and purpose != "audit_reconstruction":
            errors.append("full history requires audit_reconstruction")
        if retrieval["mode"] == "exact" and retrieval["embedding_group_ids"]:
            errors.append("exact retrieval cannot request embedding groups")
        if retrieval["mode"] in {"semantic", "hybrid"}:
            if not retrieval["embedding_group_ids"]:
                errors.append("semantic or hybrid retrieval requires embedding groups")
            if not retrieval.get("query_digest"):
                errors.append("semantic or hybrid retrieval requires a query digest")

    if name == "document-read-profile.yaml":
        selection = value["selection"]
        purposes = set(value["applies_to"].get("purpose_classes", []))
        semantic = value.get("semantic_retrieval", {"mode": "disabled", "allowed_group_ids": []})
        if selection.get("raw_access_allowed") and not purposes <= {
            "incident_response",
            "audit_reconstruction",
        }:
            errors.append("raw-enabled profiles may only serve incident or audit purposes")
        if semantic["mode"] == "disabled" and semantic["allowed_group_ids"]:
            errors.append("disabled semantic retrieval cannot allow embedding groups")
        if semantic["mode"] != "disabled" and not semantic["allowed_group_ids"]:
            errors.append("enabled semantic retrieval requires allowed embedding groups")
        if semantic.get("allow_cross_group") and len(semantic["allowed_group_ids"]) < 2:
            errors.append("cross-group retrieval requires at least two allowed groups")

    if name == "document-manifest.yaml":
        subjects = value["integrity"].get("subjects", [])
        locations = [subject.get("location") for subject in subjects]
        if len(locations) != len(set(locations)):
            errors.append("integrity subjects must have unique locations")
        if value["access"].get("contains_secrets") and value["tiers"].get("summary"):
            errors.append("secret-bearing manifests cannot publish a normal summary")
        semantic = value.get("semantic_projection")
        if semantic:
            if semantic["eligible"] and not semantic["group_memberships"]:
                errors.append("embedding-eligible documents require a group membership")
            if not semantic["eligible"] and (
                semantic["group_memberships"] or semantic["embedding_manifest_refs"]
            ):
                errors.append("ineligible documents cannot retain embedding memberships or refs")

    if name == "document-embedding-profile.yaml":
        scope = value["scope"]
        if scope["membership_mode"] == "explicit" and not scope["include_artifact_ids"]:
            errors.append("explicit embedding groups require artifact ids")
        if scope["membership_mode"] == "selector" and not (
            scope["include_artifact_types"] or scope["include_tags"]
        ):
            errors.append("selector embedding groups require artifact types or tags")
        overlap = set(scope["include_artifact_ids"]) & set(scope["exclude_artifact_ids"])
        if overlap:
            errors.append(f"embedding group includes and excludes the same artifacts: {sorted(overlap)}")
        index = value["index"]
        if index["kind"] == "graph":
            if not index.get("graph") or not index["graph"].get("algorithm"):
                errors.append("graph indexes require an algorithm")
        elif index.get("graph") is not None:
            errors.append("non-graph indexes cannot define graph configuration")
        excluded = set(value["source_selection"]["exclude_fields"])
        if not {"secrets", "credentials"} <= excluded:
            errors.append("embedding profiles must exclude secrets and credentials")

    if name == "document-embedding-manifest.yaml":
        members = value["source_members"]
        member_keys = [(member["artifact_id"], member["revision_id"]) for member in members]
        if len(member_keys) != len(set(member_keys)):
            errors.append("embedding source members must be unique by artifact and revision")
        chunk_ids = [chunk["chunk_id"] for member in members for chunk in member["chunks"]]
        if len(chunk_ids) != len(set(chunk_ids)):
            errors.append("embedding chunk ids must be unique")
        graph = value["index"].get("graph", {})
        if graph.get("used") and value["index"]["kind"] != "graph":
            errors.append("graph usage requires a graph index kind")
        if graph.get("used") and not graph.get("algorithm"):
            errors.append("used graph indexes require an algorithm")
        if value["index"]["kind"] == "graph" and not graph.get("used"):
            errors.append("graph index manifests must record graph usage")
        chunk_count = sum(len(member["chunks"]) for member in members)
        if value["index"]["record_count"] != chunk_count:
            errors.append("embedding index record count must match chunk count")
        if value["status"] == "active":
            if value["freshness"]["status"] != "current":
                errors.append("active embedding manifests must be current")
            if value["validation"]["status"] != "pass":
                errors.append("active embedding manifests must pass validation")

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
    schema_name = SCHEMA_MAPPINGS.get(name)
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


def discover_managed_documents(root: Path, manifest: dict[str, Any]) -> list[Path]:
    excluded = set(manifest["discovery"]["exclude_roots"])
    discovered: set[Path] = set()
    for pattern in manifest["discovery"]["include_patterns"]:
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if path.is_file() and not any(part in excluded for part in relative.parts):
                discovered.add(relative)
    return sorted(discovered)


def managed_document_errors(value: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    entries = value["documents"]
    paths = [entry["storage"]["canonical_location"] for entry in entries]
    artifact_ids = [entry["identity"]["artifact_id"] for entry in entries]
    if len(paths) != len(set(paths)):
        errors.append("managed document paths must be unique")
    if len(artifact_ids) != len(set(artifact_ids)):
        errors.append("managed document artifact ids must be unique")

    discovered = {path.as_posix() for path in discover_managed_documents(root, value)}
    registered = set(paths)
    missing = sorted(discovered - registered)
    extra = sorted(registered - discovered)
    if missing:
        errors.append(f"unregistered managed documents: {', '.join(missing)}")
    if extra:
        errors.append(f"registered documents are missing: {', '.join(extra)}")

    for entry in entries:
        relative = Path(entry["storage"]["canonical_location"])
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"unsafe managed document path: {relative}")
            continue
        path = root / relative
        if not path.is_file():
            continue
        content = path.read_bytes()
        actual_hash = hashlib.sha256(content).hexdigest()
        actual_size = len(content)
        identity = entry["identity"]
        integrity = entry["integrity"]
        provenance = entry["provenance"]
        expected_revision = f"REV.SHA256.{actual_hash[:16]}"
        if integrity["sha256"] != actual_hash:
            errors.append(f"{relative}: sha256 mismatch")
        if integrity["size_bytes"] != actual_size:
            errors.append(f"{relative}: size mismatch")
        if identity["revision_id"] != expected_revision:
            errors.append(f"{relative}: revision id does not match content digest")
        if relative.as_posix() not in provenance["source_refs"]:
            errors.append(f"{relative}: provenance source_refs must include canonical path")
        if entry["validation"]["status"] != "pass":
            errors.append(f"{relative}: validation status must be pass")
        if entry["freshness"]["status"] != "current":
            errors.append(f"{relative}: freshness status must be current")
        if entry["access"]["contains_secrets"]:
            errors.append(f"{relative}: public managed documents cannot contain secrets")
    return errors


def validate_managed_documents(
    manifest_path: Path = MANAGED_DOCUMENT_MANIFEST,
    root: Path = ROOT,
) -> list[Path]:
    value = load_yaml(manifest_path)
    validate_value(manifest_path.name, value)
    errors = managed_document_errors(value, root)
    if errors:
        raise ValueError(f"{manifest_path.name}: " + "; ".join(errors))
    return discover_managed_documents(root, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if args.paths:
        paths = args.paths
        for path in args.paths:
            validate_path(path)
        for path in paths:
            print(f"PASS {path}")
        print(f"TRACE contract validation passed: {len(paths)} files")
        return 0

    paths = validate_templates()
    documents = validate_managed_documents()
    for path in paths:
        print(f"PASS {path}")
    print(f"PASS {MANAGED_DOCUMENT_MANIFEST}")
    print(
        "TRACE contract validation passed: "
        f"{len(paths)} templates, {len(documents)} managed documents"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
