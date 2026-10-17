from trace_gate.context import assemble_prompt, select_context
from trace_gate.validation import TEMPLATE_DIR, load_yaml
from datetime import UTC, datetime
import hashlib


def test_context_selector_enforces_freshness_and_budget() -> None:
    intent = load_yaml(TEMPLATE_DIR / "document-access-intent.yaml")
    profile = load_yaml(TEMPLATE_DIR / "document-read-profile.yaml")
    profile["status"] = "active"
    intent["targets"]["artifact_ids"] = ["DOC.CURRENT", "DOC.STALE"]
    artifacts = [
        {
            "artifact_id": "DOC.CURRENT",
            "revision_id": "REV.1",
            "validation_status": "pass",
            "freshness_status": "current",
            "sensitivity": "internal",
            "source_digest": "sha256:" + "a" * 64,
            "validated_at": datetime.now(UTC).isoformat(),
            "tier_fields": {"summary": ["status"]},
            "tier_digests": {"summary": "sha256:" + hashlib.sha256(b"current state").hexdigest()},
            "tiers": {"summary": "current state"},
        },
        {
            "artifact_id": "DOC.STALE",
            "revision_id": "REV.1",
            "validation_status": "pass",
            "freshness_status": "stale",
            "sensitivity": "internal",
            "source_digest": "sha256:two",
            "tiers": {"summary": "stale state"},
        },
    ]
    pack = select_context(intent, profile, artifacts)
    assert pack.decision == "allow"
    assert [item["artifact_id"] for item in pack.items] == ["DOC.CURRENT"]
    assert pack.excluded == [{"artifact_id": "DOC.STALE", "reason": "not_current"}]
    prompt = assemble_prompt(
        system_instruction="Use supplied evidence only.",
        request_text="What is current?",
        context_pack=pack,
        tool_ids=["repository.read"],
    )
    assert prompt.context_pack_digest == pack.digest
    assert prompt.messages[1]["content"]["allowed_tool_ids"] == ["repository.read"]
