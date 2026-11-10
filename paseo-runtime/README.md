# Generic Paseo runtime

`ghcr.io/iarnvidr/paseo-runtime` is a small, reproducible wrapper around the
pinned official `ghcr.io/getpaseo/paseo:0.14.0` image. It prevents Git
auto-maintenance from detaching subprocesses during background workspace fetches.

This is an **upstream, site-neutral base**, not a ready-to-deploy private Paseo
installation. It contains no workspace registration, Git credentials, OAuth,
MCP profiles, plugins, managed policy, mounted secrets or user configuration.

Consumers pin the published OCI **digest**, then add their own runtime
configuration, extensions, secrets at runtime, health checks and rollback
procedure. Keep the daemon's existing entrypoint and persistent home volume;
running a new image without those inputs must not be treated as a migration.
