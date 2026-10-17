"""Deterministic Gate evaluator with reproducible receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import operator
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .validation import load_yaml, validate_value


EVALUATOR_ID = "trace-gate.reference-evaluator"
EVALUATOR_VERSION = "1.1.1"
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
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


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
        return float(value) if any(c in value.lower() for c in (".", "e")) else int(value)
    except ValueError:
        return value


def compare(actual: Any, requirement: Any) -> tuple[bool, str]:
    def compatible(expected: Any) -> bool:
        if type(expected) in (int, float):
            return type(actual) in (int, float) and math.isfinite(actual) and math.isfinite(expected)
        return type(actual) is type(expected)
    if isinstance(requirement, str):
        match = EXPRESSION.match(requirement.strip())
        if match:
            symbol, raw_expected = match.groups()
            expected = _literal(raw_expected.strip())
            if not compatible(expected):
                return False, "incompatible fact type"
            try:
                return COMPARATORS[symbol](actual, expected), f"{actual!r} {symbol} {expected!r}"
            except TypeError:
                return False, f"incomparable values: {actual!r} {symbol} {expected!r}"
    return compatible(requirement) and actual == requirement, f"{actual!r} == {requirement!r}"


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
    canonical_json(facts)  # Reject NaN/Infinity and non-JSON objects before hashing.
    if "facts_schema" in policy:
        jsonschema.Draft202012Validator.check_schema(policy["facts_schema"])
        jsonschema.validate(facts, policy["facts_schema"])
    refs = facts.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs) or len(refs) != len(set(refs)):
        raise ValueError("evidence_refs must be unique non-empty strings")
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

    status = "blocked" if missing or not refs else ("pass" if all(item.passed for item in results) else "fail")
    checks = [asdict(item) for item in results]
    checks.extend(
        {"key": key, "actual": None, "required": policy["required"][key], "passed": False, "expression": "missing"}
        for key in missing
    )
    if not refs:
        checks.append({"key": "evidence_refs", "actual": [], "required": "non-empty", "passed": False, "expression": "missing evidence references"})
    unsigned = {
        "schema_version": "1.1.0",
        "gate_id": policy["gate_id"],
        "evaluated_at": observed_at or datetime.now(UTC).isoformat(),
        "status": status,
        "evaluator": expected_evaluator,
        "input_digest": digest({"policy": policy, "facts": facts}),
        "checks": checks,
        "evidence_refs": list(facts.get("evidence_refs", [])),
        "decision": {**policy["decision"], "newly_allowed_authority": policy["decision"]["newly_allowed_authority"] if status == "pass" else []},
        "signature": {"status": "unsigned", "algorithm": "none"},
    }
    return EvaluationReceipt(**unsigned, receipt_digest=digest(unsigned))


def evaluate_bound_gate(policy, facts, evidence, *, trusted_evidence_digests,
                        run_id, subject_revision, evaluated_at, max_age_seconds):
    """Operational evidence binding; trusted digests MUST come from an independent resolver.

    Does not authenticate callers or grant execution authority. The plain evaluator
    remains a threshold calculator, not an operational promotion endpoint.
    """
    if not policy.get("facts_schema") or type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("bound gate requires facts schema and finite freshness limit")
    def timestamp(value):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result
    now = timestamp(evaluated_at)
    refs = facts.get("evidence_refs", [])
    if not refs or set(refs) != set(evidence) or set(refs) != set(trusted_evidence_digests):
        raise ValueError("evidence coverage mismatch")
    facts_digest = digest({k: v for k, v in facts.items() if k != "evidence_refs"})
    for ref in refs:
        record = evidence[ref]
        if digest(record) != trusted_evidence_digests[ref]:
            raise ValueError("untrusted evidence digest")
        if (record.get("run_id"), record.get("subject_revision"), record.get("gate_id"), record.get("facts_digest")) != (run_id, subject_revision, policy["gate_id"], facts_digest):
            raise ValueError("evidence binding mismatch")
        age = (now - timestamp(record["observed_at"])).total_seconds()
        if not 0 <= age <= max_age_seconds or now >= timestamp(record["expires_at"]) or record.get("passed") is not True:
            raise ValueError("evidence expired, future, or not passed")
    receipt = asdict(evaluate_gate(policy, facts, observed_at=evaluated_at))
    receipt["input_digest"] = digest({"policy": policy, "facts": facts,
                                     "trusted_evidence_digests": trusted_evidence_digests,
                                     "run_id": run_id, "subject_revision": subject_revision,
                                     "max_age_seconds": max_age_seconds})
    receipt["receipt_digest"] = digest({k: v for k, v in receipt.items() if k != "receipt_digest"})
    return EvaluationReceipt(**receipt)


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
