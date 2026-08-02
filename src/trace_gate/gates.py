"""Deterministic Gate evaluator with reproducible receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import operator
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .validation import load_yaml, validate_value


EVALUATOR_ID = "trace-gate.reference-evaluator"
EVALUATOR_VERSION = "1.1.0"
COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}
EXPRESSION = re.compile(r"^(>=|<=|>|<|==|!=)\s*(.+)$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _literal(value: str) -> Any:
    if value.endswith("%"):
        value = value[:-1].strip()
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def compare(actual: Any, requirement: Any) -> tuple[bool, str]:
    if isinstance(requirement, str):
        match = EXPRESSION.match(requirement.strip())
        if match:
            symbol, raw_expected = match.groups()
            expected = _literal(raw_expected.strip())
            try:
                return COMPARATORS[symbol](actual, expected), f"{actual!r} {symbol} {expected!r}"
            except TypeError:
                return False, f"incomparable values: {actual!r} {symbol} {expected!r}"
    return actual == requirement, f"{actual!r} == {requirement!r}"


@dataclass(frozen=True)
class CheckResult:
    key: str
    actual: Any
    required: Any
    passed: bool
    expression: str


@dataclass(frozen=True)
class EvaluationReceipt:
    schema_version: str
    gate_id: str
    evaluated_at: str
    status: str
    evaluator: dict[str, str]
    input_digest: str
    checks: list[dict[str, Any]]
    evidence_refs: list[str]
    decision: dict[str, list[str]]
    signature: dict[str, str]
    receipt_digest: str


def evaluate_gate(
    policy: dict[str, Any],
    facts: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> EvaluationReceipt:
    validate_value("gate-decision.yaml", policy)
    expected_evaluator = policy["evaluator"]
    if expected_evaluator != {"id": EVALUATOR_ID, "version": EVALUATOR_VERSION}:
        raise ValueError("policy evaluator id/version does not match this executable")

    results: list[CheckResult] = []
    missing: list[str] = []
    for key, requirement in sorted(policy["required"].items()):
        if key not in facts:
            missing.append(key)
            continue
        passed, expression = compare(facts[key], requirement)
        results.append(CheckResult(key, facts[key], requirement, passed, expression))

    status = "blocked" if missing else ("pass" if all(item.passed for item in results) else "fail")
    checks = [asdict(item) for item in results]
    checks.extend(
        {"key": key, "actual": None, "required": policy["required"][key], "passed": False, "expression": "missing"}
        for key in missing
    )
    unsigned = {
        "schema_version": "1.1.0",
        "gate_id": policy["gate_id"],
        "evaluated_at": observed_at or datetime.now(UTC).isoformat(),
        "status": status,
        "evaluator": expected_evaluator,
        "input_digest": digest({"policy": policy, "facts": facts}),
        "checks": checks,
        "evidence_refs": list(facts.get("evidence_refs", [])),
        "decision": policy["decision"],
        "signature": {"status": "unsigned", "algorithm": "none"},
    }
    return EvaluationReceipt(**unsigned, receipt_digest=digest(unsigned))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path)
    parser.add_argument("facts", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    policy = load_yaml(args.policy)
    facts = load_yaml(args.facts)
    receipt = asdict(evaluate_gate(policy, facts))
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if receipt["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
