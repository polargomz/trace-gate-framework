"""Provider-neutral TRACE-AH reference orchestration loop."""

from __future__ import annotations

import hashlib
import json
import math
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
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
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
        if self.effect not in {"read", "write", "destructive"} or self.risk not in {"green", "amber", "red"}:
            raise ValueError("invalid tool effect/risk")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("invalid tool timeout")
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
        tool = self.preflight(tool_id, arguments, allowed_tools=allowed_tools, authorization_refs=authorization_refs)
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

    def preflight(self, tool_id, arguments, *, allowed_tools, authorization_refs):
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        _digest(arguments)
        if tool_id not in allowed_tools:
            raise PermissionError(f"tool is outside run scope: {tool_id}")
        tool = self.get(tool_id)
        if tool.risk == "amber" and not authorization_refs:
            raise PermissionError("amber tool requires an authorization reference")
        if tool.risk == "red" and f"red:{tool_id}" not in authorization_refs:
            raise PermissionError(f"red tool requires explicit red:{tool_id} authorization")

        return tool

    def execute_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        allowed_tools: set[str],
        authorization_refs: set[str],
    ) -> list[dict[str, Any]]:
        definitions = [self.preflight(call["tool_id"], call["arguments"], allowed_tools=allowed_tools,
                                      authorization_refs=authorization_refs) for call in calls]
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
        if (type(self.max_steps) is not int or type(self.max_tool_calls) is not int
            or type(self.deadline_seconds) not in (int, float)
            or not math.isfinite(self.deadline_seconds)
            or min(self.max_steps, self.max_tool_calls, self.deadline_seconds) <= 0):
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
    expected_head: str | None = None

    def __post_init__(self) -> None:
        if self.path and self.path.exists():
            self.events = self._read()
        self.validate()
        if self.expected_head is not None and (self.events[-1]["digest"] if self.events else None) != self.expected_head:
            raise ValueError("trusted checkpoint head mismatch")

    def _read(self):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate event key")
                result[key] = value
            return result
        return [json.loads(line, object_pairs_hook=unique) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def validate(self, *, check_disk=False):
        if check_disk and self.path:
            disk = self._read() if self.path.exists() else []
            if disk != self.events:
                raise ValueError("ledger changed externally; reload and verify")
        previous = None
        per_run = {}
        kinds = {"run_started", "run_resumed", "model_started", "model_action", "attempt_started",
                 "tool_observation", "verification", "delegation_requested", "run_completed", "run_failed", "run_blocked"}
        for sequence, event in enumerate(self.events, 1):
            if (event.get("sequence") != sequence or type(event.get("sequence")) is not int
                or event.get("previous_digest") != previous
                or event.get("digest") != _digest({k: v for k, v in event.items() if k != "digest"})):
                raise ValueError("event chain integrity mismatch")
            previous = event["digest"]
            run = per_run.setdefault(event["run_id"], [])
            if event.get("type") not in kinds:
                raise ValueError("unknown event type")
            if not run and event["type"] != "run_started":
                raise ValueError("run must begin with run_started")
            if run and run[-1]["type"] == "run_completed":
                raise ValueError("completed run is terminal")
            if run and event["type"] == "run_started":
                raise ValueError("duplicate run start")
            attempts = {e["attempt_id"]: e for e in run if e["type"] == "attempt_started"}
            observed = {e.get("attempt_id") for e in run if e["type"] == "tool_observation"}
            if event["type"] == "attempt_started" and event["attempt_id"] in attempts:
                raise ValueError("duplicate attempt identity")
            if event["type"] == "tool_observation" and run[0].get("runtime_version") == 2:
                attempt = attempts.get(event.get("attempt_id"))
                if (not attempt or event["attempt_id"] in observed or attempt["tool_id"] != event["tool_id"]
                    or attempt["effect"] != event["effect"] or event["output_digest"] != _digest(event["output"])):
                    raise ValueError("observation attempt/output binding mismatch")
            if event["type"] == "verification" and (not run or run[-1]["type"] != "model_action" or run[-1]["action"].get("type") != "answer"):
                raise ValueError("verification requires candidate answer")
            if event["type"] == "run_completed":
                if any(a["effect"] != "read" and key not in observed for key, a in attempts.items()):
                    raise ValueError("unresolved mutation attempts cannot complete")
                if not run or run[-1]["type"] != "verification" or run[-1]["result"].get("passed") is not True:
                    raise ValueError("completion requires verified result")
                answers = [e for e in run if e["type"] == "model_action" and e["action"].get("type") == "answer"]
                if not answers or event.get("answer") != answers[-1]["action"]["content"]:
                    raise ValueError("completion answer binding mismatch")
            run.append(event)

    def append(self, event: dict[str, Any]) -> str:
        self.validate(check_disk=True)
        envelope = {**event, "sequence": len(self.events) + 1, "previous_digest": self.events[-1]["digest"] if self.events else None}
        envelope["digest"] = _digest(envelope)
        # Validate a detached JSON snapshot, then publish to memory only after fsync.
        envelope = json.loads(json.dumps(envelope, allow_nan=False))
        EventStore(events=[*self.events, envelope])
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(envelope, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self.events.append(envelope)
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
        self.verifier = verifier
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
            if event["type"] == "model_started":
                steps += 1
            elif event["type"] == "model_action":
                conversation.append({"role": "assistant", "content": event["action"]})
            elif event["type"] == "attempt_started":
                tool_calls += 1
            elif event["type"] == "tool_observation":
                observation = {key: value for key, value in event.items() if key not in {"type", "run_id", "sequence", "previous_digest", "digest"}}
                conversation.append({"role": "tool", "content": observation})
            elif event["type"] == "verification" and not event["result"].get("passed"):
                conversation.append({"role": "system", "content": {"verification_feedback": event["result"]}})
        return conversation, steps, tool_calls

    def run(self, messages: list[dict[str, Any]], *, resume_approval_ref: str | None = None) -> RunResult:
        # Approval references are supplied by the trusted application resolver,
        # never by the model. A resume cannot widen the original run envelope.
        self.store.validate(check_disk=True)
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
        scope = _digest({"messages": messages, "allowed_tools": sorted(self.allowed_tools),
                         "authorization_refs": sorted(self.authorization_refs), "limits": asdict(self.limits)})
        if existing:
            if existing[0].get("runtime_version") != 2 or existing[0].get("scope_digest") != scope:
                return RunResult(self.run_id, "blocked", None, steps, tool_calls, evidence, "legacy or changed run envelope; use a new authorized run")
            if not isinstance(resume_approval_ref, str) or not resume_approval_ref.strip():
                return RunResult(self.run_id, "blocked", None, steps, tool_calls, evidence, "explicit resume approval required")
            completed_attempts = {e.get("attempt_id") for e in existing if e["type"] == "tool_observation"}
            if any(e["type"] == "attempt_started" and e["effect"] != "read" and e["attempt_id"] not in completed_attempts for e in existing):
                return RunResult(self.run_id, "blocked", None, steps, tool_calls, evidence, "unresolved mutation; reconcile before a new authorized run")
            deadline = existing[0]["deadline_epoch"]
            event = {"type": "run_resumed", "run_id": self.run_id, "resume_approval_ref": resume_approval_ref}
        else:
            deadline = time.time() + self.limits.deadline_seconds
            event = {"type": "run_started", "run_id": self.run_id, "runtime_version": 2,
                     "scope_digest": scope, "limits": asdict(self.limits), "deadline_epoch": deadline}
        evidence.append(self.store.append(event))
        monotonic_deadline = time.monotonic() + max(0, deadline - time.time())
        def check_deadline():
            if time.time() >= deadline or time.monotonic() >= monotonic_deadline:
                raise TimeoutError("run deadline exceeded")

        try:
            if not callable(self.verifier):
                raise PermissionError("verifier is required")
            while steps < self.limits.max_steps:
                check_deadline()
                steps += 1
                evidence.append(self.store.append({"type": "model_started", "run_id": self.run_id}))
                action = self._parse_action(self.model(conversation))
                check_deadline()
                evidence.append(self.store.append({"type": "model_action", "run_id": self.run_id, "action": action}))

                if action["type"] == "tool_call":
                    if tool_calls >= self.limits.max_tool_calls:
                        raise RuntimeError("tool-call budget exhausted")
                    tool = self.registry.preflight(action["tool_id"], action["arguments"], allowed_tools=self.allowed_tools, authorization_refs=self.authorization_refs)
                    tool_calls += 1
                    attempt_id = f"{self.run_id}:{tool_calls}"
                    evidence.append(self.store.append({"type": "attempt_started", "run_id": self.run_id, "attempt_id": attempt_id,
                                                       "tool_id": tool.tool_id, "effect": tool.effect, "arguments_digest": _digest(action["arguments"])}))
                    observation = self.registry.execute(
                        action["tool_id"],
                        action["arguments"],
                        allowed_tools=self.allowed_tools,
                        authorization_refs=self.authorization_refs,
                    )
                    evidence.append(self.store.append({"type": "tool_observation", "run_id": self.run_id, "attempt_id": attempt_id, **observation}))
                    check_deadline()
                    conversation.append({"role": "assistant", "content": action})
                    conversation.append({"role": "tool", "content": observation})
                    continue

                if action["type"] == "tool_batch":
                    calls = action["calls"]
                    if tool_calls + len(calls) > self.limits.max_tool_calls:
                        raise RuntimeError("tool-call budget exhausted")
                    definitions = [self.registry.preflight(c["tool_id"], c["arguments"], allowed_tools=self.allowed_tools, authorization_refs=self.authorization_refs) for c in calls]
                    if any(t.effect != "read" for t in definitions):
                        raise PermissionError("batched tool calls must all be read-only")
                    attempt_ids = []
                    for call, tool in zip(calls, definitions):
                        tool_calls += 1
                        attempt_ids.append(f"{self.run_id}:{tool_calls}")
                        evidence.append(self.store.append({"type": "attempt_started", "run_id": self.run_id, "attempt_id": attempt_ids[-1],
                                                           "tool_id": tool.tool_id, "effect": tool.effect, "arguments_digest": _digest(call["arguments"])}))
                    observations = self.registry.execute_batch(
                        calls,
                        allowed_tools=self.allowed_tools,
                        authorization_refs=self.authorization_refs,
                    )
                    conversation.append({"role": "assistant", "content": action})
                    for attempt_id, observation in zip(attempt_ids, observations):
                        evidence.append(self.store.append({"type": "tool_observation", "run_id": self.run_id, "attempt_id": attempt_id, **observation}))
                        conversation.append({"role": "tool", "content": observation})
                    check_deadline()
                    continue

                if action["type"] == "delegate":
                    evidence.append(self.store.append({"type": "delegation_requested", "run_id": self.run_id, "task": action.get("task")}))
                    return RunResult(self.run_id, "awaiting_delegation", None, steps, tool_calls, evidence)

                if action["type"] == "error":
                    raise RuntimeError(str(action.get("message", "model reported an error")))

                verification = self.verifier(action["content"])
                check_deadline()
                if not isinstance(verification, dict) or type(verification.get("passed")) is not bool:
                    raise ValueError("verifier must return a boolean passed field")
                evidence.append(self.store.append({"type": "verification", "run_id": self.run_id, "result": verification}))
                if verification.get("passed") is True:
                    evidence.append(self.store.append({"type": "run_completed", "run_id": self.run_id, "answer": action["content"]}))
                    return RunResult(self.run_id, "completed", action["content"], steps, tool_calls, evidence)
                conversation.append({"role": "assistant", "content": action})
                conversation.append({"role": "system", "content": {"verification_feedback": verification}})

            raise RuntimeError("step budget exhausted")
        except PermissionError as error:
            evidence.append(self.store.append({"type": "run_blocked", "run_id": self.run_id, "error": str(error)}))
            return RunResult(self.run_id, "blocked", None, steps, tool_calls, evidence, str(error))
        except Exception as error:
            evidence.append(self.store.append({"type": "run_failed", "run_id": self.run_id, "error": str(error)}))
            return RunResult(self.run_id, "failed", None, steps, tool_calls, evidence, str(error))
