{ pkgs }:

let
  py = pkgs.python3;
  src = (import ../cleanPythonSource.nix { inherit (pkgs) lib; }) ./.;
  python = py.withPackages (ps: [
    ps.click
  ]);
in
pkgs.writeShellScriptBin "homelab" ''
  PATH="${
    pkgs.lib.makeBinPath [
      pkgs.dns
    ]
  }:$PATH"
  exec ${python}/bin/python ${src}/homelab.py "$@"
''
