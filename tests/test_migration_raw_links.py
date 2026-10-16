"""Raw dot-components must not hide OS symlink traversal from the verifier."""
from copy import deepcopy
import hashlib
import os

import pytest

from trace_gate.migration import digest
from trace_gate.migration_archive import open_directory
from trace_gate.migration_io import directory_digests
from trace_gate.migration_link_restore import restore_relative_links,verify_target,verify_actual_link
from trace_gate.validation import TEMPLATE_DIR,load_yaml,validate_value
from test_migration_optin import inventory


def fixture(tmp_path,specs):
    root=tmp_path/'stage';root.mkdir(mode=0o700)
    for name in ('sub','sub/dir','sub/dir/deep','other'):(root/name).mkdir(mode=0o700)
    (root/'file').write_bytes(b'verified-root-file')
    (root/'sub/file').write_bytes(b'different-sub-file')
    rows=[{k:r[k] for k in ('path','kind','sha256')} for r in inventory(root)['entries']]
    bypath={r['path']:r for r in rows};links=[]
    for i,(path,target,final) in enumerate(specs):
        links.append({'artifact_id':f'LINK.RAW.{i}','revision_id':'REV.1','path':path,'target':target,
            'link_text_sha256':hashlib.sha256(target.encode()).hexdigest(),
            'target_kind':bypath[final]['kind'],'target_sha256':bypath[final]['sha256']})
    hashes=directory_digests(bypath,links)
    for row in rows:
        if row['kind']=='directory':row['sha256']=hashes[row['path']]
    for row,(_,_,final) in zip(links,specs):row['target_sha256']=bypath[final]['sha256']
    p=load_yaml(TEMPLATE_DIR/'document-migration-link-restore.yaml')
    p.update(inventory=rows,links=links,verified_inventory_sha256=digest(rows))
    receipt={'status':'regular_files_verified','inventory_sha256':digest(rows),'destination_business_writes':False}
    p['approval'].update(policy_sha256=digest({k:v for k,v in p.items() if k!='approval'}),staging_receipt_sha256=digest(receipt))
    return root,p,receipt


def test_real_os_reproducer_differs_from_lexical_target(tmp_path):
    root,p,_=fixture(tmp_path,[('alias','sub/dir','sub/dir'),('x','alias/../file','file')])
    os.symlink('sub/dir',root/'alias');os.symlink('alias/../file',root/'x')
    assert (root/'x').resolve()==root/'sub/file'
    assert (root/'x').read_bytes()!=(root/'file').read_bytes()
    assert hashlib.sha256((root/'x').read_bytes()).hexdigest()!=p['links'][1]['target_sha256']


@pytest.mark.parametrize('target,final',[
    ('alias/../file','file'),('alias/../../file','file'),
    ('alias/../sub/dir','sub/dir'),('alias/.','sub/dir'),
    ('other/../alias/../file','file'),('chain/../file','file')])
def test_raw_hidden_alias_rejected_statically_before_restore_and_direct_live_check(tmp_path,target,final):
    specs=[('alias','sub/dir','sub/dir'),('chain','alias','sub/dir'),('x',target,final)]
    root,p,r=fixture(tmp_path,specs)
    with pytest.raises(ValueError,match='raw intermediate symlink'):validate_value('document-migration-link-policy.yaml',p)
    with pytest.raises(ValueError,match='raw intermediate symlink'):
        restore_relative_links(root,tmp_path/'state',p,digest(p),r)
    assert not (tmp_path/'state').exists() and not (root/'x').exists()
    # verify_target independently enforces the rule even if called without schema validation.
    os.symlink('sub/dir',root/'alias');os.symlink('alias',root/'chain')
    fd=open_directory(root)
    try:
        with pytest.raises(ValueError,match='raw intermediate symlink'):verify_target(fd,p['links'][-1],p)
    finally:os.close(fd)


@pytest.mark.parametrize('target',['file/../file','missing/../file','sub/missing/../../file'])
def test_removed_non_directory_or_missing_raw_component_rejected(tmp_path,target):
    root,p,r=fixture(tmp_path,[('x',target,'file')])
    with pytest.raises(ValueError,match='missing/non-directory'):
        restore_relative_links(root,tmp_path/'state',p,digest(p),r)
    assert not (tmp_path/'state/completed.json').exists()


