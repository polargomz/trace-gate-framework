#!/usr/bin/env python3
"""Hash-pinned local quality-adapter binding with synthetic input only.

No network, credentials, model, collector runtime, database or publication.
The module path/hash must be supplied by the operator after inspecting the module.
Hash matching is not sandboxing: only trusted, reviewed adapter code may be loaded.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace_gate.gates import digest, evaluate_bound_gate
from trace_gate.validation import TEMPLATE_DIR, load_yaml


def check(module_path: Path, expected_sha256: str):
    raw = module_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError("QUALITY MODULE IDENTITY MISMATCH")
    # Execute precisely the already hashed bytes; this is trusted local code,
    # not content fetched from the web or an unreviewed user document.
    spec = importlib.util.spec_from_loader("quality_shadow", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(module_path), "exec"), module.__dict__)
    now = datetime.now(UTC)
    policy = load_yaml(TEMPLATE_DIR / "gate-decision.yaml")
    results = []
    for event, expected in [({"status": "PASS"}, "pass"),
                            ({"status": "FAIL_CLOSED", "error_code": "SYNTHETIC"}, "fail"),
                            ({"status": "WAIT_SAFETY_BUFFER"}, "fail"),
                            ({"http_status": 200}, "fail")]:
        classification = module.classify_event(event)
        if classification["automatic_retry"] or classification["canonical_promotion"]:
            raise ValueError("QUALITY AUTHORITY DRIFT")
        facts = {"success_rate": 100 if classification["replace_last_good"] else 0,
                 "duplicate_writes": 0, "forbidden_changes": 0,
                 "rollback_rehearsal": "pass", "evidence_refs": ["EVD.SYNTHETIC"]}
        record = {"run_id": "SHADOW.SYNTHETIC", "subject_revision": actual, "gate_id": policy["gate_id"],
                  "facts_digest": digest({k: v for k, v in facts.items() if k != "evidence_refs"}),
                  "observed_at": now.isoformat(), "expires_at": (now + timedelta(minutes=1)).isoformat(),
                  "passed": True}
        receipt = evaluate_bound_gate(policy, facts, {"EVD.SYNTHETIC": record},
                                      trusted_evidence_digests={"EVD.SYNTHETIC": digest(record)},
                                      run_id="SHADOW.SYNTHETIC", subject_revision=actual,
                                      evaluated_at=now.isoformat(), max_age_seconds=60)
        if receipt.status != expected:
            raise ValueError("SHADOW CLASSIFICATION MISMATCH")
        results.append({"classification": classification["quality"], "gate_status": receipt.status,
                        "receipt_digest": receipt.receipt_digest})
    return {"status": "ISOLATED_BINDING_PASS", "quality_module_sha256": actual, "cases": results,
            "source_input": "SYNTHETIC_ONLY", "operational_evidence": False,
            "runtime_changed": False, "production_activation": False, "network_calls": 0,
            "model_calls": 0, "push_merge_deploy": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality-module", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.quality_module, args.expected_sha256), indent=2))


if __name__ == "__main__":
    main()
