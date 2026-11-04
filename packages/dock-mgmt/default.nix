{ pkgs }:

(pkgs.writeShellScriptBin "dock-mgmt" ''
  exec ${pkgs.python3}/bin/python ${./dock-mgmt.py} ${pkgs.dockutil}/bin/dockutil "$@"
'').overrideAttrs
  (old: {
    meta = old.meta // {
      inherit (pkgs.dockutil.meta) platforms;
    };
  })
