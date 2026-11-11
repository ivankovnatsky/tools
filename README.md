# tools

Declarative configuration manager for the user environment: packages (brew, bun,
npm, uv, go, cargo, flatpak), git checkouts, dotfiles, MCP servers, ollama models,
curl-piped installers, and self-hosted service settings via their HTTP APIs —
reconciled from a YAML/JSON/TOML config against a local state file.

## Usage

```console
tools diff   [--config PATH]... [--scope SECTION]... [--only SECTION:NAME]...   # dry-run preview
tools deploy [--config PATH]... [--scope SECTION]... [--only SECTION:NAME]... [--approve] [--update]
tools watch  --config PATH... [--scope SECTION]... [--only SECTION:NAME]... [--approve]   # see Watch
```

- `tools diff` shows what deploy would change and **exits 1 when changes are
  pending** (0 when clean) — usable as a check in scripts/CI. It never modifies
  the filesystem.
- `tools deploy` previews, asks for confirmation when there is anything to
  approve, then reconciles. `--approve` skips the prompt. A clean preview still
  deploys (state housekeeping, `--update` upgrades).
- A plain deploy never upgrades anything already installed: it installs what is
  missing, removes what was dropped, reinstalls what changed in config (a new
  version, source, commit or ref) and converges config files. `--update`
  additionally upgrades what is already there, combined with `--scope` like any
  other run (`--scope gitRepos --update` only pulls checkouts). `tools diff`
  never shows these upgrades, and `watch` never runs them. With `--update`:
  - `gitRepos`: `git pull --ff-only` in every tracked checkout.
  - `skills`: git sources are fetched and moved to the newest commit of their
    ref.
  - `npm`, `bun`, `go`, `cargo`: packages without a version or commit pin are reinstalled
    at the latest release; `uv` runs `uv tool upgrade` on them.
  - `brew`: `brew upgrade` on tracked formulae and casks, `mas upgrade` on
    tracked App Store apps.
  - `ollamaModels`: tracked models are pulled again; `flatpak`: tracked apps get
    `flatpak update`.
  - `fetch`: tracked files without a `sha256` pin are downloaded again (and
    their `install` script re-run).
  - `files`: every distinct `onUpdate` command runs once (see below).
  - `pi`, `mcp` and `curlShell` have no upgrade step.
- `--config` accepts a file or a directory and may be repeated; configs are
  deep-merged in order (scalars replace, dicts recurse, lists replace).
  **Relative `files:` sources always resolve against the first `--config` path's
  directory**, so keep file-carrying sections there.
- `--scope` limits the run to named sections (repeatable).
- `--only SECTION:NAME` limits a section to single items (repeatable; see
  below).
- `plan` and `apply` are hidden aliases for `diff` and `deploy`.

Sections: `bun`, `npm`, `uv`, `go`, `cargo`, `pi`, `mcp`, `curlShell`, `gitRepos`,
`sops`, `templates`, `files`, `fetch`, `env`, `skills`, `links`, `brew`,
`ollamaModels`, `flatpak`, `launchd`, `defaults`, `dock`, `mgmt`.

### Single items (`--only`)

`--only SECTION:NAME` runs one item of a section instead of all of it, e.g.
`tools deploy --only brew:jq --only launchd:caddy`. It works with `diff`,
`deploy` (with `--update` too, under `sudo` in both phases) and `watch`.

| Section | NAME |
| --- | --- |
| `brew` | a formula, cask, tap or `masApps` name |
| `uv`, `npm` | a package name |
| `files` | a target path (`~` allowed), or the `dir:` of a tree entry for the whole tree |
| `templates` | a template name |
| `launchd` | an agent or daemon name, or its label |
| `defaults` | a domain, in every scope that declares it |

- Only the selected items run; with `--only` alone the run covers just the
  named sections. `--scope X` and `--only X:...` for the same section is an
  error, as is an unknown section, a section not listed above, or a name that
  matches nothing in config.
- A narrowed run never removes anything. Every other item of the section is
  left alone: not removed, restored, moved aside, restarted or forgotten, and
  its state record is kept as it is. `brew` `onActivation.cleanup`,
  moves between `files`/`sops`/`templates`/`fetch`, a `defaults` key dropped
  from a selected domain, and pending `onChange` hooks of other entries wait
  for a full run. `npm` `configFile` is not written.
- `tools diff` with the same `--only` shows exactly what deploy would do.
- A `uv` tool dir or `npm` prefix that changed since the last deploy is an
  error: run that section whole first.
- Any config error in a narrowed section refuses the run, even in an entry
  that is not selected.
- A selected `masApps` app whose App Store id changed is an error: run `brew`
  whole.

`tools status [--config PATH]...` prints the live state of managed launchd jobs
(loaded, pid, last exit code). It is read-only.

### Running with sudo (`sudo tools deploy`)

`sudo tools deploy` runs the whole config, like `darwin-rebuild`; tools itself
never calls sudo:

```console
sudo "$(command -v tools)" deploy --config PATH   # sudo's secure_path may hide ~/.local/bin
```

- tools refuses to run as root unless sudo started it for a normal user:
  `SUDO_USER` must name a non-root user (and match `SUDO_UID` when set).
  Every subcommand checks this before doing anything.
- First, as root: `files` entries the user cannot write, `launchd` daemons,
  `defaults` `system:` keys and `settingsctl` host settings, with root's own state in
  `/var/db/tools/state.json`. Root never reads or writes the
  user's state file, the `mgmt` key, or anything under the user's home: a
  daemon whose plist, log dir, log files or `workingDirectory` falls under it
  is an error (other daemons still deploy).
- Then everything else (all other sections, other `files` entries, launchd
  agents, `user` and `currentHost` defaults) runs in a forked child that has permanently dropped
  to `SUDO_USER` (`initgroups`, `setgid`, `setuid`; it aborts if it can get
  uid 0 back). That child is a plain `tools deploy --approve` as the user: same
  config resolution, the user's state file, `HOME`/`USER`/`LOGNAME`/`SHELL`
  from the user's account, no `TMPDIR` or `SUDO_*` variables.
- `PATH` in that child is the one sudo passed, with these added in front when
  they exist and are missing: `~/.local/bin`, `/opt/homebrew/bin`,
  `/opt/homebrew/sbin`, `/usr/local/bin`; `/usr/bin:/bin:/usr/sbin:/sbin` go
  last when missing. Shell rc files are never read.
- Both diffs are shown first (root's, then the user's from a short-lived
  dropped child) and one prompt covers both. A failed system phase stops the
  run before the user phase; a failure in either exits non-zero.
- `--scope` restricts both phases; a scope without `files`, `launchd`,
  `defaults` or `settingsctl` skips the root phase. `sudo tools diff` shows both diffs the same way.
  `watch` and `reconcile` refuse root.
- Without sudo, `deploy` runs only the user part and prints one note per
  section pointing at `sudo tools deploy --scope ...` for the system part.

### Watch

```console
tools watch --config PATH... [--scope SECTION]... [--approve]
            [--debounce SECONDS] [--poll SECONDS] [--tmux]
```

- Runs once on startup, then again every time a file under the `--config` paths
  changes (same `--config`/`--scope` semantics as `deploy`, but `--config` is
  required: there is no `.` default). Changes are debounced: the run starts
  `--debounce` seconds (default 5) after the last change. Edits made during a
  run trigger a follow-up run.
- **Without `--approve` it only prints the diff**, never prompts and never
  deploys. With `--approve` every run is `tools deploy --approve`: unattended,
  no confirmation, removals included.
- A failed deploy, a config error or a crash is logged and watching continues.
  Ctrl-C stops it.
- Change detection polls each file's mode, mtime, size, inode and symlink target
  (dangling links included) every `--poll` seconds (default 1), following
  symlinked directories as the config loader does. Every poll re-stats the whole
  tree; for a large one, raise `--poll`. Only the `--config` paths are watched:
  `include:` files and `files:` sources outside them are not.
