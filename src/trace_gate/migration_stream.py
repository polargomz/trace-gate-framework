"""Opt-in bounded raw chunk-set package/restore. No tar bypass or source mutation.

Default archive limits remain unchanged. A file may span many <=256MiB parts;
metadata is bounded separately. All I/O is streaming, private, exclusive/no-follow.
"""
from __future__ import annotations
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .migration import digest,require,unique_paths,related_paths
from .migration_archive import open_directory
from .migration_io import (claim_directory,private,parent,regular,file_hash,exists,
    read_json,write_json,walk,verify_tree,stage_lock,root_key)

DEFAULT_PART_BYTES=256*1024**2


def policy_errors(value):
    try:
        s=value['settings'];a=value['approval']
        require(digest(s)==a['configuration_sha256'],'approved streaming configuration digest mismatch')
        require(s['part_bytes']<=DEFAULT_PART_BYTES and s['buffer_bytes']<=1024**2,'default part/buffer bound cannot be enlarged')
        require(s['max_file_bytes']<=s['max_payload_bytes']<=s['max_staging_bytes'],'inconsistent payload/file/staging limits')
        require(s['part_bytes']+s['max_manifest_bytes']<=s['max_temp_bytes'],'temporary storage limit too small')
        require(s['max_bundle_bytes']>=s['max_manifest_bytes']+16384,'bundle control reserve too small')
        require(not s['enabled'] or a['source_scope_frozen'],'approved source freeze evidence required')
        return []
    except ValueError as e:return [str(e)]


def inventory_shape(inventory,s):
    entries=inventory['entries'];unique_paths([r['path'] for r in entries])
    files=sorted((r for r in entries if r['kind']=='file'),key=lambda r:r['path'])
    directories=[r for r in entries if r['kind']=='directory']
    require(len(files)<=s['max_files'] and len(directories)<=s['max_directories'],'inventory item bound')
    total=sum(r['size_bytes'] for r in files)
    require(total<=s['max_payload_bytes'] and all(r['size_bytes']<=s['max_file_bytes'] for r in files),'source payload/file bound')
    require(all(r['size_bytes']==0 for r in directories),'directory size must be zero')
    require(sum(math.ceil(r['size_bytes']/s['part_bytes']) for r in files)<=s['max_parts'],'part count bound')
    base={r['path']:r for r in entries}
    for r in entries:
        for p in PurePosixPath(r['path']).parents:
            if str(p)!='.':require(str(p) in base and base[str(p)]['kind']=='directory','explicit directory closure required')
    link_policy=inventory['link_policy']
    links=[] if link_policy is None else link_policy['links']
    if link_policy:
        from .validation import validate_value
        validate_value('document-migration-link-policy.yaml',link_policy)
        reduced=[{k:r[k] for k in ('path','kind','sha256')} for r in entries]
        require(link_policy['inventory']==reduced,'stream/link inventory mismatch')
    unique_paths([r['path'] for r in entries]+[r['path'] for r in links])
    return files,links,total


def manifest_errors(value):
    try:
        s=value['settings'];require(not policy_errors(value['policy']),'invalid nested streaming policy')
        require(s==value['policy']['settings'],'package-set configuration mismatch')
        require(value['format']=='TRACE-DM-CHUNKSET/1','not a chunk-set format')
        require(value['source_inventory_sha256']==digest(value['inventory'])==s['source_inventory_sha256'],'package source inventory mismatch')
        require(value['plan_sha256']==s['plan_sha256'] and value['set_id']==s['set_id'],'cross-plan/set substitution')
        files,_,total=inventory_shape(value['inventory'],s)
        parts=value['parts'];expected=[]
        for f in files:
            for offset in range(0,f['size_bytes'],s['part_bytes']):expected.append((f['path'],offset,min(s['part_bytes'],f['size_bytes']-offset)))
        require(len(parts)==len(expected)<=s['max_parts'],'missing/duplicate part')
        for i,(part,layout) in enumerate(zip(parts,expected)):
            require((part['index'],part['name'])==(i,f'part-{i:08d}.bin'),'part order/name mismatch')
            require((part['file_path'],part['offset'],part['size_bytes'])==layout,'chunk gap/overlap/layout mismatch')
        require(sum(p['size_bytes'] for p in parts)==total,'aggregate expanded size mismatch')
        return []
    except ValueError as e:return [str(e)]


