"""Deterministic provider-neutral TRACE-AH demonstration."""

from trace_gate.runtime import RunKernel, ToolDefinition, ToolRegistry


actions = iter(
    [
        {"type": "tool_call", "tool_id": "status.read", "arguments": {"component": "collector"}},
        {"type": "answer", "content": "collector is staging-only"},
    ]
)

registry = ToolRegistry()
registry.register(
    ToolDefinition(
        tool_id="status.read",
        description="Read component state",
        effect="read",
        risk="green",
        handler=lambda arguments: {"component": arguments["component"], "authority": "staging-only"},
    )
)

result = RunKernel(
    run_id="RUN.EXAMPLE",
    model=lambda messages: next(actions),
    registry=registry,
    allowed_tools={"status.read"},
    verifier=lambda answer: {"passed": "staging-only" in answer},
).run([{"role": "user", "content": "What authority does the collector have?"}])

print(result)