- Inside a watched directory everything is watched except `.git` and tools' own
  `.tools-*` temp files, plus whatever `ignore_patterns` in that directory's
  `.tools-watch.json` lists (same rules as nix-config's `.rebuild.json`): a
  plain name excludes a file or directory of that name at any depth; a pattern
  with `*` is a glob matched against the relative path at any depth. The file
  is re-read when it changes; a missing one means no patterns, an invalid one
  is warned about and treated as empty. Example:

  ```json
  { "ignore_patterns": ["README.md", ".direnv", "*.sync-conflict-*", "*.swp"] }
  ```
- The state file and `mgmt.key` next to it (and their `.lock` and `.tmp`
  siblings) are never watched. Other deploy targets inside a watched directory make each
  deploy trigger the next; keep them outside.
- Log lines are prefixed `[tools watch]`. `--tmux` runs it in tmux window
  `tools-watch` of session `<hostname>` (reused if already running) and switches
  to it, so it never shares a window with nix-config's `rebuild watch`.

## Config

See [examples/config.yaml](examples/config.yaml) for a full annotated example
and [examples/overrides.yaml](examples/overrides.yaml) for per-host overrides.
Formats: `.yaml`, `.yml`, `.json`, `.toml`.

A config _directory_ needs a `machines/` subdirectory with one
`machines/<hostname>.yaml` per host. Only that file and its `include:` files are
read; nothing else in the directory is.

Any config file may declare `include: [relative/paths...]`; includes are
resolved relative to the including file and merged underneath it.

Each file may only use known top-level keys (every section, plus `stateFile`,
`paths`, `vars`, `include`); anything else fails the load, naming the file.

Notable keys:

- `stateFile` — where ownership state lives (default
  `~/.local/state/tools/state.json`).
- `paths` — binary/install locations the reconcilers use; documented in the
  comment block of `examples/config.yaml`.
- `files:` entries take `{source, target, mode?, secrets?}`,
  `{dir, mode?, secrets?}` or `{target, directory: true, mode?}`, each with
  optional `onChange` and `onUpdate`. `mode` must be a **quoted octal string**
  (`"0644"`); bare YAML ints are rejected as ambiguous. `secrets: true`
  suppresses inline content diffs for that entry. Removals wait while any entry
  is malformed; a target refused by another owner stays tracked and does not
  hold others.
- An existing target that differs is replaced; one tools did not write, or
  that changed since, is moved aside first (see Ownership).
- Whether an absolute `target` (after `~` expansion) is a user or a system
  file follows Unix permissions for the invoking user (`SUDO_USER` under
  sudo): walk down from `/` towards the target's directory (following
  symlinks, not the target itself), stopping at a missing path or a directory
  the user cannot search. An existing `directory: true` target reached this
  way is the user's when they own it; anything else is the user's when they
  can write the directory where the walk stopped. Otherwise it is a root-owned
  system file, deployed only
  by `sudo tools deploy` and tracked in root's state. It takes only `source`,
  `target`, `mode` and `secrets`; a target in a directory a non-root user can
  change is refused. A target not owned by root is replaced even when its
  content matches. As the user, deploy and diff leave it alone and print one
  note listing the system files that differ (and any it cannot read to
  check). The usual move-aside rule applies
  (an `/etc/static` symlink is moved aside like a file). A `system:` key is an
  error: the target alone decides.

  ```yaml
  files:
    - source: etc/shells
      target: /etc/shells
      mode: "0644"
  ```

- `skip: true` (on a `source`/`target` or `directory` entry, or a template; not
  on `dir:` trees) keeps the entry declared but
  writes nothing: the target is neither written nor removed, its state record
  stays as is, and its hooks do not run. Diff and deploy list it as
  `= skip <target>`, which alone does not prompt. Use it for files an app
  rewrites (recent-project lists and the like), and flip it off for one deploy
  to push repo changes; the target is then treated as before (moved aside once
  if it changed since tools wrote it).
- `directory: true` ensures the directory (and its parents) exists and applies
  `mode`. Only a directory tools created is tracked, and it is removed only
  while empty; parents and pre-existing directories are never removed. Other
  managed paths may live inside it. Inside `secretsDir` only mode `0751` (or
  none) is accepted.
- `onChange: "<shell command>"` (on `files:` entries and templates) runs via
  `/bin/sh -c` after the deploy creates, updates or chmods that entry, once per
  distinct command per section (a `dir:` walk runs it once however many files
  changed; templates run theirs after every phase). Unchanged deploys,
  removals and `tools diff` never run it; diff lists it as `run onChange`. A
  failing command fails the deploy, the written file stays, and the command is
  retried on each deploy until it succeeds or is no longer declared.
- `onUpdate: "<shell command>"` (on `files:` entries) runs only on
  `deploy --update` with `files` in scope, after the section converges, whether
  or not the entry changed: once per distinct command, however many entries or
  `dir:` files declare it. It is skipped when any entry declaring it was refused
  or failed to write, or when an `onChange` in the section failed. Plain deploy, `tools diff` and `watch` never run it,
  and diff does not list it. A failing command fails the deploy; it is not kept
  pending and runs again on the next `--update`. Use it to drive an app's own
  plugin manager, with `onChange` installing what a new config declares.

  ```yaml
  files:
    - source: amethyst/amethyst.yml
      target: ~/.config/amethyst/amethyst.yml
      onChange: pkill -x Amethyst; open -a Amethyst
    - source: dotfiles/.config/nvim/lua/config/pack.lua
      target: ~/.config/nvim/lua/config/pack.lua
      onChange: nvim --headless +qa
      onUpdate:
        nvim --headless "+lua vim.pack.update(nil, { force = true })" +qa
    - target: ~/Screenshots
      directory: true
      mode: "0755"
  ```

- `vars` — placeholder values for every other section; see below.

## Vars and placeholders

Resolved once, after all files and `--config` paths merge (per-host `vars`
win). String leaves only; mapping keys are never substituted.

```yaml
vars:
  dataDir: /Volumes/Data
  forgejoDir: ${dataDir}/forgejo # vars may reference vars
npm:
  packages:
    x: { postInstall: "echo $${HOME}" } # $${ is a literal ${
```

| Placeholder        | Resolves to                                       |
| ------------------ | ------------------------------------------------- |
| `${name}`          | `vars.name`; otherwise left as written            |
| `${env:NAME}`      | environment variable (unset is an error)          |
| `${sopsPath:NAME}` | path of the decrypted `sops.secrets.NAME` file    |
| `${sops:NAME}`     | the secret's value, only inside `templates` content |
| `$${`              | a literal `${`                                    |

- `name` is `[A-Za-z_][A-Za-z0-9_]*`; a string that is exactly `${name}` keeps the var's type.
- Anything else (`${API_KEY}`, `${1}`, `${FOO:-x}`, ...) passes through for the shell, claude, etc.
- The environment is read only via `${env:}`; its values are plain config, never secret.
- `links:`/`skills:` paths also expand `$VAR` from the environment; `sops`/`templates` paths expand `~` only.
- Unknown `${sops:}`/`${sopsPath:}` names are errors; an invalid `sops:` section is reported as such.

## Sops

One file per secret, decrypted with `sops` from `PATH` (or `binary:`).
Options follow sops-nix.

```yaml
sops:
  age:
    keyFile: ~/.config/sops/age/keys.txt
    sshKeyPaths: [~/.ssh/id_ed25519] # age1 recipients via in-process conversion; ssh-ed25519 only for the first readable key
  secretsDir: ~/.local/state/tools/secrets # default; must be absolute after ~ expansion
  defaultSopsFile: secrets/default.yaml # relative to the first --config path
  defaultSopsFormat: yaml # default
  secrets:
    externalDomain: {} # key defaults to the name
    aws/region: {} # nested key aws -> region; file <secretsDir>/aws/region
    dns-ip-1:
      key: nextDNS/IPs/0 # numeric segments index lists
      sopsFile: secrets/dns.yaml
      path: ~/.config/stubby/ip1
      mode: "0440" # default "0400"
      group: staff
      restartUnits: [launchd:org.example.stubby]
    ssh-key: { sopsFile: secrets/id.bin, format: binary } # whole file (or key: "")
    forgejo-jwt:
      generate: { length: 64, charset: alnum } # alnum | alpha | hex | urlsafe, or chars
```