def check_space(fd,s,remaining=0):
    v=os.fstatvfs(fd);require(v.f_bavail*v.f_frsize>=s['minimum_free_bytes']+remaining,'insufficient free disk reserve')


def write_all(fd,data):
    view=memoryview(data)
    while view:
        written=os.write(fd,view);require(written>0,'short write');view=view[written:]


def verify_parts(fd,manifest):
    s=manifest['settings'];expected={p['name'] for p in manifest['parts']}
    actual=walk(fd,s['max_parts']*2+2048)
    allowed=lambda n:(n in {'.lock','claim.json','package-set.json','completed.json','failures'} or re.fullmatch(r'attempt-\d{8}\.json',n) or re.fullmatch(r'failures/part-\d{8}\.partial\.\d{8}',n) or n in expected)
    require(all(allowed(n) and r['kind']!='link' for n,r in actual.items()),'unexpected bundle member')
    require(file_hash(fd,'package-set.json',s['buffer_bytes'])[1]==digest(manifest),'package-set canonical bytes changed')
    for part in manifest['parts']:
        require(file_hash(fd,part['name'],s['buffer_bytes'])==(part['size_bytes'],part['sha256']),'part hash/size mismatch')
    require(sum(r['size_bytes'] for r in actual.values() if r['kind']=='file')<=s['max_bundle_bytes'],'aggregate bundle storage bound')


def package_result(manifest):
    files,_,total=inventory_shape(manifest['inventory'],manifest['settings'])
    return {'status':'pass','adapter':'chunk-set/0.1.0','package_set_sha256':digest(manifest),
        'source_inventory_sha256':manifest['source_inventory_sha256'],'parts':len(manifest['parts']),
        'files':len(files),'payload_bytes':total,'source_modified':False,'destination_business_writes':False}


