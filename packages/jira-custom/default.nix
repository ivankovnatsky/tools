{ pkgs }:

let
  python = pkgs.python3.withPackages (ps: [
    ps.jira
    ps.click
    ps.markdown-it-py
    ps.rich
  ]);
in
pkgs.writeShellScriptBin "jira-custom" ''
  export PYTHONPATH="${./.}''${PYTHONPATH:+:$PYTHONPATH}"
  exec ${python}/bin/python -P -m jira_custom "$@"
''
