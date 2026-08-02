from copy import deepcopy

import pytest

from trace_gate.semantic import select_semantic_candidates
from trace_gate.validation import TEMPLATE_DIR, load_yaml


GROUP_ID = "EMBED-GROUP.DOMAIN.ACTIVE-DOCS"


def fixtures() -> tuple[dict, dict, dict, dict, dict]:
    intent = load_yaml(TEMPLATE_DIR / "document-access-intent.yaml")
    intent["retrieval"] = {
        "mode": "semantic",
        "embedding_group_ids": [GROUP_ID],
        "query_digest": "sha256:" + "1" * 64,
    }
    read_profile = load_yaml(TEMPLATE_DIR / "document-read-profile.yaml")
    read_profile["semantic_retrieval"] = {
        "mode": "optional",
        "allowed_group_ids": [GROUP_ID],
        "max_candidates": 5,
        "minimum_score": 0.7,
        "allow_cross_group": False,
        "exact_fallback": True,
    }
    embedding_profile = load_yaml(TEMPLATE_DIR / "document-embedding-profile.yaml")
    embedding_profile["status"] = "active"
    embedding_manifest = load_yaml(TEMPLATE_DIR / "document-embedding-manifest.yaml")
    embedding_manifest["status"] = "active"
    embedding_manifest["freshness"]["status"] = "current"
    embedding_manifest["validation"]["status"] = "pass"
    document_manifest = load_yaml(TEMPLATE_DIR / "document-manifest.yaml")
    document_manifest["identity"]["artifact_id"] = "DOC.EXAMPLE.ONE"
    document_manifest["identity"]["revision_id"] = "REV.20260101.0900"
    document_manifest["freshness"]["status"] = "current"
    document_manifest["validation"]["status"] = "pass"
    document_manifest["integrity"]["subjects"][0]["sha256"] = "sha256:" + "a" * 64
    document_manifest["semantic_projection"] = {
        "eligible": True,
        "group_memberships": [GROUP_ID],
        "embedding_manifest_refs": [embedding_manifest["embedding_manifest_id"]],
    }
    return intent, read_profile, embedding_profile, embedding_manifest, document_manifest


def candidate(**overrides) -> dict:
    value = {
        "artifact_id": "DOC.EXAMPLE.ONE",
        "revision_id": "REV.20260101.0900",
        "chunk_id": "CHUNK.DOC.EXAMPLE.ONE.0001",
        "embedding_group_id": GROUP_ID,
        "score": 0.91,
    }
    value.update(overrides)
    return value


def test_exact_mode_does_not_require_embedding_components() -> None:
    intent, read_profile, _, _, _ = fixtures()
    intent["retrieval"] = {"mode": "exact", "embedding_group_ids": [], "query_digest": None}
    result = select_semantic_candidates(
        access_intent=intent,
        read_profile=read_profile,
        embedding_profile=None,
        embedding_manifest=None,
        candidates=[],
        artifact_manifests={},
    )
    assert result.status == "not_requested"


def test_group_scoped_candidate_is_selected_with_provenance() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    result = select_semantic_candidates(
        access_intent=intent,
        read_profile=read_profile,
        embedding_profile=embedding_profile,
        embedding_manifest=embedding_manifest,
        candidates=[candidate()],
        artifact_manifests={"DOC.EXAMPLE.ONE": document_manifest},
    )
    assert result.status == "pass"
    assert result.candidates[0]["embedding_manifest_id"] == embedding_manifest["embedding_manifest_id"]
    assert "content" not in result.candidates[0]


def test_cross_group_candidate_is_excluded() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    result = select_semantic_candidates(
        access_intent=intent,
        read_profile=read_profile,
        embedding_profile=embedding_profile,
        embedding_manifest=embedding_manifest,
        candidates=[candidate(embedding_group_id="EMBED-GROUP.OTHER")],
        artifact_manifests={"DOC.EXAMPLE.ONE": document_manifest},
    )
    assert result.status == "blocked"
    assert result.excluded[0]["reason"] == "cross_group_candidate"


def test_stale_embedding_manifest_fails_closed() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    embedding_manifest["freshness"]["status"] = "stale"
    with pytest.raises(ValueError, match="stale"):
        select_semantic_candidates(
            access_intent=intent,
            read_profile=read_profile,
            embedding_profile=embedding_profile,
            embedding_manifest=embedding_manifest,
            candidates=[candidate()],
            artifact_manifests={"DOC.EXAMPLE.ONE": document_manifest},
        )


def test_document_revision_change_removes_candidate() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    changed = deepcopy(document_manifest)
    changed["identity"]["revision_id"] = "REV.20260102.0900"
    result = select_semantic_candidates(
        access_intent=intent,
        read_profile=read_profile,
        embedding_profile=embedding_profile,
        embedding_manifest=embedding_manifest,
        candidates=[candidate()],
        artifact_manifests={"DOC.EXAMPLE.ONE": changed},
    )
    assert result.excluded[0]["reason"] == "document_revision_changed"


def test_object_outside_explicit_group_is_excluded() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    embedding_profile["scope"]["include_artifact_ids"] = ["DOC.OTHER"]
    result = select_semantic_candidates(
        access_intent=intent,
        read_profile=read_profile,
        embedding_profile=embedding_profile,
        embedding_manifest=embedding_manifest,
        candidates=[candidate()],
        artifact_manifests={"DOC.EXAMPLE.ONE": document_manifest},
    )
    assert result.excluded[0]["reason"] == "document_outside_explicit_group"


def test_secret_document_is_never_returned_as_semantic_candidate() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    document_manifest["access"]["contains_secrets"] = True
    result = select_semantic_candidates(
        access_intent=intent,
        read_profile=read_profile,
        embedding_profile=embedding_profile,
        embedding_manifest=embedding_manifest,
        candidates=[candidate()],
        artifact_manifests={"DOC.EXAMPLE.ONE": document_manifest},
    )
    assert result.excluded[0]["reason"] == "secret_content_excluded"


def test_multiple_groups_require_explicit_cross_group_policy() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    intent["retrieval"]["embedding_group_ids"].append("EMBED-GROUP.OTHER")
    with pytest.raises(PermissionError, match="cross-group"):
        select_semantic_candidates(
            access_intent=intent,
            read_profile=read_profile,
            embedding_profile=embedding_profile,
            embedding_manifest=embedding_manifest,
            candidates=[candidate()],
            artifact_manifests={"DOC.EXAMPLE.ONE": document_manifest},
        )


def test_hybrid_mode_can_return_reduced_for_exact_fallback() -> None:
    intent, read_profile, embedding_profile, embedding_manifest, document_manifest = fixtures()
    intent["retrieval"]["mode"] = "hybrid"
    result = select_semantic_candidates(
        access_intent=intent,
        read_profile=read_profile,
        embedding_profile=embedding_profile,
        embedding_manifest=embedding_manifest,
        candidates=[candidate(score=0.1)],
        artifact_manifests={"DOC.EXAMPLE.ONE": document_manifest},
    )
    assert result.status == "reduced"
    assert result.excluded[0]["reason"] == "below_minimum_score"