def pack_chunk_set(source:Path,bundle:Path,policy,inventory,*,resume=False,resume_approval_ref=None):
    from .validation import validate_value
    import json,jsonschema
    from .validation import ROOT
    schema=json.loads((ROOT/'schemas/document-migration.schema.json').read_text())
    jsonschema.Draft202012Validator({'$ref':'#/$defs/migrationStreamInventory',**schema}).validate(inventory)
    validate_value('document-migration-stream-policy.yaml',policy);s=policy['settings']
    require(s['enabled'] and policy['approval']['source_scope_frozen'],'explicit streaming opt-in/freeze required')
    require(digest(inventory)==s['source_inventory_sha256'],'source inventory binding mismatch')
    require(not related_paths(root_key(source),root_key(bundle)),'bundle must be outside source')
    files,links,total=inventory_shape(inventory,s)
    binding={'adapter':'chunk-set/0.1.0','policy_sha256':digest(policy),'inventory_sha256':digest(inventory),'source':str(source.absolute())}
    out,lock=claim_directory(bundle,binding,resume,resume_approval_ref,s['max_manifest_bytes']);src=None;attempt=None
    try:
        attempt=sum(n.startswith('attempt-') and n.endswith('.json') for n in os.listdir(out))
        src=open_directory(source)
        verify_tree(src,inventory['entries'],links,s['max_files']+s['max_directories']+len(links),s['buffer_bytes'],allow_links=True)
        if exists(out,'completed.json'):
            manifest=read_json(out,'package-set.json',s['max_manifest_bytes']);validate_value('document-migration-package-set.yaml',manifest)
            require(manifest['policy']==policy and manifest['inventory']==inventory,'changed completed source/plan')
            verify_parts(out,manifest);result=package_result(manifest)
            require(read_json(out,'completed.json',s['max_manifest_bytes'])==result,'completed receipt drift')
            return result
        layout=walk(out,s['max_parts']*2+2048)
        require(all(r['kind']!='link' for r in layout.values()),'link in bundle state')
        used=sum(r['size_bytes'] for r in layout.values() if r['kind']=='file');failed=sum(r['size_bytes'] for n,r in layout.items() if n.startswith('failures/'))
        require(used<=s['max_bundle_bytes'] and failed<=s['max_failed_part_bytes'],'prior bundle/failure budget exceeded')
        expected_sizes={};expected_index=0
        for f in files:
            for offset in range(0,f['size_bytes'],s['part_bytes']):
                expected_sizes[f'part-{expected_index:08d}.bin']=min(s['part_bytes'],f['size_bytes']-offset);expected_index+=1
        allowed=lambda n:n in {'.lock','claim.json','package-set.json','failures'} or re.fullmatch(r'attempt-\d{8}\.json',n) or re.fullmatch(r'failures/part-\d{8}\.partial\.\d{8}',n) or n in expected_sizes or (n.endswith('.partial') and n[:-8]+'.bin' in expected_sizes)
        require(all(allowed(n) for n in layout),'unexpected bundle member')
        for n,r in layout.items():
            if n in expected_sizes:require(r['size_bytes']<=expected_sizes[n],'prior part size mismatch')
        existing_bytes=sum(expected_sizes[n] for n,r in layout.items() if n in expected_sizes and r['size_bytes']==expected_sizes[n])
        check_space(out,s,max(0,total-existing_bytes)+s['max_manifest_bytes']);parts=[];index=0
        for file in files:
            whole=hashlib.sha256()
            with regular(src,file['path']) as input_fd:
                before=os.fstat(input_fd);require(before.st_size==file['size_bytes'],'changed source size')
                for offset in range(0,file['size_bytes'],s['part_bytes']):
                    size=min(s['part_bytes'],file['size_bytes']-offset);name=f'part-{index:08d}.bin';partial=f'part-{index:08d}.partial'
                    if exists(out,partial):
                        require(resume,'partial part needs explicit resume')
                        with regular(out,partial) as f:old_size=os.fstat(f).st_size
                        require(failed+old_size<=s['max_failed_part_bytes'],'failed part retention bound')
                        with parent(out,'failures/'+partial+f'.{attempt:08d}',True) as (faildir,failedname):
                            require(not exists(out,'failures/'+partial+f'.{attempt:08d}'),'failure archive collision')
                            os.rename(partial,failedname,src_dir_fd=out,dst_dir_fd=faildir);os.fsync(faildir)
                        failed+=old_size
                    reuse=exists(out,name)
                    if reuse:
                        with regular(out,name) as old:prior_size=os.fstat(old).st_size
                        if prior_size<size:
                            require(resume and failed+prior_size<=s['max_failed_part_bytes'],'short part requires approved bounded resume')
                            with parent(out,'failures/'+partial+f'.{attempt:08d}',True) as (faildir,failedname):
                                require(not exists(out,'failures/'+partial+f'.{attempt:08d}'),'failure archive collision')
                                os.rename(name,failedname,src_dir_fd=out,dst_dir_fd=faildir);os.fsync(faildir)
                            failed+=prior_size;reuse=False
                    require(reuse or used+size+s['max_manifest_bytes']<=s['max_bundle_bytes'],'bundle storage budget')
                    check_space(out,s,0 if reuse else size)
                    f=None
                    if not reuse:f=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=out)
                    sha=hashlib.sha256();left=size
                    try:
                        while left:
                            block=os.read(input_fd,min(left,s['buffer_bytes']));require(bool(block),'short source read')
                            left-=len(block);sha.update(block);whole.update(block)
                            if f is not None:write_all(f,block)
                        if f is not None:os.fsync(f)
                    finally:
                        if f is not None:os.close(f)
                    part={'index':index,'name':name,'file_path':file['path'],'offset':offset,'size_bytes':size,'sha256':sha.hexdigest()}
                    if reuse:require(file_hash(out,name,s['buffer_bytes'])==(size,part['sha256']),'existing part differs; no overwrite')
                    else:
                        require(file_hash(out,name,s['buffer_bytes'])==(size,part['sha256']),'written part hash mismatch')
                        os.fsync(out);used+=size
                    parts.append(part);index+=1
                require(os.read(input_fd,1)==b'' and whole.hexdigest()==file['sha256'],'source changed or whole-file hash mismatch')
                after=os.fstat(input_fd)
                require((before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),'source changed during packaging')
        verify_tree(src,inventory['entries'],links,s['max_files']+s['max_directories']+len(links),s['buffer_bytes'],allow_links=True)
        manifest={'schema_version':'0.1.0','subframework_id':'TRACE-DM','format':'TRACE-DM-CHUNKSET/1','set_id':s['set_id'],
            'plan_sha256':s['plan_sha256'],'source_inventory_sha256':digest(inventory),'settings':s,'policy':policy,'inventory':inventory,'parts':parts}
        validate_value('document-migration-package-set.yaml',manifest)
        if exists(out,'package-set.json'):require(read_json(out,'package-set.json',s['max_manifest_bytes'])==manifest,'existing package-set differs')
        else:write_json(out,'package-set.json',manifest,s['max_manifest_bytes'])
        verify_parts(out,manifest)
        footprint=walk(out,s['max_parts']*2+2048)
        require(sum(v['allocated_bytes'] for v in footprint.values())+16384<=s['max_bundle_bytes'],'allocated bundle storage bound')
        result=package_result(manifest)
        write_json(out,'completed.json',result,s['max_manifest_bytes']);write_json(out,f'attempt-{attempt:08d}.json',result,s['max_manifest_bytes']);return result
    except BaseException as error:
        if attempt is not None:write_json(out,f'attempt-{attempt:08d}.json',{'status':'fail','error_code':type(error).__name__,'policy_sha256':digest(policy),'resume_approval_ref':resume_approval_ref},s['max_manifest_bytes'])
        raise
    finally:
        if src is not None:os.close(src)
        os.close(lock);os.close(out)


