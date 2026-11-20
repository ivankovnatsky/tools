{ pkgs }:

# kitty global watcher: macOS menu-bar title "<prefix>: <active tab title>".
# Referenced from kitty.conf as `watcher ${pkgs.kitty-title-prefix}/kitty-title-prefix.py`.
pkgs.runCommand "kitty-title-prefix" { } ''
  install -Dm644 ${./kitty-title-prefix.py} $out/kitty-title-prefix.py
''
