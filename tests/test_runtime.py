from trace_gate.runtime import EventStore, RunKernel, RunLimits, ToolDefinition, ToolRegistry


def test_runtime_executes_tool_then_verifies_answer() -> None:
    actions = iter(
        [
            {"type": "tool_call", "tool_id": "lookup", "arguments": {"key": "status"}},
            {"type": "answer", "content": "ready"},
        ]
    )
    registry = ToolRegistry()
    registry.register(ToolDefinition("lookup", "read state", "read", "green", lambda args: {args["key"]: "ready"}))
    kernel = RunKernel(
        run_id="RUN.TEST",
        model=lambda messages: next(actions),
        registry=registry,
        allowed_tools={"lookup"},
        verifier=lambda answer: {"passed": answer == "ready"},
    )
    result = kernel.run([{"role": "user", "content": "status?"}])
    assert result.status == "completed"
    assert result.tool_calls == 1
    assert result.answer == "ready"
    assert [event["sequence"] for event in kernel.store.events] == list(range(1, len(kernel.store.events) + 1))


def test_runtime_blocks_amber_tool_without_authority() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition("write", "write state", "write", "amber", lambda args: args, idempotency_strategy="request-key"))
    kernel = RunKernel(
        run_id="RUN.BLOCK",
        model=lambda messages: {"type": "tool_call", "tool_id": "write", "arguments": {}},
        registry=registry,
        allowed_tools={"write"},
        limits=RunLimits(max_steps=1, max_tool_calls=1, deadline_seconds=10),
    )
    result = kernel.run([{"role": "user", "content": "write"}])
    assert result.status == "blocked"
    assert "authorization" in result.last_error


def test_verification_feedback_reenters_loop() -> None:
    actions = iter([{"type": "answer", "content": "draft"}, {"type": "answer", "content": "final"}])
    registry = ToolRegistry()
    result = RunKernel(
        run_id="RUN.VERIFY",
        model=lambda messages: next(actions),
        registry=registry,
        allowed_tools=set(),
        verifier=lambda answer: {"passed": answer == "final", "reason": "must be final"},
    ).run([{"role": "user", "content": "answer"}])
    assert result.status == "completed"
    assert result.steps == 2


def test_read_only_batch_is_supported() -> None:
    actions = iter(
        [
            {
                "type": "tool_batch",
                "calls": [
                    {"tool_id": "read.one", "arguments": {}},
                    {"tool_id": "read.two", "arguments": {}},
                ],
            },
            {"type": "answer", "content": "done"},
        ]
    )
    registry = ToolRegistry()
    registry.register(ToolDefinition("read.one", "read one", "read", "green", lambda args: 1))
    registry.register(ToolDefinition("read.two", "read two", "read", "green", lambda args: 2))
    result = RunKernel(
        run_id="RUN.BATCH",
        model=lambda messages: next(actions),
        registry=registry,
        allowed_tools={"read.one", "read.two"},
    ).run([{"role": "user", "content": "read"}])
    assert result.status == "completed"
    assert result.tool_calls == 2


def test_jsonl_checkpoint_can_resume(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition("lookup", "read", "read", "green", lambda args: "value"))
    store_path = tmp_path / "run.jsonl"
    first = RunKernel(
        run_id="RUN.RESUME",
        model=lambda messages: {"type": "tool_call", "tool_id": "lookup", "arguments": {}},
        registry=registry,
        allowed_tools={"lookup"},
        limits=RunLimits(max_steps=1, max_tool_calls=2, deadline_seconds=10),
        event_store=EventStore(store_path),
    ).run([{"role": "user", "content": "read then answer"}])
    assert first.status == "failed"

    resumed = RunKernel(
        run_id="RUN.RESUME",
        model=lambda messages: {"type": "answer", "content": "resumed"},
        registry=registry,
        allowed_tools={"lookup"},
        limits=RunLimits(max_steps=3, max_tool_calls=2, deadline_seconds=10),
        event_store=EventStore(store_path),
    ).run([{"role": "user", "content": "read then answer"}])
    assert resumed.status == "completed"
    assert resumed.answer == "resumed"
    assert any(event["type"] == "run_resumed" for event in EventStore(store_path).events)
