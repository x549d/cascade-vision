"""Offline visual-change sampling for the Video AI Saver Codex plugin."""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
SCORE_WIDTH, SCORE_HEIGHT = 160, 90
MAX_EDGE = 960
CHANGE_WINDOW_S = 2.0
MIN_GAP_S = 1.0


@dataclass(frozen=True)
class Candidate:
    index: int
    seconds: float
    score: float
    jpeg: bytes


def timestamp(seconds):
    minutes, millis = divmod(round(seconds * 1000), 60000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{millis / 1000:06.3f}"


def jpeg(frame):
    height, width = frame.shape[:2]
    scale = min(1.0, MAX_EDGE / max(height, width))
    if scale < 1:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 86])
    if not ok:
        raise ValueError("Could not encode a preview frame.")
    return encoded.tobytes()


def scan(cap, sample_fps, max_frames, min_change):
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("The video has no readable frame rate; try exporting it as MP4.")

    # Use the best change per two-second window and retain a bounded candidate pool.
    # A later spacing check avoids filling the output with nearby duplicates.
    pool = []
    baseline = best = previous = None
    origin = None
    window = -1
    last_s = -1.0
    next_sample = 0.0
    frame_count = sample_count = 0
    step = 1 / min(fps, sample_fps)

    def keep(candidate):
        if candidate is not None:
            heapq.heappush(pool, (candidate.score, -candidate.index, candidate))
            if len(pool) > 4 * max_frames:
                heapq.heappop(pool)

    while cap.grab():
        index = frame_count
        frame_count += 1
        pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        if origin is None and math.isfinite(pts) and pts >= 0:
            origin = pts
        media_s = pts - origin if origin is not None else float("nan")
        if not math.isfinite(media_s) or media_s <= last_s:
            media_s = max(index / fps, last_s + 1 / fps) if index else 0.0
        last_s = media_s
        if media_s + 1e-7 < next_sample:
            continue
        # Use a time grid so fractional frame rates don't gradually shift the samples.
        next_sample = (math.floor((media_s + 1e-7) / step) + 1) * step
        ok, frame = cap.retrieve()
        if not ok or frame is None or not frame.size:
            continue
        sample_count += 1

        small = cv2.resize(frame, (SCORE_WIDTH, SCORE_HEIGHT), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (5, 5), 0)
        if baseline is None:
            baseline = Candidate(index, media_s, 0.0, jpeg(frame))
        else:
            new_window = int(media_s / CHANGE_WINDOW_S)
            if new_window != window:
                keep(best)
                best, window = None, new_window
            # Color changes matter even when two slides have a similar gray level.
            delta = cv2.absdiff(previous, small)
            score = float(np.count_nonzero(delta.max(axis=2) >= 22)) / (SCORE_WIDTH * SCORE_HEIGHT)
            if score >= min_change and (best is None or score > best.score):
                best = Candidate(index, media_s, score, jpeg(frame))
        previous = small

    if baseline is None:
        raise ValueError("No frames could be decoded from the video.")
    keep(best)
    selected = [baseline] if max_frames > 1 else []
    for _, _, candidate in sorted(pool, reverse=True):
        if len(selected) == max_frames:
            break
        if all(abs(candidate.seconds - frame.seconds) >= MIN_GAP_S for frame in selected if frame.score):
            selected.append(candidate)
    return sorted(selected or [baseline], key=lambda frame: frame.index), {
        "duration_seconds_estimate": round(last_s + 1 / fps, 3),
        "source_fps": round(fps, 3),
        "frames_read": frame_count,
        "frames_sampled": sample_count,
    }


def contact_sheet(frames):
    cols = min(4, math.ceil(math.sqrt(len(frames))))
    rows = math.ceil(len(frames) / cols)
    tile_w, image_h, label_h = 300, 169, 29
    tile_h = image_h + label_h
    sheet = np.full((rows * tile_h, cols * tile_w, 3), (25, 29, 32), dtype=np.uint8)
    for i, frame in enumerate(frames):
        image = cv2.imdecode(np.frombuffer(frame.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        h, w = image.shape[:2]
        scale = min(tile_w / w, image_h / h, 1.0)
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        x, y = (i % cols) * tile_w, (i // cols) * tile_h
        left, top = x + (tile_w - size[0]) // 2, y + (image_h - size[1]) // 2
        sheet[top : top + size[1], left : left + size[0]] = image
        label = f"{i + 1:02d}  {timestamp(frame.seconds)}"
        cv2.putText(sheet, label, (x + 8, y + image_h + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (242, 245, 245), 1)
    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("Could not encode the contact sheet.")
    return encoded.tobytes()


def write_outputs(root, source, frames, stats, args):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="triage-", dir=root))
    (output / "frames").mkdir()
    records = []
    for i, frame in enumerate(frames, start=1):
        name = f"frames/frame-{i:02d}.jpg"
        (output / name).write_bytes(frame.jpeg)
        records.append(
            {
                "file": name,
                "frame_index": frame.index,
                "time_seconds": round(frame.seconds, 3),
                "timestamp": timestamp(frame.seconds),
                "reason": "visual_change" if frame.score else "opening_context",
                "change_ratio": round(frame.score, 4),
            }
        )
    (output / "contact-sheet.jpg").write_bytes(contact_sheet(frames))
    manifest = {
        "schema_version": 1,
        "source_file": source.name,
        **stats,
        "sample_fps_requested": args.sample_fps,
        "min_change_ratio": args.min_change,
        "max_frames": args.max_frames,
        "contact_sheet": "contact-sheet.jpg",
        "frames": records,
    }
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest": str(path), "contact_sheet": str(output / "contact-sheet.jpg"), "selected_frames": len(frames)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="Path to one local MP4, MOV, M4V, MKV, WebM, or AVI file")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/video-triage"))
    parser.add_argument("--max-frames", type=int, choices=range(1, 37), default=12, metavar="1-36")
    parser.add_argument("--sample-fps", type=float, default=2.0, help="Frames per second to score (0.25-12)")
    parser.add_argument(
        "--min-change", type=float, default=0.015, help="Minimum changed-pixel ratio (0-1, exclusive of 0)"
    )
    args = parser.parse_args()
    if not 0.25 <= args.sample_fps <= 12:
        parser.error("--sample-fps must be between 0.25 and 12")
    if not 0 < args.min_change <= 1:
        parser.error("--min-change must be greater than 0 and at most 1")
    local_file_error = "Provide an existing local MP4, MOV, M4V, MKV, WebM, or AVI file."
    if "://" in args.video:
        parser.error(local_file_error)
    try:
        source = Path(args.video).resolve()
        if source.suffix.lower() not in VIDEO_EXTENSIONS or not source.is_file():
            parser.error(local_file_error)
    except (OSError, RuntimeError):
        parser.error(local_file_error)

    global cv2, np
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        parser.exit(1, f"Install the local dependencies: python -m pip install opencv-python-headless numpy\n{exc}\n")

    # Restrict FFmpeg to ordinary video containers and local files even when the
    # extension is misleading. This setting only affects the standalone process.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file|format_whitelist;mov,avi,matroska,webm"
    try:
        cap = cv2.VideoCapture(str(source), cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                raise ValueError("Could not open the video; check that the file is readable and has a supported codec.")
            frames, stats = scan(cap, args.sample_fps, args.max_frames, args.min_change)
        finally:
            cap.release()
        result = write_outputs(args.output_dir, source, frames, stats, args)
    except (ValueError, OSError, cv2.error) as exc:
        parser.exit(1, f"Video triage failed: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    sys.exit(main())
