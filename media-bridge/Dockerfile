# media-bridge: a virtual camera and microphone fed by an MPEG-TS stream, as a component image.
#
# The image is FROM scratch and holds only /opt/malrunar/media-bridge:
#   bin/malrunar-media-bridge      UDP MPEG-TS -> ffmpeg -> v4l2loopback camera + PipeWire microphone
#   bin/malrunar-xrdp-audio-tune   xrdp audio settings and default speaker/microphone
#   bin/malrunar-media-profile     prints a browser policy limiting camera/microphone to given sites
#   host/setup-v4l2loopback.sh     creates the camera devices on the HOST (kernel module)
#   share/                         supervisord and systemd examples
#   README.md, LICENSE
# It contains scripts only; ffmpeg, pactl, pw-metadata and python3 come from desktop-base. Consumers
# copy it into their own image built on desktop-base:
#   COPY --link --from=ghcr.io/iarnvidr/media-bridge:<tag> / /
# No apt, downloads or compilation take place, so there is nothing to pin beyond the recipe itself.
FROM scratch AS component
COPY --link LICENSE README.md /opt/malrunar/media-bridge/
COPY --link bin/ /opt/malrunar/media-bridge/bin/
COPY --link host/ /opt/malrunar/media-bridge/host/
COPY --link share/ /opt/malrunar/media-bridge/share/
