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
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ ];
        };

        # Keep in sync with pyproject.toml
        pythonPackages = ps: with ps; [ pyyaml ];

        toolsPackage = pkgs.python3Packages.buildPythonApplication {
          pname = "tools";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = with pkgs.python3Packages; [
            poetry-core
          ];

          dependencies = pythonPackages pkgs.python3Packages;

          meta = with pkgs.lib; {
            description = "Declarative configuration manager";
            homepage = "https://github.com/ivankovnatsky/tools";
            license = licenses.mit;
            mainProgram = "tools";
          };
        };
      in
      {
        packages = {
          tools = toolsPackage;
          default = toolsPackage;
        };

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
            echo "  just build            - nix build"
            echo "  just update           - update flake inputs"
            echo "  just update-nix-config - push and bump tools in nix-config"
            echo "  just bump             - increment patch version"
            echo "  just release          - bump and create GitHub release"
          '';
        };
      }
    );
}
