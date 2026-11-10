# Generic agent development foundation

`ghcr.io/iarnvidr/agent-dev-base` supplies the pinned code-server base, Node
24 LTS and the pinned, checksum-verified Dagger v1 CLI. It uses the normal
non-root `coder` identity, and Git auto-maintenance stays in the process tree.

This is deliberately **not** the private `agent-dev` broker image. It has
no GitHub or BWS credentials, managed Claude/Codex configuration, private
skills, worktree grants, policy wrappers, Docker socket, host engine, mounts,
task limits or automatic deployment logic. A consumer supplies those in a
private overlay and at runtime.

Dagger connects to the consumer's configured shared engine; building or
running this image does not start a separate engine. Pin the immutable
`ghcr.io/iarnvidr/agent-dev-base@sha256:...` digest after the public
release has passed PR smoke tests and signed publication.
