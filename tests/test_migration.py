from copy import deepcopy
import json

import jsonschema
import pytest

from trace_gate.migration import PHASES, REQUIRED, digest, transition
from trace_gate.validation import ROOT, TEMPLATE_DIR, load_yaml, validate_value

H = "a" * 64


def state(phase="planned"):
    value = deepcopy(load_yaml(TEMPLATE_DIR / "document-migration-transaction.yaml"))
    value["phase"] = value["last_verified_phase"] = phase
    if phase != "planned":
        value["source_write_state"] = "frozen"
        value["fencing"].update(mechanism="durable_cas", **{k:True for k in ("verified", "all_writers_quiescent", "overlap_checked", "stale_writer_checked", "offline_writer_checked", "scope_only")})
    if PHASES.index(phase) >= PHASES.index("packaged"):
        value["archive_sha256"] = H
    for stage in PHASES[:PHASES.index(phase)+1]:
        for kind in sorted(REQUIRED.get(stage, set())):
            value["evidence"].append({"evidence_id":"EVIDENCE."+kind,"kind":kind,
                "transaction_id":value["transaction_id"],"plan_sha256":value["migration_plan"]["approval"]["plan_sha256"],
                "scope_sha256":value["migration_plan"]["plan"]["scope_sha256"],"subject_sha256":H,
                "result":"pass","verifier_id":"VERIFIER.EXAMPLE","verified_at":"2026-01-01T00:00:00Z","supersedes_evidence_ids":[]})
    if phase == "completed":
        value.update(source_write_state="preservation_frozen", canonical_owner="PARTITION.DESTINATION", authority_epoch=2)
        value["cutover_receipt"] = deepcopy(load_yaml(TEMPLATE_DIR / "document-cutover-receipt.yaml"))
        resign(value)
    return value


def resign(value):
    c = value["cutover_receipt"]
    c["evidence_ids"] = sorted(r["evidence_id"] for r in value["evidence"])
    c["evidence_sha256"] = {r["evidence_id"]:digest(r) for r in value["evidence"]}
    c["receipt_sha256"] = digest({k:v for k,v in c.items() if k != "receipt_sha256"})


def reapprove(plan):
    plan["approval"]["plan_sha256"] = digest(plan["plan"])


def validate(value):
    validate_value("document-migration-transaction.yaml", value)


def test_additive_schema_valid_and_old_schema_unmodified():
    jsonschema.Draft202012Validator.check_schema(json.loads((ROOT/"schemas/document-migration.schema.json").read_text()))
    schema = json.loads((ROOT/"schemas/trace-gate.schema.json").read_text())
    assert "migrationTransaction" not in schema["$defs"]


@pytest.mark.parametrize("phase", PHASES)
def test_valid_states_and_completed_not_activated(phase):
    value = state(phase); validate(value)
    assert value["destination_business_writes"] is False


@pytest.mark.parametrize("phase", PHASES[:-1])
def test_destination_cannot_write_at_or_before_received_complete(phase):
    value = state(phase); value["destination_business_writes"] = True
    with pytest.raises(ValueError, match="activation before"):validate(value)


@pytest.mark.parametrize("kind", sorted(set().union(*REQUIRED.values())))
def test_missing_or_failed_required_evidence_cannot_complete(kind):
    value = state("completed")
    next(r for r in value["evidence"] if r["kind"] == kind)["result"] = "fail"
    resign(value)
    with pytest.raises(ValueError, match="missing or failed"):validate(value)


@pytest.mark.parametrize("field", ["verified","all_writers_quiescent","overlap_checked","stale_writer_checked","offline_writer_checked","scope_only"])
def test_unverified_stale_offline_or_overlapping_writer_blocks(field):
    value = state("received_complete"); value["fencing"][field] = False
    with pytest.raises(ValueError, match="fencing unverified"):validate(value)


@pytest.mark.parametrize("mechanism", ["memory_lock", "cloud_sync", "unverified"])
def test_memory_lock_and_cloud_sync_not_fencing(mechanism):
    value = state("received_complete");value["fencing"]["mechanism"] = mechanism
    with pytest.raises(ValueError, match="not distributed fencing"):validate(value)


def test_single_cutover_receipt_is_required():
    value = state("completed");value["cutover_receipt"] = None
    with pytest.raises(ValueError, match="cutover receipt missing"):validate(value)


