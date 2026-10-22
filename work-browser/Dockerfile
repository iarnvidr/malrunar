# work-browser: launcher scripts and a supervisord program for a persistent Firefox, as a component image.
#
# The image is FROM scratch and holds only:
#   /opt/malrunar/work-browser/bin/malrunar-work-browser        keeps Firefox running on a persistent profile
#   /opt/malrunar/work-browser/bin/malrunar-work-browser-open   opens a URL in the running browser
#   /opt/malrunar/work-browser/lib/common.sh                    shared functions (sourced by the two above)
#   /opt/malrunar/work-browser/share/                           examples for the consumer's own layers
#   /opt/malrunar/work-browser/README.md, LICENSE
#   /etc/supervisor/conf.d/malrunar-work-browser.conf           the supervisord program (the supervisor.d convention, CONVENTIONS.md)
# It contains scripts only and ships NO Firefox: it runs the unmodified official Firefox that
# desktop-base installs in /opt/malrunar/firefox (Mozilla's trademark rules), and no policies.json,
# distribution directory or preference file. Policies and user.js come from the consumer's own
# layers. It needs desktop-base (Firefox, dbus, X utilities, supervisor) and nothing else, in
# particular not node. Consumers copy it into their own image built on desktop-base:
#   COPY --link --from=ghcr.io/iarnvidr/work-browser:<tag> / /
# No apt, downloads or compilation take place, so there is nothing to pin beyond the recipe itself.
FROM scratch AS component
COPY --link LICENSE README.md /opt/malrunar/work-browser/
COPY --link bin/ /opt/malrunar/work-browser/bin/
COPY --link lib/ /opt/malrunar/work-browser/lib/
COPY --link share/ /opt/malrunar/work-browser/share/
COPY --link etc/ /etc/
