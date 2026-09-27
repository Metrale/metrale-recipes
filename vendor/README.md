# Vendored engine interfaces

## `serve-options.v2.json`

Every `met serve` flag, reflected out of the engine's own clap definition by
`met dump-serve-options` (see `crates/server/src/cli/manifest.rs` in the
engine repository), and the engine's `METRALE_*` lever table beside it. The `v2`
in the name is the document's `schema_version`; the engine bumps it when the
shape changes, not when a flag is added or removed.

It is not regenerated here. Each engine dev release attaches the
`serve-options.json` its recipes were checked against, and this file is that
asset from the release named in `engine-pin.toml`, byte for byte, alongside
`recipes/` from the same release (see below). CI refuses any other content.

`dump-serve-options` is a hidden subcommand of the engine binary. A build that
lacks it answers `unrecognized subcommand`, which is the honest failure: there
is no way to produce this file from a build that cannot describe itself, and
hand-editing it would defeat the entire point.

**This is not a public format.** The engine deliberately refuses to derive
`Serialize` on `ServeArgs`, because a cross-repo wire format makes every rename
a compatibility break. A committed snapshot is the opposite of that promise: a
rename shows up here as a reviewable diff, and `flags::coverage` turns it into a
failing test. Nothing at runtime reads this file.

It answers three questions that cannot be recovered from reading a recipe:

- **Which flags exist.** Nine keys in shipping recipes were dropped on the floor
  for the life of this project because nothing could tell you they were real.
- **Which take a value.** `video_allow_ffmpeg: true` and `gdn_fused_norm: true`
  are written identically; the first always emitted a bare flag, the second
  emitted `--gdn-fused-norm true` until the engine made every boolean bare.
  A setting the engine can pin either way (`tool_grammar`,
  `ssm_batched_recurrent`) now takes `auto`, `on` or `off` instead.
- **What each accepts.** `scheduler` offered `fcfs` for four releases;
  the engine takes only `fifo` and `slai`, so every launch that chose it died
  inside the container.

What it does *not* carry is ranges — clap has none — so every `Int`/`Float`
bound in `settings` is this project's own judgement. `every_shipped_recipe_value_satisfies_its_own_bound`
in `tests/golden.rs` is what keeps that judgement honest against the corpus.

## `engine-pin.toml`

`recipes/` and `serve-options.v2.json` are owned by
[Metrale/metrale-inference](https://github.com/Metrale/metrale-inference): its
`recipes/` directory is where recipes are written and checked against the
engine's own flag parser. Every dev release (`bNN`) attaches `recipes.tar.gz`,
`serve-options.json` and `index.json` (the commit, and the sha256 of every
recipe and of both other assets), each with a `.sha256`.

`engine-pin.toml` names one such release and the sha256 of its `index.json`.
`scripts/engine-recipes.py check` downloads that release, verifies every sidecar,
the pinned `index.json` hash, the asset hashes `index.json` lists and each
recipe's hash, then requires `recipes/` (every file, including ones that are not
recipes) and `serve-options.v2.json` to equal the release's assets byte for
byte. It runs as the `engine-mirror` CI job, so a pull request that edits either
by hand fails.

To move to a newer release, dispatch the **Engine recipes** workflow (it also
runs daily), or run it yourself and open a pull request with the result:

```sh
python3 scripts/engine-recipes.py sync --root . --latest     # or --release b8
cargo test --workspace                                       # coverage + corpus
```

A release whose `serve-options.json` changes `schema_version` is refused: the
readers in `flags/coverage` have to be updated by hand first.

Recipes are still compiled in (`include_dir!`), so a new pin reaches users with
the next metralectl release, and nothing is fetched at runtime.
