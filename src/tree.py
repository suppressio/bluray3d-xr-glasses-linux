"""tree.py — the virtual tree of folders and files: what Library (bd3d_fs.py)
builds from the sources, and what every front end serves. Independent of how
it is served: Bd3dFS (src/fuse_fs.py) wraps it for FUSE/Samba, http_server.py
and dlna.py serve it straight from here over HTTP/DLNA. Entries can be added
and removed while serving (discs).

A FileEntry (VirtualFile, decoded on the fly, or SidecarFile, a plain
passthrough for e.g. external subtitles) only needs read(offset, size),
size and the bits every front end shows (name, path, mtime_ns); it knows
nothing about FUSE or HTTP.
"""
import threading
import time
from collections.abc import Callable
from typing import Protocol

# matches pyfuse3.ROOT_INODE: Bd3dFS reuses tree ids as FUSE inodes directly
ROOT_ID = 1


class FileEntry(Protocol):
    inode: int
    parent: int
    name: str
    path: str
    size: int
    mtime_ns: int

    def read(self, offset: int, size: int) -> bytes: ...
    def stop(self) -> None: ...
    def stop_if_idle(self) -> None: ...


class Folder:
    """A directory of the tree (the root has id ROOT_ID)."""

    def __init__(self, inode: int, name: str, parent: int = ROOT_ID) -> None:
        self.inode = inode
        self.name = name
        self.parent = parent
        self.mtime_ns = time.time_ns()


Entry = Folder | FileEntry


class Tree:
    """Folders and files, addressed by id. Entries can be added and removed
    while serving (discs); by_name supports both lookup by one name under a
    parent and walking a whole path (lookup())."""

    def __init__(self) -> None:
        self.root = Folder(ROOT_ID, "")
        self.nodes: dict[int, Entry] = {self.root.inode: self.root}
        self.children: dict[int, list[Entry]] = {}
        self.by_name: dict[tuple[int, str], Entry] = {}
        self.lock = threading.Lock()
        self._next_id = ROOT_ID + 1
        # bumped on every add/remove: DLNA's SystemUpdateID, so a renderer that
        # cached a listing sees that a disc went in or out
        self.revision = 0
        # front ends that need to react to a removal (Bd3dFS: invalidate the
        # kernel's cache of the name) subscribe here; Library itself only
        # changes the tree, it does not know who else is serving it
        self.on_remove: list[Callable[[Entry], None]] = []

    def new_id(self) -> int:
        with self.lock:
            self._next_id += 1
            return self._next_id - 1

    def add(self, e: Entry) -> None:
        with self.lock:
            self.nodes[e.inode] = e
            self.children.setdefault(e.parent, []).append(e)
            self.by_name[(e.parent, e.name)] = e
            self.revision += 1

    def remove(self, e: Entry) -> None:
        with self.lock:
            self.nodes.pop(e.inode, None)
            self.children.get(e.parent, []).remove(e)
            self.by_name.pop((e.parent, e.name), None)
            self.revision += 1
        for callback in self.on_remove:
            callback(e)

    def folder(self, name: str, parent: int = ROOT_ID) -> Folder:
        """Sub-folder, created on first use."""
        e = self.by_name.get((parent, name))
        if e is None:
            e = Folder(self.new_id(), name, parent)
            self.add(e)
        if not isinstance(e, Folder):
            raise TypeError(f"{name} is a file, not a folder")
        return e

    def files(self) -> list[FileEntry]:
        with self.lock:
            return [e for e in self.nodes.values() if not isinstance(e, Folder)]

    def children_of(self, inode: int) -> list[Entry]:
        with self.lock:
            return list(self.children.get(inode, []))

    def path_of(self, inode: int) -> str:
        """The slash-separated path of a node, root excluded, e.g. "Blu-ray/x.ts"."""
        parts: list[str] = []
        node = self.nodes.get(inode)
        while node is not None and node.inode != ROOT_ID:
            parts.append(node.name)
            node = self.nodes.get(node.parent)
        return "/".join(reversed(parts))

    def lookup(self, path: str) -> Entry | None:
        """The entry at a slash-separated path from the root, or None. "" is the
        root itself."""
        node: Entry = self.root
        for part in path.strip("/").split("/") if path.strip("/") else []:
            if not isinstance(node, Folder):
                return None
            found = self.by_name.get((node.inode, part))
            if found is None:
                return None
            node = found
        return node
