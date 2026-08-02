#!/usr/bin/env python3
"""Refresh content-derived fields in the managed document manifest."""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_gate.validation import (  # noqa: E402
    MANAGED_DOCUMENT_MANIFEST,
    discover_managed_documents,
    load_yaml,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observed-at",
        default=datetime.now(UTC).isoformat(),
        help="Timestamp written to validation and freshness fields",
    )
    args = parser.parse_args(argv)

    value = load_yaml(MANAGED_DOCUMENT_MANIFEST)
    entries = {
        entry["storage"]["canonical_location"]: entry
        for entry in value["documents"]
    }
    discovered = {
        path.as_posix() for path in discover_managed_documents(ROOT, value)
    }
    missing = sorted(discovered - set(entries))
    extra = sorted(set(entries) - discovered)
    if missing or extra:
        details = []
        if missing:
            details.append(f"add metadata entries for: {', '.join(missing)}")
        if extra:
            details.append(f"remove missing paths: {', '.join(extra)}")
        raise ValueError("; ".join(details))

    for relative, entry in entries.items():
        content = (ROOT / relative).read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        entry["identity"]["revision_id"] = f"REV.SHA256.{content_hash[:16]}"
        entry["integrity"]["sha256"] = content_hash
        entry["integrity"]["size_bytes"] = len(content)
        entry["time"]["observed_at"] = args.observed_at
        entry["time"]["updated_at"] = args.observed_at
        entry["validation"]["validated_at"] = args.observed_at
        entry["freshness"]["evaluated_at"] = args.observed_at
        entry["freshness"]["status"] = "current"
        entry["freshness"]["reason"] = "content digest and size match the canonical file"

    value["time"]["observed_at"] = args.observed_at
    value["time"]["updated_at"] = args.observed_at
    value["validation"]["validated_at"] = args.observed_at
    value["freshness"]["evaluated_at"] = args.observed_at
    rendered = yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    MANAGED_DOCUMENT_MANIFEST.write_text(rendered, encoding="utf-8")
    print(f"Updated {MANAGED_DOCUMENT_MANIFEST}: {len(entries)} documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
