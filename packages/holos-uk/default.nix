{ pkgs }:

# uv and ffmpeg come from PATH; uv installs the script's dependencies.
pkgs.writeShellScriptBin "holos-uk" ''
  exec uv run --quiet --script ${./holos-uk.py} "$@"
''
