{
  lib,
  pkgs,
  pigz,
  curl,
  openssh,
  gnutar,
}:

# gnutar lands in PATH as plain "tar", shadowing Apple's. resolve_tar probes
# candidates by name and verifies each with --version, so the bsdtar backend
# still finds Apple's.
pkgs.writeShellScriptBin "backup-home" ''
  export PATH="${
    lib.makeBinPath [
      pigz
      curl
      openssh
      gnutar
    ]
  }:$PATH"
  exec ${pkgs.python3.withPackages (ps: [ ps.click ])}/bin/python ${./backup-home.py} "$@"
''
