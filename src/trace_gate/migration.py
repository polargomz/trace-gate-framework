"""Additive migration contracts; evidence validation is not distributed fencing."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import unicodedata
from typing import Any

VERSION = "0.1.0"
PHASES = ("planned", "migrating_frozen", "packaged", "delivered", "relocated", "received_complete", "cutover_pending", "completed")
REQUIRED = {
    "migrating_frozen": {"scope_verified", "dependency_verified", "writer_quiescence_verified", "scope_freeze", "backup", "restore_rehearsal"},
    "packaged": {"package_retained"},
    "delivered": {"delivery_verified"},
    "relocated": {"safe_extraction", "relocation_verified"},
    "received_complete": {"destination_acceptance"},
    "cutover_pending": {"source_transfer", "destination_prepared"},
    "completed": {"pointers_verified", "single_owner_verified", "cutover_verified"},
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def relative_path(value: str) -> str:
    require(isinstance(value, str) and bool(value), "empty path")
    require(not any(ord(c) < 32 or ord(c) == 127 for c in value), "control character path")
    require("\\" not in value and ":" not in value and not value.startswith("/"), "absolute, drive or UNC path")
    require(value == unicodedata.normalize("NFC", value), "non-NFC path")
    require(all(part not in ("", ".", "..") and not part.endswith((".", " ")) for part in value.split("/")), "unsafe path component")
    require(all(part.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL", *("COM"+str(i) for i in range(1,10)), *("LPT"+str(i) for i in range(1,10))} for part in value.split("/")), "reserved path component")
    return value


def related_paths(a: str, b: str) -> bool:
    x, y = PurePosixPath(a), PurePosixPath(b)
    return x == y or x in y.parents or y in x.parents


def unique_paths(paths: list[str]) -> None:
    aliases: dict[str, str] = {}
    explicit: set[str] = set()
    for path in paths:
        relative_path(path)
        require(path not in explicit, "duplicate path")
        explicit.add(path)
        parts = path.split("/")
        for end in range(1, len(parts)+1):
            prefix = "/".join(parts[:end]); key = prefix.casefold()
            require(key not in aliases or aliases[key] == prefix, "Unicode or case path collision")
            aliases[key] = prefix


def plan_errors(value: dict[str, Any]) -> list[str]:
    p = value["plan"]
    require(digest(p) == value["approval"]["plan_sha256"], "approved plan digest mismatch")
    require(p["source"]["partition_id"] != p["destination"]["partition_id"], "same partition")
    if p["source"]["host_id"] == p["destination"]["host_id"]:
        require(not related_paths(p["source"]["root"].casefold(), p["destination"]["root"].casefold()), "nested source/destination roots")
    for side in ("source", "destination"):
        relative_path(p[side]["root"])
    unique_paths(p["scope_paths"])
    require(not any(related_paths(a.casefold(), b.casefold()) for i,a in enumerate(p["scope_paths"]) for b in p["scope_paths"][i+1:]), "overlapping scope paths")
    keys = [(a["artifact_id"], a["revision_id"]) for a in p["artifacts"]]
    require(len(keys) == len(set(keys)), "duplicate artifact revision")
    known = {a["artifact_id"] for a in p["artifacts"]} | set(p["external_parent_ids"])
    for a in p["artifacts"]:
        relative_path(a["path"])
        require(any(a["path"] == root or PurePosixPath(root) in PurePosixPath(a["path"]).parents for root in p["scope_paths"]), "artifact outside scope")
        require(set(a["parent_ids"]) <= known, "parent closure missing")
        require(a["canonical_owner"] == p["source"]["partition_id"], "migrating artifact not source owned")
    shared = {s["artifact_id"] for s in p["shared_records"]}
    require(not shared & {a["artifact_id"] for a in p["artifacts"]}, "shared canonical record cannot be transferred")
    graph = {a["artifact_id"]:a["parent_ids"] for a in p["artifacts"]}
    visiting: set[str] = set(); done: set[str] = set()
    def visit(node: str) -> None:
        require(node not in visiting, "artifact parent cycle")
        if node in done or node not in graph:
            return
        visiting.add(node)
        for parent in graph[node]:visit(parent)
        visiting.remove(node); done.add(node)
    for node in graph:visit(node)
    for export in p["scoped_exports"]:
        require(export["source_artifact_id"] in shared, "scoped export lacks shared owner")
        require(set(export["parent_ids"]) <= known | shared, "export parent closure missing")
        require(export["original_chain_preserved"], "mixed ledger chain rewritten")
    require(p["archive"]["transfer_mode"] == "copy" and p["archive"]["retain_original"], "archive must be retained and copied")
    require(p["writer_policy"]["overlap_check_required"] and p["writer_policy"]["stale_and_offline_check_required"], "writer boundary checks required")
    return []


def package_errors(value: dict[str, Any]) -> list[str]:
    files = value["files"]
    unique_paths([r["path"] for r in files])
    names = {r["path"] for r in files}
    require("TRACE-DM-MANIFEST.json" not in names, "manifest cannot contain its own hash")
    for name in names:
        require(not any(str(p) in names for p in PurePosixPath(name).parents if str(p) != "."), "file used as parent directory")
    return []


def relocation_errors(value: dict[str, Any]) -> list[str]:
    keys = [(r["artifact_id"], r["old_revision_id"]) for r in value["maps"]]
    require(len(keys) == len(set(keys)), "duplicate relocation identity")
    for row in value["maps"]:
        relative_path(row["old_path"]); relative_path(row["new_path"])
        require(row["old_management_revision"] != row["new_management_revision"], "location change needs management revision")
        require((row["old_sha256"] != row["new_sha256"]) == row["content_changed"], "content change/hash disagreement")
        if row["content_changed"]:
            require(row["old_revision_id"] != row["new_revision_id"], "changed content needs derived revision")
        else:
            require(row["old_revision_id"] == row["new_revision_id"], "unchanged payload identity drift")
    unique_paths([r["new_path"] for r in value["maps"]])
    return []


def cutover_errors(value: dict[str, Any]) -> list[str]:
    require(value["receipt_sha256"] == digest({k:v for k,v in value.items() if k != "receipt_sha256"}), "cutover receipt digest mismatch")
    require(value["source_partition"] != value["destination_partition"], "same cutover partition")
    require(value["new_epoch"] > value["expected_epoch"], "owner epoch must increase")
    require(value["source_preservation_frozen"] and value["destination_accepted"] and value["bilateral_verified"], "bilateral cutover incomplete")
    owners = value["owners"]
    require(len({r["artifact_id"] for r in owners}) == len(owners), "multiple canonical owners")
    require(all(r["partition_id"] == value["destination_partition"] for r in owners), "incorrect final owner")
    require(set(value["evidence_sha256"]) == set(value["evidence_ids"]), "cutover evidence digest coverage")
    return []


def transaction_errors(value: dict[str, Any]) -> list[str]:
    plan_errors(value["migration_plan"])
    p = value["migration_plan"]["plan"]; binding = value["migration_plan"]["approval"]["plan_sha256"]
    require(value["transaction_id"] == p["transaction_id"], "transaction identity mismatch")
    require(value["idempotency_key"] == p["idempotency_key"], "idempotency mismatch")
    phase = value["phase"]
    rank = PHASES.index(phase) if phase in PHASES else PHASES.index(value["last_verified_phase"])
    require(PHASES.index(value["last_verified_phase"]) <= rank, "last verified phase ahead of state")
    if phase in PHASES:
        require(value["last_verified_phase"] == phase, "normal phase must be verified, not merely attempted")
    receipts = value["evidence"]
    require(len({r["evidence_id"] for r in receipts}) == len(receipts), "duplicate evidence id")
    for r in receipts:
        require(r["transaction_id"] == value["transaction_id"] and r["plan_sha256"] == binding and r["scope_sha256"] == p["scope_sha256"], "evidence binding mismatch")
    seen = {}; superseded = set()
    for r in receipts:
        for previous in r["supersedes_evidence_ids"]:
            require(previous in seen and seen[previous]["kind"] == r["kind"], "invalid evidence supersession/closure")
            require(r["result"] == "pass", "failed correction cannot supersede evidence")
            superseded.add(previous)
        seen[r["evidence_id"]] = r
    effective = [r for r in receipts if r["evidence_id"] not in superseded]
    passed = {r["kind"] for r in effective if r["result"] == "pass"}
    needed = set().union(*(REQUIRED.get(s, set()) for s in PHASES[:rank+1]))
    require(needed <= passed, "required stage evidence missing or failed")
    if rank >= PHASES.index("packaged"):
        require(value["archive_sha256"] is not None, "archive digest required")
        for r in effective:
            if r["kind"] in {"package_retained", "delivery_verified"}:
                require(r["subject_sha256"] == value["archive_sha256"], "retained/delivered archive digest mismatch")
    if rank > 0:
        require(value["source_write_state"] in {"frozen", "preservation_frozen"}, "source scope must remain frozen")
        f = value["fencing"]
        require(f["mechanism"] in {"durable_cas", "coordinator_barrier"}, "memory lock/sync is not distributed fencing")
        require(all(f[k] for k in ("verified", "all_writers_quiescent", "overlap_checked", "stale_writer_checked", "offline_writer_checked", "scope_only")), "writer fencing unverified")
    if phase != "completed":
        require(not value["destination_business_writes"], "destination activation before completed cutover")
        if phase not in {"partial_commit", "blocked", "failed"}:
            require(value["canonical_owner"] == p["source"]["partition_id"], "pre-cutover canonical owner drift")
    if phase == "completed":
        require(not value["blockers"] and all(r["result"] == "pass" for r in effective), "failure cannot be completed")
        require(value["source_write_state"] == "preservation_frozen", "completed source must retain scoped freeze")
        c = value["cutover_receipt"]; require(c is not None, "single cutover receipt missing"); cutover_errors(c)
        require(c["transaction_id"] == value["transaction_id"] and c["plan_sha256"] == binding and c["scope_sha256"] == p["scope_sha256"], "cutover binding mismatch")
        require(c["source_partition"] == p["source"]["partition_id"] and c["destination_partition"] == p["destination"]["partition_id"], "cutover partition mismatch")
        require(c["archive_sha256"] == value["archive_sha256"], "cutover archive mismatch")
        require(c["evidence_ids"] == sorted(r["evidence_id"] for r in receipts), "cutover evidence closure mismatch")
        require(c["evidence_sha256"] == {r["evidence_id"]:digest(r) for r in receipts}, "cutover evidence content mismatch")
        require({r["artifact_id"] for r in c["owners"]} == {r["artifact_id"] for r in p["artifacts"]}, "final owner scope incomplete")
        require(value["canonical_owner"] == p["destination"]["partition_id"] and value["authority_epoch"] == c["new_epoch"], "owner/cutover epoch mismatch")
        if value["destination_business_writes"]:
            require(bool(value["business_resume_approval_ref"]), "completion is not business restart approval")
    if phase in {"blocked", "failed", "partial_commit"}:
        require(bool(value["blockers"]) and not value["destination_business_writes"], "failure must hold writes and explain blocker")
    if phase == "partial_commit":
        require(value["source_write_state"] in {"frozen", "preservation_frozen"}, "partial cutover cannot thaw source")
    return []


def compensation_errors(value: dict[str, Any]) -> list[str]:
    require(value["original_transaction_id"] != value["compensation_transaction_id"], "compensation needs distinct transaction")
    require(value["new_epoch"] > value["previous_epoch"], "compensation epoch must increase")
    if value["result"] == "pass":
        require(not value["blockers"] and value["destination_quiesced"] and value["restore_verified"], "rollback guards unverified")
        if value["new_data_present"]:
            require(value["new_data_preserved"] and value["new_data_backup_sha256"] is not None, "new data must be backed up before rollback")
    else:
        require(bool(value["blockers"]), "blocked rollback needs reason")
    return []


def semantic_errors(name: str, value: dict[str, Any]) -> list[str]:
    functions = {"document-migration-plan.yaml":plan_errors, "document-migration-package.yaml":package_errors,
                 "document-relocation-map.yaml":relocation_errors, "document-migration-transaction.yaml":transaction_errors,
                 "document-cutover-receipt.yaml":cutover_errors,
                 "document-migration-compensation.yaml":compensation_errors}
    try:
        return functions[name](value)
    except ValueError as error:
        return [str(error)]


def transition(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Pure checkpoint comparison; callers still need durable CAS/append receipts."""
    from .validation import validate_value
    for obj in (current, candidate):
        validate_value("document-migration-transaction.yaml", obj)
    require(current["transaction_id"] == candidate["transaction_id"] and current["idempotency_key"] == candidate["idempotency_key"], "resume identity mismatch")
    require(current["migration_plan"] == candidate["migration_plan"], "resume approved plan drift")
    if digest(current) == digest(candidate):
        return deepcopy(current)  # Idempotent replay, not a second commit.
    before, after = current["phase"], candidate["phase"]
    if after == "completed" and before != "completed":
        require(candidate["cutover_receipt"]["expected_epoch"] == current["authority_epoch"], "cutover expected epoch stale")
    elif before != "completed":
        require(candidate["authority_epoch"] == current["authority_epoch"], "authority epoch changed before cutover")
    if before == "completed":
        allowed = {"destination_business_writes", "business_resume_approval_ref"}
        require(after == "completed" and not current["destination_business_writes"] and candidate["destination_business_writes"], "rollback needs separate compensation transaction and new-data backup")
        require({k:v for k,v in current.items() if k not in allowed} == {k:v for k,v in candidate.items() if k not in allowed}, "activation cannot rewrite completed cutover")
        return deepcopy(candidate)
    if after not in {"blocked", "failed", "partial_commit"}:
        start = PHASES.index(before) if before in PHASES else PHASES.index(current["last_verified_phase"])
        require(after in PHASES and PHASES.index(after) in {start, start+1}, "migration stage skipped")
    old = {r["evidence_id"]:r for r in current["evidence"]}
    new = {r["evidence_id"]:r for r in candidate["evidence"]}
    require(all(new.get(k) == r for k,r in old.items()), "historical evidence rewritten or deleted")
    return deepcopy(candidate)
