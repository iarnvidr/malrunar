# Generic Paseo runtime

`ghcr.io/iarnvidr/paseo-runtime` is a reproducible wrapper around the official
Paseo image, pinned by **tag and OCI digest** in [`pins.env`](pins.env). It
prevents Git auto-maintenance from detaching subprocesses during background
workspace fetches. It also installs exact, checked versions of the public Codex
and Claude Code CLIs, so the daemon can launch either without private tooling.

This is an **upstream, site-neutral base**, not a ready-to-deploy private Paseo
installation. It contains no workspace registration, Git credentials, OAuth,
MCP profiles, plugins, managed policy, mounted secrets or user configuration.

Consumers pin the published OCI **digest**, then add their own runtime
configuration, extensions, secrets at runtime, health checks and rollback
procedure. Keep the daemon's existing entrypoint and persistent home volume;
running a new image without those inputs must not be treated as a migration.

## Updates and rebuilds

`python3 -I scripts/bump_agent_runtimes.py --paseo-only` from the repository
root resolves the latest **stable** Paseo GitHub release and the GHCR manifest
digest. Without `--paseo-only`, the updater also refreshes Codex and Claude
Code versions for this image and `agent-dev-base` from their npm releases.
Use `--dry-run` to inspect changes first. The nightly workflow performs the
same update and opens a PR; it does not directly upgrade a running daemon.

PR CI builds and smoke-tests the new image hash. After review and merge,
`publish.yml` promotes the tested image and signs its digest. If no accepted
candidate is available, `publish.yml` can be dispatched on `main` with
`trusted_rebuild=true` to build that **new, not-yet-published hash**. An
already published `h-` tag is immutable and will be reused, not overwritten.

The separate [nightly image workflow](../.github/workflows/nightly-agent-images.yml)
builds, tests and signs a preview image every night from the latest stable
pins, without waiting for a PR. Its unique `nightly-*` tag is intentionally
separate from the reviewed production `h-*` release path.
