# Restricted Privileged Host Actions (Phase 2)

Phase 2 adds one new tool:

- `request_privileged_host_action`

## Behavior

- The feature is disabled by default with `terminal.privileged_host_actions_enabled: false`.
- Operators must explicitly allowlist actions under `terminal.privileged_host_actions`.
- Each action must point to a root-owned executable wrapper and define:
  - `wrapper`
  - `allowed_args`
  - `timeout`
- The tool only accepts one-shot approval: `once` or `deny`.
- Hermes invokes the wrapper directly with `subprocess.run(..., shell=False)` and never exposes a generic root shell, sudo password mode, or arbitrary host command execution.
- The tool returns structured JSON with `success`, `action`, `approval`, `exit_code`, `stdout`, and `stderr`.

## Default Exposure

- Included by default in `terminal`, `hermes-cli`, and `hermes-acp`.
- Not included by default in `hermes-api-server` or messaging platform toolsets.
- If approval is requested through the gateway, the existing message-platform approval UX still handles the one-shot approval flow.

## Future Phase 3 Constraints

Dynamic host-path mounting remains out of scope for this phase.

If Phase 3 is implemented later:

- do not rely on ordinary Docker runtime add-mount commands for a running container
- prefer host mount-namespace operations
- if the host lacks `nsenter`, a privileged helper container may be used to enter the host namespaces before performing mount operations
