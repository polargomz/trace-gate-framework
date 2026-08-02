from dataclasses import asdict

from trace_gate.gates import evaluate_gate
from trace_gate.validation import TEMPLATE_DIR, load_yaml


def policy() -> dict:
    return load_yaml(TEMPLATE_DIR / "gate-decision.yaml")


def test_gate_pass_is_reproducible() -> None:
    facts = {
        "success_rate": 99.5,
        "duplicate_writes": 0,
        "forbidden_changes": 0,
        "rollback_rehearsal": "pass",
        "evidence_refs": ["EVD.1"],
    }
    first = evaluate_gate(policy(), facts, observed_at="2026-01-01T00:00:00Z")
    second = evaluate_gate(policy(), facts, observed_at="2026-01-01T00:00:00Z")
    assert first.status == "pass"
    assert asdict(first) == asdict(second)
    assert first.input_digest.startswith("sha256:")
    assert first.receipt_digest.startswith("sha256:")


def test_gate_fails_on_threshold() -> None:
    facts = {
        "success_rate": 98,
        "duplicate_writes": 0,
        "forbidden_changes": 0,
        "rollback_rehearsal": "pass",
    }
    assert evaluate_gate(policy(), facts).status == "fail"


def test_gate_blocks_on_missing_evidence() -> None:
    assert evaluate_gate(policy(), {"success_rate": 100}).status == "blocked"