- Each sops file is decrypted once per run, in memory; `tools diff` decrypts too and shows only path + action.
- Writes are atomic, mode/owner set before content; unchanged files are not rewritten.
- New dirs inside `secretsDir` are `0751` (as sops-nix); parents outside it use the normal umask.
- One failing secret doesn't stop the rest; its templates are blocked; the run exits non-zero.
- `generate` creates a value once and never overwrites; an existing untracked file is adopted as the value.
- Values must be strings (as sops-nix); quote numbers in the sops file. Numeric key segments index lists (`nextDNS/IPs/0`), as nix-config uses.
- An existing file with different content is replaced; one tools did not write, or that changed since, is moved aside first (see Ownership).
- One secret's path may not contain another's (`a` vs `a/b`).
- A whole-file secret (`key: ""`) is decrypted with input and output type set to its `format`, as sops-nix does.
- An `sshKeyPaths` key that can't be read or converted is skipped with a warning, as in sops-nix. Conversion is done in Python with the same result as `ssh-to-age -private-key` (no external binary); only unencrypted OpenSSH ed25519 keys convert. The converted key is never written to disk.
- `restartUnits: [launchd:<label>]` restarts that launchd job when the secret's content is rewritten (create, update or replace; a chmod alone does not). See "restartUnits" under launchd. Other unit kinds only print `notify <unit> (changed: ...)`.

## Templates

Renders a file from YAML data or text. Options follow sops-nix templates.

```yaml
templates:
  app.ini: # path defaults to <secretsDir>/rendered/app.ini
    format: ini # raw (default) | yaml | json | toml | ini | env
    content:
      server: { DOMAIN: "git.${sops:externalDomain}", HTTP_PORT: 3000 }
      security: { INTERNAL_TOKEN_URI: "file:${sopsPath:forgejo-jwt}" }
    restartUnits: [launchd:org.example.forgejo]
  transmission:
    path: ~/.config/transmission/settings.json
    format: json
    content: { rpc-port: 9091, rpc-password: "${sops:transmission-password}" }
  caddyfile:
    path: ~/.config/caddy/Caddyfile
    file: templates/Caddyfile # raw text, relative to the first --config path
```

- `content`: text for `raw`, mapping for `ini`/`env`/`toml`, mapping or list for `yaml`/`json`.
- `env` values are shell single-quoted; `quote: false` writes `KEY=value`.
- `${sops:}` renders from the same decryption as `sops`, so diff and deploy agree.
- Default mode `0400`; `owner`/`group` and new dirs as in sops.
- An existing file with different content is replaced; one tools did not write, or that changed since, is moved aside first (see Ownership).
- With an invalid `sops:` section, templates without `path` are errors; there's no fallback to the default directory.
- Content is masked in diff/deploy output when it uses `${sops:}`, has `secrets: true`, was secret before, or looks like a key.
- `restartUnits` works as in sops: a rewritten file restarts each `launchd:<label>` job.

## Ownership (files, sops, templates)

- A path declared by two of `files`/`sops`/`templates`/`fetch`/`env` is refused by all of them. Moving a path between `files`, `sops`, `templates` and `fetch` moves its state record first (`~ move <path> (files -> templates)`), so the new section takes the file over in the same deploy; a file edited since tools wrote it keeps its old record and stays refused.
- Refused as well: symlinks managed by `links`/`skills`, paths through them or inside their targets, skill copy dirs and the skills cache.
- Symlinked parents and case (on case-insensitive volumes) are normalized before comparing, so aliases match.
- A managed path may not be inside another one (for example, a secret named `rendered`).
- An unmanaged symlink at a target is never followed; it is moved aside like a file.
- Only tracked, unmodified files are removed; any invalid entry holds removals.
- Nothing is removed through a managed link.
- sops/templates state keeps device, inode and mtime, never values or hashes.
- A target whose content differs is replaced. If tools did not write it (untracked) or it changed since tools wrote it, it is first renamed to `<target>.tools`, keeping its mode, and the backup path is logged; a file tools wrote and nobody changed is replaced without a backup. This applies to `files`, `sops`, `templates`, `fetch` and `env`. A symlink at the target is never followed: the link itself is renamed. Backups are never removed by tools.
- A target is moved aside at most once: if it needs replacing again while `<target>.tools` already exists, that entry fails (`!` in diff) and the file is left as is; other entries proceed. Remove the backup to let tools move it aside again.
- The replacement is written to a temp file first, so a failed write or download leaves the old file in place.
- `tools diff` shows such a write as `~ replace <target> (<reason>; old file moved aside to <target>.tools ...)`.
- Content tools did not write, or that changed since, is never shown.
- `force:` was removed; setting it is a config error.
- State entries match declared paths by file identity, so a respelled path (alias, case) is the same entry.
- A tracked entry also records the file's device and inode; another file at that path (for example behind a repointed parent symlink) counts as untracked. A changed device number alone (APFS renumbers at mount) is accepted when the inode and mtime (or the `files:` hash) still match, and is re-recorded.
- `sops`/`templates` paths outside every `gitRepos` destination are written before `gitRepos` (so clones can use them); paths inside one, secrets whose `sopsFile` is inside one, templates using such a secret, then removals, after it. `tools diff` lists them in that order (`sops (after gitRepos)`).
- A path inside a `gitRepos` destination that is still not a checkout at write time (clone failed, section not run, or a plain directory; a `.git` file, as in linked worktrees and submodules, counts as a checkout) is refused, as is one where any enclosing destination is not a checkout.
- Ownership is re-checked after cloning, so a symlink a checkout brings in can't make two sections' paths collide unnoticed.
- `${sops:}` of a generated secret reads its file only while it is the file tools wrote.
- With config errors, `tools diff` lists the removals deploy holds (`~ hold`).

## Differences from nix-config/sops-nix

| Option / behaviour | sops-nix | tools | Why |
| --- | --- | --- | --- |
| Secrets location | `/run/secrets`, `~/.config/sops-nix/secrets` | `secretsDir` (`~/.local/state/tools/secrets`) | runs beside sops-nix during migration |
| `path` | symlink into the secrets dir | regular file written at `path` | no generation dirs to link into |
| `sops.placeholder.NAME` | Nix placeholder string | `${sops:NAME}` | no Nix evaluation |
| Template `content` | text only | text, or YAML data via `format` | services get their native format |
| `restartUnits` | restarts systemd units | `launchd:<label>` restarts that launchd job; other kinds print a notice | launchd instead of systemd |
| `age.sshKeyPaths` | converted in-process | first readable key passed to `sops` (`SOPS_AGE_SSH_PRIVATE_KEY_FILE`, for `ssh-ed25519` recipients); every unencrypted ed25519 key also converted in-process (as `ssh-to-age`) into `SOPS_AGE_KEY` | `sops` only reads one SSH key, and only for `ssh-ed25519` recipients |
| Removal | old generations pruned | only tracked, unmodified files | never delete what tools didn't write |
| Ownership | none | one owner per path; no writes through symlinks | safety |
| Diff | n/a | path + action, content masked | no secrets in logs |
| `generate` | n/a | create-once random secrets | first-run app secrets |
| `owner` default | `root` (system module) | the user running tools | user-level tool |
| Untracked or changed file at `path`/template path | overwritten | moved aside to `<path>.tools`, then written | never lose hand-written files |

## Skills

`skills:` installs [Agent Skills](https://agentskills.io) (directories
containing `SKILL.md`) from git repos or local directories into every agent's
skills directory:

```yaml
skills:
  sources:
    anthropic:
      url: https://github.com/anthropics/skills
      ref: main          # optional branch, tag or commit
      subdir: skills     # optional
    mine:
      path: ./skills     # relative to the first --config path
  targets: [~/.agents/skills, ~/.codex/skills]   # copies
  linkTargets: [~/.claude/skills]                 # symlinks to the first copy
  enable:
    - anthropic/pdf
    - mine/*             # every skill in a source
    - skill: mine/claw-only
      targets: [~/.openclaw-skills]   # only here
      linkTargets: []                 # no links
      as: claw                        # optional install name
```

