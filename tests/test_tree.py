"""tree.py: the generic folder/file tree Library builds and every front end
(FUSE for Samba, HTTP/DLNA) serves from."""
import pytest

from tree import ROOT_ID, Folder, Tree


def test_folder_created_on_first_use_and_reused() -> None:
    tree = Tree()
    a = tree.folder("Blu-ray")
    b = tree.folder("Blu-ray")
    assert a is b and a.parent == ROOT_ID
    nested = tree.folder("Light", a.inode)
    assert nested.parent == a.inode


def test_folder_name_clash_with_a_file_raises() -> None:
    tree = Tree()

    class Dummy:
        inode, parent, name = tree.new_id(), ROOT_ID, "x"

    tree.add(Dummy())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        tree.folder("x")


def test_add_remove_and_children_of() -> None:
    tree = Tree()
    folder = tree.folder("Blu-ray")
    leaf = Folder(tree.new_id(), "Light", folder.inode)
    tree.add(leaf)
    assert tree.children_of(folder.inode) == [leaf]
    assert tree.files() == []                              # folders are not files
    tree.remove(leaf)
    assert tree.children_of(folder.inode) == []
    assert leaf.inode not in tree.nodes


def test_on_remove_hooks_run_after_removal() -> None:
    tree = Tree()
    folder = tree.folder("Blu-ray")
    seen: list[str] = []
    tree.on_remove.append(lambda e: seen.append(e.name))
    tree.remove(folder)
    assert seen == ["Blu-ray"]
    assert folder.inode not in tree.nodes


def test_path_of() -> None:
    tree = Tree()
    assert tree.path_of(ROOT_ID) == ""
    a = tree.folder("Blu-ray 3D")
    b = tree.folder("Light", a.inode)
    assert tree.path_of(a.inode) == "Blu-ray 3D"
    assert tree.path_of(b.inode) == "Blu-ray 3D/Light"


def test_lookup() -> None:
    tree = Tree()
    a = tree.folder("Blu-ray 3D")
    tree.folder("Light", a.inode)
    assert tree.lookup("") is tree.root
    assert tree.lookup("Blu-ray 3D") is a
    assert tree.lookup("Blu-ray 3D/Light") is not None
    assert tree.lookup("Blu-ray 3D/Light/") is tree.lookup("Blu-ray 3D/Light")
    assert tree.lookup("nope") is None
    assert tree.lookup("Blu-ray 3D/nope") is None


def test_lookup_through_a_file_fails() -> None:
    tree = Tree()
    a = tree.folder("Blu-ray 3D")
    file_id = tree.new_id()

    class Dummy:
        inode, parent, name = file_id, a.inode, "movie.ts"

    tree.add(Dummy())  # type: ignore[arg-type]
    assert tree.lookup("Blu-ray 3D/movie.ts") is not None
    assert tree.lookup("Blu-ray 3D/movie.ts/x") is None
