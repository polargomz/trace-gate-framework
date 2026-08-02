"""Optional TRACE-AH multi-agent coordination contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    parent_run_id: str
    child_trace_id: str
    objective: str
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    writer_key: str | None
    expires_at: datetime
    cancel_on_parent_cancel: bool = True


@dataclass
class TaskState:
    contract: TaskContract
    status: str = "planned"
    artifacts: dict[str, str] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)


class LeaseRegistry:
    def __init__(self) -> None:
        self._leases: dict[str, tuple[str, datetime]] = {}

    def acquire(self, writer_key: str, task_id: str, expires_at: datetime) -> None:
        current = self._leases.get(writer_key)
        now = datetime.now(UTC)
        if current and current[1] > now and current[0] != task_id:
            raise PermissionError(f"writer lease already held by {current[0]}")
        self._leases[writer_key] = (task_id, expires_at)

    def release(self, writer_key: str, task_id: str) -> None:
        if self._leases.get(writer_key, (None,))[0] == task_id:
            del self._leases[writer_key]


class AgentCoordinator:
    """Fail-closed task registry with leases, cancellation, and merge checks."""

    def __init__(self) -> None:
        self.tasks: dict[str, TaskState] = {}
        self.leases = LeaseRegistry()
        self.parent_scopes: dict[str, dict[str, set[str]]] = {}

    def register_parent_scope(
        self,
        parent_run_id: str,
        *,
        read_roots: set[str],
        write_roots: set[str],
        allowed_tools: set[str],
    ) -> None:
        self.parent_scopes[parent_run_id] = {
            "read_roots": read_roots,
            "write_roots": write_roots,
            "allowed_tools": allowed_tools,
        }

    def delegate(self, contract: TaskContract) -> None:
        if contract.task_id in self.tasks:
            raise ValueError(f"duplicate task: {contract.task_id}")
        if contract.write_roots and not contract.writer_key:
            raise ValueError("write-capable subagents require a writer key")
        if contract.expires_at <= datetime.now(UTC):
            raise ValueError("task lease is already expired")
        parent = self.parent_scopes.get(contract.parent_run_id)
        if parent is None:
            raise PermissionError("parent run scope is not registered")
        if not set(contract.read_roots) <= parent["read_roots"]:
            raise PermissionError("child read scope exceeds parent scope")
        if not set(contract.write_roots) <= parent["write_roots"]:
            raise PermissionError("child write scope exceeds parent scope")
        if not set(contract.allowed_tools) <= parent["allowed_tools"]:
            raise PermissionError("child tool scope exceeds parent scope")
        for task in self.tasks.values():
            if task.status == "running" and set(task.contract.write_roots) & set(contract.write_roots):
                raise PermissionError(f"write scope overlaps active task {task.contract.task_id}")
        if contract.writer_key:
            self.leases.acquire(contract.writer_key, contract.task_id, contract.expires_at)
        self.tasks[contract.task_id] = TaskState(contract=contract, status="running")

    def complete(
        self,
        task_id: str,
        *,
        artifacts: dict[str, str],
        evidence_refs: list[str],
        verification_passed: bool,
    ) -> None:
        task = self.tasks[task_id]
        if task.status != "running":
            raise ValueError(f"task is not running: {task.status}")
        if not verification_passed:
            raise PermissionError("child result requires independent verification")
        task.artifacts = artifacts
        task.evidence_refs = evidence_refs
        task.status = "completed"
        if task.contract.writer_key:
            self.leases.release(task.contract.writer_key, task_id)

    def cancel_parent(self, parent_run_id: str) -> list[str]:
        cancelled: list[str] = []
        for task_id, task in self.tasks.items():
            if task.contract.parent_run_id == parent_run_id and task.contract.cancel_on_parent_cancel and task.status == "running":
                task.status = "cancelled"
                cancelled.append(task_id)
                if task.contract.writer_key:
                    self.leases.release(task.contract.writer_key, task_id)
        return cancelled

    def merge(self, task_ids: list[str]) -> dict[str, Any]:
        merged: dict[str, str] = {}
        evidence: list[str] = []
        conflicts: list[dict[str, str]] = []
        for task_id in task_ids:
            task = self.tasks[task_id]
            if task.status != "completed":
                raise ValueError(f"cannot merge non-completed task: {task_id}")
            for path, artifact_digest in task.artifacts.items():
                if path in merged and merged[path] != artifact_digest:
                    conflicts.append({"path": path, "task_id": task_id})
                else:
                    merged[path] = artifact_digest
            evidence.extend(task.evidence_refs)
        return {
            "status": "blocked" if conflicts else "pass",
            "artifacts": merged,
            "conflicts": conflicts,
            "evidence_refs": evidence,
        }