- Skill IDs are `<source>/<path to the dir holding SKILL.md>`; nested skills
  install flat as `<target>/<last path segment>/`.
- Git sources are cloned into `cacheDir` (default `~/.cache/tools/skills`),
  which tools owns outright. `diff` never touches the network: it previews
  first fetches and url/ref changes. A deploy only fetches an existing clone
  when its url, ref or subdir changed; new upstream commits on an unchanged ref
  need `deploy --update`, like `gitRepos` pulls.
- Ownership follows `files:`. An existing directory tools did not create is
  never overwritten, a copy modified since tools wrote it is neither updated
  nor removed, and removals only run when every source fetched and every
  entry resolved.
- `linkTargets` get `<linkTarget>/<name>` as an absolute symlink to the
  skill's copy in its first target, made only once that copy is in place.
  Links follow `links:` ownership: foreign files and symlinks are skipped, a
  symlink already pointing at the copy is adopted. A path cannot be both a copy
  and a link.
- Moving a dir from `targets` to `linkTargets` replaces each unmodified copy
  there with a link in one deploy; a modified copy is kept and not linked. The
  reverse move replaces the links with copies.
- A path tracked by `links:` is skipped here, and the other way round.

## Links

`links:` manages symlinks:

```yaml
links:
  - source: ~/.agents/skills/jira   # what the link points at
    target: ~/.claude/skills/jira   # the symlink to create
```

- `~` and `$VARS` expand; both paths must be absolute afterwards and each
  target may appear once. Links get absolute targets; parent dirs are created.
- An existing file, directory or foreign symlink is never replaced. A symlink
  already pointing at the source (relative or absolute) is adopted silently.
- A missing source is skipped with a warning, so no dangling link is made.
- A tracked link is repointed or removed only while it still points where
  tools left it, and removals are held while any entry is invalid.

## fetch

`fetch:` downloads files from https URLs, the nix `fetchurl` equivalent (a vim
spell file, a release binary with no brew formula):

```yaml
fetch:
  ~/.config/nvim/spell/uk.utf-8.spl: https://ftp.nluug.nl/vim/runtime/spell/uk.utf-8.spl
  ~/.local/bin/tool:
    url: https://example.com/tool-v1.2
    sha256: 3b0c...   # optional
    mode: "0755"      # quoted octal, default 0644
    restartUnits: [launchd:com.example.tool] # kickstarted when rewritten
  /Applications/Tool.app:
    url: https://example.com/tool-v1.2.zip
    sha256: 9f2a...   # of the download
    install: |        # like nix's installPhase
      ditto -x -k "$src" .
      mv dist/Tool.app "$out"
      codesign --force --deep -s - "$out"
```

- `install` is a shell script, run with `sh -e` once the download is verified
  against `sha256`. `$src` is the downloaded file, `$tmp` an empty scratch
  directory (the working directory) and `$out` a staging path beside the
  target that the script must create, as a file or a directory. On success
  `$out` is moved into place (the old target stays until then). A failing
  script, or one that leaves no `$out`, fails that entry and keeps the old
  target; other entries still run. Its output is shown only when it fails (or
  with `--verbose`).
- Only the inputs are tracked: state records `url`, `sha256`, the script text
  and the target's type (file or directory). The result is never hashed or
  read; plan, diff and deploy only `lstat` the target. The script runs again
  when `url` (unpinned), `sha256` or the script changes, when the target is
  missing, or on `deploy --update` without `sha256`. Edits inside the result
  are left alone. `mode` applies only to a file result, and only when given;
  otherwise the script's mode is kept.
- A target in state is owned. An untracked one is moved aside on first
  install (an install result is never adopted), as is a tracked one whose type
  changed. A dropped entry's target is removed, edited or not, unless its type
  changed.
- `restartUnits: [launchd:<label>]` works as in sops and templates: a fetch,
  update or replace (not a chmod) restarts that job; see "restartUnits" under
  launchd.

- A missing target is downloaded to a temp file beside it and renamed into
  place; parent directories are created. A file tools wrote and nobody changed
  is not downloaded again on a plain deploy; an edited one is moved aside and
  downloaded again. Without `sha256`, a changed `url` or
  `deploy --update` re-downloads it; with `sha256`, only a changed `sha256`
  does (a matching file is kept even if its `url` changes).
- With `sha256`, a download that does not match is not installed, and an
  untracked file that already matches is adopted without downloading.
- https only, redirects included; 60s network timeout. A failed download fails
  the deploy but not the other entries.
- Ownership of plain (non-`install`) entries follows `files:`: an untracked
  existing file, or one modified since tools wrote it, is moved aside and downloaded again (with `sha256`, an
  untracked file that already matches is adopted instead); a modified file is
  not removed, a dropped entry's file is removed, and removals wait while any
  entry is malformed.
- `tools diff` never touches the network or runs `install`: it shows
  `+ fetch <target> <- <url>` for missing files (with ` (install)` for a
  script), `~ fetch` for url/sha256/install changes,
  `~ replace <target> <- <url> (<reason>; old file moved aside to <target>.tools)`
  for untracked, edited or symlinked targets, and removals.

## Env

`env:` declares session environment variables and `PATH` entries, the
home-manager `home.sessionVariables` / `home.sessionPath` equivalent:

```yaml
env:
  vars:
    EDITOR: nvim
    HF_HOME: ${externalStoragePath}/.cache/huggingface
    UV_PYTHON_INSTALL_DIR: ~/.local/share/uv/python
  path:                           # prepended to PATH, first entry first
    - ~/.local/bin
  shells: [sh, fish, nu]          # default: all three
  dir: ~/.local/state/tools/env   # default
  launchd: false                  # macOS: also `launchctl setenv` each var
```

Deploy renders one snippet per shell into `dir`: `env.sh` (bash, zsh, dash),
`env.fish` and `env.nu`. Source it from the rc file:

```sh
# ~/.zshenv, ~/.bashrc, ~/.profile
[ -r ~/.local/state/tools/env/env.sh ] && . ~/.local/state/tools/env/env.sh
```

```fish
# ~/.config/fish/config.fish
test -r ~/.local/state/tools/env/env.fish; and source ~/.local/state/tools/env/env.fish
```

```nu
# config.nu; `source` is resolved at parse time, so the file must exist
source ~/.local/state/tools/env/env.nu
```

- Placeholders (`${var}`, `${env:NAME}`, `${sopsPath:NAME}`) resolve at load
  time as everywhere else. `${sops:NAME}` is a config error, and a value that
  looks like a secret (a PEM key, an `@VAR@` placeholder) is refused: the
  snippets are world-readable (0644) and every process inherits the values.
  Point at a file instead (`GH_TOKEN_FILE: ${sopsPath:gh-token}`).
- Values are written literally, quoted for each shell: `$HOME` stays `$HOME`.
  Only a leading `~` is expanded, at deploy time. Names are
  `[A-Za-z_][A-Za-z0-9_]*`; `PATH` is set through `path:`. Numbers become
  strings; booleans must be quoted. Control characters are rejected.
- `path:` entries must be absolute after `~` expansion and may not contain
  `:`. Sourcing moves each one to the front of `PATH` and drops duplicates, so
  re-sourcing (tmux, nested shells, macOS `path_helper`) never grows `PATH`.
- Deploy exports the same vars and `PATH` into its own process before any
  section runs, so `uv`, `npm` and the rest see them (e.g.
  `UV_PYTHON_INSTALL_DIR`). This happens whenever `env:` is valid, even under
  a `--scope` that excludes `env`; `tools diff` does not export.
- The snippets are tracked in state. Dropping a var rewrites them without it;
  dropping a shell from `shells:`, changing `dir:` or removing the section
  removes the files tools wrote, unless they were edited since (then they are
  released and left in place). Shells already running keep their environment
  until restarted. Any config error holds the whole section. A path another
  section (`files`, `templates`, `sops`) also claims is refused by both. A
  differing snippet tools did not write, or that was edited since, is moved
  aside first, as in Ownership.
