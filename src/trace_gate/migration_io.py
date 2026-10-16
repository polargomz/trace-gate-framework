"""Private dirfd I/O shared by opt-in adapters; not a distributed lease."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import unicodedata

from .migration import require, relative_path, digest, unique_paths
from .migration_archive import open_directory, object_no_duplicates


def root_key(path):
    return unicodedata.normalize('NFC',os.path.abspath(path)).casefold()


def private(fd):
    info=os.fstat(fd)
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700,"directory must be private 0700")


@contextmanager
def stage_lock(path):
    require(path.name not in {'','.','..'},'invalid stage name')
    p=open_directory(path.parent);f=None
    try:
        private(p);f=os.open('.'+path.name+'.trace-stage.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=p)
        info=os.fstat(f);require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600,'unsafe stage lock')
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally:
        if f is not None:os.close(f)
        os.close(p)


@contextmanager
def parent(fd, path, create=False):
    relative_path(path);current=os.dup(fd)
    try:
        for part in PurePosixPath(path).parts[:-1]:
            if create:
                try:os.mkdir(part,0o700,dir_fd=current);os.fsync(current)
                except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
            os.close(current);current=child
            if create:private(current)
        yield current,PurePosixPath(path).name
    finally:os.close(current)


@contextmanager
def regular(fd, path, flags=os.O_RDONLY):
    with parent(fd,path) as (directory,name):
        file_fd=os.open(name,flags|os.O_NOFOLLOW,dir_fd=directory)
    try:
        info=os.fstat(file_fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1,"non-regular or hardlinked file")
        yield file_fd
    finally:os.close(file_fd)


def file_hash(fd, path, buffer=65536, max_bytes=None):
    with regular(fd,path) as file_fd:
        before=os.fstat(file_fd);sha=hashlib.sha256();size=0
        require(max_bytes is None or before.st_size<=max_bytes,'file byte bound')
        while block:=os.read(file_fd,buffer):
            sha.update(block);size+=len(block)
            require(max_bytes is None or size<=max_bytes,'file grew beyond byte bound')
        after=os.fstat(file_fd)
        require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),"file changed during read")
        return size,sha.hexdigest()


def exists(fd,path):
    try:
        with parent(fd,path) as (directory,name):os.stat(name,dir_fd=directory,follow_symlinks=False)
        return True
    except FileNotFoundError:return False


def read_json(fd,path,limit):
    with regular(fd,path) as f:
        info=os.fstat(f);require(info.st_size<=limit,"JSON size bound")
        raw=bytearray()
        while block:=os.read(f,min(65536,limit-len(raw)+1)):
            raw.extend(block);require(len(raw)<=limit,"JSON size bound")
    return json.loads(raw,object_pairs_hook=object_no_duplicates)


def write_json(fd,path,value,limit):
    raw=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
    require(len(raw)<=limit,"JSON size bound")
    with parent(fd,path,True) as (directory,name):
        f=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(f,"wb") as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.fsync(directory)


def claim_directory(path:Path,binding,resume=False,resume_approval_ref=None,limit=2*1024**2):
    require(not resume or bool(resume_approval_ref),"explicit resume approval required")
    p=open_directory(path.parent)
    try:
        private(p)
        try:os.mkdir(path.name,0o700,dir_fd=p)
        except FileExistsError:require(resume,"destination exists; explicit resume required")
        fd=os.open(path.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=p)
        try:private(fd)
        except BaseException:os.close(fd);raise
    finally:os.close(p)
    lock=None
    try:
        lock=os.open(".lock",os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=fd)
        info=os.fstat(lock);require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600,"unsafe adapter lock")
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if exists(fd,"claim.json"):
            require(resume and read_json(fd,"claim.json",limit)==binding,"source/plan/configuration changed on resume")
        else:
            require(not resume,"missing claim; resume cannot create a new identity")
            write_json(fd,"claim.json",binding,limit)
        return fd,lock
    except BaseException:
        if lock is not None:os.close(lock)
        os.close(fd);raise


def walk(fd,max_items):
    # Queue bounded paths, not one open descriptor per pending directory.
    result={};pending=[""]
    while pending:
        prefix=pending.pop()
        if prefix:
            with parent(fd,prefix+'placeholder') as (child,_):directory=os.dup(child)
        else:directory=os.dup(fd)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path=prefix+entry.name;relative_path(path)
                    require(len(path.encode())<=512,"inventory path bound")
                    info=os.stat(entry.name,dir_fd=directory,follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        kind="directory";pending.append(path+"/")
                    elif stat.S_ISREG(info.st_mode):
                        require(info.st_nlink==1,"hardlinked payload unsupported");kind="file"
                    elif stat.S_ISLNK(info.st_mode):kind="link"
                    else:raise ValueError("special file unsupported")
                    result[path]={"kind":kind,"size_bytes":info.st_size,"allocated_bytes":info.st_blocks*512}
                    if kind=="link":result[path]["target"]=os.readlink(entry.name,dir_fd=directory)
                    require(len(result)<=max_items,"inventory entry bound")
        finally:os.close(directory)
    unique_paths(list(result));return result


def directory_digest(path,inventory,links):
    return directory_digests(inventory,links)[path]


def directory_digests(inventory,links):
    states={p:hashlib.sha256(b'[') for p,r in inventory.items() if r['kind']=='directory'};counts={p:0 for p in states}
    records={p:{'kind':r['kind'],**({'sha256':r['sha256']} if r['kind']=='file' else {})} for p,r in inventory.items()}
    records.update({r['path']:{'kind':'link','target':r['target'],'link_text_sha256':r['link_text_sha256']} for r in links})
    for path,row in sorted(records.items()):
        for parent_path in PurePosixPath(path).parents:
            p=str(parent_path)
            if p not in states:continue
            relative=path[len(p)+1:];raw=json.dumps({'path':relative,**row},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
            if counts[p]:states[p].update(b',')
            states[p].update(raw);counts[p]+=1
    for state in states.values():state.update(b']')
    return {p:state.hexdigest() for p,state in states.items()}


def verify_tree(fd,inventory,links,max_items,buffer=65536,allow_links=False,require_all_links=True):
    expected={r["path"]:r for r in inventory};linkmap={r["path"]:r for r in links}
    unique_paths(list(expected)+list(linkmap));actual=walk(fd,max_items)
    inferred=set()
    for path in set(expected)|set(linkmap):inferred.update(str(p) for p in PurePosixPath(path).parents if str(p)!=".")
    allowed=set(expected)|set(linkmap)|inferred
    require(set(actual)<=allowed and set(expected)<=set(actual) and inferred<=set(actual),"staging/source inventory coverage mismatch")
    require(not require_all_links or set(linkmap)<=set(actual),"required links missing")
    for path,item in actual.items():
        if path in linkmap:
            require(allow_links and item["kind"]=="link" and item["target"]==linkmap[path]["target"],"unexpected or changed link")
        else:require(item["kind"]==(expected[path]["kind"] if path in expected else "directory"),"inventory type mismatch")
    for path,item in expected.items():
        if item["kind"]=="file":
            size,sha=file_hash(fd,path,buffer,item.get('size_bytes'))
            require(sha==item["sha256"] and ("size_bytes" not in item or size==item["size_bytes"]),"file inventory digest/size mismatch")
    directories=directory_digests(expected,links)
    for path,item in expected.items():
        if item["kind"]=="directory":require(directories[path]==item["sha256"],"directory inventory digest mismatch")
    return actual
