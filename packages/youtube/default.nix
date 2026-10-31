{ pkgs }:

let
  src = (import ../cleanPythonSource.nix { inherit (pkgs) lib; }) ./.;
in
pkgs.writeShellScriptBin "youtube" ''
  exec ${
    pkgs.python3.withPackages (ps: [
      ps.requests
      ps.click
    ])
  }/bin/python ${src}/youtube.py "$@"
''