def restore_chunk_set(bundle:Path,staging:Path,state_dir:Path,policy,expected_set_sha256,*,resume=False,resume_approval_ref=None):
    from .validation import validate_value
    validate_value('document-migration-stream-policy.yaml',policy);s=policy['settings']
    require(s['enabled'],'explicit streaming opt-in required')
    require(not related_paths(root_key(staging),root_key(state_dir)) and not related_paths(root_key(bundle),root_key(staging)) and not related_paths(root_key(bundle),root_key(state_dir)),'state/bundle/payload roots overlap')
    b=open_directory(bundle);state=None;lock=None;root=None;attempt=None;guard=None
    try:
        manifest=read_json(b,'package-set.json',s['max_manifest_bytes']);validate_value('document-migration-package-set.yaml',manifest)
        require(digest(manifest)==expected_set_sha256 and manifest['policy']==policy,'package-set/approved configuration mismatch')
        verify_parts(b,manifest);files,links,total=inventory_shape(manifest['inventory'],s)
        binding={'adapter':'chunk-restore/0.1.0','package_set_sha256':expected_set_sha256,'policy_sha256':digest(policy),'staging':str(staging.absolute())}
        state,lock=claim_directory(state_dir,binding,resume,resume_approval_ref,s['max_manifest_bytes'])
        attempt=sum(n.startswith('attempt-') and n.endswith('.json') for n in os.listdir(state))
        guard=stage_lock(staging);guard.__enter__()
        p=open_directory(staging.parent)
        try:
            private(p)
            try:os.mkdir(staging.name,0o700,dir_fd=p)
            except FileExistsError:require(resume,'staging already exists')
            root=os.open(staging.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=p);private(root)
        finally:os.close(p)
        existing=walk(root,s['max_files']+s['max_directories'])
        require(not any(v['kind']=='link' for v in existing.values()),'regular restore sealed once links exist')
        approved={r['path']:r for r in manifest['inventory']['entries']}
        require(set(existing)<=set(approved) and all(v['kind']==approved[n]['kind'] for n,v in existing.items()),'unexpected staging member')
        used=sum(v['size_bytes'] for v in existing.values() if v['kind']=='file')
        require(used<=total<=s['max_staging_bytes'],'staging storage bound');check_space(root,s,total-used)
        for row in manifest['inventory']['entries']:
            if row['kind']=='directory':
                with parent(root,row['path']+'/placeholder',True):pass
        footprint=walk(root,s['max_files']+s['max_directories'])
        allocated=sum(v['allocated_bytes'] for v in footprint.values())
        require(allocated<=s['max_staging_bytes'],'allocated staging storage bound')
        grouped={r['path']:[] for r in files}
        for part in manifest['parts']:grouped[part['file_path']].append(part)
        for file in files:
            with parent(root,file['path'],True) as (directory,name):
                flags=os.O_RDWR|os.O_NOFOLLOW
                out=os.open(name,flags if exists(root,file['path']) else flags|os.O_CREAT|os.O_EXCL,0o600,dir_fd=directory)
            try:
                info=os.fstat(out);require(info.st_nlink==1 and stat.S_ISREG(info.st_mode),'unsafe reconstructed file')
                initial=info.st_size;require(initial<=file['size_bytes'],'reconstructed size exceeds bound')
                prior_allocated=info.st_blocks*512;baseline_allocated=allocated-prior_allocated
                for part in grouped[file['path']]:
                    check_space(root,s,0);sha=hashlib.sha256();offset=part['offset'];left=part['size_bytes']
                    with regular(b,part['name']) as source:
                        while left:
                            block=os.read(source,min(left,s['buffer_bytes']));require(bool(block),'truncated part')
                            left-=len(block);sha.update(block)
                            overlap=max(0,min(len(block),initial-offset))
                            if overlap:require(os.pread(out,overlap,offset)==block[:overlap],'existing prefix mismatch; no overwrite')
                            if overlap<len(block):os.lseek(out,offset+overlap,os.SEEK_SET);write_all(out,block[overlap:])
                            offset+=len(block)
                        require(not os.read(source,1) and sha.hexdigest()==part['sha256'],'part changed during restore')
                    os.fsync(out)
                    require(baseline_allocated+os.fstat(out).st_blocks*512<=s['max_staging_bytes'],'allocated staging storage bound')
                require(os.fstat(out).st_size==file['size_bytes'],'reconstructed size mismatch')
                allocated=baseline_allocated+os.fstat(out).st_blocks*512
            finally:os.close(out)
            require(file_hash(root,file['path'],s['buffer_bytes'])==(file['size_bytes'],file['sha256']),'reconstructed whole-file hash mismatch')
        final_tree=verify_tree(root,manifest['inventory']['entries'],links,s['max_files']+s['max_directories']+len(links),s['buffer_bytes'],allow_links=False,require_all_links=False)
        require(sum(v['allocated_bytes'] for v in final_tree.values())<=s['max_staging_bytes'],'final allocated staging storage bound')
        reduced=[{k:r[k] for k in ('path','kind','sha256')} for r in manifest['inventory']['entries']]
        result={'status':'regular_files_verified' if links else 'pass','adapter':'chunk-restore/0.1.0','package_set_sha256':expected_set_sha256,
            'inventory_sha256':digest(reduced),'files_verified':len(files),'payload_bytes':total,'links_pending':len(links),
            'destination_business_writes':False,'canonical_cutover_performed':False}
        if exists(state,'completed.json'):require(read_json(state,'completed.json',s['max_manifest_bytes'])==result,'completed receipt drift')
        else:write_json(state,'completed.json',result,s['max_manifest_bytes'])
        write_json(state,f'attempt-{attempt:08d}.json',result,s['max_manifest_bytes']);return result
    except BaseException as error:
        if state is not None and attempt is not None:write_json(state,f'attempt-{attempt:08d}.json',{'status':'fail','error_code':type(error).__name__,'package_set_sha256':expected_set_sha256,'business_writes_held':True},s['max_manifest_bytes'])
        raise
    finally:
        if root is not None:os.close(root)
        if guard is not None:guard.__exit__(None,None,None)
        if lock is not None:os.close(lock)
        if state is not None:os.close(state)
        os.close(b)
