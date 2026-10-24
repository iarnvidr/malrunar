cmake_minimum_required(VERSION 3.28)
project(malrunar_obs_ffmpeg C)

# Build only the patched plugin against the exact pinned OBS source headers and
# the installed PPA libobs. The existing OBS executable, CEF and other plugins
# remain the pinned binaries; no replacement libobs is built or shipped.
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFMPEG REQUIRED IMPORTED_TARGET
  libavcodec libavfilter libavdevice libavutil libswscale libavformat libswresample
  libva libva-drm libpci libdrm srt librist)
find_library(LIBOBS NAMES obs libobs.so.0 REQUIRED)

set(OBS_DATA_PATH "share/obs")
set(OBS_PLUGIN_PATH "lib/x86_64-linux-gnu/obs-plugins")
set(OBS_PLUGIN_DESTINATION "lib/x86_64-linux-gnu/obs-plugins")
set(OBS_RELEASE_CANDIDATE 0)
set(OBS_BETA 0)
configure_file("${OBS_SOURCE}/libobs/obsconfig.h.in" "config/obsconfig.h")

set(plugin "${OBS_SOURCE}/plugins/obs-ffmpeg")
add_library(obs-ffmpeg MODULE
  "${plugin}/obs-ffmpeg-mpegts.c"
  "${plugin}/obs-ffmpeg-vaapi.c"
  "${plugin}/vaapi-utils.c"
  "${plugin}/obs-ffmpeg-audio-encoders.c"
  "${plugin}/obs-ffmpeg-av1.c"
  "${plugin}/obs-ffmpeg-openh264.c"
  "${plugin}/obs-ffmpeg-hls-mux.c"
  "${plugin}/obs-ffmpeg-mux.c"
  "${plugin}/obs-ffmpeg-output.c"
  "${plugin}/obs-ffmpeg-source.c"
  "${plugin}/obs-ffmpeg-video-encoders.c"
  "${plugin}/obs-ffmpeg.c"
  "${OBS_SOURCE}/shared/media-playback/media-playback/cache.c"
  "${OBS_SOURCE}/shared/media-playback/media-playback/decode.c"
  "${OBS_SOURCE}/shared/media-playback/media-playback/media-playback.c"
  "${OBS_SOURCE}/shared/media-playback/media-playback/media.c"
  "${OBS_SOURCE}/shared/opts-parser/opts-parser.c")
target_include_directories(obs-ffmpeg PRIVATE
  "${OBS_SOURCE}/libobs" "${CMAKE_CURRENT_BINARY_DIR}/config"
  "${OBS_SOURCE}/shared/media-playback" "${OBS_SOURCE}/shared/opts-parser")
target_compile_definitions(obs-ffmpeg PRIVATE _GNU_SOURCE NEW_MPEGTS_OUTPUT ENABLE_HEVC OBS_INSTALL_PREFIX="/usr")
target_link_libraries(obs-ffmpeg PRIVATE "${LIBOBS}" PkgConfig::FFMPEG m pthread)
target_link_options(obs-ffmpeg PRIVATE -Wl,-z,defs)
set_target_properties(obs-ffmpeg PROPERTIES PREFIX "" C_STANDARD 17)
