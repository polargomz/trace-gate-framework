"""Preserve imported revision strings; never relax stable identifier contracts."""
from copy import deepcopy
import hashlib
import json

import jsonschema
import pytest
import yaml

from trace_gate.migration import digest
from trace_gate.migration_archive import extract_verified
from trace_gate.validation import ROOT,TEMPLATE_DIR,UniqueKeyLoader,load_yaml,validate_value
from test_migration_archive import pack

SCHEMA_PATHS=(
    ('$defs','migrationPlan','properties','plan','properties','artifacts','items','properties','revision_id'),
    ('$defs','migrationPackage','properties','files','items','properties','revision_id'),
    ('$defs','relocationMap','properties','maps','items','properties','old_revision_id'),
    ('$defs','relocationMap','properties','maps','items','properties','new_revision_id'),
)


def get(value,path):
    for key in path:value=value[key]
    return value


def documents(revision):
    payload=b'data';sha=hashlib.sha256(payload).hexdigest()
    plan=load_yaml(TEMPLATE_DIR/'document-migration-plan.yaml')
    plan['plan']['artifacts'][0].update(revision_id=revision,sha256=sha)
    plan['approval']['plan_sha256']=digest(plan['plan']);binding=plan['approval']['plan_sha256']
    package=load_yaml(TEMPLATE_DIR/'document-migration-package.yaml')
    package['plan_sha256']=binding;package['files'][0].update(revision_id=revision,sha256=sha,size_bytes=len(payload))
    relocation=load_yaml(TEMPLATE_DIR/'document-relocation-map.yaml');relocation['plan_sha256']=binding
    relocation['maps'][0].update(old_revision_id=revision,new_revision_id=revision,old_sha256=sha,new_sha256=sha)
    transaction=load_yaml(TEMPLATE_DIR/'document-migration-transaction.yaml');transaction['migration_plan']=deepcopy(plan)
    return {'document-migration-plan.yaml':plan,'document-migration-package.yaml':package,
        'document-relocation-map.yaml':relocation,'document-migration-transaction.yaml':transaction}


@pytest.mark.parametrize('revision',['r1','r2','r3','r4','r5','r','1','REV.EXAMPLE.1','r1._:-A9'])
@pytest.mark.parametrize('encoding',['json','yaml'])
def test_original_revision_and_content_hash_survive_all_contract_roundtrips(revision,encoding):
    docs=documents(revision);before=deepcopy(docs)
    for name,value in docs.items():
        validate_value(name,value)
        restored=json.loads(json.dumps(value)) if encoding=='json' else yaml.load(yaml.safe_dump(value),Loader=UniqueKeyLoader)
        validate_value(name,restored);assert restored==value==before[name]
    sha=hashlib.sha256(b'data').hexdigest()
    a=docs['document-migration-plan.yaml']['plan']['artifacts'][0]
    f=docs['document-migration-package.yaml']['files'][0]
    m=docs['document-relocation-map.yaml']['maps'][0]
    t=docs['document-migration-transaction.yaml']['migration_plan']['plan']['artifacts'][0]
    assert a['revision_id']==f['revision_id']==m['old_revision_id']==m['new_revision_id']==t['revision_id']==revision
    assert a['sha256']==f['sha256']==m['old_sha256']==m['new_sha256']==t['sha256']==sha


@pytest.mark.parametrize('revision',['r1','r5'])
def test_archive_roundtrip_keeps_revision_manifest_and_payload_bytes(tmp_path,revision):
    docs=documents(revision);package=docs['document-migration-package.yaml']
    def replace_manifest(value):value.update(deepcopy(package))
    archive,ah,mh=pack(tmp_path,transform=replace_manifest);before=archive.read_bytes()
    destination=tmp_path/'stage';result=extract_verified(archive,destination,ah,mh)
    assert result['status']=='pass' and not result['destination_business_writes']
    assert (destination/'payload/example.txt').read_bytes()==b'data'
    assert archive.read_bytes()==before and package['files'][0]['revision_id']==revision
    import tarfile
    with tarfile.open(archive) as source:
        recovered=json.load(source.extractfile('TRACE-DM-MANIFEST.json'))
    validate_value('document-migration-package.yaml',recovered);assert recovered==package


