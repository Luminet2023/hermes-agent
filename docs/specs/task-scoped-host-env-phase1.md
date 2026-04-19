# Task-Scoped Host Environment Switch (Phase 1)

Docker remains the default terminal backend.

Phase 1 adds two task-scoped tools:

- `request_host_env`
- `restore_sandbox_env`

## Behavior

- `request_host_env` asks for explicit approval and only allows `once` or `deny`.
- On approval, Hermes records the task's previous backend, cleans up the current task environment, clears the file-tool cache for that task, and switches only that task to the local/host backend.
- `restore_sandbox_env` cleans up the current task environment, clears the file-tool cache, and restores the task to `previous_env_type`. If there is no previous backend recorded, it falls back to the default configured backend.
- If the task is already on the target backend, the tool returns a structured no-op success result.

## Scope

This phase does not include privileged host wrappers, runtime Docker mount expansion, or any root-action interface.
