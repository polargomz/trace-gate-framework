"""Executable reference components for TRACE Gate Framework."""

from .gates import EvaluationReceipt, evaluate_gate
from .runtime import RunKernel, RunLimits, RunResult, ToolDefinition, ToolRegistry

__all__ = [
    "EvaluationReceipt",
    "RunKernel",
    "RunLimits",
    "RunResult",
    "ToolDefinition",
    "ToolRegistry",
    "evaluate_gate",
]

__version__ = "1.1.0"
