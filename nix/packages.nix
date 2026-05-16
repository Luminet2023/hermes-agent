# nix/packages.nix — Hermes Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    { pkgs, inputs', ... }:
    let
      hermesAgent = pkgs.callPackage ./hermes-agent.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
      };

      hermesNpmLib = pkgs.callPackage ./lib.nix {
        npm-lockfile-fix = inputs'.npm-lockfile-fix.packages.default;
        # Only embed clean revs — dirtyRev doesn't represent any upstream
        # commit, so comparing it would always claim "update available".
        rev = inputs.self.rev or null;
      };

      hermesTui = pkgs.callPackage ./tui.nix {
        inherit hermesNpmLib;
      };

      # Import bundled skills, excluding runtime caches
      bundledSkills = pkgs.lib.cleanSourceWith {
        src = ../skills;
        filter = path: _type: !(pkgs.lib.hasInfix "/index-cache/" path);
      };

      hermesWeb = pkgs.callPackage ./web.nix {
        inherit hermesNpmLib;
      };

      runtimeDeps = with pkgs; [
        nodejs_22
        ripgrep
        git
        openssh
        ffmpeg
        tirith
      ];

      runtimePath = pkgs.lib.makeBinPath runtimeDeps;

      # Lockfile hashes for dev shell stamps
      pyprojectHash = builtins.hashString "sha256" (builtins.readFile ../pyproject.toml);
      uvLockHash =
        if builtins.pathExists ../uv.lock then
          builtins.hashString "sha256" (builtins.readFile ../uv.lock)
        else
          "none";
    in
    {
      packages = {
        default = hermesAgent;
        tui = hermesAgent.hermesTui;
        web = hermesAgent.hermesWeb;

        fix-lockfiles = hermesAgent.hermesNpmLib.mkFixLockfiles {
          packages = [ hermesAgent.hermesTui hermesAgent.hermesWeb ];
        };

        tui = hermesTui;
        web = hermesWeb;

        fix-lockfiles = hermesNpmLib.mkFixLockfiles {
          packages = [ hermesTui hermesWeb ];
        };
      };
    };
}
