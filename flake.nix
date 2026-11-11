{
  description = "tools - Declarative configuration manager";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    let
      inherit (nixpkgs) lib;
      programNames = builtins.attrNames (
        lib.filterAttrs (
          name: type: type == "directory" && builtins.pathExists (./packages + "/${name}/default.nix")
        ) (builtins.readDir ./packages)
      );
    in
    {
      # Programs under packages/, built against the consumer's nixpkgs.
      overlays.default =
        _final: prev: lib.genAttrs programNames (name: prev.callPackage (./packages + "/${name}") { });
    }
    // flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ self.overlays.default ];
        };

        programs = lib.filterAttrs (_: lib.meta.availableOn pkgs.stdenv.hostPlatform) (
          lib.genAttrs programNames (name: pkgs.${name})
        );

        # Keep in sync with pyproject.toml
        pythonPackages =
          ps: with ps; [
            click
            pyyaml
          ];

        # Only the Python package: packages/ edits must not rebuild tools.
        sourceWith =
          extra:
          lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions (
              [
                ./pyproject.toml
                ./README.md
                ./tools
              ]
              ++ extra
            );
          };

        toolsPackage = pkgs.python3Packages.buildPythonApplication {
          pname = "tools";
          # Read from pyproject.toml so `just bump` has one place to edit.
          version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
          pyproject = true;

          src = sourceWith [ ];

          build-system = with pkgs.python3Packages; [
            poetry-core
          ];

          dependencies = pythonPackages pkgs.python3Packages;

          # Tests run in `checks`, not here: this derivation is rebuilt on
          # every nix-config switch and must not pay for them.
          doCheck = false;

          meta = with pkgs.lib; {
            description = "Declarative configuration manager";
            homepage = "https://github.com/ivankovnatsky/tools";
            license = licenses.mit;
            mainProgram = "tools";
          };
        };
      in
      {
        packages = programs // {
          tools = toolsPackage;
          default = toolsPackage;
        };

        checks.default = toolsPackage.overridePythonAttrs (_: {
          src = sourceWith [ ./tests ];
          doCheck = true;
          nativeCheckInputs = [ pkgs.git ];
          checkPhase = ''
            runHook preCheck
            python -m unittest discover -s tests -v
            runHook postCheck
          '';
        });

        checks.settingsctl =
          pkgs.runCommand "settingsctl-tests"
            {
              nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.click ])) ];
              src = (import ./packages/cleanPythonSource.nix { inherit lib; }) ./packages/settingsctl;
            }
            ''
              cp -r "$src" settingsctl
              chmod -R u+w settingsctl
              cd settingsctl
              python -m unittest discover -s tests -v
              touch "$out"
            '';

        devShells.default = pkgs.mkShell {
          buildInputs = [
            (pkgs.python3.withPackages pythonPackages)
            pkgs.poetry
            pkgs.treefmt
            pkgs.nixfmt-rfc-style
            pkgs.ruff
            pkgs.prettier
            pkgs.python3Packages.pre-commit-hooks
            pkgs.just
          ];

          shellHook = ''
            echo "tools dev shell"
            echo "  just format           - format code"
            echo "  just lint             - lint without modifying"
            echo "  just clean            - remove build artifacts"
            echo "  just diff             - show pending changes"
            echo "  just deploy           - apply config"
            echo "  just bump             - increment patch version"
            echo "  just release          - bump and create GitHub release"
          '';
        };
      }
    );
}
