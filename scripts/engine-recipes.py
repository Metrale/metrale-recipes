#!/usr/bin/env python3
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Keep ``recipes/`` and the flag snapshot equal to one engine release.

The recipes are owned by Metrale/metrale-inference. Each engine dev release
attaches ``recipes.tar.gz`` (its ``recipes/`` directory), ``serve-options.json``
(the ``met serve`` flag surface those recipes were checked against) and
``index.json`` (the commit, every recipe's sha256 and both assets' sha256), each
with a ``.sha256`` sidecar. ``vendor/engine-pin.toml`` names one release and the
sha256 of its ``index.json``; this repository's ``recipes/`` and
``vendor/serve-options.v2.json`` are that release's assets, byte for byte.

    check  Fails unless the tree equals the pinned release. CI runs it on every
           pull request, so a hand edit to recipes/ or the snapshot is refused:
           the only way to change them is to move the pin to a release whose
           assets are the new content.
    sync   Moves the pin to a release (``--release bNN`` or ``--latest``) and
           rewrites recipes/, the snapshot and the pin from its assets. The
           scheduled workflow runs it and opens a pull request.

Both verify the whole chain before trusting a byte: every sidecar against the
file it names, ``index.json`` against the pin (check) or its sidecar (sync), both
assets against the hashes ``index.json`` lists, and every recipe in the tarball
against its ``index.json`` entry. ``--assets-dir`` reads the six files from a
directory instead of downloading them; the tests use it.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PIN_PATH = Path("vendor/engine-pin.toml")
RECIPES_DIR = Path("recipes")
SNAPSHOT_PATH = Path("vendor/serve-options.v2.json")
INDEX = "index.json"
TARBALL = "recipes.tar.gz"
SERVE_OPTIONS = "serve-options.json"
ASSETS = (INDEX, TARBALL, SERVE_OPTIONS)


class Refused(Exception):
    """The release or the tree failed verification; the message says how."""


@dataclass(frozen=True)
class Pin:
    repository: str
    release: str
    commit: str
    index_sha256: str


@dataclass(frozen=True)
class Release:
    commit: str
    index_sha256: str
    recipes: dict[str, bytes]  # path relative to recipes/ -> bytes
    serve_options: bytes


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- pin file


def read_pin(root: Path) -> Pin:
    path = root / PIN_PATH
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise Refused(f"{PIN_PATH} is missing") from e
    fields = ("repository", "release", "commit", "index_sha256")
    missing = [f for f in fields if not isinstance(raw.get(f), str) or not raw[f]]
    if missing:
        raise Refused(f"{PIN_PATH} lacks {', '.join(missing)}")
    extra = sorted(set(raw) - set(fields))
    if extra:
        raise Refused(f"{PIN_PATH} has unknown keys: {', '.join(extra)}")
    return Pin(**{f: raw[f] for f in fields})


def render_pin(pin: Pin) -> str:
    return (
        "# The engine release that recipes/ and vendor/serve-options.v2.json mirror.\n"
        "# Written by `scripts/engine-recipes.py sync`; CI refuses a tree that differs\n"
        "# from this release's assets. Change recipes in the engine repository.\n"
        f'repository = "{pin.repository}"\n'
        f'release = "{pin.release}"\n'
        f'commit = "{pin.commit}"\n'
        f'index_sha256 = "{pin.index_sha256}"\n'
    )


# ---------------------------------------------------------------- I/O


