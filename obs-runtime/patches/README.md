# Custom FFmpeg recording-output teardown

OBS 32.2.0 `ffmpeg_output_full_stop()` calls `obs_output_end_data_capture()` and
then immediately frees the FFmpeg encoder contexts. Capture disconnection runs
on `end_data_capture_thread`, so a raw-video callback can still be inside
`avcodec_send_frame()` while its encoder is freed. Repeated take-mode canvas
changes exposed x264 assertions, websocket disconnects and active-output errors.

The patch connects a native `deactivate` signal barrier before requesting capture
shutdown. That signal is emitted after the raw-video and raw-audio callbacks are
disconnected. Only then may FFmpeg stop its writer and free its contexts. There
is no teardown sleep or retry. This fixes the custom recording output; all other
OBS output implementations retain their upstream behavior.

The image rebuilds only `obs-ffmpeg.so` from the existing, SHA256-pinned OBS source
archive, using the pinned PPA libobs and snapshot FFmpeg libraries. The OBS
executable, libobs, CEF and remaining plugins stay unchanged. The source offer
already mirrors that original archive; this patch and its build recipe are also
installed under `/usr/local/share/malrunar/obs-runtime/`.
