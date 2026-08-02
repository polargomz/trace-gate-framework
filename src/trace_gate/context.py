"""Purpose-bound TRACE-DM context selection reference implementation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


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
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def select_context(
    access_intent: dict[str, Any],
    profile: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> ContextPack:
    """Select verified context without silently widening purpose or sensitivity."""
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
        if artifact.get("validation_status") != "pass":
            excluded.append({"artifact_id": artifact_id, "reason": "validation_not_passed"})
            continue
        if artifact.get("freshness_status") != "current":
            excluded.append({"artifact_id": artifact_id, "reason": "not_current"})
            continue
        if SENSITIVITY[artifact.get("sensitivity", "restricted")] > max_sensitivity:
            excluded.append({"artifact_id": artifact_id, "reason": "sensitivity_exceeded"})
            continue
        tier = next((candidate for candidate in allowed_tiers if candidate in artifact["tiers"]), None)
        if tier is None:
            excluded.append({"artifact_id": artifact_id, "reason": "no_allowed_tier"})
            continue
        content = artifact["tiers"][tier]
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
