#!/usr/bin/env python3
"""Sync local git repositories to/from remotes.

Safe-only operations: ff-only pull, no-force push. Skips and alerts on conflicts.

TODO: reconsider the name. With `remoteUrl = null` repos, this also creates and
maintains repositories it never syncs (git init, identity, .gitignore), so
"reposync" no longer describes everything it does.

Commands:
  reposync init   --config-file <path>   Idempotent repo/remote setup
  reposync sync   --config-file <path>   Sync all configured repos
  reposync status --config-file <path>   Show sync state of all repos
"""

from cli import main

if __name__ == "__main__":
    main(prog_name="reposync")