@pytest.mark.parametrize('specs',[
    [('sub/x','../file','file')],
    [('x','sub/dir/../../file','file')],
    [('sub/dir/x','../../file','file')],
    [('alias','sub/dir','sub/dir'),('chain','alias','sub/dir')],
    [('alias','sub/../file','file'),('chain','alias','file')],
    [('x','sub/dir/../dir','sub/dir')]])
def test_normal_parent_components_terminal_chains_and_directory_targets_match_os(tmp_path,specs):
    root,p,r=fixture(tmp_path,specs);original=deepcopy(p)
    validate_value('document-migration-link-policy.yaml',p)
    result=restore_relative_links(root,tmp_path/'state',p,digest(p),r)
    assert result['status']=='pass' and not result['destination_business_writes'] and p==original
    fd=open_directory(root)
    try:
        for row,(_,target,final) in zip(p['links'],specs):
            assert os.readlink(root/row['path'])==target
            assert (root/row['path']).resolve()==root/final
            assert (root/row['path']).stat().st_ino==(root/final).stat().st_ino
            verify_actual_link(fd,row,p)
            if row['target_kind']=='file':assert hashlib.sha256((root/row['path']).read_bytes()).hexdigest()==row['target_sha256']
        assert restore_relative_links(root,tmp_path/'state',p,digest(p),r,resume=True,resume_approval_ref='APPROVAL.RESUME')==result
    finally:os.close(fd)


def test_removed_directory_replaced_with_undeclared_live_symlink_is_rejected(tmp_path):
    root,p,r=fixture(tmp_path,[('x','other/../file','file')])
    validate_value('document-migration-link-policy.yaml',p)
    (root/'other').rmdir();os.symlink('sub/dir',root/'other')
    fd=open_directory(root)
    try:
        with pytest.raises(OSError):verify_target(fd,p['links'][0],p)
    finally:os.close(fd)
    with pytest.raises(ValueError):restore_relative_links(root,tmp_path/'state',p,digest(p),r)
    assert not (tmp_path/'state/completed.json').exists()


def test_terminal_chain_changed_text_or_target_hash_cannot_pass(tmp_path):
    root,p,r=fixture(tmp_path,[('alias','file','file'),('x','alias','file')])
    restore_relative_links(root,tmp_path/'state',p,digest(p),r)
    fd=open_directory(root)
    try:
        (root/'alias').unlink();os.symlink('sub/file',root/'alias')
        with pytest.raises(ValueError,match='terminal link text'):verify_target(fd,p['links'][1],p)
        (root/'alias').unlink();os.symlink('file',root/'alias')
        (root/'file').write_bytes(b'changed')
        with pytest.raises(ValueError,match='target hash mismatch'):verify_target(fd,p['links'][1],p)
    finally:os.close(fd)
    with pytest.raises(ValueError):restore_relative_links(root,tmp_path/'state',p,digest(p),r,resume=True,resume_approval_ref='APPROVAL.RESUME')


def test_kernel_target_identity_mismatch_stops_terminal_receipt(tmp_path,monkeypatch):
    import trace_gate.migration_link_restore as m
    root,p,r=fixture(tmp_path,[('x','file','file')]);original=os.stat
    other=(root/'sub/file').stat()
    def changed(path,*args,**kwargs):
        if path=='x' and kwargs.get('follow_symlinks') is True and 'dir_fd' in kwargs:return other
        return original(path,*args,**kwargs)
    monkeypatch.setattr(m.os,'stat',changed)
    with pytest.raises(ValueError,match='OS link target differs'):
        restore_relative_links(root,tmp_path/'state',p,digest(p),r)
    assert not (tmp_path/'state/completed.json').exists()
    assert (tmp_path/'state/attempt-00000000.json').exists()


@pytest.mark.parametrize('field,value',[('target_sha256','b'*64),('target_kind','directory')])
def test_direct_live_verifier_rejects_unbound_target_kind_or_hash(tmp_path,field,value):
    root,p,_=fixture(tmp_path,[('x','file','file')]);p['links'][0][field]=value
    fd=open_directory(root)
    try:
        with pytest.raises(ValueError,match='kind/hash binding mismatch'):verify_target(fd,p['links'][0],p)
    finally:os.close(fd)
