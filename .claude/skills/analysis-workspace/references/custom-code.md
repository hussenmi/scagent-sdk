# Trusted Iris execution contract

`run_analysis_code` injects `np`, `pd`, `sc`, `ad`, `plt`, `Path`, `workspace`, `session_dir`, and
`register_artifact(path, name, media_type)`. It runs in the brokered scientific environment and
starts in a temporary staging directory. Ordinary Python imports and builtins are available.

Save intended outputs beneath `workspace` and register them. The runner always saves the exact
source plus stdout/stderr, so the operation can be inspected after the session.

`run_shell_command` starts `bash -lc` with an explicit working directory and timeout. Shell syntax
is supported. The command and captured streams are registered when it succeeds. A nonzero exit is
reported as a failure with the output tail retained in the capability failure record.

This mode is designed for trusted in-process Iris use and is not a containment mechanism. The
process has the invoking user's filesystem and process permissions. OpenShell/container execution
should provide the security boundary on hosts where it is supported.
