#!/usr/bin/env python3
# SPDX-License-Identifier: MIT OR Apache-2.0
"""scripts/engine-recipes.py refuses every way the mirror can be wrong.

Each case builds a release and a tree that agree, proves `check` accepts them,
then breaks exactly one thing and proves `check` refuses it. A guard that
accepts the good case and one bad case is not tested; one that refuses the good
case is broken. Offline: releases are written to a directory and read through
`--assets-dir`, the same code path as a download after the bytes arrive.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "engine-recipes.py"
COMMIT = "a" * 40
RECIPES = {
    "README.md": b"# recipes\n",
    "fam/one.yaml": b"recipe_version: \"2\"\nmodel: org/one\n",
    "fam/two.yaml": b"recipe_version: \"2\"\nmodel: org/two\n",
}
SNAPSHOT = b'{\n  "schema_version": 2,\n  "flags": []\n}\n'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tarball(files: dict[str, bytes], extra: list[tarfile.TarInfo] = ()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(f"recipes/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra:
            tar.addfile(info, io.BytesIO(b"x" * info.size))
    return buf.getvalue()


def write_release(d: Path, files=RECIPES, snapshot=SNAPSHOT, commit=COMMIT,
                  tar_extra=(), index_edit=None) -> str:
    d.mkdir(parents=True, exist_ok=True)
    tar = tarball(files, list(tar_extra))
    index = {
        "schema_version": 1,
        "commit": commit,
        "assets": {"recipes.tar.gz": sha(tar), "serve-options.json": sha(snapshot)},
        "recipes": [
            {"id": r[:-5], "path": f"recipes/{r}", "sha256": sha(b)}
            for r, b in files.items() if r.endswith(".yaml")
        ],
    }
    if index_edit:
        index_edit(index)
    idx = json.dumps(index, indent=2).encode()
    for name, data in (("index.json", idx), ("recipes.tar.gz", tar),
                       ("serve-options.json", snapshot)):
        (d / name).write_bytes(data)
        (d / f"{name}.sha256").write_text(f"{sha(data)}  {name}\n")
    return sha(idx)


def write_tree(root: Path, index_sha: str, files=RECIPES, snapshot=SNAPSHOT,
               release="b1", commit=COMMIT) -> None:
    for rel, data in files.items():
        p = root / "recipes" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "vendor").mkdir(parents=True, exist_ok=True)
    (root / "vendor/serve-options.v2.json").write_bytes(snapshot)
    (root / "vendor/engine-pin.toml").write_text(
        f'repository = "o/r"\nrelease = "{release}"\n'
        f'commit = "{commit}"\nindex_sha256 = "{index_sha}"\n'
    )


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, check=False)


class Guard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.rel = self.tmp / "rel"
        self.root = self.tmp / "repo"
        self.index_sha = write_release(self.rel)
        write_tree(self.root, self.index_sha)

    def check(self) -> subprocess.CompletedProcess:
        return run("check", "--root", str(self.root), "--assets-dir", str(self.rel))

    def assert_refused(self, needle: str) -> None:
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(needle, r.stderr)

    def test_matching_tree_passes(self) -> None:
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("equal o/r b1", r.stdout)

    # ---- hand edits to the tree

    def test_edited_recipe_is_refused(self) -> None:
        (self.root / "recipes/fam/one.yaml").write_bytes(b"model: org/other\n")
        self.assert_refused("recipes/fam/one.yaml: differs")

    def test_one_byte_whitespace_edit_is_refused(self) -> None:
        p = self.root / "recipes/fam/two.yaml"
        p.write_bytes(p.read_bytes() + b"\n")
        self.assert_refused("recipes/fam/two.yaml: differs")

    def test_added_recipe_is_refused(self) -> None:
        (self.root / "recipes/fam/three.yaml").write_bytes(b"model: org/three\n")
        self.assert_refused("recipes/fam/three.yaml: here, not in the release")

    def test_deleted_recipe_is_refused(self) -> None:
        (self.root / "recipes/fam/one.yaml").unlink()
        self.assert_refused("recipes/fam/one.yaml: in the release, missing here")

    def test_non_recipe_file_counts_too(self) -> None:
        (self.root / "recipes/README.md").write_bytes(b"# edited\n")
        self.assert_refused("recipes/README.md: differs")

    def test_symlinked_recipe_is_refused(self) -> None:
        p = self.root / "recipes/fam/one.yaml"
        target = self.tmp / "one.yaml"
        target.write_bytes(p.read_bytes())
        p.unlink()
        p.symlink_to(target)
        self.assert_refused("is a symlink")

    def test_edited_snapshot_is_refused(self) -> None:
        (self.root / "vendor/serve-options.v2.json").write_bytes(SNAPSHOT + b" ")
        self.assert_refused("serve-options.v2.json: differs")

    # ---- a pin that does not describe the release

    def test_pin_naming_another_index_is_refused(self) -> None:
        write_tree(self.root, "0" * 64)
        self.assert_refused("the pin says " + "0" * 64)

    def test_pin_naming_another_commit_is_refused(self) -> None:
        write_tree(self.root, self.index_sha, commit="b" * 40)
        self.assert_refused("the pin says " + "b" * 40)

    def test_pin_with_unknown_key_is_refused(self) -> None:
        pin = self.root / "vendor/engine-pin.toml"
        pin.write_text(pin.read_text() + 'skip_check = "yes"\n')
        self.assert_refused("unknown keys: skip_check")

    # ---- a release whose own chain does not hold

    def test_sidecar_mismatch_is_refused(self) -> None:
        (self.rel / "recipes.tar.gz.sha256").write_text(f"{'1' * 64}  recipes.tar.gz\n")
        self.assert_refused("its sidecar says")

    def test_asset_not_listed_by_index_is_refused(self) -> None:
        # Tarball and sidecar agree with each other, but not with index.json.
        write_release(self.rel, index_edit=lambda i: i["assets"].update(
            {"recipes.tar.gz": "2" * 64}))
        write_tree(self.root, sha((self.rel / "index.json").read_bytes()))
        self.assert_refused("index.json lists " + "2" * 64)

    def test_recipe_not_matching_its_index_entry_is_refused(self) -> None:
        def edit(i):
            i["recipes"][0]["sha256"] = "3" * 64
        write_tree(self.root, write_release(self.rel, index_edit=edit))
        self.assert_refused("does not hash to its index.json entry")

    def test_index_missing_a_recipe_is_refused(self) -> None:
        write_tree(self.root, write_release(self.rel, index_edit=lambda i: i["recipes"].pop()))
        self.assert_refused("disagree on the recipe set")

    def test_tar_member_outside_recipes_is_refused(self) -> None:
        evil = tarfile.TarInfo("recipes/../escape.yaml")
        evil.size = 1
        write_tree(self.root, write_release(self.rel, tar_extra=[evil]))
        self.assert_refused("member outside recipes/")

    def test_tar_symlink_is_refused(self) -> None:
        link = tarfile.TarInfo("recipes/fam/link.yaml")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        write_tree(self.root, write_release(self.rel, tar_extra=[link]))
        self.assert_refused("not a regular file")


class Sync(unittest.TestCase):
    def test_sync_rewrites_tree_and_pin_to_a_release_check_accepts(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        old, new, root = tmp / "old", tmp / "new", tmp / "repo"
        write_tree(root, write_release(old))
        files = {"README.md": b"# v2\n", "fam/one.yaml": b"model: org/one-v2\n"}
        snapshot = SNAPSHOT.replace(b"[]", b'[{"key": "port"}]')
        new_sha = write_release(new, files=files, snapshot=snapshot, commit="c" * 40)
        (root / "recipes/fam/stale.yaml").write_bytes(b"hand-added\n")

        r = run("sync", "--root", str(root), "--release", "b2", "--assets-dir", str(new))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((root / "recipes/fam/two.yaml").exists())
        self.assertFalse((root / "recipes/fam/stale.yaml").exists())
        self.assertEqual((root / "recipes/fam/one.yaml").read_bytes(), b"model: org/one-v2\n")
        self.assertEqual((root / "vendor/serve-options.v2.json").read_bytes(), snapshot)
        pin = (root / "vendor/engine-pin.toml").read_text()
        self.assertIn('release = "b2"', pin)
        self.assertIn(f'index_sha256 = "{new_sha}"', pin)
        self.assertIn(f'commit = "{"c" * 40}"', pin)
        r = run("check", "--root", str(root), "--assets-dir", str(new))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sync_refuses_a_snapshot_schema_change_and_writes_nothing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        root, new = tmp / "repo", tmp / "new"
        write_tree(root, write_release(tmp / "old"))
        before = (root / "vendor/engine-pin.toml").read_text()
        write_release(new, snapshot=b'{"schema_version": 3}\n')
        r = run("sync", "--root", str(root), "--release", "b2", "--assets-dir", str(new))
        self.assertEqual(r.returncode, 1)
        self.assertIn("schema 3", r.stderr)
        self.assertEqual((root / "vendor/engine-pin.toml").read_text(), before)
        self.assertTrue((root / "recipes/fam/two.yaml").exists())

    def test_sync_refuses_a_broken_release_and_writes_nothing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        root, new = tmp / "repo", tmp / "new"
        write_tree(root, write_release(tmp / "old"))
        write_release(new)
        (new / "serve-options.json").write_bytes(b"{}")
        r = run("sync", "--root", str(root), "--release", "b2", "--assets-dir", str(new))
        self.assertEqual(r.returncode, 1)
        self.assertIn('release = "b1"', (root / "vendor/engine-pin.toml").read_text())


if __name__ == "__main__":
    unittest.main()
