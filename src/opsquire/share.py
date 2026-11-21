"""Several operators (OS users in one group) working in one home. `opsquire share <group>` gives that group the
owner's access to the home and everything in it (read and write; read-only files such as blobs stay read-only) and
sets setgid on every directory, so whatever anyone creates there later belongs to the group too. A home whose root is
group-writable with setgid is shared (`model.shared_home`), and opsquire then creates its files group-writable.
Nothing outside the home changes: a file hard-linked from outside is left alone and named, the group and its members
are the system's, made with its own tools, and the sticky bit is never set, since it would keep operators from
renaming each other's files and folders."""

from __future__ import annotations

import contextlib
import grp
import os
import stat
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .model import OpsquireError, main_home, shared_home

SHOWN = 5


@dataclass
class Shared:
    changed: int = 0
    right: int = 0
    not_mine: list[Path] = field(default_factory=list)
    linked: list[Path] = field(default_factory=list)


def group_id(name: str) -> int:
    """The gid of a group this process is in; refuses any other group."""
    try:
        gid = grp.getgrnam(name).gr_gid
    except KeyError:
        raise OpsquireError(f"there is no group {name!r}; create it with the system's tools first") from None
    if gid != os.getegid() and gid not in os.getgroups():
        raise OpsquireError(
            f"you are not in group {name!r}: add yourself with the system's tools and log in again (`id -nG` lists "
            "the groups this shell has)"
        )
    return gid


def group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def wanted(st: os.stat_result) -> int:
    """The mode a path has in a shared home: a directory gives the group everything and has setgid, a file gives the
    group what it gives its owner."""
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        return mode | 0o070 | stat.S_ISGID
    return mode | ((mode & 0o700) >> 3)


def paths(home: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """The home and everything in it, with their stats; symlinks are left out and never followed."""
    for path in (home, *(Path(root) / n for root, dirs, files in os.walk(home) for n in [*dirs, *files])):
        with contextlib.suppress(FileNotFoundError):
            st = path.lstat()
            if not stat.S_ISLNK(st.st_mode):
                yield path, st


def linked_from_outside(found: list[tuple[Path, os.stat_result]]) -> set[tuple[int, int]]:
    """Files with more hard links than the home holds: another name outside the home shares the inode, and a change
    of group or mode here would change that file too. Links inside the home (snapshots sharing a blob) are fine."""
    names = Counter((st.st_dev, st.st_ino) for _, st in found if stat.S_ISREG(st.st_mode))
    return {
        (st.st_dev, st.st_ino)
        for _, st in found
        if stat.S_ISREG(st.st_mode) and st.st_nlink > names[st.st_dev, st.st_ino]
    }


def drift(home: Path, gid: int) -> list[Path]:
    """What in the home is not the group's, or gives the group less than `share` does."""
    return [p for p, st in paths(home) if st.st_gid != gid or stat.S_IMODE(st.st_mode) != wanted(st)]


def share(home: Path, group: str, dry: bool) -> Shared:
    """Give the home to `group`; paths other operators own are left for them and listed."""
    gid = group_id(group)
    out = Shared()
    found = list(paths(home))
    outside = linked_from_outside(found)
    for path, st in found:
        if st.st_gid == gid and stat.S_IMODE(st.st_mode) == wanted(st):
            out.right += 1
        elif (st.st_dev, st.st_ino) in outside:
            out.linked.append(path)
        elif st.st_uid != os.geteuid():
            out.not_mine.append(path)
        else:
            if not dry:
                with contextlib.suppress(FileNotFoundError):
                    os.chown(path, -1, gid, follow_symlinks=False)
                    os.chmod(path, wanted(st))
            out.changed += 1
    return out


def adopt(folders: list[Path]) -> None:
    """In a shared home, give the group what `share` gives it on the caller's own paths in these folders: an agent's
    or an editor's files keep their program's umask otherwise. Other operators' paths, and files with a second hard
    link (which may be outside the home), are left as they are."""
    home = main_home()
    if not shared_home(home):
        return
    gid = home.stat().st_gid
    for folder in folders:
        for path, st in paths(folder):
            if st.st_uid != os.geteuid() or (st.st_gid == gid and stat.S_IMODE(st.st_mode) == wanted(st)):
                continue
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                continue
            with contextlib.suppress(OSError):
                os.chown(path, -1, gid, follow_symlinks=False)
            with contextlib.suppress(OSError):
                os.chmod(path, wanted(st))


def listed(home: Path, found: list[Path]) -> str:
    return ", ".join(str(p.relative_to(home)) for p in found[:SHOWN]) + (", ..." if len(found) > SHOWN else "")


def check(home: Path) -> tuple[bool, str]:
    """`opsquire doctor`'s line about a shared home."""
    gid = home.stat().st_gid
    name, off = group_name(gid), drift(home, gid)
    if not off:
        return True, f"shared home: group {name}; everything in it is the group's to read and write"
    return False, (
        f"shared home: group {name}; the group cannot read and write {len(off)} of its paths ({listed(home, off)}): "
        f"`opsquire share {name}`, run by their owners"
    )