- `launchd: true` (macOS) runs `launchctl setenv` for each var so GUI apps
  launched afterwards see it; `PATH` is not set this way. `launchctl` values
  do not survive logout or reboot: the diff compares against
  `launchctl getenv` and the next deploy sets them again. A var dropped from
  config (or `launchd: false`) is `unsetenv`-ed only while it still holds the
  value tools set. On other platforms the flag is a warning.

## Brew

macOS only. Keys (anything else is a config error and the whole section is
skipped):

```yaml
brew:
  taps: [user/homebrew-tap] # `user/homebrew-foo` == `user/foo`
  brews: [jq, user/tap/tool]
  casks: [kitty]
  masApps: { Pages: 409201541 } # name: App Store id, needs `mas`
  environment: { HOMEBREW_NO_ENV_HINTS: "1" }
  caskArgs: { no_quarantine: true } # adds --no-quarantine to cask installs
  trust:
    taps: [user/homebrew-tap] # `brew trust --tap`, Homebrew 6 tap trust
  onActivation:
    autoUpdate: false # false: HOMEBREW_NO_AUTO_UPDATE=1; true: unset it (metadata only)
    cleanup: zap # uninstall | zap; omit for ownership-only removal
```

`autoUpdate` only controls Homebrew's metadata refresh (`brew update`) before a
brew command; it never upgrades installed packages. Without `deploy --update`,
every brew command runs with `HOMEBREW_NO_INSTALL_UPGRADE=1` and
`HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1`, so installing a package does not
upgrade one already installed or its dependents (Homebrew still upgrades an
outdated dependency the new package requires). `environment` can override both.
With `--update`, `brew upgrade` runs on the tracked formulae and casks and
`mas upgrade` on the tracked App Store apps.

