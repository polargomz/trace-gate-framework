from copy import deepcopy
import hashlib
import os
from pathlib import Path

import pytest

from trace_gate.migration import digest,transition
from trace_gate.migration_io import directory_digests
from trace_gate.validation import TEMPLATE_DIR,load_yaml,validate_value
from trace_gate.migration_link_restore import restore_relative_links
from trace_gate.migration_stream import pack_chunk_set,restore_chunk_set
from test_migration import state


def inventory(root):
    rows=[]
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root).as_posix()
        if path.is_dir():rows.append({'path':rel,'kind':'directory','size_bytes':0,'sha256':'a'*64,'artifact_id':'DIR.'+str(len(rows)),'revision_id':'REV.1'})
        else:
            raw=path.read_bytes();rows.append({'path':rel,'kind':'file','size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'artifact_id':'FILE.'+str(len(rows)),'revision_id':'REV.1'})
    hashes=directory_digests({r['path']:r for r in rows},[])
    for r in rows:
        if r['kind']=='directory':r['sha256']=hashes[r['path']]
    return {'entries':rows,'link_policy':None}


def streaming(root):
    inv=inventory(root);p=load_yaml(TEMPLATE_DIR/'document-migration-stream-policy.yaml');s=p['settings']
    s.update(enabled=True,source_inventory_sha256=digest(inv),part_bytes=4,buffer_bytes=2,
        max_payload_bytes=1000,max_file_bytes=1000,max_files=100,max_directories=100,max_parts=100,
        max_manifest_bytes=32768,max_bundle_bytes=1024**2,max_staging_bytes=1024**2,minimum_free_bytes=1,max_temp_bytes=65536,max_failed_part_bytes=1024)
    p['approval'].update(source_scope_frozen=True,configuration_sha256=digest(s))
    return p,inv


def source(tmp_path):
    root=tmp_path/'source';root.mkdir(mode=0o700);(root/'payload').mkdir(mode=0o700)
    (root/'payload/large.bin').write_bytes(b'123456789ABC');(root/'payload/empty').write_bytes(b'')
    return root


def link_case(tmp_path):
    root=tmp_path/'stage';root.mkdir(mode=0o700)
    for name in ('generated/lnInclude','run0/shared','jobs/shard0/run0'):(root/name).mkdir(parents=True,mode=0o700)
    (root/'generated/function.h').write_bytes(b'header')
    inv=inventory(root)['entries'];policy=load_yaml(TEMPLATE_DIR/'document-migration-link-restore.yaml')
    policy['inventory']=[{k:r[k] for k in ('path','kind','sha256')} for r in inv]
    # Directory hashes include the declared link descriptors, never dereference their targets.
    expected={r['path']:r for r in policy['inventory']}
    for r in policy['links']:
        final='generated/function.h' if r['target_kind']=='file' else 'run0/shared'
        r['target_sha256']=expected[final]['sha256']
    hashes=directory_digests(expected,policy['links'])
    for row in policy['inventory']:
        if row['kind']=='directory':row['sha256']=hashes[row['path']]
    policy['verified_inventory_sha256']=digest(policy['inventory'])
    receipt={'status':'regular_files_verified','inventory_sha256':policy['verified_inventory_sha256'],'destination_business_writes':False}
    policy['approval'].update(policy_sha256=digest({k:v for k,v in policy.items() if k!='approval'}),staging_receipt_sha256=digest(receipt))
    return root,policy,receipt


def test_streaming_large_single_file_spans_parts_and_restores_whole_hash(tmp_path):
    src=source(tmp_path);policy,inv=streaming(src);bundle=tmp_path/'bundle'
    result=pack_chunk_set(src,bundle,policy,inv);assert result['parts']==3
    restored=restore_chunk_set(bundle,tmp_path/'stage',tmp_path/'restore-state',policy,result['package_set_sha256'])
    assert restored['status']=='pass' and not restored['destination_business_writes']
    assert (tmp_path/'stage/payload/large.bin').read_bytes()==b'123456789ABC'
    assert (tmp_path/'stage/payload/empty').read_bytes()==b''
    assert pack_chunk_set(src,bundle,policy,inv,resume=True,resume_approval_ref='APPROVAL.RESUME')==result


def test_pack_partial_failure_preserved_explicit_resume_reuses_verified_parts(tmp_path,monkeypatch):
    import trace_gate.migration_stream as m
    src=source(tmp_path);policy,inv=streaming(src);bundle=tmp_path/'bundle';original=m.write_all;count=0
    def fail(fd,data):
        nonlocal count
        count+=1
        if count==4:raise OSError('synthetic write failure')
        return original(fd,data)
    monkeypatch.setattr(m,'write_all',fail)
    with pytest.raises(OSError):pack_chunk_set(src,bundle,policy,inv)
    failed=(bundle/'attempt-00000000.json').read_bytes();part=(bundle/'part-00000000.bin').read_bytes()
    monkeypatch.setattr(m,'write_all',original)
    with pytest.raises(ValueError,match='explicit resume'):pack_chunk_set(src,bundle,policy,inv)
    result=pack_chunk_set(src,bundle,policy,inv,resume=True,resume_approval_ref='APPROVAL.RESUME')
    assert result['status']=='pass' and (bundle/'attempt-00000000.json').read_bytes()==failed
    assert (bundle/'part-00000000.bin').read_bytes()==part and list((bundle/'failures').iterdir())


def test_restore_crash_keeps_prefix_and_resumes_without_overwrite(tmp_path,monkeypatch):
    import trace_gate.migration_stream as m
    src=source(tmp_path);policy,inv=streaming(src);bundle=tmp_path/'bundle';result=pack_chunk_set(src,bundle,policy,inv)
    original=m.write_all;count=0
    def fail(fd,data):
        nonlocal count
        count+=1
        if count==3:raise OSError('synthetic disk full')
        return original(fd,data)
    monkeypatch.setattr(m,'write_all',fail)
    with pytest.raises(OSError):restore_chunk_set(bundle,tmp_path/'stage',tmp_path/'state',policy,result['package_set_sha256'])
    prefix=(tmp_path/'stage/payload/large.bin').read_bytes();assert prefix==b'1234'
    monkeypatch.setattr(m,'write_all',original)
    restored=restore_chunk_set(bundle,tmp_path/'stage',tmp_path/'state',policy,result['package_set_sha256'],resume=True,resume_approval_ref='APPROVAL.RESUME')
    assert restored['status']=='pass' and (tmp_path/'stage/payload/large.bin').read_bytes().startswith(prefix)


@pytest.mark.parametrize('mutation',['missing','duplicate','order','gap','overlap','hash','cross_plan'])
def test_corrupted_part_set_never_restores(tmp_path,mutation):
    import json
    src=source(tmp_path);policy,inv=streaming(src);bundle=tmp_path/'bundle';result=pack_chunk_set(src,bundle,policy,inv)
    manifest=json.loads((bundle/'package-set.json').read_bytes())
    if mutation=='missing':manifest['parts'].pop()
    elif mutation=='duplicate':manifest['parts'].append(deepcopy(manifest['parts'][0]))
    elif mutation=='order':manifest['parts'].reverse()
    elif mutation=='gap':manifest['parts'][1]['offset']+=1
    elif mutation=='overlap':manifest['parts'][1]['offset']-=1
    elif mutation=='hash':(bundle/'part-00000000.bin').write_bytes(b'evil')
    elif mutation=='cross_plan':manifest['plan_sha256']='b'*64
    if mutation!='hash':(bundle/'package-set.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):restore_chunk_set(bundle,tmp_path/'stage',tmp_path/'state',policy,digest(manifest))
    assert not (tmp_path/'stage').exists()


@pytest.mark.parametrize('field,value',[('enabled',False),('max_payload_bytes',8),('max_file_bytes',8),('max_parts',2),('part_bytes',256*1024**2+1),('minimum_free_bytes',2**62)])
def test_optin_aggregate_file_part_and_disk_bounds(tmp_path,field,value):
    src=source(tmp_path);policy,inv=streaming(src);policy['settings'][field]=value;policy['approval']['configuration_sha256']=digest(policy['settings'])
    with pytest.raises(ValueError):pack_chunk_set(src,tmp_path/'bundle',policy,inv)
    assert not (tmp_path/'bundle/completed.json').exists()


def test_changed_source_plan_or_configuration_rejected_on_resume(tmp_path):
    src=source(tmp_path);policy,inv=streaming(src);bundle=tmp_path/'bundle';pack_chunk_set(src,bundle,policy,inv)
    (src/'payload/large.bin').write_bytes(b'changed-data')
    with pytest.raises(ValueError):pack_chunk_set(src,bundle,policy,inv,resume=True,resume_approval_ref='APPROVAL.RESUME')
    changed=deepcopy(policy);changed['settings']['plan_sha256']='b'*64;changed['approval']['configuration_sha256']=digest(changed['settings'])
    with pytest.raises(ValueError,match='changed on resume'):pack_chunk_set(src,bundle,changed,inv,resume=True,resume_approval_ref='APPROVAL.RESUME')


def test_optin_links_restore_real_relative_file_and_directory_targets(tmp_path):
    root,p,receipt=link_case(tmp_path);before=(root/'generated/function.h').read_bytes()
    result=restore_relative_links(root,tmp_path/'link-state',p,digest(p),receipt)
    assert result['status']=='pass' and result['links_restored']==2
    for row in p['links']:assert os.readlink(root/row['path'])==row['target']
    assert (root/'generated/lnInclude/function.h').read_bytes()==before
    assert (root/'jobs/shard0/run0/shared').resolve()==root/'run0/shared'
    assert restore_relative_links(root,tmp_path/'link-state',p,digest(p),receipt,resume=True,resume_approval_ref='APPROVAL.RESUME')==result


@pytest.mark.parametrize('change',['target','prefix','overwrite','receipt','approval'])
def test_link_restore_requires_exact_inventory_target_and_approval(tmp_path,change):
    root,p,receipt=link_case(tmp_path)
    if change=='target':(root/'generated/function.h').write_bytes(b'changed')
    elif change=='prefix':(root/'generated/lnInclude').rmdir();(root/'generated/lnInclude').symlink_to(root/'run0/shared')
    elif change=='overwrite':(root/'generated/lnInclude/function.h').write_bytes(b'existing')
    elif change=='receipt':receipt['status']='fail'
    elif change=='approval':p['approval']['enabled']=False
    with pytest.raises((ValueError,OSError)):restore_relative_links(root,tmp_path/'link-state',p,digest(p),receipt)
    assert not (tmp_path/'link-state/completed.json').exists()


def test_link_target_race_fails_closed_without_outside_writes(tmp_path,monkeypatch):
    root,p,receipt=link_case(tmp_path);outside=tmp_path/'outside';outside.write_bytes(b'preserve');original=os.symlink;done=False
    def race(target,path,*args,**kwargs):
        nonlocal done
        if not done:
            done=True;(root/'generated/function.h').unlink();original(str(outside),root/'generated/function.h')
        return original(target,path,*args,**kwargs)
    monkeypatch.setattr(os,'symlink',race)
    with pytest.raises(ValueError):restore_relative_links(root,tmp_path/'link-state',p,digest(p),receipt)
    assert outside.read_bytes()==b'preserve' and not (tmp_path/'link-state/completed.json').exists()


@pytest.mark.parametrize('phase',['blocked','failed','partial_commit'])
def test_failure_cannot_forge_last_verified_checkpoint_or_pass_evidence(phase):
    forged=state('cutover_pending');forged.update(phase=phase,blockers=['synthetic failure'])
    with pytest.raises(ValueError,match='cannot advance'):transition(state(),forged)
    forged=state();forged.update(phase=phase,source_write_state='frozen',blockers=['synthetic failure'])
    forged['evidence']=state('migrating_frozen')['evidence']
    with pytest.raises(ValueError,match='cannot inject'):transition(state(),forged)


@pytest.mark.parametrize('phase',['blocked','failed','partial_commit'])
def test_normal_failure_resume_only_adjacent_next_stage(phase):
    before=state('received_complete');blocked=deepcopy(before);blocked.update(phase=phase,blockers=['retry requires approval'])
    assert transition(before,blocked)==blocked
    assert transition(blocked,state('cutover_pending'))['phase']=='cutover_pending'
    with pytest.raises(ValueError,match='stage skipped'):transition(blocked,state('completed'))


def test_explicit_abort_holds_both_scopes_and_is_terminal():
    before=state('received_complete');after=deepcopy(before);after.update(phase='aborted',abort_approval_ref='APPROVAL.ABORT',abort_hold_evidence_sha256='a'*64)
    assert transition(before,after)==after and not after['destination_business_writes']
    with pytest.raises(ValueError,match='terminal'):transition(after,state('cutover_pending'))
    del after['abort_approval_ref']
    with pytest.raises(ValueError,match='explicit approval'):transition(before,after)


def test_chunk_set_links_pending_then_optin_restore_and_regular_stage_is_sealed(tmp_path):
    root,p,unused=link_case(tmp_path);policy,inv=streaming(root)
    descriptor=deepcopy(p);descriptor.update(mode='descriptor_plan_only',restore_supported=False);descriptor.pop('approval')
    hashes={r['path']:r['sha256'] for r in descriptor['inventory']}
    for row in inv['entries']:row['sha256']=hashes[row['path']]
    inv['link_policy']=descriptor
    policy['settings']['source_inventory_sha256']=digest(inv);policy['approval']['configuration_sha256']=digest(policy['settings'])
    for row in descriptor['links']:os.symlink(row['target'],root/row['path'])
    bundle=tmp_path/'bundle';result=pack_chunk_set(root,bundle,policy,inv)
    stage=tmp_path/'destination';restored=restore_chunk_set(bundle,stage,tmp_path/'restore-state',policy,result['package_set_sha256'])
    assert restored['status']=='regular_files_verified' and restored['links_pending']==2
    p['approval'].update(staging_receipt_sha256=digest(restored))
    linked=restore_relative_links(stage,tmp_path/'link-state',p,digest(p),restored)
    assert linked['status']=='pass'
    with pytest.raises(ValueError,match='sealed once links exist'):
        restore_chunk_set(bundle,stage,tmp_path/'restore-state',policy,result['package_set_sha256'],resume=True,resume_approval_ref='APPROVAL.RESUME')


def test_stream_io_buffer_bound_and_exact_file_boundary(tmp_path,monkeypatch):
    src=source(tmp_path);policy,inv=streaming(src);policy['settings'].update(max_file_bytes=12,max_payload_bytes=12)
    policy['approval']['configuration_sha256']=digest(policy['settings'])
    original=os.read;source_inodes={p.stat().st_ino for p in src.rglob('*') if p.is_file()};sizes=[]
    def bounded(fd,n):
        if os.fstat(fd).st_ino in source_inodes:sizes.append(n)
        return original(fd,n)
    monkeypatch.setattr(os,'read',bounded)
    result=pack_chunk_set(src,tmp_path/'bundle',policy,inv)
    assert result['payload_bytes']==12 and max(sizes)<=2


def test_part_name_case_collision_and_control_configuration_tamper(tmp_path):
    src=source(tmp_path);policy,inv=streaming(src);policy['settings']['part_bytes']=8
    with pytest.raises(ValueError,match='configuration digest'):pack_chunk_set(src,tmp_path/'bundle',policy,inv)
    policy,inv=streaming(src);inv['entries'].append({**inv['entries'][-1],'path':'Payload/other'})
    policy['settings']['source_inventory_sha256']=digest(inv);policy['approval']['configuration_sha256']=digest(policy['settings'])
    with pytest.raises(ValueError,match='collision'):pack_chunk_set(src,tmp_path/'bundle',policy,inv)


def test_restore_wrong_existing_prefix_is_not_overwritten(tmp_path,monkeypatch):
    import trace_gate.migration_stream as m
    src=source(tmp_path);policy,inv=streaming(src);bundle=tmp_path/'bundle';r=pack_chunk_set(src,bundle,policy,inv)
    original=m.write_all;calls=0
    def fail(fd,data):
        nonlocal calls
        calls+=1
        if calls==2:raise OSError('interrupted')
        original(fd,data)
    monkeypatch.setattr(m,'write_all',fail)
    with pytest.raises(OSError):restore_chunk_set(bundle,tmp_path/'stage',tmp_path/'state',policy,r['package_set_sha256'])
    monkeypatch.setattr(m,'write_all',original);file=tmp_path/'stage/payload/large.bin';file.write_bytes(b'XX')
    with pytest.raises(ValueError,match='prefix mismatch'):
        restore_chunk_set(bundle,tmp_path/'stage',tmp_path/'state',policy,r['package_set_sha256'],resume=True,resume_approval_ref='APPROVAL.RESUME')
    assert file.read_bytes()==b'XX'


def test_stage_lock_blocks_concurrent_link_restoration(tmp_path):
    from trace_gate.migration_io import stage_lock
    root,p,r=link_case(tmp_path)
    with stage_lock(root),pytest.raises(BlockingIOError):restore_relative_links(root,tmp_path/'link-state',p,digest(p),r)
    assert not (tmp_path/'link-state/completed.json').exists()


def test_directory_symlink_cycles_are_refused(tmp_path):
    root,p,r=link_case(tmp_path);p['links'][1].update(path='run0/loop',target='shared/..')
    p['links'][1]['link_text_sha256']=hashlib.sha256(b'shared/..').hexdigest()
    p['links'][1]['target_sha256']=next(row['sha256'] for row in p['inventory'] if row['path']=='run0')
    p['approval']['policy_sha256']=digest({k:v for k,v in p.items() if k!='approval'})
    with pytest.raises(ValueError,match='directory link cycle'):validate_value('document-migration-link-policy.yaml',p)


def test_chunk_set_digest_is_not_silently_single_archive_digest():
    from test_migration import resign
    value=state('completed');value.update(package_format='chunk_set',archive_sha256=None,package_set_sha256='a'*64)
    value['cutover_receipt'].update(package_format='chunk_set',archive_sha256=None,package_set_sha256='a'*64);resign(value)
    validate_value('document-migration-transaction.yaml',value)
    value['archive_sha256']='a'*64
    with pytest.raises(ValueError,match='distinct'):validate_value('document-migration-transaction.yaml',value)


@pytest.mark.parametrize('adapter',['pack','links'])
def test_resume_does_not_trust_changed_terminal_receipt(tmp_path,adapter):
    import json
    if adapter=='pack':
        src=source(tmp_path);p,inv=streaming(src);folder=tmp_path/'bundle'
        pack_chunk_set(src,folder,p,inv)
        resume=lambda:pack_chunk_set(src,folder,p,inv,resume=True,resume_approval_ref='APPROVAL.RESUME')
    else:
        root,p,r=link_case(tmp_path);folder=tmp_path/'state'
        restore_relative_links(root,folder,p,digest(p),r)
        resume=lambda:restore_relative_links(root,folder,p,digest(p),r,resume=True,resume_approval_ref='APPROVAL.RESUME')
    path=folder/'completed.json';value=json.loads(path.read_bytes());value['destination_business_writes']=True
    changed=json.dumps(value).encode();path.write_bytes(changed)
    with pytest.raises(ValueError,match='receipt drift'):resume()
    assert path.read_bytes()==changed and (folder/'attempt-00000001.json').exists()


def test_tree_walk_does_not_hold_one_fd_per_directory(tmp_path,monkeypatch):
    from trace_gate.migration_io import walk
    from trace_gate.migration_archive import open_directory
    root=tmp_path/'wide';root.mkdir(mode=0o700)
    for i in range(40):(root/f'directory-{i:02d}').mkdir(mode=0o700)
    fd=open_directory(root);opened=set();peak=0;original_open=os.open;original_close=os.close;original_dup=os.dup
    def track(value):
        nonlocal peak
        opened.add(value);peak=max(peak,len(opened));return value
    def close(value):opened.discard(value);return original_close(value)
    monkeypatch.setattr(os,'open',lambda *a,**k:track(original_open(*a,**k)))
    monkeypatch.setattr(os,'dup',lambda value:track(original_dup(value)))
    monkeypatch.setattr(os,'close',close)
    try:assert len(walk(fd,100))==40 and peak<=3 and not opened
    finally:original_close(fd)
