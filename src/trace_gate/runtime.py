"""Provider-neutral TRACE-AH reference orchestration loop."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal


Effect = Literal["read", "write", "destructive"]
Risk = Literal["green", "amber", "red"]
ModelAdapter = Callable[[list[dict[str, Any]]], dict[str, Any]]
Verifier = Callable[[str], dict[str, Any]]


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    description: str
    effect: Effect
    risk: Risk
    handler: Callable[[dict[str, Any]], Any]
    timeout_seconds: float = 30
    idempotency_strategy: str | None = None

    def __post_init__(self) -> None:
        if self.effect == "destructive" and self.risk != "red":
            raise ValueError("destructive tools must be red risk")
        if self.effect == "write" and self.risk == "green":
            raise ValueError("write tools cannot be green risk")
        if self.effect != "read" and not self.idempotency_strategy:
            raise ValueError("mutating tools require an idempotency strategy")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._mutation_lock = threading.Lock()

    def register(self, definition: ToolDefinition) -> None:
        if definition.tool_id in self._tools:
            raise ValueError(f"duplicate tool: {definition.tool_id}")
        self._tools[definition.tool_id] = definition

    def get(self, tool_id: str) -> ToolDefinition:
        try:
            return self._tools[tool_id]
        except KeyError as error:
            raise PermissionError(f"tool is not registered: {tool_id}") from error

    def execute(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        allowed_tools: set[str],
        authorization_refs: set[str],
    ) -> dict[str, Any]:
        if tool_id not in allowed_tools:
            raise PermissionError(f"tool is outside run scope: {tool_id}")
        tool = self.get(tool_id)
        if tool.risk == "amber" and not authorization_refs:
            raise PermissionError("amber tool requires an authorization reference")
        if tool.risk == "red" and f"red:{tool_id}" not in authorization_refs:
            raise PermissionError(f"red tool requires explicit red:{tool_id} authorization")

        started = time.monotonic()
        if tool.effect == "read":
            output = tool.handler(arguments)
        else:
            with self._mutation_lock:
                output = tool.handler(arguments)
        elapsed = time.monotonic() - started
        if elapsed > tool.timeout_seconds:
            raise TimeoutError(f"tool exceeded timeout after {elapsed:.3f}s")
        return {
            "tool_id": tool_id,
            "effect": tool.effect,
            "risk": tool.risk,
            "elapsed_seconds": elapsed,
            "output": output,
            "output_digest": _digest(output),
        }

    def execute_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        allowed_tools: set[str],
        authorization_refs: set[str],
    ) -> list[dict[str, Any]]:
        definitions = [self.get(call["tool_id"]) for call in calls]
        if any(tool.effect != "read" for tool in definitions):
            raise PermissionError("batched tool calls must all be read-only")
        with ThreadPoolExecutor(max_workers=min(len(calls), 8)) as executor:
            futures = [
                executor.submit(
                    self.execute,
                    call["tool_id"],
                    call["arguments"],
                    allowed_tools=allowed_tools,
                    authorization_refs=authorization_refs,
                )
                for call in calls
            ]
            return [future.result() for future in futures]


@dataclass(frozen=True)
class RunLimits:
    max_steps: int = 20
    max_tool_calls: int = 10
    deadline_seconds: float = 300

    def __post_init__(self) -> None:
        if min(self.max_steps, self.max_tool_calls, self.deadline_seconds) <= 0:
            raise ValueError("run limits must be positive")


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    answer: str | None
    steps: int
    tool_calls: int
    evidence_refs: list[str]
    last_error: str | None = None


@dataclass
class EventStore:
    """Append-only JSONL ledger suitable for checkpoint and replay."""

    path: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path and self.path.exists():
            self.events = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def append(self, event: dict[str, Any]) -> str:
        envelope = {**event, "sequence": len(self.events) + 1, "previous_digest": self.events[-1]["digest"] if self.events else None}
        envelope["digest"] = _digest(envelope)
        self.events.append(envelope)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return envelope["digest"]


class RunKernel:
    """Bounded model/tool/verification loop with durable observations."""

    def __init__(
        self,
        *,
        run_id: str,
        model: ModelAdapter,
        registry: ToolRegistry,
        allowed_tools: set[str],
        authorization_refs: set[str] | None = None,
        verifier: Verifier | None = None,
        limits: RunLimits | None = None,
        event_store: EventStore | None = None,
    ) -> None:
        self.run_id = run_id
        self.model = model
        self.registry = registry
        self.allowed_tools = allowed_tools
        self.authorization_refs = authorization_refs or set()
        self.verifier = verifier or (lambda answer: {"passed": True, "reason": "not configured"})
        self.limits = limits or RunLimits()
        self.store = event_store or EventStore()

    @staticmethod
    def _parse_action(action: Any) -> dict[str, Any]:
        if not isinstance(action, dict) or action.get("type") not in {"tool_call", "tool_batch", "answer", "error", "delegate"}:
            raise ValueError("model output must be a typed TRACE-AH action")
        if action["type"] == "tool_call" and not {"tool_id", "arguments"} <= action.keys():
            raise ValueError("tool_call requires tool_id and arguments")
        if action["type"] == "tool_batch":
            calls = action.get("calls")
            if not isinstance(calls, list) or not calls:
                raise ValueError("tool_batch requires a non-empty calls list")
            if any(not isinstance(call, dict) or not {"tool_id", "arguments"} <= call.keys() for call in calls):
                raise ValueError("every batched call requires tool_id and arguments")
        if action["type"] == "answer" and not isinstance(action.get("content"), str):
            raise ValueError("answer requires string content")
        return action

    def _rehydrate(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
        conversation = list(messages)
        steps = 0
        tool_calls = 0
        for event in self.store.events:
            if event.get("run_id") != self.run_id:
                continue
            if event["type"] == "model_action":
                steps += 1
                conversation.append({"role": "assistant", "content": event["action"]})
            elif event["type"] == "tool_observation":
                tool_calls += 1
                observation = {key: value for key, value in event.items() if key not in {"type", "run_id", "sequence", "previous_digest", "digest"}}
                conversation.append({"role": "tool", "content": observation})
            elif event["type"] == "verification" and not event["result"].get("passed"):
                conversation.append({"role": "system", "content": {"verification_feedback": event["result"]}})
        return conversation, steps, tool_calls

    def run(self, messages: list[dict[str, Any]]) -> RunResult:
        started = time.monotonic()
        conversation, steps, tool_calls = self._rehydrate(messages)
        existing = [event for event in self.store.events if event.get("run_id") == self.run_id]
        evidence: list[str] = [event["digest"] for event in existing if "digest" in event]
        if existing and existing[-1]["type"] == "run_completed":
            return RunResult(
                self.run_id,
                "completed",
                existing[-1].get("answer"),
                steps,
                tool_calls,
                evidence,
            )
        event_type = "run_resumed" if self.store.events else "run_started"
        self.store.append({"type": event_type, "run_id": self.run_id, "limits": asdict(self.limits)})

        try:
            while steps < self.limits.max_steps:
                if time.monotonic() - started > self.limits.deadline_seconds:
                    raise TimeoutError("run deadline exceeded")
                steps += 1
                action = self._parse_action(self.model(conversation))
                evidence.append(self.store.append({"type": "model_action", "run_id": self.run_id, "action": action}))

                if action["type"] == "tool_call":
                    if tool_calls >= self.limits.max_tool_calls:
                        raise RuntimeError("tool-call budget exhausted")
                    tool_calls += 1
                    observation = self.registry.execute(
                        action["tool_id"],
                        action["arguments"],
                        allowed_tools=self.allowed_tools,
                        authorization_refs=self.authorization_refs,
                    )
                    evidence.append(self.store.append({"type": "tool_observation", "run_id": self.run_id, **observation}))
                    conversation.append({"role": "assistant", "content": action})
                    conversation.append({"role": "tool", "content": observation})
                    continue

                if action["type"] == "tool_batch":
                    calls = action["calls"]
                    if tool_calls + len(calls) > self.limits.max_tool_calls:
                        raise RuntimeError("tool-call budget exhausted")
                    tool_calls += len(calls)
                    observations = self.registry.execute_batch(
                        calls,
                        allowed_tools=self.allowed_tools,
                        authorization_refs=self.authorization_refs,
                    )
                    conversation.append({"role": "assistant", "content": action})
                    for observation in observations:
                        evidence.append(self.store.append({"type": "tool_observation", "run_id": self.run_id, **observation}))
                        conversation.append({"role": "tool", "content": observation})
                    continue

                if action["type"] == "delegate":
                    evidence.append(self.store.append({"type": "delegation_requested", "run_id": self.run_id, "task": action.get("task")}))
                    return RunResult(self.run_id, "awaiting_delegation", None, steps, tool_calls, evidence)

                if action["type"] == "error":
                    raise RuntimeError(str(action.get("message", "model reported an error")))

                verification = self.verifier(action["content"])
                evidence.append(self.store.append({"type": "verification", "run_id": self.run_id, "result": verification}))
                if verification.get("passed") is True:
                    self.store.append({"type": "run_completed", "run_id": self.run_id, "answer": action["content"]})
                    return RunResult(self.run_id, "completed", action["content"], steps, tool_calls, evidence)
                conversation.append({"role": "assistant", "content": action})
                conversation.append({"role": "system", "content": {"verification_feedback": verification}})

            raise RuntimeError("step budget exhausted")
        except PermissionError as error:
            self.store.append({"type": "run_blocked", "run_id": self.run_id, "error": str(error)})
            return RunResult(self.run_id, "blocked", None, steps, tool_calls, evidence, str(error))
        except Exception as error:
            self.store.append({"type": "run_failed", "run_id": self.run_id, "error": str(error)})
            return RunResult(self.run_id, "failed", None, steps, tool_calls, evidence, str(error))
