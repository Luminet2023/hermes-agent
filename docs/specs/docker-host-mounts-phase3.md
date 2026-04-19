# Phase 3: Dynamic Docker Host Mounts

`request_host_mount` lets a running Docker task sandbox access one additional allowlisted host path after explicit one-shot approval.

Security model:
- Default off via `terminal.docker_dynamic_mounts_enabled: false`
- Host paths must resolve under `terminal.docker_dynamic_mounts_allowed_roots`
- Every mount request requires one-shot approval: `once` or `deny`
- The tool does not expose a generic root shell, sudo password mode, or arbitrary host command execution
- Approval can still be completed through existing CLI and gateway/message approval transports, but the tool itself is only in local-development toolsets by default

Runtime model:
- Docker sandboxes started while the feature is enabled get a per-task host mount hub bind-mounted at `/__hermes_host_mounts`
- The hub uses shared bind propagation so later host bind mounts become visible inside the already-running container
- Host namespace mount operations are delegated to a configured root-owned wrapper selected by `terminal.docker_mount_helper`
- Supported helper modes:
  - `host-nsenter`
  - `privileged-helper-container`

Required config:

```yaml
terminal:
  docker_dynamic_mounts_enabled: false
  docker_dynamic_mounts_root: ""
  docker_dynamic_mounts_allowed_roots: []
  docker_mount_helper:
    mode: host-nsenter
    wrapper: /abs/path/to/docker-mount-helper
    helper_image: ""
    helper_prepare_command: ""
    timeout: 60
```

Behavior notes:
- `readonly: true` must complete an explicit readonly remount; if that step fails, Hermes unbinds the mount and returns an error instead of silently falling back to read-write
- `cleanup_vm(task_id)`, Docker-to-host switching, idle reaping, and process shutdown all attempt to clean task-scoped dynamic mounts
- Sandboxes created before the feature was enabled do not have the mount hub; recreate the task sandbox before calling `request_host_mount`
