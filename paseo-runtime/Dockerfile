# Generic, digest-pinned upstream Paseo runtime. No site plugins, credentials, host mounts or workspace policy.
ARG PASEO_IMAGE
FROM ${PASEO_IMAGE} AS runtime
USER root
# Paseo periodically fetches workspaces. Keep Git maintenance inside the caller's process tree.
RUN git config --system maintenance.autoDetach false \
 && git config --system gc.autoDetach false \
 && test "$(git config --system --get maintenance.autoDetach)" = false \
 && test "$(git config --system --get gc.autoDetach)" = false
USER paseo
