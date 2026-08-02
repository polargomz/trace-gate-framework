"""Executable reference components for TRACE Gate Framework."""

from .gates import EvaluationReceipt, evaluate_gate
from .runtime import RunKernel, RunLimits, RunResult, ToolDefinition, ToolRegistry
from .semantic import SemanticCandidateSet, select_semantic_candidates

__all__ = [
    "EvaluationReceipt",
    "RunKernel",
    "RunLimits",
    "RunResult",
    "SemanticCandidateSet",
    "ToolDefinition",
    "ToolRegistry",
    "evaluate_gate",
    "select_semantic_candidates",
]

__version__ = "1.1.0"
