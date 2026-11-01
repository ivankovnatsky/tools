{ pkgs }:

let
  py = pkgs.python3;

  # anker-solix-api is not published to PyPI or nixpkgs; build it from source.
  anker-solix-api = py.pkgs.buildPythonPackage {
    pname = "anker-solix-api";
    version = "3.9.0";
    pyproject = true;

    src = pkgs.fetchFromGitHub {
      owner = "thomluther";
      repo = "anker-solix-api";
      rev = "c2f87696f49151d8ead790b744bc6970e6258c2c";
      hash = "sha256-Ay9c75ApmLtYuwEYVF+IU4zLmL3t+tY+XcjMTpa1ZJE=";
    };

    build-system = [ py.pkgs.poetry-core ];

    # Upstream pins newer minimums than nixpkgs ships; the API used here works
    # with the nixpkgs versions.
    pythonRelaxDeps = [
      "aiohttp"
      "cryptography"
    ];

    # Upstream pyproject.toml ships no [build-system] table, so pip falls back to
    # setuptools; declare the poetry-core backend it actually uses.
    postPatch = ''
      printf '\n[build-system]\nrequires = ["poetry-core>=2.1"]\nbuild-backend = "poetry.core.masonry.api"\n' >> pyproject.toml
    '';

    dependencies = with py.pkgs; [
      aiohttp
      aiofiles
      cryptography
      paho-mqtt
      python-dotenv
    ];

    doCheck = false;
    pythonImportsCheck = [ "anker_solix_api" ];
  };

  src = (import ../cleanPythonSource.nix { inherit (pkgs) lib; }) ./.;
  discordSrc = (import ../cleanPythonSource.nix { inherit (pkgs) lib; }) ../discord;
  python = py.withPackages (ps: [
    ps.click
    ps.discord-webhook
    anker-solix-api
  ]);
in
pkgs.writeShellScriptBin "battery-monitoring" ''
  export PYTHONPATH="${discordSrc}''${PYTHONPATH:+:$PYTHONPATH}"
  exec ${python}/bin/python ${src}/battery-monitoring.py "$@"
''