Without `cleanup`, brew follows [State and ownership](#state-and-ownership):
deploy never queries live brew and removes only formulae, casks, taps, App Store
apps and trusted taps it recorded installing. Trusted taps it adds are untrusted
when dropped from config.

`onActivation.cleanup` is the explicit opt-in to remove what tools did **not**
install. On every deploy and diff it reads `brew leaves`, `brew tap`,
`brew info --json=v2 --installed` and, for configured packages not installed
yet, `brew deps` plus `brew info --cask` (cask requirements); then it also
removes:

- formulae in `brew leaves` that are neither in `brews` nor recorded in state
  (names, full names, aliases and old names all match; `mas` is kept while
  `masApps` is set or App Store apps are being removed);
- installed casks neither in `casks` nor recorded in state;
- dependency-only formulae nothing remaining needs (listed and removed
  explicitly instead of `brew autoremove`);
- taps neither in `taps`, `trust.taps` nor recorded in state that no remaining
  installed package comes from. `homebrew/core`, `homebrew/cask` and
  `homebrew/bundle` are never untapped (`homebrew/core` not even when dropped
  from `taps`; it is just forgotten from state).

Nothing a remaining formula or cask, or a pending install, depends on (runtime
formula deps, cask `depends_on` formulae and casks) is targeted. Removals run
one package per command with `HOMEBREW_NO_AUTOREMOVE=1`, casks then formulae,
dependents before their dependencies; taps go last. A refused removal fails the
deploy but does not block the rest. Without `cleanup`, owned removals stay one
`brew uninstall` batch followed by `brew autoremove`.

While a configured tap (in `taps`, or of a fully-qualified package) is not
tapped yet, its packages' dependencies can't be resolved, so cleanup is
deferred: diff shows `? cleanup deferred until tapped: ...` and the deploy that
adds the tap removes nothing extra; the next one does. A deferred or failed
cleanup still runs owned removals the way it does without `cleanup`.

`zap` additionally passes `--zap` to every cask uninstall, owned ones included,
deleting the app's preferences, caches and support files. App Store apps are
never removed by cleanup, only by ownership. Each such removal shows in
`tools diff` with `(not installed by tools)`. If brew cannot be queried, only
the cleanup removals are skipped (deploy reports failure) and diff shows
`? cannot plan cleanup`.

### Differences from nix-config

| Option                                                            | nix-darwin `homebrew`          | tools                                  |
| ----------------------------------------------------------------- | ------------------------------ | -------------------------------------- |
| no `cleanup`                                                      | `none`: never removes anything | removes what tools recorded installing |
| `cleanup: none`                                                   | accepted                       | config error; omit the key instead     |
| `autoUpdate` default                                              | `false`                        | brew's own default (auto-update on)    |
| `trust.taps` (nix-homebrew)                                       | never untrusted                | untrusted when dropped                 |
| `onActivation.upgrade`                                            | upgrades outdated packages     | `deploy --update` (tracked only)       |
| per-entry args, `global.*`, `extraConfig`, pinned taps            | supported                      | not supported                          |
## launchd

`agents` (user domain, `gui/<uid>`) and `daemons` (system domain). macOS only.
Agents mirror nix-config's home `local.launchd`, daemons its darwin one; see the differences below.
`tools deploy` manages agents; `sudo tools deploy` manages daemons as root, then
agents as the user (see "Running with sudo").

```yaml
launchd:
  labelPrefix: com.example # label = <labelPrefix>.<name> unless `label:` is set
  agents:
    notes-sync:
      command: notes-sync --watch
  daemons:
    caddy:
      waitForSecrets: true
      waitForPaths: [/Volumes/Storage]
      dataDir: /var/lib/caddy
      command: /opt/homebrew/bin/caddy run --config /var/lib/caddy/Caddyfile
      restartOn: [/var/lib/caddy/Caddyfile]
```

| Option | Default | Notes |
| --- | --- | --- |
| `command` / `programArguments` | — | exactly one; wrapper ends `exec <command>` (as nix) or `exec "$@"` |
| `waitForSecrets` | false | wait until `sops.secretsDir` exists, for agents and daemons alike (tools' sops is user-scope; a daemon's `user` must be able to reach that dir); a config error when the sops section is invalid or declares no secrets |
| `waitForPath` / `waitForPaths` | — | wait until each path exists |
| `dataDir`, `extraDirs` | — | `mkdir -p` at start; `extraDirs` also takes `{path, mode, owner, group}` (root daemons only) |
| `preStart` | — | shell run before the command |
| `keepAlive` | true | bool or `{successfulExit, crashed, pathState, ...}` |
| `runAtLoad` / `throttleInterval` | true / 10 | |
| `logDir` | `/tmp/agents/log/launchd` (agents), `/var/log/tools/launchd` (daemons) | logs `<name>.log`, `<name>.error.log`; `stdoutPath`/`stderrPath` override; also settable for the whole section (`launchd.logDir`), a service's own `logDir` wins |
| `user` / `group` | — | daemons only; or `extraServiceConfig.UserName/GroupName`, not both |
| `bash` (section) | `/bin/bash` | shell that runs the wrapper; for daemons every path component (symlinks too) and the target must be root-owned, not group/world-writable; the plist gets the resolved target |
| `restartOn` | — | files hashed into state; a change (or a file appearing) runs `kickstart -k`; an unreadable file is logged and not tracked; for sops/templates/fetch files use their `restartUnits` (see below) |
| `restartOnChange` | true | false: write a changed plist but leave the running job |
| `disabled` | false | keep the plist, `launchctl disable` and boot out |
| `secrets` | false | plist `0600`, diff suppressed |
| `environment`, `workingDirectory`, `processType`, `nice`, `startInterval`, `startCalendarInterval`, `extraServiceConfig` | — | plist keys |

Behaviour:

- Wrapper, always (also for `programArguments`), as nix: `set -e`, `PATH=/bin:/usr/bin:$PATH`, `mkdir -p` + `chmod 755` logDir, waits, dirs, `preStart`, `exec`.
- A section-level error (e.g. a bad `logDir`) holds every create, update and removal.
- The old `launchd.services`, `launchd.sudo` and per-job `domain` keys are config errors naming their replacement.
- The same label under `agents` and `daemons` is a config error.
- A label loaded from another plist is never booted out: a conflict error on create, forgotten with a warning on removal.
- Plists are compared parsed; a change is written atomically, then `bootout` + `bootstrap`. A job that isn't loaded gets loaded.
- Agent logs are pre-created by tools. Every daemon gets its missing `logDir` created as root, one root-only directory per component; with a user also `touch -h` and `chown -h user:group` (group default `staff`). Symlinked log paths are skipped.
- A missing `workingDirectory` is created at deploy, since launchd enters it before the wrapper runs: agents as the user; daemons as root only inside an existing `/var/lib` or `/Library/Application Support`, one root-only directory per component, with no symlink below that root. Otherwise a job that will be started is skipped (`! skip` in `tools diff`) until you create the directory; `disabled: true` still disables and unloads it.
- Paths only the wrapper uses (`waitForPath(s)`, `dataDir`, `extraDirs`) keep `$NAME`/`${NAME}` for the job's shell to expand at runtime, as nix-config does; `~` is your home for agents and the job's `$HOME` for daemons. Other path options (`workingDirectory`, `logDir`, `stdoutPath`/`stderrPath`, `restartOn`, `keepAlive.pathState`) are read by launchd or tools from the plist or at deploy, so their `$VARS`/`~` expand at deploy from your environment; for a daemon running as another user (root included) they are rejected.
- `extraDirs` `mode`/`owner`/`group` are rejected unless the job is a root daemon: the wrapper's `chmod`/`chown` would fail under `set -e` and crash-loop the job.
- `disabled` is compared with launchd's live override (`print-disabled`), so changes made outside tools are corrected.
- Daemon log paths must be absolute, normalized, without `..`, under `/var/log`. As root, every existing component of a daemon's log dir (and of each log file's parent) must be root-owned and not group/world-writable, checked at plan and again at apply; missing components are created one `mkdir` at a time and each must come out a root-only directory. A failing daemon is not written or loaded; if it is `disabled: true`, it is still disabled and booted out (plist and logs left untouched).
- State records only steps that succeeded; a failed activation shows as `retry` in `tools diff`.
- `tools diff` lists every change deploy would make to tracked jobs: creates, updates, adoptions, loads, restarts, removals, and removals held by a config error (`! held: remove ...`).
- `launchd:`, `launchd.agents` or `launchd.daemons` that is not a mapping (e.g. `[]`, `false`) is a config error that holds every change; only an absent or null key means "no jobs".
- `tools status` shows one row per domain and label (configured agents and daemons, plus what this run's state tracks).
- Each domain has its own state (root's for daemons, the user's for agents): moving a job from `agents` to `daemons` is created as root and then removed from the user domain by the same `sudo tools deploy`.
- A plist tools didn't write is never replaced (an identical one is adopted) and never removed.
- Removal re-reads the plist and stops if it changed since tools wrote it; a disabled label is re-enabled first.
- An unreadable plist or a malformed state entry for a configured job holds that job and fails the deploy, with a fix hint.
- As root every daemon change is a fixed command run directly (`install`, `launchctl`, `rm`, log `mkdir`/`touch`/`chown`) on paths derived from the label or the checked logDir; tools never calls sudo.
- Unlike nix-config's `/tmp/log`, daemon logs never live under a world- or group-writable directory, so a local user can't redirect root's `mkdir`/`touch`/`chown` or launchd's log opens.
- A missing daemon `workingDirectory` is created the same root-only way.

### restartUnits

`sops`/`templates`/`fetch` entries name the jobs that read them, as sops-nix's `restartUnits`:

```yaml
templates:
  forgejo-app-ini:
    path: ~/.local/state/tools/secrets/rendered/forgejo-app.ini
    content: { ... }
    restartUnits: [launchd:com.example.forgejo]
```

- A create, update or replace of the file (not a chmod) records the label as owed in the user's state; the launchd section then pays it. Content is compared in memory, so no hash of a secret is stored.
- Agents: a loaded job is restarted with `kickstart -k`; one this run created, updated, loaded or re-enabled already started with the new file; a disabled one has nothing to restart. A failed kickstart, or a job held by an error, stays owed for the next deploy.
- Daemons: sops, templates and fetch are user-scope, so they change during the user phase of `sudo tools deploy`, after root has handled daemons. The user phase hands the labels to root, which kickstarts each one after it, only if it is a configured, enabled daemon whose plist tools wrote and loaded, then clears the ones it handled from the user's state (a failed kickstart stays owed). A plain `tools deploy` keeps them owed and says so; `tools diff` lists them as `~ restart system/<label> (...; under sudo tools deploy)`.
- A label no configured job has is dropped with a warning (not while the launchd section has errors, or under `--only`).
- Prefer this to `restartOn` for files sops/templates/fetch write: a daemon's `restartOn` hash is read in the root phase, before the user phase rewrites them, so it sees the change one deploy late, and it keeps a hash of secret content in state.

### Differences from nix-config

| Option | nix-config | tools | Why |
| --- | --- | --- | --- |
| domain | daemons only | `agents` (user) and `daemons` (system) | tools also manages agents; put jobs under `daemons` when migrating |
| label | `com.ivankovnatsky.<name>` | `labelPrefix` required | no personal default in a public tool |
| `enable` | per service, default false | listing a service enables it | YAML list is the switch |
| `waitForPath` | one path | also `waitForPaths` list | several secrets/volumes |
| `waitForSecrets` | `/run/secrets` (daemons), `~/.config/sops-nix/secrets` (agents) | `sops.secretsDir` for both | waits on the dir tools writes secrets to |
| bash | `pkgs.bash` | `/bin/bash`, or `bash:` | stable path; root-owned for daemons |
| logDir `chmod 755` | can fail and kill the job | agents skip it if not owner; daemons `\|\| true` | avoids a crash loop |
| extraServiceConfig | can override anything | log paths checked; `Label`/`Program*` reserved | log-path safety |
| ownership | nix-darwin owns all plists | state; never touches foreign plists | safe migration |
| errors | eval fails | bad entry holds removals, deploy fails | partial apply stays honest |
| privilege | activation runs as root | daemons only via `sudo tools deploy`; root never writes in user space | same model as `darwin-rebuild` |
| extras | — | `restartOn`, `restartOnChange`, `disabled`, `secrets`, `tools diff` | |
| tools-only options | — | `programArguments`, `keepAlive` mapping, `stdoutPath`/`stderrPath`, `user`/`group`, `workingDirectory`, `processType`, `nice`, `startInterval`, `startCalendarInterval`, `extraDirs` mode/owner/group, numeric `environment` values (made strings) | plain plist keys or conveniences |
| foreign loaded label | n/a | never booted out (create or removal) | ownership |
| daemon logDir | `/tmp/log/launchd`; `mkdir`/`touch`/`chown` only with `UserName` | `/var/log/tools/launchd`, root-only dirs checked at apply; `mkdir` for every daemon; `touch -h`/`chown -h` with a user | launchd opens logs as root before the wrapper; `/tmp` is world-writable |
| `workingDirectory` | not created | created at deploy (daemons only under the allowed roots) | launchd `chdir`s before the wrapper |
| section `logDir` | none (per service only) | `launchd.logDir` sets every job's default | one place for a log root |
| `$VARS`/`~` in plist and deploy-time paths | left literal in the plist | expanded at deploy; rejected for daemons running as another user | launchd never expands them; deploy-time expansion would use your env, not the job's |
| `~` in wrapper paths | literal (not expanded inside quotes) | your home (agents), job's `$HOME` (daemons) | |
| `extraDirs` mode/owner/group | not supported | root daemons only | non-root `chown` fails under `set -e` |
| disabled override | not managed | compared with live `print-disabled` | outside changes corrected |

## Defaults

`defaults:` reconciles macOS preference keys (skipped with a log line on other
platforms):

```yaml
defaults:
  user:                              # defaults write <domain> ...
    NSGlobalDomain:
      AppleKeyboardUIMode: 3
    com.apple.dock:
      autohide-delay: 1000.0         # float -> <real>; 1000 would be <integer>
    com.apple.HIToolbox:
      AppleEnabledInputSources:      # arrays and nested dicts are fine
        - InputSourceKind: Keyboard Layout
          KeyboardLayout ID: 252
    ~/Library/Preferences/ByHost/com.apple.controlcenter:  # same file as nix-darwin
      Weather: 2
  currentHost:                       # defaults -currentHost (<domain>.<UUID>.plist)
    com.example.app:
      k: 1
  system:                            # root only: sudo tools deploy --scope defaults
    /Library/Preferences/com.example.app:
      Enabled: true
  restart:                           # killall after a change in that domain
    com.apple.dock: Dock
    com.apple.finder: [Finder]
```

- Values are typed XML plists, compared type-strictly; equal keys are not
  rewritten. A key set to `null` is unmanaged (as in nix-darwin).
- One plist is one domain: `~/Library/Preferences/<name>` is `<name>`, `-g` and
  `.GlobalPreferences` (any case on case-insensitive volumes) are
  `NSGlobalDomain`, other paths resolve symlinks and on-disk case (folded on
  case-insensitive volumes); a bare name is its file in
  `~/Library/Preferences`. Two spellings of the same plist in one scope are a
  config error. `defaults` is always given the declared spelling; the resolved
  form only identifies the plist in config, state and `restart`, so a
  `restart` entry applies to every spelling of its plist. Identity is per
  scope: a currentHost bare name is its `ByHost` file, a currentHost path is
  `<path>.<UUID>`. A path whose symlink now leads to another plist is a new
  resource: the old plist is restored or deleted, the new one gets its own
  prior.
- `system` keys are written only by root (`sudo tools deploy --scope defaults`),
  which in turn never writes `user`/`currentHost` keys. As the user, `deploy`
  skips them with a note and `diff` previews them read-only; an unreadable
  plist is reported, never escalated. Runs under sudo and as your user keep
  separate state. Under sudo, `<domain>.plist` must not be a symlink and every
  existing ancestor must be root-owned and not group/world-writable (checked
  at plan and again before each write); otherwise that domain is an error.
- `restart` processes are killed once, after `dock:` too, only if a key in
  that domain changed.
- Any config error holds the whole section: no writes, restores or restarts.
- `persistent-apps`/`persistent-others` belong to `dock:`.
- After a run that changed `user` or `currentHost` keys, `activateSettings -u`
  runs once so running apps pick up the new values (`diff` lists it).

## Dock

`dock:` sets the Dock layout, like nix-config's dockutil module:

```yaml
dock:
  apps:                              # persistent-apps, in order
    - /System/Applications/Mail.app
    - spacer                         # also small-spacer, flexible-spacer
    - {spacer: small}
    - /Applications/Safari.app
  others:                            # persistent-others
    - path: ~/Downloads
      view: grid                     # auto | fan | grid | list
      display: stack                 # stack | folder
      sort: dateadded                # name | dateadded | datemodified | datecreated | kind
    - {url: "https://example.com", label: Example}
  restart: false                     # default, as dockutil --no-restart
```

- Only declared sections are managed; `apps: []` empties one, bare `apps:`
  does not. Comparison is by resolved path, spacer kind and the folder options
  you set. Tiles, folder defaults (auto, folder, date modified) and
  `/Applications` path resolution follow dockutil.
- A missing app keeps its live tile; a section with no existing paths is left
  untouched.

### Removal from config

Prior values are recorded on first write; `written` is saved before every
write and `confirmed` after it lands, so a crash always lands in one of these
rows:

| Live value on removal | Action |
| --- | --- |
| equals prior | forget |
| equals `written` or `confirmed`, prior recorded | restore prior (retried next deploy if it fails) |
| equals `written` or `confirmed`, no prior (unset before tools) | `defaults delete` the key or Dock section (retried next deploy if it fails) |
| anything else (changed or deleted by hand) | leave as is, forget |

Inside declared sections, `diff` lists every change deploy makes: keys
written, restored or deleted, and Dock tiles added, changed or removed
(including tiles added by hand).

### Differences from nix-config

| | nix-config | tools |
| --- | --- | --- |
| Writes | every key, every activation | only differing keys |
| Restart | nix-darwin `killall Dock` (wrapped to a no-op); dockutil `--no-restart` | none unless `restart` |
| Dock sections | both rebuilt from scratch | only declared sections |
| Missing app | dropped from Dock | live tile kept |
| Folder option change | not detected | rewrites the section |
| Removal | value left as is | restored or deleted (table above) |
| Config error | eval fails | section held |
| `diff` | none | per key, restarts listed once |

## settingsctl

`settingsctl:` drives settingsctl (`settings`, from `packages/settingsctl`) on
macOS only. Each declared list is passed as is on every deploy; settingsctl
decides what to change. Every other key is read first and set only when it
differs; `diff` shows the commands deploy would run and the current values.

```yaml
settingsctl:
  path: /usr/local/bin/settings               # optional, see below
  fullDiskAccess: [kitty, Terminal]           # settings fulldiskaccess --enable
  accessibility: [Hammerspoon, kitty]         # settings accessibility set
  loginItems: [Hammerspoon, LosslessSwitcher] # settings login set
  appearanceInit: true    # settings appearance --init, while its state file is missing
  defaultBrowser: firefox # settings defaultbrowser set; macOS asks to confirm
  # root only: sudo tools deploy
  hostName: my-mac        # settings hostname set --host-name
  localHostName: my-mac   # ... --local-host-name (Bonjour)
  computerName: My Mac    # ... --computer-name
  timeZone: Europe/Uzhgorod   # settings timezone set
  sleep: { computer: never, display: 10, disk: never } # settings sleep set (pmset -a)
  remoteLogin: true       # settings remotelogin set on|off
```

- Reads use `settings <cmd> get --json`. A time zone linked to the declared
  one in tzdata is not drift, since automatic
  time zone may pick either. Remote Login is on when `com.openssh.sshd` is
  enabled and loaded, off when it is neither.
- Host settings run in the sudo phase; root never runs the other keys. Without
  sudo, `deploy` and `diff` read them and print a note pointing at
  `sudo tools deploy --scope settingsctl`.
- Finding `settings`: `path` when set. Otherwise the user looks on `PATH`, then
  in `paths.uvBin`; root never uses its own `PATH` and looks in `paths.uvBin`,
  `~/.local/bin`, `/opt/homebrew/bin`, `/opt/homebrew/sbin` and `/usr/local/bin`
  of the invoking user, failing when none has it. On a fresh machine run
  `tools deploy` once without sudo so `uv` installs it. Root then runs a
  binary the user can replace, as with any other command in this config.
- `appearanceInit` switches to dark mode once; settingsctl then writes
  `~/.local/state/settings/appearance/last-run` and later toggles are yours.
- A key that leaves config is left as is. Any config error holds the section.

## plist

`plist:` sets single keys in plist files an app owns, such as a
`config.plist` under `~/Library/Application Support`. Use `defaults:` for
preference domains.

```yaml
plist:
  ~/Library/Application Support/com.nuebling.mac-mouse-fix/config.plist:
    General:showMenuBarItem: false # `:` separates nested dict keys
```

- Only declared keys change; the rest of the file, its format (XML or binary)
  and mode are kept. Values compare type-strictly, as in `defaults`.
- A file that does not exist yet is skipped (`diff` shows `= skip`): the app
  creates it on first launch and a later deploy sets the keys.
- A key that leaves config is left as is. Runs as the user.

## mgmt

`mgmt:` configures running services through their HTTP APIs (ported from the
nix-config `*-mgmt` tools). One sub-key per service; a missing sub-key is not
managed and an absent section makes no network calls.

```yaml
mgmt:
  jellyfin:
    baseUrl: http://127.0.0.1:8096
    apiKeyFile: /run/secrets/jellyfin-api-key
    bindAddress: 0.0.0.0
    libraries:
      - name: Movies
        type: movies
        paths: [/media/movies]
        automaticRefreshIntervalDays: 7
  beszel:
    baseUrl: https://beszel.@DOMAIN@ # @NAME@ filled from baseUrlFiles
    baseUrlFiles: { DOMAIN: /run/secrets/external-domain }
    emailFile: /run/secrets/beszel-email
    passwordFile: /run/secrets/beszel-password
    systems: [{ name: server, host: 127.0.0.1 }]
```

Full shapes for `arr` (radarr/sonarr/lidarr/prowlarr), `beszel`, `forgejo`,
`jellyfin`, `nextdns` and `syncthing` are in
[examples/config.yaml](examples/config.yaml). Every service also takes `timeout`
(default 30s), `retries` (5), `retryDelay` (2s, doubled) and `prune`.

- Secrets are file paths only (`*File`); values are redacted as `***` and never stored.
- Masked secrets are compared against an HMAC of the last value pushed to that endpoint (key in `mgmt.key`, 0600).
- Only differences are written; updates merge into the full remote object.
- `diff` reads only (no retries, no file changes); `deploy` retries reads but not writes.
- A bad read shape, a duplicate identity, or an unresolved name aborts that service before any write.
- Input files of every arr app and NextDNS profile are read before the first write; a bad one holds the whole service.
- One failing service doesn't stop the others.

Config is desired state: `diff` shows every create (`+`), change (`~`) and
removal (`-`) deploy would make, including ids dropped by a replacing write
(NextDNS blocklists and parental-control lists, Syncthing folder devices).
Exceptions, each shown by `diff`:

- Removing every remote object of a kind needs `prune: true`; without it the service fails with `! refusing to remove all ...`.
- Dropping a whole service never deletes remote objects: `= <service> removed from config; remote objects are kept`, shown until the next deploy forgets its state.
- Items of a never-deleted kind (table below) that leave the config are shown once as `= <item> removed from config, kept (...)`. State holds an HMAC of each identity, so a name moved into a `*File` is still the same item.

| Service   | Deleted when not listed                                   | Never deleted (shown as `=`)          | Never deleted, not tracked                           |
| --------- | --------------------------------------------------------- | ------------------------------------- | ---------------------------------------------------- |
| arr       | `rootFolders`; Prowlarr `indexers`, `applications`        | download clients                      |                                                      |
| beszel    | `systems`                                                 |                                       | Discord webhook (a replaced one stays)               |
| forgejo   | older `tools-admin-*`/`tools-user-*` tokens on rotation   | users, repositories                   | GPG keys, tokens with other names                    |
| jellyfin  | media paths inside a listed library                       | libraries                             |                                                      |
| nextdns   | `denylist`, `allowlist`, `rewrites` in the profile file   | profiles                              |                                                      |
| syncthing | `devices` (plus folder-named devices), `folders`          |                                       | the local device                                     |

Set only on create, never diffed afterwards: arr download client
`enable`/`priority`/`remove*`, Prowlarr application `enable`, Jellyfin library
`type`, Forgejo repository `autoInit` and existing users' email and password.

### Differences from nix-config

Business logic matches the `*-mgmt` tools. Differences below are safety or
correctness only.

All services:

| Behaviour | Original | tools | Why |
| --- | --- | --- | --- |
| Absent/null list key | Nix module sent `[]` | unmanaged | no accidental wipe |
| Delete every object of a kind | allowed | needs `prune: true` | guards commented-out config |
| Replacement order | remove, then add | add, then remove; skip removes if an add failed | never left with neither |
| Unexpected read shape | treated as empty | aborts the service | no writes from a bad picture |
| Input files | read as needed | all read before the first write | no half-applied run |
| Item or service dropped from config | silent | `=` row in `diff` | nothing kept silently |
| 2xx body with `errors` | success | failed write | no deletes after a rejected add |
| Duplicate identities | last one wins | error before writes | ambiguous target |
| Inline secrets | allowed | `*File` only | no secrets in config |
| Unchanged sections | written every run | written only when different | idempotent |
| One service fails | whole run stops | others continue | isolation |

| Service | Behaviour | Original | tools | Why |
| --- | --- | --- | --- | --- |
| arr | Masked password/apiKey | pushed every run | pushed when the file changes | convergence |
| arr | Update PUT fields | our fields only | merged into server fields | keeps unmanaged settings |
| arr | Root folder paths | exact match | trailing slash ignored | convergence |
| arr | Bad input file in one app | other apps still written | no app written | no half-applied run |
| beszel | System listing | first page only | all pages | no duplicate creates |
| forgejo | Token names | fixed `forgejo-mgmt` | `tools-admin-*`/`tools-user-*`, older ones deleted | a lost token can be recreated |
| forgejo | Admin token without `tokenFile` | minted every run | same, but not shown as a diff | parity without noise |
| forgejo | GPG key gpg can't parse | upload tried, 422 ignored | config error | 422 also means malformed |
| forgejo | User token | printed to the log | written to `tokenFile` (0600) | no token in logs |
| forgejo | `gpgKeyFile` on an admin | ignored | config error | not silently unmanaged |
| jellyfin | Path compare | exact | trailing slash ignored | convergence |
| jellyfin | Null lists from server | crash | empty | robustness |
| jellyfin | Path containing a comma | split by Jellyfin | config error | query-string paths split on commas |
| nextdns | `@NAME@` vars | `sed` on JSON text | replaced inside JSON strings | no JSON injection |
| nextdns | Domain/rewrite match | case-sensitive | case-insensitive | no delete/re-add churn |
| nextdns | Profile name matches several | first one | error | wrong-profile writes |
| nextdns | Bad input file in one profile | other profiles still written | no profile written | no half-applied run |
| nextdns | Id file without `profileIdFile` | fixed `~/.config/sops-nix/secrets` | `profileIdDir` (default `sops.secretsDir`, which must be valid and hold secrets; null disables) | reads ids tools' sops wrote |
| syncthing | Local device | removable | never removed | keeps the node reachable |
| syncthing | Local device name | `devices` rename after `localDeviceName` | `localDeviceName` wins when set | no rename loop |
| syncthing | Device IDs | exact string | canonical form | no delete/re-add |
| syncthing | Folder device list | replaced | merged | keeps per-device settings |
| syncthing | Unknown device name | used as a raw ID | error | real device not removed |
| syncthing | Raw ID not configured | written, dropped by server | folder held | convergence |
| syncthing | Folder path | exact match | trailing slash ignored | convergence |
| syncthing | Absent label/`ignorePatterns` | reset every run | unmanaged | no `.stignore` wipe |
| syncthing | GUI password | bcrypt locally, every run | plaintext, server hashes, on change | no bcrypt dependency |
| syncthing | `restart` | every run | only after changes | idempotent |

## State and ownership

Deploy records what it installs in the state file and only ever removes what it
recorded — anything installed by hand is never touched (the one opt-in
exception is brew's [`onActivation.cleanup`](#brew)). Removing an entry from
config removes it from the system on the next deploy (where an uninstall exists;
`go`, `cargo` and `curlShell` only release tracking). Git checkout removal refuses to
delete repos with uncommitted, unpushed, stashed, or local-ref-only work.

Every deploy (including each `watch` run) holds an exclusive lock on
`<stateFile>.lock` (0600) from reading the state to its last save, so
overlapping runs (`watch`, scheduled and manual deploys) never drop each
other's records; a second run logs `Waiting for another tools run to finish`
and waits. Under sudo, root locks `/var/db/tools/state.json.lock` and the user
child locks its own. `diff` and `status` only read and never wait. Saves go to
a unique temp file beside the state and are renamed into place.

## Security notes

- `curlShell` pipes remote scripts into a shell, and `brew` bootstrap runs the
  official Homebrew installer — both execute remote code you declared in config.
- `sops` and `templates` never log, diff or persist secret values; see their
  sections for exactly what is shown.
- MCP servers with secret headers substitute secrets from `secretPaths` files at
  register time. The `claude mcp add` CLI only accepts headers as argv, so the
  resolved secret is briefly visible in the process list while the command runs.
  Secrets are never logged or persisted to state.

## Packages

`packages/` holds standalone programs moved as is from nix-config (`*-mgmt`
reconcilers, `rebuild`, `reposync`, `settingsctl` and other scripts). They are
exposed through `overlays.default` and as `packages.<system>.<name>`, and are
not part of the `tools` CLI. See [packages/README.md](packages/README.md).

## Development

```console
nix develop        # dev shell (see `just` targets inside)
just lint          # ruff check + format check
nix flake check    # build + full unit test suite, plus settingsctl's tests
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
