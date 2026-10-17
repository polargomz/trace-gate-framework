"""Purpose-bound TRACE-DM context selection reference implementation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any

from .validation import validate_value


SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


@dataclass(frozen=True)
class ContextPack:
    decision: str
    profile_id: str
    items: list[dict[str, Any]]
    excluded: list[dict[str, str]]
    bytes_used: int
    digest: str


@dataclass(frozen=True)
class PromptEnvelope:
    messages: list[dict[str, Any]]
    context_pack_digest: str
    tool_ids: list[str]
    digest: str


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def select_context(
    access_intent: dict[str, Any],
    profile: dict[str, Any],
    artifacts: list[dict[str, Any]],
    *,
    evaluated_at: datetime | None = None,
) -> ContextPack:
    """Select adapter-verified artifacts; not an authentication/manifest resolver.

    The trusted adapter supplies validated_at, tier_fields and tier_digests after
    manifest/access/lineage checks. Unstructured text is never heuristically redacted.
    """
    validate_value("document-access-intent.yaml", access_intent)
    validate_value("document-read-profile.yaml", profile)
    if profile["status"] != "active":
        raise PermissionError("read profile is not active")
    targets = access_intent["targets"]
    allowed_ids = targets.get("artifact_ids", [])
    if not isinstance(allowed_ids, list) or not allowed_ids or any(not isinstance(x, str) or not x for x in allowed_ids):
        raise PermissionError("resolve explicit artifact targets before context selection")
    if targets.get("ledger_ids") or any(v is not None for v in targets.get("time_range", {}).values()):
        raise PermissionError("ledger/time-range selection requires a separate resolver")
    if access_intent["constraints"]["freshness_requirement"] != "current":
        raise PermissionError("historical selection requires a separate resolver")
    now = evaluated_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("context evaluation time requires timezone")
    for key in ("max_context_bytes", "max_records", "max_age_seconds"):
        if type(profile["limits"].get(key)) is not int or profile["limits"][key] <= 0:
            raise ValueError(f"invalid profile limit: {key}")
    purpose = access_intent["purpose"]["purpose_class"]
    operation = access_intent["purpose"]["operation"]
    applies = profile["applies_to"]
    if purpose not in applies.get("purpose_classes", []):
        raise PermissionError("read profile does not authorize the declared purpose")
    if operation not in applies.get("operations", []):
        raise PermissionError("read profile does not authorize the operation")

    intent_limit = access_intent["constraints"]["context_budget_bytes"]
    profile_limit = profile["limits"]["max_context_bytes"]
    budget = min(intent_limit, profile_limit)
    max_sensitivity = min(
        SENSITIVITY[access_intent["constraints"]["maximum_sensitivity"]],
        SENSITIVITY[profile["limits"]["maximum_sensitivity"]],
    )
    allowed_tiers = list(profile["selection"]["default_tiers"])
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    used = 0

    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        if artifact_id not in allowed_ids:
            excluded.append({"artifact_id": artifact_id, "reason": "outside_requested_targets"})
            continue
        if artifact_id in {item["artifact_id"] for item in selected}:
            excluded.append({"artifact_id": artifact_id, "reason": "duplicate_artifact"})
            continue
        if len(selected) >= profile["limits"]["max_records"]:
            excluded.append({"artifact_id": artifact_id, "reason": "record_limit_exceeded"})
            continue
        if applies.get("artifact_types") and artifact.get("artifact_type") not in applies["artifact_types"]:
            excluded.append({"artifact_id": artifact_id, "reason": "artifact_type_not_allowed"})
            continue
        if artifact.get("validation_status") != "pass":
            excluded.append({"artifact_id": artifact_id, "reason": "validation_not_passed"})
            continue
        if artifact.get("freshness_status") != "current":
            excluded.append({"artifact_id": artifact_id, "reason": "not_current"})
            continue
        try:
            stamp = datetime.fromisoformat(artifact["validated_at"].replace("Z", "+00:00"))
            age = (now - stamp).total_seconds()
            fresh = stamp.tzinfo is not None and 0 <= age <= profile["limits"]["max_age_seconds"]
        except (KeyError, ValueError, TypeError):
            fresh = False
        if not fresh:
            excluded.append({"artifact_id": artifact_id, "reason": "validation_expired_or_missing"})
            continue
        if SENSITIVITY[artifact.get("sensitivity", "restricted")] > max_sensitivity:
            excluded.append({"artifact_id": artifact_id, "reason": "sensitivity_exceeded"})
            continue
        tier = next((candidate for candidate in allowed_tiers if candidate in artifact["tiers"]), None)
        if tier is None:
            excluded.append({"artifact_id": artifact_id, "reason": "no_allowed_tier"})
            continue
        if ((tier == "raw" and not (profile["selection"]["raw_access_allowed"] and access_intent["constraints"]["raw_access_requested"]))
            or (tier == "full_records" and not (profile["selection"]["full_history_allowed"] and access_intent["constraints"].get("full_history_requested")))):
            excluded.append({"artifact_id": artifact_id, "reason": "tier_not_authorized"})
            continue
        content = artifact["tiers"][tier]
        fields = artifact.get("tier_fields", {}).get(tier)
        selection = profile["selection"]
        if (not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)
            or set(fields) & (set(selection.get("field_denylist", [])) | {"secrets", "credentials"})
            or (selection.get("field_allowlist") and not set(fields) <= set(selection["field_allowlist"]))):
            excluded.append({"artifact_id": artifact_id, "reason": "field_policy_unverified_or_denied"})
            continue
        if (not isinstance(content, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", artifact.get("source_digest", ""))
            or artifact.get("tier_digests", {}).get(tier) != "sha256:" + hashlib.sha256(content.encode()).hexdigest()):
            excluded.append({"artifact_id": artifact_id, "reason": "content_integrity_mismatch"})
            continue
        size = len(content.encode())
        if used + size > budget:
            excluded.append({"artifact_id": artifact_id, "reason": "context_budget_exceeded"})
            continue
        selected.append(
            {
                "artifact_id": artifact_id,
                "revision_id": artifact["revision_id"],
                "tier": tier,
                "content": content,
                "source_digest": artifact["source_digest"],
            }
        )
        used += size

    decision = "allow" if selected else "blocked"
    body = {
        "decision": decision,
        "profile_id": profile["profile_id"],
        "items": selected,
        "excluded": excluded,
        "bytes_used": used,
    }
    return ContextPack(**body, digest=_digest(body))


def assemble_prompt(
    *,
    system_instruction: str,
    request_text: str,
    context_pack: ContextPack,
    tool_ids: list[str],
) -> PromptEnvelope:
    """Build a provider-neutral prompt with explicit context provenance."""
    if context_pack.decision != "allow":
        raise PermissionError("a blocked context pack cannot be assembled into a prompt")
    if context_pack.digest != _digest({k: v for k, v in vars(context_pack).items() if k != "digest"}):
        raise ValueError("context pack changed after selection")
    messages = [
        {"role": "system", "content": system_instruction},
        {
            "role": "system",
            "content": {
                "trace_context": context_pack.items,
                "context_pack_digest": context_pack.digest,
                "allowed_tool_ids": sorted(tool_ids),
            },
        },
        {"role": "user", "content": request_text},
    ]
    body = {
        "messages": messages,
        "context_pack_digest": context_pack.digest,
        "tool_ids": sorted(tool_ids),
    }
    return PromptEnvelope(**body, digest=_digest(body))
