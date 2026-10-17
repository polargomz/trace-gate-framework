"""Public synthetic quality adapter; no consumer code or runtime access."""
import hashlib
import importlib.util
from pathlib import Path

import pytest


def checker():
    path = Path(__file__).resolve().parents[1] / "scripts/check_quality_shadow_binding.py"
    spec = importlib.util.spec_from_file_location("shadow_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


def test_quality_shadow_synthetic_contract(tmp_path):
    path = tmp_path / "quality.py"
    path.write_text('''def classify_event(event):
    status = event.get("status")
    quality = {"PASS": "VALIDATED", "FAIL_CLOSED": "COLLECTION_ERROR",
               "WAIT_SAFETY_BUFFER": "WAITING_STABILITY"}.get(status, "UNVALIDATED")
    return {"quality": quality, "replace_last_good": status == "PASS",
            "automatic_retry": False, "canonical_promotion": False}
''')
    result = checker()(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert result["status"] == "ISOLATED_BINDING_PASS"
    assert [case["gate_status"] for case in result["cases"]] == ["pass", "fail", "fail", "fail"]
    assert result["network_calls"] == result["model_calls"] == 0
    assert result["operational_evidence"] is False


def test_quality_shadow_rejects_hash_before_execution(tmp_path):
    path = tmp_path / "quality.py"
    path.write_text('raise AssertionError("must never execute")')
    with pytest.raises(ValueError, match="IDENTITY MISMATCH"):
        checker()(path, "0" * 64)
