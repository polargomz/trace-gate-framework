import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile

import pytest

from trace_gate.migration_archive import Limits, MANIFEST, extract_verified
from trace_gate.validation import TEMPLATE_DIR, load_yaml


def pack(tmp_path, extras=(), *, compressed=False, transform=None):
    entries=[("payload/example.txt",tarfile.REGTYPE,b"data",0o600,"")]+list(extras)
    manifest=load_yaml(TEMPLATE_DIR/"document-migration-package.yaml")
    manifest["files"]=[{"path":name,"size_bytes":len(body),"sha256":hashlib.sha256(body).hexdigest(),
        "artifact_id":"ARTIFACT.EXAMPLE","revision_id":"REV.EXAMPLE.1"} for name,kind,body,mode,link in entries if kind==tarfile.REGTYPE]
    if transform:transform(manifest)
    raw=json.dumps(manifest).encode(); out=io.BytesIO()
    with tarfile.open(fileobj=out,mode="w",format=tarfile.USTAR_FORMAT) as archive:
        for name,kind,body,mode,link in [(MANIFEST,tarfile.REGTYPE,raw,0o600,"")]+entries:
            info=tarfile.TarInfo(name);info.type=kind;info.mode=mode;info.linkname=link
            info.size=len(body) if kind in (tarfile.REGTYPE,tarfile.XHDTYPE,tarfile.GNUTYPE_LONGNAME) else 0
            archive.addfile(info,io.BytesIO(body) if info.size else None)
    data=gzip.compress(out.getvalue()) if compressed else out.getvalue()
    path=tmp_path/("package.tar.gz" if compressed else "package.tar");path.write_bytes(data)
    return path,hashlib.sha256(data).hexdigest(),hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("compressed",[False,True])
def test_strict_package_verified_private_source_unchanged(tmp_path,compressed):
    archive,ah,mh=pack(tmp_path,compressed=compressed);before=archive.read_bytes()
    destination=tmp_path/"staging";receipt=extract_verified(archive,destination,ah,mh)
    assert receipt["status"]=="pass" and not receipt["destination_business_writes"]
    assert (destination/"payload/example.txt").read_bytes()==b"data"
    assert archive.read_bytes()==before
    assert stat.S_IMODE(destination.stat().st_mode)==0o700
    assert stat.S_IMODE((destination/"payload/example.txt").stat().st_mode)==0o600


@pytest.mark.parametrize("name",["../escape","payload/../../escape","/absolute","C:/drive","C:\\drive","\\\\host\\share\\file","//host/share/file",
    "payload/./file","payload//file","payload/file\n","payload/CON.txt","payload/trailing.","payload/trailing ","payload/e\u0301.txt","payload/good\0hidden"])
def test_path_escape_alias_unicode_or_nul_rejected_before_staging(tmp_path,name):
    archive,ah,mh=pack(tmp_path,[(name,tarfile.REGTYPE,b"evil",0o600,"")]);destination=tmp_path/"staging"
    with pytest.raises((ValueError,tarfile.HeaderError,UnicodeError)):extract_verified(archive,destination,ah,mh)
    assert not destination.exists()


@pytest.mark.parametrize("kind,link",[(tarfile.SYMTYPE,"../../outside"),(tarfile.LNKTYPE,"/outside"),
    (tarfile.SYMTYPE,"example.txt"),(tarfile.FIFOTYPE,""),(tarfile.CHRTYPE,""),(tarfile.BLKTYPE,""),
    (tarfile.GNUTYPE_SPARSE,""),(tarfile.XHDTYPE,""),(tarfile.GNUTYPE_LONGNAME,"")])
def test_links_including_internal_and_special_extensions_fail_closed(tmp_path,kind,link):
    archive,ah,mh=pack(tmp_path,[("payload/link",kind,b"x",0o600,link)])
    with pytest.raises(ValueError,match="links, extensions or special"):extract_verified(archive,tmp_path/"staging",ah,mh)
    assert not (tmp_path/"staging").exists()


def test_link_followed_by_member_cannot_write_outside(tmp_path):
    outside=tmp_path/"outside";outside.mkdir();(outside/"sentinel").write_bytes(b"preserve")
    entries=[("redirect",tarfile.SYMTYPE,b"",0o700,"../outside"),("redirect/evil",tarfile.REGTYPE,b"evil",0o600,"")]
    archive,ah,mh=pack(tmp_path,entries)
    with pytest.raises(ValueError):extract_verified(archive,tmp_path/"staging",ah,mh)
    assert sorted(p.name for p in outside.iterdir())==["sentinel"]


