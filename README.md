# tools

Declarative configuration manager for the user environment: packages
(brew, bun, npm, uv, go, flatpak), git checkouts, dotfiles, MCP servers,
ollama models, and curl-piped installers — reconciled from a YAML/JSON/TOML
config against a local state file.

## Usage

```console
tools diff   [--config PATH]... [--scope SECTION]...   # dry-run preview
tools deploy [--config PATH]... [--scope SECTION]... [--approve]
```

- `tools diff` shows what deploy would change and **exits 1 when changes
  are pending** (0 when clean) — usable as a check in scripts/CI.
  It never modifies the filesystem.
- `tools deploy` previews, asks for confirmation when there is anything
  to approve, then reconciles. `--approve` skips the prompt.
  A clean preview still deploys: non-destructive maintenance (pulling
  tracked git checkouts) runs on every deploy.
- `--config` accepts a file or a directory and may be repeated; configs
  are deep-merged in order (scalars replace, dicts recurse, lists
  replace). **Relative `files:` sources always resolve against the first
  `--config` path's directory**, so keep file-carrying sections there.
- `--scope` limits the run to named sections (repeatable).
- `plan` and `apply` are hidden aliases for `diff` and `deploy`.

Sections: `bun`, `npm`, `uv`, `go`, `mcp`, `curlShell`, `gitRepos`,
`files`, `brew`, `ollamaModels`, `flatpak`.

## Config

See [examples/config.yaml](examples/config.yaml) for a full annotated
example and [examples/overrides.yaml](examples/overrides.yaml) for
per-host overrides. Formats: `.yaml`, `.yml`, `.json`, `.toml`.

A config *directory* supports two layouts:

- **Host-based**: a `machines/` subdirectory with one
  `machines/<hostname>.yaml` per host. Top-level files (e.g. a
  nix-generated paths JSON) are merged underneath the host config.
- **Flat**: every supported file in the directory, merged in
  lexicographic order.

Any config file may declare `include: [relative/paths...]`; includes are
resolved relative to the including file and merged underneath it.

Notable keys:

- `stateFile` — where ownership state lives
  (default `~/.local/state/tools/state.json`).
- `paths` — binary/install locations the reconcilers use; documented in
  the comment block of `examples/config.yaml`.
- `files:` entries take `{source, target, mode?, secrets?}` or
  `{dir, mode?, secrets?}`. `mode` must be a **quoted octal string**
  (`"0644"`); bare YAML ints are rejected as ambiguous. `secrets: true`
  suppresses inline content diffs for that entry.

## State and ownership

Deploy records what it installs in the state file and only ever removes
what it recorded — anything installed by hand is never touched. Removing
an entry from config removes it from the system on the next deploy
(where an uninstall exists; `go` and `curlShell` only release tracking).
Git checkout removal refuses to delete repos with uncommitted, unpushed,
stashed, or local-ref-only work.

## Security notes

- `curlShell` pipes remote scripts into a shell, and `brew` bootstrap
  runs the official Homebrew installer — both execute remote code you
  declared in config.
- MCP servers with secret headers substitute secrets from `secretPaths`
  files at register time. The `claude mcp add` CLI only accepts headers
  as argv, so the resolved secret is briefly visible in the process list
  while the command runs. Secrets are never logged or persisted to
  state.

## Development

```console
nix develop        # dev shell (see `just` targets inside)
just lint          # ruff check + format check
nix flake check    # build + full unit test suite
just release       # bump patch version, tag, GitHub release
```

## Bootstrap

Remote repo created:

```console
gh repo create ivankovnatsky/tools --public \
  --description "Declarative configuration manager"
```

Cloned locally:

```console
gh repo clone ivankovnatsky/tools
```

Local `main` branch initialized with an empty commit:

```console
git checkout -b main
git commit --allow-empty -S -m "Initial commit"
```
