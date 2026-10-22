# session-kit: small tools for an XFCE desktop session, as a component image.
#
# The image is FROM scratch and holds only:
#   /opt/malrunar/session-kit/bin/malrunar-session   one command: workspaces, restore-tabs, open-tabs, check,
#                                                    launchers, clear-super-shortcuts
#   /opt/malrunar/session-kit/lib/                   the code (python, standard library only) and a shell helper
#   /opt/malrunar/session-kit/share/                 examples for the consumer's own layers
#   /opt/malrunar/session-kit/README.md, LICENSE
#   /etc/supervisor/conf.d/malrunar-session-kit.conf the supervisord program (the supervisor.d convention, CONVENTIONS.md)
# It needs desktop-base (python3, the X11 utilities, xfconf, liblz4) and, for the workspace and window
# tools, the xdotool package that the consumer installs in its own layer. It does not need the
# work-browser component: the tools that hand pages to it are used only when it is present, and say so
# when it is not. No apt, downloads or compilation take place, so there is nothing to pin beyond the
# recipe itself. Consumers copy it into their own image built on desktop-base:
#   COPY --link --from=ghcr.io/iarnvidr/session-kit:<tag> / /
FROM scratch AS component
COPY --link LICENSE README.md /opt/malrunar/session-kit/
COPY --link bin/ /opt/malrunar/session-kit/bin/
COPY --link lib/ /opt/malrunar/session-kit/lib/
COPY --link share/ /opt/malrunar/session-kit/share/
COPY --link etc/ /etc/