@pytest.mark.parametrize("entries",[
    [("payload/example.txt",tarfile.REGTYPE,b"evil",0o600,"")],
    [("A/one",tarfile.REGTYPE,b"a",0o600,""),("a/two",tarfile.REGTYPE,b"b",0o600,"")],
    [("leaf",tarfile.REGTYPE,b"x",0o600,""),("leaf/child",tarfile.REGTYPE,b"y",0o600,"")],
])
def test_duplicate_case_parent_collision(tmp_path,entries):
    archive,ah,mh=pack(tmp_path,entries)
    with pytest.raises(ValueError):extract_verified(archive,tmp_path/"staging",ah,mh)
    assert not (tmp_path/"staging").exists()


@pytest.mark.parametrize("mode",[0o777,0o4755,0o2644,0o1644,0o666,0o755])
def test_unsafe_or_executable_permissions_not_replayed(tmp_path,mode):
    archive,ah,mh=pack(tmp_path,[("payload/unsafe",tarfile.REGTYPE,b"x",mode,"")])
    with pytest.raises(ValueError):extract_verified(archive,tmp_path/"staging",ah,mh)
    assert not (tmp_path/"staging").exists()


@pytest.mark.parametrize("limits",[Limits(members=1),Limits(payload_bytes=1),Limits(file_bytes=3),Limits(manifest_bytes=1),Limits(path_bytes=4),Limits(archive_bytes=1)])
def test_independent_resource_bounds_prevent_output(tmp_path,limits):
    archive,ah,mh=pack(tmp_path)
    with pytest.raises(ValueError):extract_verified(archive,tmp_path/"staging",ah,mh,limits)
    assert not (tmp_path/"staging").exists()


def test_compressed_expansion_bomb_bounded_before_tar_header_parsing(tmp_path):
    archive,ah,mh=pack(tmp_path,[("payload/bomb",tarfile.REGTYPE,b"0"*100000,0o600,"")],compressed=True)
    with pytest.raises(ValueError,match="expansion bound"):extract_verified(archive,tmp_path/"staging",ah,mh,Limits(expanded_bytes=4096))
    assert not (tmp_path/"staging").exists()


def test_archive_and_internal_manifest_digests_independent(tmp_path):
    archive,ah,mh=pack(tmp_path)
    with pytest.raises(ValueError,match="archive digest"):extract_verified(archive,tmp_path/"staging", "b"*64,mh)
    with pytest.raises(ValueError,match="internal manifest digest"):extract_verified(archive,tmp_path/"staging", ah,"b"*64)
    assert not (tmp_path/"staging").exists()


def test_per_file_hash_and_exact_manifest_coverage(tmp_path):
    archive,ah,mh=pack(tmp_path,transform=lambda m:m["files"][0].update(sha256="b"*64))
    with pytest.raises(ValueError,match="per-file hash"):extract_verified(archive,tmp_path/"staging",ah,mh)
    archive,ah,mh=pack(tmp_path,transform=lambda m:m["files"][0].update(path="payload/missing"))
    with pytest.raises(ValueError,match="coverage"):extract_verified(archive,tmp_path/"staging",ah,mh)


def test_existing_destination_no_overwrite_or_automatic_retry(tmp_path):
    archive,ah,mh=pack(tmp_path);destination=tmp_path/"staging";destination.mkdir();(destination/"existing").write_bytes(b"preserve")
    with pytest.raises(FileExistsError):extract_verified(archive,destination,ah,mh)
    assert (destination/"existing").read_bytes()==b"preserve"


def test_archive_symlink_and_destination_parent_symlink_refused(tmp_path):
    archive,ah,mh=pack(tmp_path);alias=tmp_path/"archive-link";alias.symlink_to(archive)
    with pytest.raises(OSError):extract_verified(alias,tmp_path/"staging",ah,mh)
    parent=tmp_path/"outside";parent.mkdir();link=tmp_path/"parent-link";link.symlink_to(parent,target_is_directory=True)
    with pytest.raises(OSError):extract_verified(archive,link/"staging",ah,mh)
    assert not (parent/"staging").exists()


def test_child_symlink_substitution_caught_by_fd_no_follow(tmp_path,monkeypatch):
    archive,ah,mh=pack(tmp_path);outside=tmp_path/"outside";outside.mkdir();original=os.open;done=False
    def race(path,flags,*args,**kwargs):
        nonlocal done
        if path=="payload" and kwargs.get("dir_fd") is not None and not done:
            done=True;fd=kwargs["dir_fd"];os.rmdir("payload",dir_fd=fd);os.symlink(str(outside),"payload",dir_fd=fd)
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,"open",race)
    with pytest.raises(OSError):extract_verified(archive,tmp_path/"staging",ah,mh)
    assert not list(outside.iterdir())


def test_trailing_or_truncated_archive_not_accepted(tmp_path):
    archive,ah,mh=pack(tmp_path);data=archive.read_bytes()
    for changed in (data[:600],data+b"unexpected"):
        archive.write_bytes(changed)
        with pytest.raises(ValueError):extract_verified(archive,tmp_path/"staging",hashlib.sha256(changed).hexdigest(),mh)
        assert not (tmp_path/"staging").exists()
