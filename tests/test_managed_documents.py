from copy import deepcopy

import pytest

from trace_gate.validation import (
    MANAGED_DOCUMENT_MANIFEST,
    ROOT,
    load_yaml,
    managed_document_errors,
    validate_managed_documents,
    validate_value,
)


def test_all_repository_markdown_is_registered_and_current() -> None:
    paths = validate_managed_documents()
    assert len(paths) == 9
    assert {path.as_posix() for path in paths} == {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "README.md",
        "docs/REFERENCE-IMPLEMENTATION.ko.md",
        "docs/TRACE-GATE-FRAMEWORK.ko.md",
        "docs/subframeworks/TRACE-AGENT-HARNESS.ko.md",
        "docs/subframeworks/TRACE-DOCUMENT-MANAGEMENT.ko.md",
        "examples/README.md",
        "templates/HANDOFF.md",
    }


def test_manifest_rejects_an_unregistered_markdown_document() -> None:
    value = deepcopy(load_yaml(MANAGED_DOCUMENT_MANIFEST))
    value["documents"].pop()

    errors = managed_document_errors(value, ROOT)

    assert any("unregistered managed documents" in error for error in errors)


def test_manifest_rejects_stale_content_digest() -> None:
    value = deepcopy(load_yaml(MANAGED_DOCUMENT_MANIFEST))
    value["documents"][0]["integrity"]["sha256"] = "0" * 64

    errors = managed_document_errors(value, ROOT)

    assert "README.md: sha256 mismatch" in errors


def test_manifest_rejects_duplicate_document_identity_and_path() -> None:
    value = deepcopy(load_yaml(MANAGED_DOCUMENT_MANIFEST))
    value["documents"].append(deepcopy(value["documents"][0]))

    errors = managed_document_errors(value, ROOT)

    assert "managed document paths must be unique" in errors
    assert "managed document artifact ids must be unique" in errors


@pytest.mark.parametrize(
    ("area", "field"),
    [("scope", "stage"), ("time", "observed_at")],
)
def test_manifest_requires_every_section_eight_minimum_field(
    area: str,
    field: str,
) -> None:
    value = deepcopy(load_yaml(MANAGED_DOCUMENT_MANIFEST))
    del value["documents"][0][area][field]

    with pytest.raises(ValueError, match=f"'{field}' is a required property"):
        validate_value(MANAGED_DOCUMENT_MANIFEST.name, value)
