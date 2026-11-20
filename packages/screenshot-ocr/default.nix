{ pkgs }:

# Needs the Xcode Command Line Tools (xcrun swiftc) at runtime to build
# its Vision OCR helper; Vision is macOS-only.
pkgs.writeShellScriptBin "screenshot-ocr" ''
  exec ${pkgs.python3}/bin/python3 -I ${./screenshot-ocr.py} "$@"
''
