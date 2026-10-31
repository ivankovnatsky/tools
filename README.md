# tools

Declarative configuration manager for the user environment: packages (brew, bun,
npm, uv, go, flatpak), git checkouts, dotfiles, MCP servers, ollama models, and
curl-piped installers — reconciled from a YAML/JSON/TOML config against a local
state file.

## Usage

```console
tools diff   [--config PATH]... [--scope SECTION]...   # dry-run preview
tools deploy [--config PATH]... [--scope SECTION]... [--approve]
```

- `tools diff` shows what deploy would change and **exits 1 when changes are
  pending** (0 when clean) — usable as a check in scripts/CI. It never modifies
  the filesystem.
- `tools deploy` previews, asks for confirmation when there is anything to
  approve, then reconciles. `--approve` skips the prompt. A clean preview still
  deploys: non-destructive maintenance (pulling tracked git checkouts) runs on
  every deploy.
- `--config` accepts a file or a directory and may be repeated; configs are
  deep-merged in order (scalars replace, dicts recurse, lists replace).
  **Relative `files:` sources always resolve against the first `--config` path's
  directory**, so keep file-carrying sections there.
- `--scope` limits the run to named sections (repeatable).
- `plan` and `apply` are hidden aliases for `diff` and `deploy`.

Sections: `bun`, `npm`, `uv`, `go`, `pi`, `mcp`, `curlShell`, `gitRepos`,
`files`, `skills`, `links`, `brew`, `ollamaModels`, `flatpak`, `launchd`,
`defaults`, `dock`.

`tools status [--config PATH]...` prints the live state of managed launchd jobs
(loaded, pid, last exit code). It is read-only and never uses sudo.

## Config

See [examples/config.yaml](examples/config.yaml) for a full annotated example
and [examples/overrides.yaml](examples/overrides.yaml) for per-host overrides.
Formats: `.yaml`, `.yml`, `.json`, `.toml`.

A config _directory_ supports two layouts:

- **Host-based**: a `machines/` subdirectory with one `machines/<hostname>.yaml`
  per host. Top-level files (e.g. a nix-generated paths JSON) are merged
  underneath the host config.
- **Flat**: every supported file in the directory, merged in lexicographic
  order.

Any config file may declare `include: [relative/paths...]`; includes are
resolved relative to the including file and merged underneath it.

Notable keys:

- `stateFile` — where ownership state lives (default
  `~/.local/state/tools/state.json`).
- `paths` — binary/install locations the reconcilers use; documented in the
  comment block of `examples/config.yaml`.
- `files:` entries take `{source, target, mode?, secrets?}` or
  `{dir, mode?, secrets?}`. `mode` must be a **quoted octal string** (`"0644"`);
  bare YAML ints are rejected as ambiguous. `secrets: true` suppresses inline
  content diffs for that entry.

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
  first fetches and url/ref changes, while new upstream commits on an
  unchanged ref land on the next deploy, like `gitRepos` pulls.
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

## launchd

Agents (`domain: user`, `gui/<uid>`) and daemons (`domain: system`). macOS only.
Agents mirror nix-config's home `local.launchd`, daemons its darwin one; see the differences below.

```yaml
launchd:
  labelPrefix: com.example # label = <labelPrefix>.<name> unless `label:` is set
  sudo: true # needed for domain: system (tools runs `sudo -n`; run `sudo -v` first)
  services:
    caddy:
      domain: system
      waitForSecrets: true
      waitForPaths: [/Volumes/Storage]
      dataDir: /var/lib/caddy
      command: /opt/homebrew/bin/caddy run --config /var/lib/caddy/Caddyfile
      restartOn: [/var/lib/caddy/Caddyfile]
```

| Option | Default | Notes |
| --- | --- | --- |
| `command` / `programArguments` | — | exactly one; wrapper ends `exec <command>` (as nix) or `exec "$@"` |
| `waitForSecrets` | false | wait for `~/.config/sops-nix/secrets` (agents) or `/run/secrets` (daemons) |
| `waitForPath` / `waitForPaths` | — | wait until each path exists |
| `dataDir`, `extraDirs` | — | `mkdir -p` at start; `extraDirs` also takes `{path, mode, owner, group}` |
| `preStart` | — | shell run before the command |
| `keepAlive` | true | bool or `{successfulExit, crashed, pathState, ...}` |
| `runAtLoad` / `throttleInterval` | true / 10 | |
| `logDir` | `/tmp/agents/log/launchd` (agents), `/tmp/log/launchd` (daemons) | logs `<name>.log`, `<name>.error.log`; `stdoutPath`/`stderrPath` override |
| `user` / `group` | — | daemons only; or `extraServiceConfig.UserName/GroupName`, not both |
| `bash` (section) | `/bin/bash` | shell that runs the wrapper; for daemons every path component (symlinks too) and the target must be root-owned, not group/world-writable; the plist gets the resolved target |
| `restartOn` | — | files hashed into state; a change (or a file appearing) runs `kickstart -k`; root-only daemon files are hashed via `sudo -n shasum` |
| `restartOnChange` | true | false: write a changed plist but leave the running job |
| `disabled` | false | keep the plist, `launchctl disable` and boot out |
| `secrets` | false | plist `0600`, diff suppressed |
| `environment`, `workingDirectory`, `processType`, `nice`, `startInterval`, `startCalendarInterval`, `extraServiceConfig` | — | plist keys |

Behaviour:

