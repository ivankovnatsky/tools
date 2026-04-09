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
        pythonPackages = ps: with ps; [ ];

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
            pkgs.python3
            pkgs.poetry
            pkgs.treefmt
            pkgs.nixfmt-rfc-style
            pkgs.ruff
            pkgs.nodePackages.prettier
            pkgs.just
          ];
        };
      }
    );
}