def http_get(url: str, accept: str) -> bytes:
    headers = {"Accept": accept, "User-Agent": "metralectl-engine-recipes"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def latest_release(repository: str) -> str:
    """The newest published release that carries every recipe asset."""
    url = f"https://api.github.com/repos/{repository}/releases?per_page=50"
    releases = json.loads(http_get(url, "application/vnd.github+json"))
    wanted = {n for a in ASSETS for n in (a, a + ".sha256")}
    candidates = [
        r
        for r in releases
        if not r["draft"] and wanted <= {a["name"] for a in r["assets"]}
    ]
    if not candidates:
        raise Refused(f"no release of {repository} carries {', '.join(ASSETS)}")
    return max(candidates, key=lambda r: r["published_at"])["tag_name"]


def load_assets(repository: str, release: str, assets_dir: Path | None) -> dict[str, bytes]:
    out = {}
    for name in (n for a in ASSETS for n in (a, a + ".sha256")):
        if assets_dir is not None:
            out[name] = (assets_dir / name).read_bytes()
        else:
            url = f"https://github.com/{repository}/releases/download/{release}/{name}"
            try:
                out[name] = http_get(url, "application/octet-stream")
            except OSError as e:
                raise Refused(f"cannot download {url}: {e}") from e
    return out


def tree_files(root: Path) -> dict[str, bytes]:
    base = root / RECIPES_DIR
    out = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if p.is_symlink():
            raise Refused(f"recipes/{rel} is a symlink; the release has none")
        if p.is_file():
            out[rel] = p.read_bytes()
    return out


# ---------------------------------------------------------------- verification


def verify_sidecar(assets: dict[str, bytes], name: str) -> str:
    digest = sha256(assets[name])
    fields = assets[name + ".sha256"].decode("ascii", "replace").split()
    if len(fields) != 2 or fields[1].lstrip("*") != name:
        raise Refused(f"{name}.sha256 is not a `<sha256>  {name}` line")
    if fields[0].lower() != digest:
        raise Refused(f"{name} hashes to {digest}, its sidecar says {fields[0]}")
    return digest


def unpack_recipes(tarball: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("recipes",):
                raise Refused(f"{TARBALL} member outside recipes/: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise Refused(f"{TARBALL} member is not a regular file: {member.name}")
            rel = PurePosixPath(*path.parts[1:]).as_posix()
            if rel in out:
                raise Refused(f"{TARBALL} carries {member.name} twice")
            data = tar.extractfile(member)
            assert data is not None
            out[rel] = data.read()
    return out


def verify_release(assets: dict[str, bytes], expect_index_sha256: str | None) -> Release:
    digests = {name: verify_sidecar(assets, name) for name in ASSETS}
    if expect_index_sha256 is not None and digests[INDEX] != expect_index_sha256:
        raise Refused(
            f"{INDEX} hashes to {digests[INDEX]}, the pin says {expect_index_sha256}"
        )
    index = json.loads(assets[INDEX])
    for name in (TARBALL, SERVE_OPTIONS):
        listed = index.get("assets", {}).get(name)
        if listed != digests[name]:
            raise Refused(f"{name} hashes to {digests[name]}, {INDEX} lists {listed}")
    recipes = unpack_recipes(assets[TARBALL])
    listed = {}
    for entry in index.get("recipes", []):
        rel = PurePosixPath(entry["path"])
        if rel.parts[:1] != ("recipes",):
            raise Refused(f"{INDEX} lists a recipe outside recipes/: {entry['path']}")
        listed[PurePosixPath(*rel.parts[1:]).as_posix()] = entry["sha256"]
    shipped = {k for k in recipes if k.endswith(".yaml")}
    if shipped != set(listed):
        raise Refused(
            f"{TARBALL} and {INDEX} disagree on the recipe set: "
            f"only in tarball {sorted(shipped - set(listed))}, "
            f"only in index {sorted(set(listed) - shipped)}"
        )
    for rel, want in listed.items():
        if sha256(recipes[rel]) != want:
            raise Refused(f"recipes/{rel} does not hash to its {INDEX} entry")
    commit = index.get("commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise Refused(f"{INDEX} names no full commit sha")
    return Release(commit, digests[INDEX], recipes, assets[SERVE_OPTIONS])


def snapshot_schema(data: bytes, what: str) -> int:
    schema = json.loads(data).get("schema_version")
    if not isinstance(schema, int):
        raise Refused(f"{what} declares no schema_version")
    return schema


def differences(release: Release, tree: dict[str, bytes], snapshot: bytes | None) -> list[str]:
    out = []
    for rel in sorted(set(release.recipes) | set(tree)):
        if rel not in tree:
            out.append(f"recipes/{rel}: in the release, missing here")
        elif rel not in release.recipes:
            out.append(f"recipes/{rel}: here, not in the release")
        elif tree[rel] != release.recipes[rel]:
            out.append(f"recipes/{rel}: differs from the release")
    if snapshot is None:
        out.append(f"{SNAPSHOT_PATH}: missing here")
    elif snapshot != release.serve_options:
        out.append(f"{SNAPSHOT_PATH}: differs from the release's {SERVE_OPTIONS}")
    return out


# ---------------------------------------------------------------- commands


def cmd_check(root: Path, assets_dir: Path | None) -> None:
    pin = read_pin(root)
    release = verify_release(
        load_assets(pin.repository, pin.release, assets_dir), pin.index_sha256
    )
    if release.commit != pin.commit:
        raise Refused(f"{INDEX} names commit {release.commit}, the pin says {pin.commit}")
    snapshot_file = root / SNAPSHOT_PATH
    snapshot = snapshot_file.read_bytes() if snapshot_file.exists() else None
    diffs = differences(release, tree_files(root), snapshot)
    if diffs:
        raise Refused(
            f"the tree does not match {pin.repository} {pin.release}:\n  "
            + "\n  ".join(diffs)
            + "\nrecipes/ and the flag snapshot are mirrored from the engine release"
            " named in vendor/engine-pin.toml. Change recipes in the engine"
            " repository, then run `scripts/engine-recipes.py sync`."
        )
    print(
        f"recipes/ ({len(release.recipes)} files) and {SNAPSHOT_PATH} equal "
        f"{pin.repository} {pin.release} ({pin.commit[:12]})"
    )


def cmd_sync(root: Path, release_tag: str | None, assets_dir: Path | None) -> None:
    current = read_pin(root)
    tag = release_tag or latest_release(current.repository)
    release = verify_release(load_assets(current.repository, tag, assets_dir), None)
    want = snapshot_schema((root / SNAPSHOT_PATH).read_bytes(), str(SNAPSHOT_PATH))
    got = snapshot_schema(release.serve_options, f"{tag}'s {SERVE_OPTIONS}")
    if got != want:
        raise Refused(
            f"{tag}'s {SERVE_OPTIONS} is schema {got}; this repository reads schema "
            f"{want}. Update the readers (flags/coverage) by hand first."
        )
    base = root / RECIPES_DIR
    shutil.rmtree(base)
    for rel, data in sorted(release.recipes.items()):
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (root / SNAPSHOT_PATH).write_bytes(release.serve_options)
    pin = Pin(current.repository, tag, release.commit, release.index_sha256)
    (root / PIN_PATH).write_text(render_pin(pin), encoding="utf-8")
    print(f"pinned {pin.repository} {tag} ({release.commit[:12]})")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "sync"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True, help="repository root")
        p.add_argument("--assets-dir", type=Path, help="read assets from here")
        if name == "sync":
            which = p.add_mutually_exclusive_group(required=True)
            which.add_argument("--release", help="release tag to pin, e.g. b7")
            which.add_argument("--latest", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            cmd_check(args.root, args.assets_dir)
        else:
            cmd_sync(args.root, args.release, args.assets_dir)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
