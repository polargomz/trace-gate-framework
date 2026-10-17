"""Negative boundaries and explicit recovery; all inputs are synthetic."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

import jsonschema
import pytest

from trace_gate.context import assemble_prompt, select_context
from trace_gate.gates import compare, digest, evaluate_bound_gate, evaluate_gate
from trace_gate.multi_agent import AgentCoordinator, TaskContract
from trace_gate.runtime import EventStore, RunKernel, RunLimits, ToolDefinition, ToolRegistry
from trace_gate.validation import load_yaml, TEMPLATE_DIR


def kernel(**kwargs):
    defaults = dict(run_id="TEST", model=lambda _: {"type": "answer", "content": "ok"},
                    verifier=lambda _: {"passed": True}, registry=ToolRegistry(), allowed_tools=set())
    return RunKernel(**(defaults | kwargs))


@pytest.mark.parametrize("mutation", ["digest", "previous_digest", "sequence", "reorder", "duplicate"])
def test_event_chain_rejects_corruption(tmp_path, mutation):
    path = tmp_path / "ledger"
    kernel(event_store=EventStore(path)).run([])
    events = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "reorder":
        events[1], events[2] = events[2], events[1]
    elif mutation == "duplicate":
        events.append(events[-1])
    else:
        events[1][mutation] = 88 if mutation == "sequence" else "wrong"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    with pytest.raises(ValueError):
        EventStore(path)


def test_truncated_tail_requires_trusted_head(tmp_path):
    path = tmp_path / "ledger"
    store = EventStore(path)
    kernel(event_store=store).run([])
    head = store.events[-1]["digest"]
    path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    with pytest.raises(ValueError, match="head mismatch"):
        EventStore(path, expected_head=head)


def test_terminal_cache_checks_disk(tmp_path):
    path = tmp_path / "ledger"
    k = kernel(event_store=EventStore(path))
    k.run([])
    path.write_text(path.read_text().replace('"ok"', '"changed"'))
    with pytest.raises(ValueError):
        k.run([])


def test_no_completion_without_verification():
    store = EventStore()
    store.append({"type": "run_started", "run_id": "TEST"})
    with pytest.raises(ValueError, match="verified result"):
        store.append({"type": "run_completed", "run_id": "TEST", "answer": "forged"})
    assert len(store.events) == 1


def test_failed_fsync_does_not_advance_memory(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "ledger")
    monkeypatch.setattr("trace_gate.runtime.os.fsync", lambda _: (_ for _ in ()).throw(OSError("synthetic fsync failure")))
    with pytest.raises(OSError):
        store.append({"type": "run_started", "run_id": "TEST"})
    assert store.events == []
    with pytest.raises(ValueError, match="changed externally"):
        store.append({"type": "run_started", "run_id": "TEST"})


def test_approved_resume_preserves_failed_read_attempt_budget():
    calls = []
    def fail(_):
        calls.append(1)
        raise RuntimeError("synthetic read failure")
    registry = ToolRegistry()
    registry.register(ToolDefinition("read", "read", "read", "green", fail))
    k = kernel(registry=registry, allowed_tools={"read"}, limits=RunLimits(max_tool_calls=1),
               model=lambda _: {"type": "tool_call", "tool_id": "read", "arguments": {}})
    assert k.run([]).status == "failed"
    result = k.run([], resume_approval_ref="APPROVED.ONCE")
    assert result.status == "failed" and "budget" in result.last_error
    assert result.tool_calls == 1 and len(calls) == 1


def test_resume_cannot_widen_original_limits():
    k = kernel(model=lambda _: {"type": "error", "message": "synthetic"})
    k.run([])
    widened = kernel(event_store=k.store, limits=RunLimits(max_steps=21))
    assert "changed run envelope" in widened.run([], resume_approval_ref="approved").last_error


def test_approved_resume_does_not_reset_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("trace_gate.runtime.time.time", lambda: clock[0])
    k = kernel(model=lambda _: {"type": "delegate", "task": {}}, limits=RunLimits(deadline_seconds=10))
    assert k.run([]).status == "awaiting_delegation"
    clock[0] = 111.0
    assert "deadline" in k.run([], resume_approval_ref="approved").last_error


def test_late_model_answer_cannot_complete(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("trace_gate.runtime.time.time", lambda: clock[0])
    def model(_):
        clock[0] += 11
        return {"type": "answer", "content": "late"}
    assert kernel(model=model, limits=RunLimits(deadline_seconds=10)).run([]).status == "failed"


@pytest.mark.parametrize("result", [{"passed": 1}, {"passed": "true"}, {}, None])
def test_malformed_verifier_does_not_complete(result):
    assert kernel(verifier=lambda _: result).run([]).status == "failed"


def test_batch_preflight_precedes_any_execution():
    calls = []
    reg = ToolRegistry()
    for name in ("allowed", "denied"):
        reg.register(ToolDefinition(name, "read", "read", "green", lambda _: calls.append(1)))
    with pytest.raises(PermissionError):
        reg.execute_batch([{"tool_id": name, "arguments": {}} for name in ("allowed", "denied")],
                          allowed_tools={"allowed"}, authorization_refs=set())
    assert calls == []


@pytest.mark.parametrize("value,required", [(False, 0), (True, 1), (1, True), (float("inf"), "> 1"), (float("nan"), "!= 0"), ("1", 1)])
def test_strict_fact_types(value, required):
    assert compare(value, required)[0] is False


def fact_fixture():
    policy = load_yaml(TEMPLATE_DIR / "gate-decision.yaml")
    facts = {"success_rate": 99.5, "duplicate_writes": 0, "forbidden_changes": 0,
             "rollback_rehearsal": "pass", "evidence_refs": ["EVD.TEST"]}
    return policy, facts


def test_missing_evidence_blocks_and_does_not_grant_authority():
    policy, facts = fact_fixture()
    policy["decision"]["newly_allowed_authority"] = ["publish"]
    facts.pop("evidence_refs")
    receipt = evaluate_gate(policy, facts)
    assert receipt.status == "blocked" and receipt.decision["newly_allowed_authority"] == []


@pytest.mark.parametrize("value", [False, -1, 101, "99.5"])
def test_percentage_contract_rejects_invalid_values(value):
    policy, facts = fact_fixture()
    facts["success_rate"] = value
    with pytest.raises(jsonschema.ValidationError):
        evaluate_gate(policy, facts)


def bound_fixture():
    policy, facts = fact_fixture()
    record = {"run_id": "TEST", "subject_revision": "REV.TEST", "gate_id": policy["gate_id"],
              "facts_digest": digest({k: v for k, v in facts.items() if k != "evidence_refs"}),
              "observed_at": "2026-10-17T00:00:00+00:00", "expires_at": "2026-10-17T01:00:00+00:00", "passed": True}
    options = dict(trusted_evidence_digests={"EVD.TEST": digest(record)}, run_id="TEST", subject_revision="REV.TEST",
                   evaluated_at="2026-10-17T00:05:00+00:00", max_age_seconds=600)
    return policy, facts, {"EVD.TEST": record}, options


def test_bound_gate_accepts_exact_trusted_evidence():
    policy, facts, evidence, options = bound_fixture()
    assert evaluate_bound_gate(policy, facts, evidence, **options).status == "pass"


def test_bound_receipt_commits_to_evidence_not_only_fact_values():
    policy, facts, evidence, options = bound_fixture()
    before = evaluate_bound_gate(policy, facts, evidence, **options)
    evidence["EVD.TEST"]["observed_at"] = "2026-10-17T00:01:00+00:00"
    options["trusted_evidence_digests"]["EVD.TEST"] = digest(evidence["EVD.TEST"])
    after = evaluate_bound_gate(policy, facts, evidence, **options)
    assert before.input_digest != after.input_digest
    assert before.receipt_digest != after.receipt_digest


@pytest.mark.parametrize("change", ["hash", "run", "revision", "gate", "facts", "expired", "future", "failed", "missing"])
def test_bound_gate_rejects_invalid_evidence(change):
    policy, facts, evidence, options = bound_fixture()
    record = evidence["EVD.TEST"]
    if change in {"run", "revision", "gate", "facts"}:
        field = {"run": "run_id", "revision": "subject_revision", "gate": "gate_id", "facts": "facts_digest"}[change]
        record[field] = "different"
    elif change == "expired": options["evaluated_at"] = record["expires_at"]
    elif change == "future": options["evaluated_at"] = "2026-10-16T23:59:00+00:00"
    elif change == "failed": record["passed"] = False
    elif change == "missing": evidence.clear()
    elif change == "hash": record["untrusted"] = True
    if change not in {"hash", "missing"}:
        options["trusted_evidence_digests"]["EVD.TEST"] = digest(record)
    with pytest.raises(ValueError):
        evaluate_bound_gate(policy, facts, evidence, **options)


def context_fixture():
    intent = load_yaml(TEMPLATE_DIR / "document-access-intent.yaml")
    intent["targets"]["artifact_ids"] = ["DOC.ONE", "DOC.TWO"]
    profile = load_yaml(TEMPLATE_DIR / "document-read-profile.yaml")
    profile["status"] = "active"
    artifact = {"artifact_id": "DOC.ONE", "validation_status": "pass", "freshness_status": "current",
                "sensitivity": "internal", "revision_id": "REV.ONE", "source_digest": "sha256:" + "a" * 64,
                "validated_at": "2026-10-17T00:00:00+00:00", "tiers": {"summary": "synthetic"},
                "tier_fields": {"summary": ["status"]},
                "tier_digests": {"summary": "sha256:" + hashlib.sha256(b"synthetic").hexdigest()}}
    return intent, profile, artifact, datetime(2026, 10, 17, 0, 1, tzinfo=UTC)


def test_context_explicit_targets_and_count():
    intent, profile, a, now = context_fixture()
    profile["limits"]["max_records"] = 1
    pack = select_context(intent, profile, [dict(a, artifact_id="DOC.OUTSIDE"), a, dict(a, artifact_id="DOC.TWO")], evaluated_at=now)
    assert [i["artifact_id"] for i in pack.items] == ["DOC.ONE"]
    assert [e["reason"] for e in pack.excluded] == ["outside_requested_targets", "record_limit_exceeded"]


@pytest.mark.parametrize("change", ["fields_missing", "secret", "allowlist", "hash", "stale", "future", "type"])
def test_context_rejects_unverified_content(change):
    intent, profile, a, now = context_fixture()
    if change == "fields_missing": a.pop("tier_fields")
    elif change == "secret": a["tier_fields"]["summary"] = ["credentials"]
    elif change == "allowlist": profile["selection"]["field_allowlist"] = ["different"]
    elif change == "hash": a["tiers"]["summary"] = "altered"
    elif change == "stale": a["validated_at"] = "2026-10-16T00:00:00+00:00"
    elif change == "future": a["validated_at"] = "2026-10-18T00:00:00+00:00"
    elif change == "type": profile["applies_to"]["artifact_types"] = ["specific"]
    assert select_context(intent, profile, [a], evaluated_at=now).decision == "blocked"


def test_changed_context_pack_cannot_be_assembled():
    intent, profile, a, now = context_fixture()
    pack = select_context(intent, profile, [a], evaluated_at=now)
    pack.items[0]["content"] = "changed"
    with pytest.raises(ValueError):
        assemble_prompt(system_instruction="test", request_text="test", context_pack=pack, tool_ids=[])


def test_writer_alias_is_same_resource(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    coordinator = AgentCoordinator()
    coordinator.register_parent_scope("P", read_roots=set(), write_roots={str(root), str(alias)}, allowed_tools=set())
    def task(name, path):
        return TaskContract(name, "P", name, "test", (), (str(path),), (), name, datetime.now(UTC) + timedelta(minutes=1))
    coordinator.delegate(task("one", root))
    with pytest.raises(PermissionError):
        coordinator.delegate(task("two", alias))


def test_expired_writer_cannot_complete():
    coordinator = AgentCoordinator()
    coordinator.register_parent_scope("P", read_roots=set(), write_roots={"docs"}, allowed_tools=set())
    task = TaskContract("one", "P", "one", "test", (), ("docs",), (), "writer", datetime.now(UTC) + timedelta(minutes=1))
    coordinator.delegate(task)
    coordinator.tasks["one"].contract = replace(task, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(PermissionError):
        coordinator.complete("one", artifacts={}, evidence_refs=[], verification_passed=True)


def test_expired_writer_key_is_not_reassigned_without_reconciliation():
    from trace_gate.multi_agent import LeaseRegistry
    registry = LeaseRegistry()
    registry.acquire("shared-resource", "old", datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(PermissionError):
        registry.acquire("shared-resource", "new", datetime.now(UTC) + timedelta(minutes=1))


@pytest.mark.parametrize("limit", [float("inf"), float("nan"), True, -1])
def test_invalid_run_deadline_rejected(limit):
    with pytest.raises(ValueError):
        RunLimits(deadline_seconds=limit)


def test_read_profile_cannot_silently_expand_empty_targets():
    intent, profile, artifact, now = context_fixture()
    intent["targets"]["artifact_ids"] = []
    with pytest.raises(PermissionError):
        select_context(intent, profile, [artifact], evaluated_at=now)
