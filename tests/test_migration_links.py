from copy import deepcopy
import hashlib

import pytest

from trace_gate.migration import digest
from trace_gate.validation import TEMPLATE_DIR, load_yaml, validate_value

NAME="document-migration-link-policy.yaml"
def value():return deepcopy(load_yaml(TEMPLATE_DIR/NAME))
def set_target(v,target,index=0):
    v["links"][index]["target"]=target
    v["links"][index]["link_text_sha256"]=hashlib.sha256(target.encode()).hexdigest()


def test_contained_relative_file_and_directory_link_descriptors_preserve_text():
    v=value();before=deepcopy(v);validate_value(NAME,v)
    assert v==before and v["restore_supported"] is False


@pytest.mark.parametrize("target",["../../../outside","/absolute","C:/drive","\\\\host\\share","../missing","../function.h\0hidden","../e\u0301.h"])
def test_escape_absolute_dangling_unicode_and_nul_targets(target):
    v=value();set_target(v,target)
    with pytest.raises(ValueError):validate_value(NAME,v)


def test_cycle_in_link_chain_refused():
    v=value();v["links"]=v["links"][:1];set_target(v,"function.h")
    with pytest.raises(ValueError,match="link cycle"):validate_value(NAME,v)


def test_internal_chain_allowed_but_intermediate_symlink_traversal_refused():
    v=value();entry=deepcopy(v["links"][0]);entry.update(artifact_id="LINK.EXAMPLE.CHAIN",path="generated/alias.h")
    entry["target"]="lnInclude/function.h";entry["link_text_sha256"]=hashlib.sha256(entry["target"].encode()).hexdigest();v["links"].append(entry)
    validate_value(NAME,v)
    v=value();v["links"][1]["path"]="generated/alias";set_target(v,"../step0/uniform",1)
    v["links"][0]["path"]="generated/alias/child.h";set_target(v,"../function.h")
    with pytest.raises(ValueError,match="intermediate symlink"):validate_value(NAME,v)


def test_target_hash_inventory_digest_and_original_text_hash_guard():
    for field in ("target_sha256","link_text_sha256"):
        v=value();v["links"][0][field]="b"*64
        with pytest.raises(ValueError):validate_value(NAME,v)
    v=value();v["inventory"][0]["sha256"]="b"*64
    with pytest.raises(ValueError,match="inventory digest"):validate_value(NAME,v)


def test_inventory_file_cannot_act_as_parent_directory():
    v=value();v["inventory"].append({"path":"generated","kind":"file","sha256":"a"*64});v["verified_inventory_sha256"]=digest(v["inventory"])
    with pytest.raises(ValueError,match="non-directory"):validate_value(NAME,v)


def test_restore_support_cannot_be_inferred_from_descriptor_validation():
    v=value();v["restore_supported"]=True
    with pytest.raises(ValueError):validate_value(NAME,v)


def test_case_and_identity_collisions_are_refused():
    v=value();entry=deepcopy(v["links"][0]);entry["path"]="Generated/lnInclude/alias.h";v["links"].append(entry)
    with pytest.raises(ValueError,match="collision"):validate_value(NAME,v)
    entry["path"]="generated/lnInclude/alias.h"
    with pytest.raises(ValueError,match="duplicate link identity"):validate_value(NAME,v)


def test_long_link_chain_fails_with_bound_not_recursion_error():
    v=value();v["links"]=[]
    for i in range(66):
        target="chain"+str(i+1) if i<65 else "generated/function.h"
        v["links"].append({"artifact_id":"LINK.CHAIN."+str(i),"revision_id":"REV.EXAMPLE.1",
            "path":"chain"+str(i),"target":target,"link_text_sha256":hashlib.sha256(target.encode()).hexdigest(),
            "target_sha256":"a"*64,"target_kind":"file"})
    with pytest.raises(ValueError,match="chain depth bound"):validate_value(NAME,v)