- Wrapper, always (also for `programArguments`), as nix: `set -e`, `PATH=/bin:/usr/bin:$PATH`, `mkdir -p` + `chmod 755` logDir, waits, dirs, `preStart`, `exec`.
- A section-level error (e.g. `sudo: yes`) holds every create, update and removal.
- A label loaded from another plist is never booted out: a conflict error on create, forgotten with a warning on removal.
- Plists are compared parsed; a change is written atomically, then `bootout` + `bootstrap`. A job that isn't loaded gets loaded.
- Agent logs are pre-created by tools. Every daemon gets `sudo -n mkdir -p <logDir>` (if missing); with a user also `touch -h` and `chown -h user:group` (group default `staff`). Symlinked log paths are skipped.
- A missing `workingDirectory` is created at deploy (agents as the user, daemons via `sudo -n mkdir -p`), since launchd enters it before the wrapper runs.
- `disabled` is compared with launchd's live override (`print-disabled`), so changes made outside tools are corrected.
- Daemon log paths must be absolute, normalized, without `..`, under `/tmp/log`, `/Library/Logs` or `/var/log`.
- State records only steps that succeeded; a failed activation shows as `retry` in `tools diff`.
- A plist tools didn't write is never replaced (an identical one is adopted) and never removed.
- Removal re-reads the plist and stops if it changed since tools wrote it; a disabled label is re-enabled first.
- An unreadable plist or a malformed state entry for a configured job holds that job and fails the deploy, with a fix hint.
- tools refuses to run as root. Daemons need `sudo: true`; every privileged call is a fixed `sudo -n` command (`install`, `launchctl`, `rm`, `plutil`, `shasum`, log `mkdir`/`touch`/`chown`) on paths derived from the label or the checked logDir.
- Known limit (as nix): `/tmp/log` is world-writable, so a local user could plant symlinks or hard links there before root touches/chowns daemon logs.

### Differences from nix-config

| Option | nix-config | tools | Why |
| --- | --- | --- | --- |
| domain | daemons only | `user` (default) or `system` | tools also manages agents; set `domain: system` when migrating |
| label | `com.ivankovnatsky.<name>` | `labelPrefix` required | no personal default in a public tool |
| `enable` | per service, default false | listing a service enables it | YAML list is the switch |
| `waitForPath` | one path | also `waitForPaths` list | several secrets/volumes |
| bash | `pkgs.bash` | `/bin/bash`, or `bash:` | stable path; root-owned for daemons |
| logDir `chmod 755` | can fail and kill the job | agents skip it if not owner; daemons `\|\| true` | avoids a crash loop |
| extraServiceConfig | can override anything | log paths checked; `Label`/`Program*` reserved | log-path safety |
| ownership | nix-darwin owns all plists | state; never touches foreign plists | safe migration |
| errors | eval fails | bad entry holds removals, deploy fails | partial apply stays honest |
| privilege | activation runs as root | refuses root; fixed `sudo -n` commands | no root process |
| extras | — | `restartOn`, `restartOnChange`, `disabled`, `secrets`, `tools diff` | |
| tools-only options | — | `programArguments`, `keepAlive` mapping, `stdoutPath`/`stderrPath`, `user`/`group`, `workingDirectory`, `processType`, `nice`, `startInterval`, `startCalendarInterval`, `extraDirs` mode/owner/group, numeric `environment` values (made strings) | plain plist keys or conveniences |
| foreign loaded label | n/a | never booted out (create or removal) | ownership |
| daemon logDir | `mkdir`/`touch`/`chown` only with `UserName` | `mkdir` for every daemon; `touch -h`/`chown -h` with a user | launchd opens logs before the wrapper |
| `workingDirectory` | not created | created at deploy | launchd `chdir`s before the wrapper |
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
  system:                            # sudo -n defaults write <path> ...
    /Library/Preferences/com.example.app:
      Enabled: true
  restart:                           # killall after a change in that domain
    com.apple.dock: Dock
    com.apple.finder: [Finder]
```

- Values are typed XML plists, compared type-strictly; equal keys are not
  rewritten. A key set to `null` is unmanaged (as in nix-darwin).
- One plist is one domain: `~/Library/Preferences/<name>` is `<name>`, `-g` is
  `NSGlobalDomain`, other paths resolve symlinks and on-disk case. Two spellings
  of the same plist are a config error.
- `system` writes use `sudo -n` (run `sudo -v` first); `diff` never escalates.
- `restart` processes are killed once, after `dock:` too, only if a key in
  that domain changed.
- Any config error holds the whole section: no writes, restores or restarts.
- `persistent-apps`/`persistent-others` belong to `dock:`.

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
| no prior (key was unset before tools) | leave as is, forget |
| equals `written` or `confirmed` | restore prior (retried next deploy if it fails) |
| anything else (changed or deleted by hand) | leave as is, forget |

### Differences from nix-config

| | nix-config | tools |
| --- | --- | --- |
| Writes | every key, every activation | only differing keys |
| Restart | nix-darwin `killall Dock` (wrapped to a no-op); dockutil `--no-restart` | none unless `restart` |
| Dock sections | both rebuilt from scratch | only declared sections |
| Missing app | dropped from Dock | live tile kept |
| Folder option change | not detected | rewrites the section |
| Removal | value left as is | table above |
| Config error | eval fails | section held |
| `diff` | none | per key, restarts listed once |

## State and ownership

Deploy records what it installs in the state file and only ever removes what it
recorded — anything installed by hand is never touched. Removing an entry from
config removes it from the system on the next deploy (where an uninstall exists;
`go` and `curlShell` only release tracking). Git checkout removal refuses to
delete repos with uncommitted, unpushed, stashed, or local-ref-only work.

## Security notes

- `curlShell` pipes remote scripts into a shell, and `brew` bootstrap runs the
  official Homebrew installer — both execute remote code you declared in config.
- MCP servers with secret headers substitute secrets from `secretPaths` files at
  register time. The `claude mcp add` CLI only accepts headers as argv, so the
  resolved secret is briefly visible in the process list while the command runs.
  Secrets are never logged or persisted to state.

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
