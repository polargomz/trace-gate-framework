"""Opt-in relative symlink reconstruction AFTER regular-file staging verification."""
from pathlib import Path
import os
import stat

from .migration import digest,require,related_paths
from .migration_archive import open_directory
from .migration_io import private,claim_directory,write_json,read_json,exists,verify_tree,parent,file_hash,stage_lock,root_key
from .migration_links import target_path


def verify_target(root,row,policy):
    links={r['path']:r for r in policy['links']};inventory={r['path']:r for r in policy['inventory']}
    path=target_path(row['path'],row['target'])
    while path in links:path=target_path(path,links[path]['target'])
    expected=inventory[path]
    with parent(root,path) as (directory,name):
        info=os.stat(name,dir_fd=directory,follow_symlinks=False)
        if expected['kind']=='file':
            require(stat.S_ISREG(info.st_mode),'link target no longer a regular file')
            require(file_hash(root,path)[1]==expected['sha256'],'live link target hash mismatch')
        else:
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            try:private(fd)
            finally:os.close(fd)


def restore_relative_links(staging:Path,state_dir:Path,policy,expected_policy_sha256,staging_receipt,*,resume=False,resume_approval_ref=None):
    """Private dirfd/no-follow stage only; same-UID/root hostile writers excluded.

    Existing approved links are accepted only on explicit same-policy resume.
    Failures keep private partial links and immutable failure receipts, never activate.
    """
    from .validation import validate_value
    validate_value('document-migration-link-policy.yaml',policy)
    require(policy['mode']=='restore_relative_symlinks','descriptor-only policy cannot restore')
    require(digest(policy)==expected_policy_sha256,'link policy identity mismatch')
    approval=policy['approval']
    max_items=approval['max_inventory_entries']+approval['max_links']
    require(digest(staging_receipt)==approval['staging_receipt_sha256'] and staging_receipt['inventory_sha256']==policy['verified_inventory_sha256'],'staging receipt binding mismatch')
    require(staging_receipt['status'] in {'pass','regular_files_verified'} and staging_receipt['destination_business_writes'] is False,'staging is not verified/inactive')
    require(not related_paths(root_key(staging),root_key(state_dir)),'link state must be outside payload')
    binding={'adapter':'relative-links/0.1.0','policy_sha256':expected_policy_sha256,'staging_receipt_sha256':digest(staging_receipt),'staging':str(staging.absolute())}
    state,lock=claim_directory(state_dir,binding,resume,resume_approval_ref);root=None;attempt=None;guard=None
    result={'status':'pass','adapter':'relative-links/0.1.0','policy_sha256':expected_policy_sha256,
        'links_restored':len(policy['links']),'original_target_strings_preserved':True,'regular_files_reverified':True,
        'destination_business_writes':False,'canonical_cutover_performed':False}
    try:
        attempt=sum(name.startswith('attempt-') and name.endswith('.json') for name in os.listdir(state))
        guard=stage_lock(staging);guard.__enter__()
        root=open_directory(staging);private(root)
        from .migration_io import walk
        observed=walk(root,max_items)
        require(sum(r['size_bytes'] for r in observed.values() if r['kind']=='file')<=approval['max_payload_bytes'] and all(r['size_bytes']<=approval['max_file_bytes'] for r in observed.values() if r['kind']=='file'),'approved link target byte bound')
        if exists(state,'completed.json'):
            verify_tree(root,policy['inventory'],policy['links'],max_items,allow_links=True)
            require(read_json(state,'completed.json',2*1024**2)==result,'completed link receipt drift')
            return result
        actual=verify_tree(root,policy['inventory'],policy['links'],max_items,allow_links=resume,require_all_links=False)
        require(resume or not any(v['kind']=='link' for v in actual.values()),'links already present before restore')
        links={r['path']:r for r in policy['links']};ordered=[];done=set()
        def order(path):
            if path in done:return
            row=links[path];target=target_path(path,row['target'])
            if target in links:order(target)
            done.add(path);ordered.append(row)
        for path in sorted(links):order(path)
        for row in ordered:
            verify_target(root,row,policy)
            with parent(root,row['path']) as (directory,name):
                if exists(root,row['path']):
                    require(resume and os.readlink(name,dir_fd=directory)==row['target'],'link path exists or changed')
                else:
                    os.symlink(row['target'],name,dir_fd=directory)
                    require(os.readlink(name,dir_fd=directory)==row['target'],'restored link text mismatch')
                    os.fsync(directory)
        verify_tree(root,policy['inventory'],policy['links'],max_items,allow_links=True)
        write_json(state,'completed.json',result,2*1024**2)
        write_json(state,f'attempt-{attempt:08d}.json',result,2*1024**2);return result
    except BaseException as error:
        if attempt is not None:write_json(state,f'attempt-{attempt:08d}.json',{'status':'fail','error_code':type(error).__name__,'policy_sha256':expected_policy_sha256,'resume_approval_ref':resume_approval_ref,'business_writes_held':True},2*1024**2)
        raise
    finally:
        if root is not None:os.close(root)
        if guard is not None:guard.__exit__(None,None,None)
        os.close(lock);os.close(state)