def test_cutover_evidence_subject_change_not_hidden_by_same_ids():
    value = state("completed");value["evidence"][0]["subject_sha256"] = "b"*64
    with pytest.raises(ValueError, match="evidence content mismatch"):validate(value)


def test_bilateral_incomplete_and_source_thaw_rejected():
    value = state("completed");value["cutover_receipt"]["bilateral_verified"] = False;resign(value)
    with pytest.raises(ValueError, match="bilateral"):validate(value)
    value = state("completed");value["source_write_state"] = "active"
    with pytest.raises(ValueError, match="frozen"):validate(value)


def test_completed_does_not_implicitly_authorize_business_restart():
    value = state("completed");value["destination_business_writes"] = True
    with pytest.raises(ValueError, match="restart approval"):validate(value)
    current = state("completed");value["business_resume_approval_ref"] = "APPROVAL.EXPLICIT.RESUME"
    assert transition(current,value) == value


def test_activation_cannot_change_owner_cutover_history():
    current = state("completed");candidate = deepcopy(current)
    candidate.update(destination_business_writes=True,business_resume_approval_ref="APPROVAL.EXPLICIT.RESUME")
    candidate["fencing"]["proof_sha256"] = "b"*64
    with pytest.raises(ValueError, match="rewrite completed cutover"):transition(current,candidate)


def test_scope_and_plan_digest_drift_are_blocking():
    value = state();value["migration_plan"]["plan"]["scope_paths"] = ["other"]
    with pytest.raises(ValueError, match="plan digest mismatch"):validate(value)
    candidate = state();candidate["migration_plan"]["plan"]["cutoff"] = "2026-01-02T00:00:00Z";reapprove(candidate["migration_plan"])
    with pytest.raises(ValueError, match="plan drift"):transition(state(),candidate)


@pytest.mark.parametrize("change,error", [
    (lambda p:p["scope_paths"].append("payload/sub"),"overlapping"),
    (lambda p:p["artifacts"][0]["parent_ids"].append("MISSING.PARENT"),"parent closure"),
    (lambda p:p["artifacts"][0]["parent_ids"].append("ARTIFACT.EXAMPLE"),"parent cycle"),
    (lambda p:p["artifacts"].append(deepcopy(p["artifacts"][0])),"duplicate artifact"),
    (lambda p:p["shared_records"].append({"artifact_id":"ARTIFACT.EXAMPLE","canonical_owner":"PARTITION.SOURCE","transfer":False}),"shared canonical"),
])
def test_scope_partition_parent_and_shared_owner_guards(change,error):
    value = state();change(value["migration_plan"]["plan"]);reapprove(value["migration_plan"])
    with pytest.raises(ValueError, match=error):validate(value)


def test_nested_source_destination_root_blocks():
    value = state();p=value["migration_plan"]["plan"];p["destination"].update(host_id=p["source"]["host_id"],root="projects/source/child");reapprove(value["migration_plan"])
    with pytest.raises(ValueError, match="nested"):validate(value)


def test_mixed_ledger_export_requires_anchor_and_parent_closure():
    value = state();p=value["migration_plan"]["plan"]
    p["shared_records"]=[{"artifact_id":"LEDGER.SHARED","canonical_owner":"PARTITION.SHARED","transfer":False}]
    export={"source_artifact_id":"LEDGER.SHARED","original_ledger_sha256":H,"original_anchor_sha256":H,"selection_sha256":H,"export_sha256":H,"parent_ids":[],"original_chain_preserved":True}
    p["scoped_exports"]=[export];reapprove(value["migration_plan"]);validate(value)
    export["parent_ids"]=["MISSING.PARENT"];reapprove(value["migration_plan"])
    with pytest.raises(ValueError, match="parent closure"):validate(value)


def test_relocation_new_management_revision_and_payload_identity():
    value = load_yaml(TEMPLATE_DIR/"document-relocation-map.yaml")
    validate_value("document-relocation-map.yaml",value)
    value["maps"][0]["new_sha256"]="b"*64
    with pytest.raises(ValueError, match="content change"):validate_value("document-relocation-map.yaml",value)
    value["maps"][0]["content_changed"]=True
    with pytest.raises(ValueError, match="derived revision"):validate_value("document-relocation-map.yaml",value)


def test_archive_hash_and_delivery_are_not_restore_receipt():
    value = state("received_complete")
    value["evidence"]=[r for r in value["evidence"] if r["kind"]!="restore_rehearsal"]
    with pytest.raises(ValueError, match="missing or failed"):validate(value)


