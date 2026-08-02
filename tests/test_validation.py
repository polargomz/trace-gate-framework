from copy import deepcopy

import jsonschema
import pytest

from trace_gate.validation import TEMPLATE_DIR, load_schema, load_yaml, validate_templates, validate_value


def test_all_templates_have_schema_and_pass() -> None:
    jsonschema.Draft202012Validator.check_schema(load_schema())
    paths = validate_templates()
    assert len(paths) == 13


def test_raw_access_fails_closed_for_lookup() -> None:
    value = load_yaml(TEMPLATE_DIR / "document-access-intent.yaml")
    value["constraints"]["raw_access_requested"] = True
    with pytest.raises(ValueError, match="raw access requires"):
        validate_value("document-access-intent.yaml", value)


def test_destructive_tool_cannot_be_amber() -> None:
    value = deepcopy(load_yaml(TEMPLATE_DIR / "tool-definition.yaml"))
    value["effect"] = "destructive"
    value["risk"] = "amber"
    with pytest.raises(ValueError, match="destructive tools must be red"):
        validate_value("tool-definition.yaml", value)


def test_semantic_intent_requires_group_and_query_digest() -> None:
    value = load_yaml(TEMPLATE_DIR / "document-access-intent.yaml")
    value["retrieval"]["mode"] = "semantic"
    with pytest.raises(ValueError, match="requires embedding groups.*requires a query digest"):
        validate_value("document-access-intent.yaml", value)


def test_embedding_group_can_use_optional_graph_index() -> None:
    value = load_yaml(TEMPLATE_DIR / "document-embedding-profile.yaml")
    value["index"]["kind"] = "graph"
    value["index"]["graph"] = {"algorithm": "hnsw", "parameters": {"m": 16}}
    validate_value("document-embedding-profile.yaml", value)


def test_graph_configuration_is_rejected_for_flat_index() -> None:
    value = load_yaml(TEMPLATE_DIR / "document-embedding-profile.yaml")
    value["index"]["graph"] = {"algorithm": "hnsw", "parameters": {}}
    with pytest.raises(ValueError, match="non-graph indexes"):
        validate_value("document-embedding-profile.yaml", value)
