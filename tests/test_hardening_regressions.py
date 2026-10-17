"""Six review reproductions; synthetic only, no provider or operational I/O."""
from datetime import UTC, datetime, timedelta

import pytest

from trace_gate.context import select_context
from trace_gate.gates import evaluate_gate
from trace_gate.multi_agent import AgentCoordinator, TaskContract
from trace_gate.runtime import EventStore, RunKernel, RunLimits, ToolDefinition, ToolRegistry
from trace_gate.validation import TEMPLATE_DIR, load_yaml


def test_missing_verifier_cannot_complete():
    called = []
    kernel = RunKernel(run_id="review", model=lambda _: called.append(1) or
                       {"type": "answer", "content": "unverified"},
                       registry=ToolRegistry(), allowed_tools=set())
    assert kernel.run([]).status == "blocked"
    assert called == []


def test_modified_checkpoint_is_rejected(tmp_path):
    path = tmp_path / "events.jsonl"
    kernel = RunKernel(run_id="review", model=lambda _: {"type": "answer", "content": "original"},
                       verifier=lambda _: {"passed": True}, registry=ToolRegistry(),
                       allowed_tools=set(), event_store=EventStore(path))
    assert kernel.run([]).status == "completed"
    path.write_text(path.read_text().replace("original", "modified"))
    with pytest.raises(ValueError):
        EventStore(path)


def test_failed_write_is_not_replayed_on_resume():
    calls = []
    def handler(_):
        calls.append(1)
        raise RuntimeError("after synthetic effect")
    registry = ToolRegistry()
    registry.register(ToolDefinition("write", "test", "write", "amber", handler,
                                     idempotency_strategy="external-key"))
    kernel = RunKernel(run_id="review", model=lambda _: {"type": "tool_call", "tool_id": "write", "arguments": {}},
                       verifier=lambda _: {"passed": True}, registry=registry, allowed_tools={"write"},
                       authorization_refs={"test"}, limits=RunLimits(max_tool_calls=1))
    assert kernel.run([]).status == "failed"
    assert kernel.run([], resume_approval_ref="APPROVAL.REVIEW").status == "blocked"
    assert len(calls) == 1


def test_nonfinite_and_boolean_facts_cannot_pass():
    policy = load_yaml(TEMPLATE_DIR / "gate-decision.yaml")
    facts = {"success_rate": float("inf"), "duplicate_writes": False,
             "forbidden_changes": False, "rollback_rehearsal": "pass"}
    with pytest.raises(ValueError):
        evaluate_gate(policy, facts)


def test_deprecated_context_profile_rejected():
    intent = load_yaml(TEMPLATE_DIR / "document-access-intent.yaml")
    intent["targets"]["artifact_ids"] = ["DOC.allowed"]
    profile = load_yaml(TEMPLATE_DIR / "document-read-profile.yaml")
    profile["status"] = "deprecated"
    profile["limits"]["max_records"] = 1
    artifacts = [{"artifact_id": "DOC.other", "validation_status": "pass", "freshness_status": "current",
                  "sensitivity": "internal", "revision_id": "r01", "source_digest": "sha256:" + "a" * 64,
                  "tiers": {"summary": "synthetic"}}]
    with pytest.raises(PermissionError):
        select_context(intent, profile, artifacts)


def test_nested_writer_roots_rejected():
    coordinator = AgentCoordinator()
    coordinator.register_parent_scope("parent", read_roots={"docs"}, write_roots={"docs", "docs/child"}, allowed_tools=set())
    def task(name, root):
        return TaskContract(name, "parent", name, "synthetic", ("docs",), (root,), (), name,
                            datetime.now(UTC) + timedelta(minutes=1))
    coordinator.delegate(task("first", "docs"))
    with pytest.raises(PermissionError):
        coordinator.delegate(task("second", "docs/child"))
