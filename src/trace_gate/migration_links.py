"""Relative-link descriptor verification only. Does not restore filesystem links."""
import hashlib
import unicodedata
from pathlib import PurePosixPath

from .migration import digest, relative_path, require, unique_paths


def target_path(path: str, target: str) -> str:
    relative_path(path)
    require(len(path.encode()) <= 512 and len(target.encode()) <= 1024, "link descriptor path bound")
    require(bool(target) and not target.startswith("/") and "\\" not in target and ":" not in target, "absolute/drive/UNC link target")
    require(target == unicodedata.normalize("NFC", target) and not any(ord(c)<32 or ord(c)==127 for c in target), "unsafe Unicode/control link target")
    parts = path.split("/")[:-1]
    for part in target.split("/"):
        require(bool(part), "empty link target component")
        if part == ".":
            continue
        if part == "..":
            require(bool(parts), "link target escapes root"); parts.pop()
        else:
            parts.append(part)
    value = "/".join(parts);relative_path(value);return value


def descriptor_errors(value):
    try:
        require(value["mode"] == "descriptor_plan_only" and not value["restore_supported"], "filesystem link restoration unsupported")
        require(digest(value["inventory"]) == value["verified_inventory_sha256"], "link inventory digest mismatch")
        inventory = {r["path"]:r for r in value["inventory"]};links={r["path"]:r for r in value["links"]}
        unique_paths([r["path"] for r in value["inventory"]]+[r["path"] for r in value["links"]])
        keys=[(r["artifact_id"],r["revision_id"]) for r in value["links"]]
        require(len(keys)==len(set(keys)), "duplicate link identity")
        for path in set(inventory)|set(links):
            for parent in PurePosixPath(path).parents:
                key=str(parent)
                require(key not in links, "intermediate symlink path unsupported")
                require(key not in inventory or inventory[key]["kind"]=="directory", "non-directory inventory parent")
        resolved={};visiting=set()
        def resolve(path):
            if path in inventory:return inventory[path]
            require(path in links, "dangling link target")
            require(path not in visiting, "link cycle")
            require(len(visiting) < 64, "link chain depth bound")
            if path in resolved:return resolved[path]
            visiting.add(path);row=links[path]
            require(hashlib.sha256(row["target"].encode()).hexdigest()==row["link_text_sha256"], "original link text hash mismatch")
            destination=target_path(path,row["target"])
            require(not any(str(parent) in links for parent in PurePosixPath(destination).parents), "intermediate symlink target unsupported")
            final=resolve(destination)
            require(final["sha256"]==row["target_sha256"] and final["kind"]==row["target_kind"], "link target inventory mismatch")
            visiting.remove(path);resolved[path]=final;return final
        for path in links:resolve(path)
        return []
    except ValueError as error:return [str(error)]
