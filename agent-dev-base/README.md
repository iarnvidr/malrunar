# Generic agent development foundation

`ghcr.io/iarnvidr/agent-dev-base` supplies the pinned code-server base, Node
24 LTS, the checksum-verified Dagger v1 CLI, and version-pinned public Codex
and Claude Code CLIs. It uses the normal non-root `coder` identity, and Git
auto-maintenance stays in the process tree.

This is deliberately **not** the private `agent-dev` broker image. It has
no GitHub or BWS credentials, managed Claude/Codex configuration, private
skills, worktree grants, policy wrappers, Docker socket, host engine, mounts,
task limits or automatic deployment logic. A consumer supplies those in a
private overlay and at runtime.

The nightly agent-runtime pin updater checks the npm `latest` versions of both
CLIs and opens a reviewed PR when either changes. The base layers and Dagger
installation are cached; a CLI refresh invalidates the final CLI layer only.
`python3 -I scripts/bump_agent_runtimes.py --clis-only --dry-run` from the
repository root previews an update without changing files.

The [nightly image workflow](../.github/workflows/nightly-agent-images.yml)
also builds and signs a separate, smoke-tested preview tag every night. These
preview images are not live agent-dev containers: the private broker/deployment
owns the runtime overlay, container creation and rollout.

Dagger connects to the consumer's configured shared engine; building or
running this image does not start a separate engine. Pin the immutable
`ghcr.io/iarnvidr/agent-dev-base@sha256:...` digest after the public
release has passed PR smoke tests and signed publication.