@pytest.mark.parametrize('field',['plan','package','old','new','transaction'])
@pytest.mark.parametrize('bad',['',' ',' r1','r1 ','r/1','r1\n','r1\r','r1\t','r1\0','r1\x1f','r1\x7f','r1\u2028','r１','_r1',None,1])
def test_invalid_revisions_rejected_without_normalization(field,bad):
    docs=documents('r1')
    if field=='plan':name='document-migration-plan.yaml';target=docs[name]['plan']['artifacts'][0];key='revision_id'
    elif field=='package':name='document-migration-package.yaml';target=docs[name]['files'][0];key='revision_id'
    elif field in {'old','new'}:name='document-relocation-map.yaml';target=docs[name]['maps'][0];key=field+'_revision_id'
    else:name='document-migration-transaction.yaml';target=docs[name]['migration_plan']['plan']['artifacts'][0];key='revision_id'
    target[key]=bad
    if field in {'plan','transaction'}:
        p=docs[name] if field=='plan' else docs[name]['migration_plan'];p['approval']['plan_sha256']=digest(p['plan'])
    before=deepcopy(docs[name])
    with pytest.raises(ValueError):validate_value(name,docs[name])
    assert docs[name]==before


def test_revision_policy_is_minimum_one_not_an_unmotivated_minimum_two():
    schema=json.loads((ROOT/'schemas/document-migration.schema.json').read_text())
    for path in SCHEMA_PATHS:
        field=get(schema,path);assert field['minLength']==1
        jsonschema.Draft202012Validator(field).validate('r')
        for bad in ('','r1\n'):
            with pytest.raises(jsonschema.ValidationError):jsonschema.Draft202012Validator(field).validate(bad)
    defs=schema['$defs']
    assert defs['migrationLinkPolicy']['properties']['links']['items']['properties']['revision_id']['minLength']==1
    assert defs['migrationStreamInventory']['properties']['entries']['items']['properties']['revision_id']['minLength']==1


def test_other_identifier_minimums_remain_three_in_migration_and_core():
    migration=json.loads((ROOT/'schemas/document-migration.schema.json').read_text())
    def visit(value,path=()):
        if isinstance(value,dict):
            if value.get('minLength')==3:
                assert path not in SCHEMA_PATHS
                with pytest.raises(jsonschema.ValidationError):jsonschema.Draft202012Validator(value).validate('r1')
                yield path
            for key,item in value.items():yield from visit(item,path+(key,))
        elif isinstance(value,list):
            for i,item in enumerate(value):yield from visit(item,path+(i,))
    constrained=list(visit(migration));assert constrained
    for field in ('artifact_id','partition_id','project_id','transaction_id','evidence_id'):
        assert any(path[-1]==field for path in constrained),field
    core=json.loads((ROOT/'schemas/trace-gate.schema.json').read_text())['$defs']['identifier']
    assert core['minLength']==3
    with pytest.raises(jsonschema.ValidationError):jsonschema.Draft202012Validator(core).validate('r1')


def test_preserved_revision_still_requires_reapproval_and_derived_content_revision():
    docs=documents('r1');plan=docs['document-migration-plan.yaml'];plan['plan']['artifacts'][0]['revision_id']='r5'
    with pytest.raises(ValueError,match='plan digest mismatch'):validate_value('document-migration-plan.yaml',plan)
    row=docs['document-relocation-map.yaml']['maps'][0];row['new_revision_id']='r5'
    with pytest.raises(ValueError,match='unchanged payload identity drift'):validate_value('document-relocation-map.yaml',docs['document-relocation-map.yaml'])
    row.update(content_changed=True,new_sha256=hashlib.sha256(b'changed').hexdigest())
    validate_value('document-relocation-map.yaml',docs['document-relocation-map.yaml'])
