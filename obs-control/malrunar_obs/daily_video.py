# SPDX-License-Identifier: Apache-2.0
"""Select quiet, low-motion spans and assemble the automatic b-roll video."""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

CROSSFADE_SECONDS = 0.35


def usable_duration(sources):
    """Duration after the joins, so capture collects enough footage for the loop."""
    spans = [span for _path, source_spans in sources for span in source_spans]
    return max(0.0, sum(end - start for start, end in spans) - CROSSFADE_SECONDS * max(0, len(spans) - 1))


def exclude_voice_activity(spans, markers, *, minimum=3.0, padding=1.0):
    """Remove speech measured before the mic gate, including when the mic is muted."""
    remaining = list(spans)
    for at in sorted(markers):
        blocked_start, blocked_end = max(0, at - padding), at + padding
        next_spans = []
        for start, end in remaining:
            if blocked_end <= start or blocked_start >= end:
                next_spans.append((start, end))
            else:
                if start < blocked_start:
                    next_spans.append((start, min(end, blocked_start)))
                if end > blocked_end:
                    next_spans.append((max(start, blocked_end), end))
        remaining = next_spans
    return [(start, end) for start, end in remaining if end - start >= minimum]


async def _ffmpeg_output(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise
    output = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(output.strip() or "FFmpeg failed")
    return output


async def _duration(path: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())
    return float(stdout.decode().strip())


def _intersect(
    left: list[tuple[float, float]],
    right: list[tuple[float, float]],
    minimum: float,
) -> list[tuple[float, float]]:
    result = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            start, end = max(left_start, right_start), min(left_end, right_end)
            if end - start >= minimum:
                result.append((start, end))
    return result


async def find_clean_spans(
    path: Path,
    *,
    silence_db: float,
    motion_threshold: float,
    minimum: float,
) -> list[tuple[float, float]]:
    """Return spans that are both quiet and visually steady."""
    duration = await _duration(path)
    silence_log = await _ffmpeg_output(
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={silence_db}dB:d=1.5",
        "-vn",
        "-f",
        "null",
        "-",
    )
    quiet = []
    silence_start = None
    for line in silence_log.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if start_match:
            silence_start = float(start_match.group(1))
        elif end_match and silence_start is not None:
            start = silence_start + 0.75
            end = min(duration, float(end_match.group(1))) - 0.75
            if end - start >= minimum:
                quiet.append((start, end))
            silence_start = None
    if silence_start is not None:
        start, end = silence_start + 0.75, duration - 0.75
        if end - start >= minimum:
            quiet.append((start, end))

    motion_log = await _ffmpeg_output(
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-vf",
        "scale=320:-2,fps=2,select='gte(scene,0)',metadata=print",
        "-an",
        "-f",
        "null",
        "-",
    )
    samples = []
    pending_time = None
    for line in motion_log.splitlines():
        time_match = re.search(r"pts_time:([0-9.]+)", line)
        score_match = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if time_match:
            pending_time = float(time_match.group(1))
        elif score_match and pending_time is not None:
            samples.append((pending_time, float(score_match.group(1))))
            pending_time = None

    steady = []
    stable_start = None
    stable_end = None
    for sample_time, score in samples:
        if score <= motion_threshold:
            if stable_start is None:
                stable_start = sample_time
            stable_end = min(duration, sample_time + 0.5)
        elif stable_start is not None:
            if stable_end - stable_start >= minimum:
                steady.append((stable_start, stable_end))
            stable_start = stable_end = None
    if stable_start is not None and stable_end - stable_start >= minimum:
        steady.append((stable_start, stable_end))

    return _intersect(quiet, steady, minimum)


async def sample_audio_db(path: Path, *, window: float = 0.5) -> list[tuple[float, float]]:
    """Per-window RMS level in dB over the whole file (not just silence spans).

    silencedetect() above only tells you whether a moment crossed a threshold
    you already picked; this samples the actual envelope so a threshold can be
    picked FROM data instead of guessed. Windowing is done with asetnsamples
    (per-output-frame reset) rather than a fixed-rate filter, since astats has
    no periodic-print mode of its own -- mirrors find_clean_spans' own
    scale+fps sampling trick for motion, below.
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-af",
        f"asetnsamples=n={int(48000 * window)}:p=0,"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-vn",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    samples = []
    pending_time = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        time_match = re.search(r"pts_time:([0-9.]+)", line)
        db_match = re.search(r"RMS_level=(-?[0-9.]+|-inf)", line)
        if time_match:
            pending_time = float(time_match.group(1))
        elif db_match and pending_time is not None:
            raw = db_match.group(1)
            samples.append((pending_time, float("-120") if raw == "-inf" else float(raw)))
            pending_time = None
    return samples


async def sample_motion_scores(path: Path) -> list[tuple[float, float]]:
    """The exact motion-sampling half of find_clean_spans, exposed standalone
    so a diagnostic can see the raw samples find_clean_spans only ever
    reduces to pass/fail."""
    motion_log = await _ffmpeg_output(
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-vf",
        "scale=320:-2,fps=2,select='gte(scene,0)',metadata=print",
        "-an",
        "-f",
        "null",
        "-",
    )
    samples = []
    pending_time = None
    for line in motion_log.splitlines():
        time_match = re.search(r"pts_time:([0-9.]+)", line)
        score_match = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if time_match:
            pending_time = float(time_match.group(1))
        elif score_match and pending_time is not None:
            samples.append((pending_time, float(score_match.group(1))))
            pending_time = None
    return samples


def _histogram(values: list[float], *, buckets: int = 10) -> str:
    if not values:
        return "  (no samples)"
    lo, hi = min(values), max(values)
    if lo == hi:
        return f"  all {len(values)} samples == {lo:.4f}"
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for v in values:
        idx = min(buckets - 1, int((v - lo) / width))
        counts[idx] += 1
    peak = max(counts) or 1
    lines = []
    for i, count in enumerate(counts):
        lo_edge = lo + i * width
        bar = "#" * int(40 * count / peak)
        lines.append(f"  [{lo_edge:8.3f}, {lo_edge + width:8.3f}) {count:5d} {bar}")
    return "\n".join(lines)


def _longest_run(
    times_values: list[tuple[float, float]],
    *,
    threshold: float,
    step: float,
) -> float:
    """Longest contiguous run of samples at or below threshold, in the same
    units find_clean_spans' own stable_start/stable_end tracking uses (a run
    extends by one sample step for each qualifying sample). Both audio
    quietness and motion steadiness are "value <= threshold" in this
    codebase, so one comparison serves both callers below."""
    best = current = 0.0
    for _, value in times_values:
        if value <= threshold:
            current += step
            best = max(best, current)
        else:
            current = 0.0
    return best


async def diagnose(path: Path, *, minimum: float) -> None:
    """Print the real distribution behind a 0:00-usable result, and what the
    longest qualifying run would be at a spread of candidate thresholds --
    so thresholds get picked from this file's actual data, not guessed."""
    duration = await _duration(path)
    print(f"file: {path}")
    print(f"duration: {duration:.1f}s")
    print()

    audio = await sample_audio_db(path)
    print(f"=== audio RMS level (dB), {len(audio)} windows of 0.5s ===")
    print(_histogram([db for _, db in audio]))
    if audio:
        print(f"  min={min(db for _, db in audio):.2f}dB  "
              f"max={max(db for _, db in audio):.2f}dB  "
              f"median={sorted(db for _, db in audio)[len(audio)//2]:.2f}dB")
    print()

    motion = await sample_motion_scores(path)
    print(f"=== scene-change score, {len(motion)} windows of 0.5s ===")
    print(_histogram([s for _, s in motion]))
    if motion:
        print(f"  min={min(s for _, s in motion):.5f}  "
              f"max={max(s for _, s in motion):.5f}  "
              f"median={sorted(s for _, s in motion)[len(motion)//2]:.5f}")
    print()

    print(f"=== longest qualifying run at candidate thresholds (need >= {minimum:.1f}s) ===")
    audio_candidates = sorted({round(v, -1) for _, v in audio} | {-38.0}) if audio else [-38.0]
    motion_candidates = sorted({round(v, 3) for _, v in motion} | {0.025}) if motion else [0.025]
    # Full cross product is noisy; report each axis independently held at the
    # codebase's current default, plus the actual default pair, since that
    # isolates "audio never qualifies" from "motion never qualifies" from
    # "neither alone is the problem, only their intersection is."
    print("-- audio dB threshold, motion held at current default (0.025) --")
    for db in audio_candidates:
        run = _longest_run(audio, threshold=db, step=0.5)
        flag = " <-- current default" if db == -38.0 else ""
        print(f"  silence_db={db:7.1f}  longest_quiet_run={run:6.1f}s{flag}")
    print("-- motion threshold, audio held at current default (-38dB) --")
    for mt in motion_candidates:
        run = _longest_run(motion, threshold=mt, step=0.5)
        flag = " <-- current default" if mt == 0.025 else ""
        print(f"  motion_threshold={mt:7.4f}  longest_steady_run={run:6.1f}s{flag}")
    print()
    current = await find_clean_spans(path, silence_db=-38.0, motion_threshold=0.025, minimum=minimum)
    total = sum(end - start for start, end in current)
    print(f"=== find_clean_spans() at current defaults: {len(current)} span(s), {total:.1f}s total ===")
    for start, end in current:
        print(f"  {start:7.1f}s - {end:7.1f}s  ({end - start:.1f}s)")


