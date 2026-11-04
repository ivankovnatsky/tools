{ lib, pkgs }:

pkgs.writeShellScriptBin "agent-sessions" ''
  export PATH="${lib.makeBinPath [ pkgs.rsync ]}:$PATH"
  exec ${pkgs.python3}/bin/python3 -I ${./agent-sessions.py} "$@"
''
