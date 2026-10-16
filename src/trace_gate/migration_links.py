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
        require(value["mode"] in {"descriptor_plan_only","restore_relative_symlinks"}, "unsupported link policy")
        require(value["restore_supported"]==(value["mode"]=="restore_relative_symlinks"), "restore flag/mode mismatch")
        if value["mode"]=="restore_relative_symlinks":
            a=value.get('approval',{})
            require(a.get('enabled') is True and a.get('approval_ref') and a.get('source_scope_frozen') is True, 'explicit opt-in/freeze approval required')
            require(a.get('policy_sha256')==digest({k:v for k,v in value.items() if k!='approval'}), 'approved link policy digest mismatch')
        else:
            require(len(value['inventory'])<=4096 and len(value['links'])<=1024,'default descriptor count bound')
        if value['mode']=='restore_relative_symlinks':
            require(len(value['inventory'])<=a['max_inventory_entries'] and len(value['links'])<=a['max_links'],'approved link inventory bound')
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
        directories={''}|{p for p,r in inventory.items() if r['kind']=='directory'}
        for path in set(inventory)|set(links):directories.update(str(p) for p in PurePosixPath(path).parents if str(p)!='.')
        graph={d:set() for d in directories}
        for d in directories-{''}:
            parent=str(PurePosixPath(d).parent);graph['' if parent=='.' else parent].add(d)
        for path,final in resolved.items():
            if final['kind']=='directory':
                parent=str(PurePosixPath(path).parent);graph['' if parent=='.' else parent].add(final['path'])
        incoming={d:0 for d in graph}
        for children in graph.values():
            for d in children:incoming[d]+=1
        pending=[d for d,n in incoming.items() if not n];count=0
        while pending:
            d=pending.pop();count+=1
            for child in graph[d]:
                incoming[child]-=1
                if not incoming[child]:pending.append(child)
        require(count==len(graph), 'directory link cycle')
        return []
    except ValueError as error:return [str(error)]
