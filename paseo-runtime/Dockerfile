# Generic, digest-pinned upstream Paseo runtime. No site plugins, credentials, host mounts or workspace policy.
ARG PASEO_IMAGE
FROM ${PASEO_IMAGE} AS runtime
USER root
# Paseo periodically fetches workspaces. Keep Git maintenance inside the caller's process tree.
RUN git config --system maintenance.autoDetach false \
 && git config --system gc.autoDetach false \
 && test "$(git config --system --get maintenance.autoDetach)" = false \
 && test "$(git config --system --get gc.autoDetach)" = false

# Versioned agent harnesses are generic tools; all credentials and policy stay in the consumer.
# Keep this last so refreshing either CLI reuses the upstream Paseo and Git-safety layers.
ARG CODEX_VERSION
ARG CLAUDE_CODE_VERSION
RUN npm install --global --no-audit --no-fund \
      "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && test "$(codex --version | awk '{print $2}')" = "${CODEX_VERSION}" \
 && test "$(claude --version | awk '{print $1}')" = "${CLAUDE_CODE_VERSION}"
USER paseo
