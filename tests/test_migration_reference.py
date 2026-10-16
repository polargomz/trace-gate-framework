"""Reference-only completion transfers control metadata, never external payload."""
from copy import deepcopy

import pytest

from trace_gate.migration import PHASES, REFERENCE_REQUIRED, digest, transition
from trace_gate.validation import TEMPLATE_DIR, load_yaml, validate_value
from test_migration import reapprove, resign, state


def reference_state(phase='planned'):
    value=state(phase)
    value['migration_plan']=load_yaml(TEMPLATE_DIR/'document-migration-reference-plan.yaml')
    binding=value['migration_plan']['approval']['plan_sha256']
    manifest=value['migration_plan']['plan']['reference_manifest_sha256']
    value.update(package_format='reference_manifest',archive_sha256=None,reference_manifest_sha256=manifest,evidence=[])
    for stage in PHASES[:PHASES.index(phase)+1]:
        for kind in sorted(REFERENCE_REQUIRED.get(stage,set())):
            value['evidence'].append({'evidence_id':'EVIDENCE.'+kind,'kind':kind,
                'transaction_id':value['transaction_id'],'plan_sha256':binding,
                'scope_sha256':value['migration_plan']['plan']['scope_sha256'],
                'subject_sha256':manifest if kind.startswith('reference_') else 'a'*64,
                'result':'pass','verifier_id':'VERIFIER.EXAMPLE',
                'verified_at':'2026-01-01T00:00:00Z','supersedes_evidence_ids':[]})
    if phase=='completed':
        value['cutover_receipt'].update(package_format='reference_manifest',archive_sha256=None,
            reference_manifest_sha256=manifest,plan_sha256=binding,
            owners=[{'artifact_id':'REF.EXAMPLE','partition_id':'PARTITION.DESTINATION'}])
        resign(value)
    return value


@pytest.mark.parametrize('phase',PHASES)
def test_reference_only_valid_states_never_transfer_payload_owner_or_activate(phase):
    value=reference_state(phase);validate_value('document-migration-transaction.yaml',value)
    assert value['archive_sha256'] is None and not value['destination_business_writes']
    assert value['migration_plan']['plan']['reference_bindings'][0]['canonical_owner']=='PARTITION.EXTERNAL'
    if phase=='completed':assert value['cutover_receipt']['owners'][0]['artifact_id']=='REF.EXAMPLE'


def test_reference_only_adjacent_checkpoints_complete_without_payload_copy_proofs():
    current=reference_state()
    for phase in PHASES[1:]:current=transition(current,reference_state(phase))
    assert current['phase']=='completed'
    assert not {'package_retained','delivery_verified','safe_extraction','destination_acceptance'}&{r['kind'] for r in current['evidence']}


@pytest.mark.parametrize('kind',sorted(set().union(*(REFERENCE_REQUIRED[p] for p in ('packaged','delivered','relocated','received_complete')))))
def test_reference_completion_requires_each_distinct_reference_proof(kind):
    value=reference_state('completed')
    value['evidence']=[r for r in value['evidence'] if r['kind']!=kind];resign(value)
    with pytest.raises(ValueError,match='required stage evidence'):validate_value('document-migration-transaction.yaml',value)


@pytest.mark.parametrize('mutation',['manifest','subject','payload_owner_scope','physical_proof','cutover','duplicate_reference','parent_closure'])
def test_reference_binding_and_completion_cannot_masquerade_as_physical(mutation):
    value=reference_state('completed');p=value['migration_plan']['plan']
    if mutation=='manifest':value['reference_manifest_sha256']='b'*64
    elif mutation=='subject':next(r for r in value['evidence'] if r['kind']=='reference_access_verified')['subject_sha256']='b'*64
    elif mutation=='payload_owner_scope':p['reference_bindings'][0]['payload_artifact_id']='REF.EXAMPLE'
    elif mutation=='physical_proof':value.update(package_format='single_archive',archive_sha256='a'*64)
    elif mutation=='cutover':value['cutover_receipt']['reference_manifest_sha256']='b'*64
    elif mutation=='duplicate_reference':p['reference_bindings'].append(deepcopy(p['reference_bindings'][0]))
    elif mutation=='parent_closure':p['external_parent_ids']=[];p['artifacts'][0]['parent_ids']=[]
    if mutation in {'payload_owner_scope','duplicate_reference','parent_closure'}:
        p['reference_manifest_sha256']=digest(p['reference_bindings']);reapprove(value['migration_plan'])
    resign(value)
    with pytest.raises(ValueError):validate_value('document-migration-transaction.yaml',value)


@pytest.mark.parametrize('mode',['single_archive','chunk_set'])
def test_physical_package_rejects_extra_reference_digest(mode):
    value=state('completed');value['reference_manifest_sha256']='b'*64
    if mode=='chunk_set':value.update(package_format=mode,archive_sha256=None,package_set_sha256='a'*64)
    with pytest.raises(ValueError,match='ambiguity'):validate_value('document-migration-transaction.yaml',value)


def test_physical_plan_cannot_silently_be_converted_to_reference_only():
    value=reference_state();value['migration_plan']['plan']['migration_mode']='physical_relocation';reapprove(value['migration_plan'])
    with pytest.raises(ValueError,match='silently'):validate_value('document-migration-transaction.yaml',value)
    with pytest.raises(ValueError,match='plan drift'):transition(state(),reference_state())
