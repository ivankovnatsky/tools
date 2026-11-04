{ pkgs }:

pkgs.writeShellScriptBin "dock-mgmt" ''
  exec ${pkgs.python3}/bin/python ${./dock-mgmt.py} ${pkgs.dockutil}/bin/dockutil "$@"
''
