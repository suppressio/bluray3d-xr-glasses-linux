# The part of pyfuse3's API this project uses, with the types it has at runtime.
# pyfuse3 releases type themselves differently (3.3: not at all, 3.4: NewTypes,
# 3.5: plain aliases); pyright reads this file instead, so the checks give the
# same result whatever version the system has. Not used at runtime.

InodeT = int
FileHandleT = int
FileNameT = bytes
FlagT = int
ModeT = int

ROOT_INODE: InodeT
default_options: frozenset[str]

class ReaddirToken: ...

class RequestContext:
    @property
    def uid(self) -> int: ...
    @property
    def pid(self) -> int: ...
    @property
    def gid(self) -> int: ...

class EntryAttributes:
    st_ino: InodeT
    generation: int
    entry_timeout: float
    attr_timeout: float
    st_mode: ModeT
    st_nlink: int
    st_uid: int
    st_gid: int
    st_rdev: int
    st_size: int
    st_blksize: int
    st_blocks: int
    st_atime_ns: int
    st_ctime_ns: int
    st_mtime_ns: int
    def __init__(self) -> None: ...

class FileInfo:
    fh: FileHandleT
    direct_io: bool
    keep_cache: bool
    nonseekable: bool
    def __init__(self, fh: FileHandleT = ..., direct_io: bool = ..., keep_cache: bool = ...,
                 nonseekable: bool = ...) -> None: ...

class StatvfsData:
    f_bsize: int
    f_frsize: int
    f_blocks: int
    f_bfree: int
    f_bavail: int
    f_files: int
    f_ffree: int
    f_favail: int
    f_namemax: int
    def __init__(self) -> None: ...

class FUSEError(Exception):
    def __init__(self, errno: int) -> None: ...
    @property
    def errno(self) -> int: ...

class Operations:
    def __init__(self) -> None: ...
    async def lookup(self, parent_inode: InodeT, name: FileNameT,
                     ctx: RequestContext) -> EntryAttributes: ...
    async def getattr(self, inode: InodeT, ctx: RequestContext) -> EntryAttributes: ...
    async def open(self, inode: InodeT, flags: FlagT, ctx: RequestContext) -> FileInfo: ...
    async def read(self, fh: FileHandleT, off: int, size: int) -> bytes: ...
    async def opendir(self, inode: InodeT, ctx: RequestContext) -> FileHandleT: ...
    async def readdir(self, fh: FileHandleT, start_id: int, token: ReaddirToken) -> None: ...
    async def statfs(self, ctx: RequestContext) -> StatvfsData: ...

def init(ops: Operations, mountpoint: str, options: set[str] = ...) -> None: ...
async def main(min_tasks: int = ..., max_tasks: int = ...) -> None: ...
def terminate() -> None: ...
def close(unmount: bool = ...) -> None: ...
def invalidate_entry_async(inode_p: InodeT, name: FileNameT, deleted: InodeT = ...,
                           ignore_enoent: bool = ...) -> None: ...
def readdir_reply(token: ReaddirToken, name: FileNameT, attr: EntryAttributes,
                  next_id: int) -> bool: ...
