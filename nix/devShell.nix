# nix/devShell.nix — Dev shell that delegates setup to each package
#
# Each package in inputsFrom might expose passthru.devShellHook — a bash snippet
# with stamp-checked setup logic. This file collects and runs them all.
{ ... }:
{
  perSystem =
    { pkgs, self', ... }:
    let
      hermes-agent = inputs.self.packages.${system}.default;
      hermes-tui = inputs.self.packages.${system}.tui;
      hermes-web = inputs.self.packages.${system}.web;
      packages = [ hermes-agent hermes-tui hermes-web ];
    in {
      devShells.default = pkgs.mkShell {
        inputsFrom = packages;
        packages = with pkgs; [
          uv
        ];
        shellHook =
          let
            hooks = map (p: p.passthru.devShellHook or "") packages;
            combined = pkgs.lib.concatStringsSep "\n" (builtins.filter (h: h != "") hooks);
          in
          ''
            echo "Hermes Agent dev shell"
            ${combined}
            echo "Ready. Run 'hermes' to start."
          '';
      };
    };
}
