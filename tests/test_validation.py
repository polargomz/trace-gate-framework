from copy import deepcopy

import jsonschema
import pytest

from trace_gate.validation import TEMPLATE_DIR, load_schema, load_yaml, validate_templates, validate_value


def test_all_templates_have_schema_and_pass() -> None:
    jsonschema.Draft202012Validator.check_schema(load_schema())
    paths = validate_templates()
    assert len(paths) == 11


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
