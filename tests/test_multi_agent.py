from datetime import UTC, datetime, timedelta

import pytest

from trace_gate.multi_agent import AgentCoordinator, TaskContract


def contract(task_id: str, writer_key: str | None = None) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        parent_run_id="RUN.PARENT",
        child_trace_id=f"TRACE.{task_id}",
        objective="bounded independent task",
        read_roots=("docs",),
        write_roots=(("docs",) if writer_key else ()),
        allowed_tools=("repository.read",),
        writer_key=writer_key,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def coordinator() -> AgentCoordinator:
    value = AgentCoordinator()
    value.register_parent_scope(
        "RUN.PARENT",
        read_roots={"docs"},
        write_roots={"docs"},
        allowed_tools={"repository.read"},
    )
    return value


def test_single_writer_lease_blocks_second_agent() -> None:
    agent_coordinator = coordinator()
    agent_coordinator.delegate(contract("TASK.ONE", "docs-writer"))
    with pytest.raises(PermissionError, match="overlaps active"):
        agent_coordinator.delegate(contract("TASK.TWO", "docs-writer"))


def test_merge_blocks_conflicting_artifact_digests() -> None:
    agent_coordinator = coordinator()
    agent_coordinator.delegate(contract("TASK.ONE"))
    agent_coordinator.delegate(contract("TASK.TWO"))
    agent_coordinator.complete("TASK.ONE", artifacts={"README.md": "sha256:one"}, evidence_refs=["EVD.1"], verification_passed=True)
    agent_coordinator.complete("TASK.TWO", artifacts={"README.md": "sha256:two"}, evidence_refs=["EVD.2"], verification_passed=True)
    result = agent_coordinator.merge(["TASK.ONE", "TASK.TWO"])
    assert result["status"] == "blocked"
    assert result["conflicts"][0]["path"] == "README.md"


def test_parent_cancel_propagates() -> None:
    agent_coordinator = coordinator()
    agent_coordinator.delegate(contract("TASK.ONE", "docs-writer"))
    assert agent_coordinator.cancel_parent("RUN.PARENT") == ["TASK.ONE"]
    assert agent_coordinator.tasks["TASK.ONE"].status == "cancelled"


def test_child_cannot_exceed_parent_scope() -> None:
    agent_coordinator = coordinator()
    widened = TaskContract(
        **{**contract("TASK.WIDE").__dict__, "allowed_tools": ("repository.write",)}
    )
    with pytest.raises(PermissionError, match="exceeds parent"):
        agent_coordinator.delegate(widened)