def test_retained_and_delivered_archive_digest_must_agree():
    value = state("delivered");value["archive_sha256"]="b"*64
    with pytest.raises(ValueError, match="archive digest mismatch"):validate(value)


def test_no_duplicate_final_owner_or_empty_scope():
    value = state("completed");value["cutover_receipt"]["owners"].append({"artifact_id":"ARTIFACT.EXAMPLE","partition_id":"PARTITION.SOURCE"});resign(value)
    with pytest.raises(ValueError, match="multiple canonical"):validate(value)


def test_partial_commit_keeps_both_business_writers_held():
    value = state("cutover_pending");value.update(phase="partial_commit",canonical_owner=None,blockers=["Source pointer committed, destination ack uncertain"])
    validate(value)
    value["destination_business_writes"]=True
    with pytest.raises(ValueError, match="activation before"):validate(value)


def test_transition_idempotent_replay_and_step_order():
    before = state("received_complete");after = state("cutover_pending")
    assert transition(before,after)==after
    assert transition(after,deepcopy(after))==after
    with pytest.raises(ValueError, match="stage skipped"):transition(state(),state("received_complete"))


def test_resume_preserves_failed_history_with_explicit_supersession():
    value=state("completed");failed=deepcopy(value["evidence"][0]);failed.update(evidence_id="EVIDENCE.FAILED",result="fail")
    value["evidence"].insert(0,failed);value["evidence"][1]["supersedes_evidence_ids"]=[failed["evidence_id"]];resign(value)
    validate(value);assert value["evidence"][0]["result"]=="fail"
    value["evidence"][1]["supersedes_evidence_ids"]=[];resign(value)
    with pytest.raises(ValueError, match="failure cannot be completed"):validate(value)


def test_historical_evidence_cannot_be_rewritten_on_resume():
    before=state("received_complete");after=state("cutover_pending");after["evidence"][0]["verified_at"]="2026-01-02T00:00:00Z"
    with pytest.raises(ValueError, match="historical evidence rewritten"):transition(before,after)


def test_rollback_preserves_new_development_and_never_enables_writes():
    value=load_yaml(TEMPLATE_DIR/"document-migration-compensation.yaml")
    validate_value("document-migration-compensation.yaml",value)
    value.update(result="pass",blockers=[],destination_quiesced=True,restore_verified=True)
    with pytest.raises(ValueError, match="new data"):validate_value("document-migration-compensation.yaml",value)
    value.update(new_data_preserved=True,new_data_backup_sha256=H);validate_value("document-migration-compensation.yaml",value)
    value["t0_overwrite"]=True
    with pytest.raises(ValueError):validate_value("document-migration-compensation.yaml",value)


def test_declared_completed_with_old_verified_phase_is_rejected():
    value=state("completed");value["last_verified_phase"]="received_complete"
    with pytest.raises(ValueError,match="merely attempted"):validate(value)


def test_stale_expected_epoch_and_early_epoch_change_are_rejected():
    before=state("cutover_pending");before["authority_epoch"]=9
    with pytest.raises(ValueError,match="expected epoch stale"):transition(before,state("completed"))
    after=state("cutover_pending");after["authority_epoch"]=2
    with pytest.raises(ValueError,match="epoch changed before"):transition(state("received_complete"),after)


def test_unknown_evidence_and_binding_mismatch_cannot_receive():
    value=state("received_complete");value["evidence"][0]["result"]="unknown"
    with pytest.raises(ValueError,match="missing or failed"):validate(value)
    value=state("received_complete");value["evidence"][0]["plan_sha256"]="b"*64
    with pytest.raises(ValueError,match="binding mismatch"):validate(value)


def test_mixed_ledger_cannot_claim_rewritten_chain_is_preserved():
    value=state();p=value["migration_plan"]["plan"]
    p["shared_records"]=[{"artifact_id":"LEDGER.SHARED","canonical_owner":"PARTITION.SHARED","transfer":False}]
    p["scoped_exports"]=[{"source_artifact_id":"LEDGER.SHARED","original_ledger_sha256":H,"original_anchor_sha256":H,"selection_sha256":H,"export_sha256":H,"parent_ids":[],"original_chain_preserved":False}]
    reapprove(value["migration_plan"])
    with pytest.raises(ValueError):validate(value)