async def assemble(
    sources: list[tuple[Path, list[tuple[float, float]]]],
    final_path: Path,
    *,
    target_seconds: int,
    size: tuple[int, int] = (640, 360),
) -> dict:
    """Build an MP4 from the longest clean spans, or report a shortfall."""
    candidates = [(path, start, end) for path, spans in sources for start, end in spans]
    candidates.sort(key=lambda item: item[2] - item[1], reverse=True)
    selected = []
    selected_seconds = 0.0
    transition = CROSSFADE_SECONDS
    for path, start, end in candidates:
        effective = selected_seconds - transition * max(0, len(selected) - 1)
        remaining = target_seconds - effective
        if remaining <= 0:
            break
        # Every additional span overlaps the preceding one during the subtle
        # dissolve, so retain enough material to keep the finished duration.
        end = min(end, start + remaining + (transition if selected else 0))
        if end - start >= 2:
            selected.append((path, start, end))
            selected_seconds += end - start
    effective_seconds = selected_seconds - transition * max(0, len(selected) - 1)
    if effective_seconds < target_seconds - 0.05:
        return {"ready": False, "usable_seconds": int(effective_seconds)}
    selected.sort(key=lambda item: (item[0].name, item[1]))

    final_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = final_path.with_name(f".{final_path.stem}.partial.mp4")
    command = ["-y", "-hide_banner", "-loglevel", "error"]
    for path, _start, _end in selected:
        command.extend(["-fflags", "+genpts+discardcorrupt", "-i", str(path)])
    filters = []
    for index, (_path, start, end) in enumerate(selected):
        filters.append(
            f"[{index}:v]trim=start={start:.3f}:end={end:.3f},"
            "setpts=PTS-STARTPTS,"
            f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
            f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{index}]"
        )
        filters.append(
            f"[{index}:a]atrim=start={start:.3f}:end={end:.3f},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
    if len(selected) == 1:
        filters.extend(["[v0]null[outv]", "[a0]anull[outa]"])
    else:
        running_duration = selected[0][2] - selected[0][1]
        video_label, audio_label = "v0", "a0"
        for index in range(1, len(selected)):
            output_video = "outv" if index == len(selected) - 1 else f"vx{index}"
            output_audio = "outa" if index == len(selected) - 1 else f"ax{index}"
            offset = running_duration - transition
            filters.append(
                f"[{video_label}][v{index}]xfade=transition=fade:"
                f"duration={transition}:offset={offset:.3f}[{output_video}]"
            )
            filters.append(
                f"[{audio_label}][a{index}]acrossfade=d={transition}:"
                f"c1=tri:c2=tri[{output_audio}]"
            )
            running_duration += selected[index][2] - selected[index][1] - transition
            video_label, audio_label = output_video, output_audio
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-t",
            str(target_seconds),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-r",
            "24",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(partial_path),
        ]
    )
    try:
        await _ffmpeg_output(*command)
        os.replace(partial_path, final_path)
    finally:
        partial_path.unlink(missing_ok=True)
    return {
        "ready": True,
        "usable_seconds": int(effective_seconds),
        "segments": len(selected),
    }


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose a '0:00 usable' b-roll result: dump the real "
            "audio-dB and scene-score distribution for a recorded chunk, and "
            "what the longest qualifying run would be at a spread of "
            "candidate thresholds, instead of guessing new defaults blind."
        )
    )
    parser.add_argument("path", type=Path, help="A .ts/.mp4 chunk to analyze")
    parser.add_argument(
        "--minimum",
        type=float,
        default=6.0,
        help="[broll] min_segment_seconds equivalent (default: 6.0)",
    )
    args = parser.parse_args()
    if not args.path.is_file():
        print(f"Not a file: {args.path}", file=sys.stderr)
        return 1
    asyncio.run(diagnose(args.path, minimum=args.minimum))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
