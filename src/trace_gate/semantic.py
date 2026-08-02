"""Optional group-scoped semantic retrieval for TRACE-DM."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class SemanticCandidateSet:
    status: str
    embedding_group_id: str | None
    candidates: list[dict[str, Any]]
    excluded: list[dict[str, str]]
    query_digest: str | None
    digest: str


def select_semantic_candidates(
    *,
    access_intent: dict[str, Any],
    read_profile: dict[str, Any],
    embedding_profile: dict[str, Any] | None,
    embedding_manifest: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    artifact_manifests: dict[str, dict[str, Any]],
) -> SemanticCandidateSet:
    """Validate semantic candidates before normal TRACE-DM content selection.

    The function returns identities and scores, never document content. The
    caller must still run ordinary manifest, access, freshness, and tier gates.
    """
    retrieval = access_intent.get("retrieval", {"mode": "exact", "embedding_group_ids": []})
    semantic_policy = read_profile.get("semantic_retrieval", {"mode": "disabled"})
    if retrieval["mode"] == "exact":
        if semantic_policy["mode"] == "required":
            raise PermissionError("read profile requires semantic retrieval")
        return SemanticCandidateSet("not_requested", None, [], [], None, _digest({"status": "not_requested"}))
    if semantic_policy["mode"] == "disabled":
        raise PermissionError("read profile disables semantic retrieval")
    if embedding_profile is None or embedding_manifest is None:
        raise ValueError("semantic retrieval requires profile and manifest")

    group_id = embedding_profile["embedding_group_id"]
    requested_groups = set(retrieval["embedding_group_ids"])
    if len(requested_groups) > 1 and not semantic_policy.get("allow_cross_group"):
        raise PermissionError("read profile forbids cross-group retrieval")
    if len(requested_groups) > 1 and not embedding_profile["retrieval"].get("allow_cross_group"):
        raise PermissionError("embedding profile forbids cross-group retrieval")
    if group_id not in requested_groups:
        raise PermissionError("embedding group was not requested")
    if group_id not in set(semantic_policy["allowed_group_ids"]):
        raise PermissionError("embedding group is outside the read profile")
    if embedding_profile["status"] != "active":
        raise ValueError("embedding profile is not active")
    if embedding_manifest["status"] != "active":
        raise ValueError("embedding manifest is not active")
    if embedding_manifest["freshness"]["status"] != "current":
        raise ValueError("embedding manifest is stale")
    if embedding_manifest["validation"]["status"] != "pass":
        raise ValueError("embedding manifest is not validated")
    if embedding_manifest["embedding_group_id"] != group_id:
        raise ValueError("embedding profile and manifest group mismatch")
    if embedding_manifest["model"] != embedding_profile["model"]:
        raise ValueError("embedding model contract mismatch")

    purpose = access_intent["purpose"]["purpose_class"]
    if purpose not in embedding_profile["retrieval"]["allowed_purpose_classes"]:
        raise PermissionError("declared purpose cannot use this embedding group")

    intent_sensitivity = SENSITIVITY[access_intent["constraints"]["maximum_sensitivity"]]
    profile_sensitivity = SENSITIVITY[read_profile["limits"]["maximum_sensitivity"]]
    embedding_sensitivity = SENSITIVITY[embedding_profile["access"]["maximum_sensitivity"]]
    maximum_sensitivity = min(intent_sensitivity, profile_sensitivity, embedding_sensitivity)
    minimum_score = max(
        semantic_policy["minimum_score"],
        embedding_profile["retrieval"]["minimum_score"],
    )
    max_candidates = min(
        semantic_policy["max_candidates"],
        embedding_profile["retrieval"]["max_candidates"],
    )
    source_index = {
        (member["artifact_id"], member["revision_id"]): member
        for member in embedding_manifest["source_members"]
    }
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []

    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        artifact_id = candidate["artifact_id"]
        reason: str | None = None
        if candidate["embedding_group_id"] != group_id:
            reason = "cross_group_candidate"
        elif not 0 <= candidate["score"] <= 1:
            reason = "invalid_score"
        elif candidate["score"] < minimum_score:
            reason = "below_minimum_score"
        elif (artifact_id, candidate["revision_id"]) not in source_index:
            reason = "source_revision_not_indexed"
        elif candidate["chunk_id"] not in {
            chunk["chunk_id"]
            for chunk in source_index[(artifact_id, candidate["revision_id"])]["chunks"]
        }:
            reason = "chunk_not_indexed"
        elif artifact_id not in artifact_manifests:
            reason = "document_manifest_missing"
        else:
            manifest = artifact_manifests[artifact_id]
            scope = embedding_profile["scope"]
            if artifact_id in scope["exclude_artifact_ids"]:
                reason = "document_excluded_from_group"
            elif scope["membership_mode"] == "explicit" and artifact_id not in scope["include_artifact_ids"]:
                reason = "document_outside_explicit_group"
            elif scope["membership_mode"] == "selector" and not (
                manifest["identity"].get("artifact_type") in scope["include_artifact_types"]
                or set(manifest.get("scope", {}).get("tags", [])) & set(scope["include_tags"])
            ):
                reason = "document_does_not_match_group_selector"
            elif manifest["identity"]["revision_id"] != candidate["revision_id"]:
                reason = "document_revision_changed"
            elif manifest["freshness"]["status"] != "current":
                reason = "document_not_current"
            elif manifest["validation"]["status"] != "pass":
                reason = "document_not_validated"
            elif manifest["access"].get("contains_secrets"):
                reason = "secret_content_excluded"
            elif SENSITIVITY[manifest["access"]["sensitivity"]] > maximum_sensitivity:
                reason = "sensitivity_exceeded"
            elif group_id not in manifest.get("semantic_projection", {}).get("group_memberships", []):
                reason = "document_group_membership_missing"
            elif embedding_manifest["embedding_manifest_id"] not in manifest.get("semantic_projection", {}).get(
                "embedding_manifest_refs", []
            ):
                reason = "embedding_manifest_ref_missing"
            elif source_index[(artifact_id, candidate["revision_id"])]["source_digest"] not in {
                subject.get("sha256") for subject in manifest["integrity"].get("subjects", [])
            }:
                reason = "source_digest_mismatch"
        if reason:
            excluded.append({"artifact_id": artifact_id, "reason": reason})
            continue
        selected.append(
            {
                "artifact_id": artifact_id,
                "revision_id": candidate["revision_id"],
                "chunk_id": candidate["chunk_id"],
                "score": candidate["score"],
                "embedding_group_id": group_id,
                "embedding_manifest_id": embedding_manifest["embedding_manifest_id"],
            }
        )
        if len(selected) >= max_candidates:
            break

    if selected:
        status = "pass"
    elif retrieval["mode"] == "hybrid" and semantic_policy.get("exact_fallback"):
        status = "reduced"
    else:
        status = "blocked"
    body = {
        "status": status,
        "embedding_group_id": group_id,
        "candidates": selected,
        "excluded": excluded,
        "query_digest": retrieval.get("query_digest"),
    }
    return SemanticCandidateSet(**body, digest=_digest(body))
