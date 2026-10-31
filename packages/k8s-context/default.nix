{ pkgs }:

pkgs.writeShellScriptBin "k8s-context" ''
  export PATH="${pkgs.lib.makeBinPath [ pkgs.fzf ]}:$PATH"
  exec ${
    pkgs.python3.withPackages (ps: [
      ps.click
      ps.pyyaml
    ])
  }/bin/python ${./k8s-context.py} "$@"
''
