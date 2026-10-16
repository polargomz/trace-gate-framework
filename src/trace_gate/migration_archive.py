"""Strict USTAR(+gzip), regular files only; private no-follow staged extraction.

No extractall, links, extensions, owner/ACL replay, scripts or canonical cutover.
The expected digests must come from a separately authorized, trusted receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
from typing import BinaryIO

from .migration import relative_path, require, unique_paths

MANIFEST = "TRACE-DM-MANIFEST.json"


@dataclass(frozen=True)
class Limits:
    archive_bytes: int = 256 * 1024**2
    expanded_bytes: int = 272 * 1024**2
    payload_bytes: int = 256 * 1024**2
    file_bytes: int = 64 * 1024**2
    members: int = 4096
    manifest_bytes: int = 2 * 1024**2
    path_bytes: int = 512


def copy_bounded(source: BinaryIO, target: BinaryIO, limit: int) -> str:
    total = 0; sha = hashlib.sha256()
    while True:
        chunk = source.read(min(65536, limit-total+1))
        if not chunk:
            break
        total += len(chunk); require(total <= limit, "archive expansion bound")
        target.write(chunk); sha.update(chunk)
    target.flush(); target.seek(0)
    return sha.hexdigest()


def object_no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate manifest key"); result[key] = value
    return result


def scan(stream: BinaryIO, limits: Limits) -> list[tuple[str, bool, int, int]]:
    members = []; total = 0; explicit = set()
    while True:
        buf = stream.read(512)
        require(len(buf) == 512, "truncated tar header")
        if buf == b"\0"*512:
            require(stream.read(512) == b"\0"*512, "invalid tar terminator")
            while chunk := stream.read(65536):
                require(not chunk.strip(b"\0"), "trailing tar content")
            break
        require(buf[257:263] == b"ustar\0", "only strict USTAR supported")
        for field in (buf[:100], buf[157:257], buf[345:500]):
            if b"\0" in field:
                require(not field.split(b"\0",1)[1].strip(b"\0"), "embedded NUL path metadata")
        info = tarfile.TarInfo.frombuf(buf, "utf-8", "strict")
        require(info.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE), "links, extensions or special files forbidden")
        is_dir = info.type == tarfile.DIRTYPE
        name = info.name[:-1] if is_dir and info.name.endswith("/") else info.name
        relative_path(name)
        require(len(name.encode()) <= limits.path_bytes, "archive path bound")
        require(name not in explicit, "duplicate archive member"); explicit.add(name)
        require(not info.linkname, "archive link metadata forbidden")
        require(not info.mode & 0o7022, "unsafe archive permissions")
        require(is_dir or not info.mode & 0o111, "executable payload permissions forbidden")
        require(0 <= info.size <= limits.file_bytes and (not is_dir or info.size == 0), "archive member size bound")
        total += info.size; require(total <= limits.payload_bytes, "archive payload bound")
        offset = stream.tell(); members.append((name, is_dir, offset, info.size))
        require(len(members) <= limits.members, "archive member count bound")
        stream.seek(((info.size+511)//512)*512, os.SEEK_CUR)
    unique_paths([r[0] for r in members])
    files = {r[0] for r in members if not r[1]}
    require(not any(str(parent) in files for r in members for parent in PurePosixPath(r[0]).parents if str(parent) != "."), "file is parent of member")
    return members


def open_directory(path: Path) -> int:
    require(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"), "no-follow directory operations unsupported")
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = next_fd
        return fd
    except BaseException:
        os.close(fd); raise


def member_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(next_fd)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "staging directory not private")
            os.close(fd); fd = next_fd
        return fd
    except BaseException:
        os.close(fd); raise


def extract_verified(archive: Path, destination: Path, archive_sha256: str, manifest_sha256: str, limits: Limits = Limits()) -> dict:
    """Fail closed. Destination must be new; failures may retain private partial staging.

    Safety assumes the same UID/root and the destination's trusted parent are not
    hostile. Callers enforce writer quiescence; this is not a cross-host lease.
    """
    from .validation import validate_value
    require(all(type(n) is int and n > 0 for n in vars(limits).values()), "invalid extraction limits")
    raw_fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(raw_fd, "rb") as source, tempfile.TemporaryFile() as snapshot, tempfile.TemporaryFile() as expanded:
        info = os.fstat(source.fileno()); require(stat.S_ISREG(info.st_mode) and info.st_size <= limits.archive_bytes, "archive not bounded regular file")
        require(copy_bounded(source, snapshot, limits.archive_bytes) == archive_sha256, "archive digest mismatch")
        magic = snapshot.read(2); snapshot.seek(0)
        if magic == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=snapshot) as uncompressed:
                copy_bounded(uncompressed, expanded, limits.expanded_bytes)
        else:
            copy_bounded(snapshot, expanded, limits.expanded_bytes)
        members = scan(expanded, limits)
        manifests = [r for r in members if r[0] == MANIFEST and not r[1]]
        require(len(manifests) == 1 and manifests[0][3] <= limits.manifest_bytes, "internal manifest missing or oversized")
        _, _, offset, size = manifests[0]; expanded.seek(offset); manifest_raw = expanded.read(size)
        require(hashlib.sha256(manifest_raw).hexdigest() == manifest_sha256, "internal manifest digest mismatch")
        manifest = json.loads(manifest_raw, object_pairs_hook=object_no_duplicates)
        validate_value("document-migration-package.yaml", manifest)
        expected = {r["path"]:r for r in manifest["files"]}
        actual = {r[0] for r in members if not r[1] and r[0] != MANIFEST}
        require(actual == set(expected), "package manifest coverage mismatch")
        for name, is_dir, offset, size in members:
            if is_dir or name == MANIFEST:
                continue
            require(size == expected[name]["size_bytes"], "package file size mismatch")
            expanded.seek(offset); sha = hashlib.sha256(); remaining = size
            while remaining:
                chunk = expanded.read(min(65536, remaining)); require(bool(chunk), "truncated payload")
                sha.update(chunk); remaining -= len(chunk)
            require(sha.hexdigest() == expected[name]["sha256"], "package per-file hash mismatch")
        # Pin parent descriptors; never resolve an archive-created link or overwrite an existing path.
        require(destination.name not in ("", ".", ".."), "invalid staging destination")
        parent_fd = open_directory(destination.parent)
        root_fd = None
        try:
            parent_info = os.fstat(parent_fd)
            require(parent_info.st_uid == os.getuid() and not parent_info.st_mode & 0o022, "destination parent not trusted")
            os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
            root_fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            for name, is_dir, offset, size in members:
                parts = PurePosixPath(name).parts; fd = member_parent(root_fd, parts if is_dir else parts[:-1])
                try:
                    if is_dir:
                        continue
                    output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    with os.fdopen(output, "wb") as target:
                        expanded.seek(offset); remaining = size
                        while remaining:
                            chunk = expanded.read(min(65536, remaining)); require(bool(chunk), "truncated staged payload")
                            target.write(chunk); remaining -= len(chunk)
                        target.flush(); os.fsync(target.fileno())
                    verify_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                    with os.fdopen(verify_fd, "rb") as verified:
                        check_hash = hashlib.file_digest(verified, "sha256").hexdigest()
                    expected_hash = manifest_sha256 if name == MANIFEST else expected[name]["sha256"]
                    require(check_hash == expected_hash, "restored file hash mismatch")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.fsync(root_fd); os.fsync(parent_fd)
        finally:
            if root_fd is not None:
                os.close(root_fd)
            os.close(parent_fd)
    return {"status":"pass", "archive_sha256":archive_sha256, "manifest_sha256":manifest_sha256,
            "files_verified":len(expected), "destination_business_writes":False,
            "canonical_cutover_performed":False, "source_modified":False}
