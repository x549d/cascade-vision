"""Reproducible annotated-video comparisons and event-level scoring."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

import cv2
import numpy as np

from .config import CascadeConfig
from .events import CallBudget, JsonlEventWriter
from .pipeline import CascadePipeline


def load_manifest(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not data.get("clips"):
        raise ValueError("Manifest needs schema_version=1 and a non-empty clips list")
    labels = data.get("event_labels", [])
    if not labels or len(set(labels)) != len(labels) or any(not isinstance(x, str) or not x.strip() for x in labels):
        raise ValueError("Manifest needs unique non-empty event_labels")
    ids = set()
    for clip in data["clips"]:
        clip_id = clip.get("id", "")
        if not clip_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in clip_id
        ):
            raise ValueError("Clip IDs may contain only letters, numbers, underscores and hyphens")
        if clip_id in ids:
            raise ValueError("Clip IDs must be unique")
        ids.add(clip_id)
        if clip.get("footage_type") not in {"real", "synthetic"}:
            raise ValueError("Declare footage_type as real or synthetic for each clip")
        if not isinstance(clip.get("license"), str) or not clip["license"].strip():
            raise ValueError("Record the footage license/permission in each clip")
        clip["resolved_path"] = str((path.parent / clip["path"]).resolve())
        if not Path(clip["resolved_path"]).is_file():
            raise ValueError(f"Missing local video for clip {clip_id}")
        event_ids = set()
        for event in clip.get("events", []):
            start, end = event.get("start_s"), event.get("end_s")
            if (
                event.get("label") not in labels
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or not event.get("id")
                or event["id"] in event_ids
            ):
                raise ValueError(f"Invalid annotated event in clip {clip_id}")
            event_ids.add(event["id"])
    return data


def cluster_predictions(events, merge_gap_s=1.0):
    if not math.isfinite(merge_gap_s) or merge_gap_s < 0:
        raise ValueError("merge_gap_s must be finite and non-negative")
    points = defaultdict(list)
    for event in events:
        if (
            event.get("status") != "completed"
            or not event.get("applied", True)
            or not event.get("verdict", {}).get("is_valid_target", True)
        ):
            continue
        timestamp = event.get("media_timestamp_s")
        if timestamp is None or not math.isfinite(timestamp):
            continue
        for label in set(event["verdict"].get("observed_events", [])):
            points[label].append((timestamp, event.get("completion_elapsed_s")))
    clusters = []
    for label in sorted(points):
        current = None
        for timestamp, completed in sorted(points[label]):
            if current is None or timestamp - current["end_s"] > merge_gap_s:
                current = {"label": label, "start_s": timestamp, "end_s": timestamp, "points": []}
                clusters.append(current)
            current["end_s"] = timestamp
            current["points"].append((timestamp, completed))
    return clusters


def score_events(truth, events, duration_s, *, merge_gap_s=1.0, realtime=False):
    """Match same-label overlapping episodes one-to-one; maximize match count.

    A match requires at least one observed point inside the annotation, avoiding
    an apparent overlap created solely by bridging a gap between predictions.
    """
    predicted = cluster_predictions(events, merge_gap_s)
    candidates = []
    for prediction in predicted:
        candidates.append(
            [
                i
                for i, target in enumerate(truth)
                if target["label"] == prediction["label"]
                and any(target["start_s"] <= t <= target["end_s"] for t, _ in prediction["points"])
            ]
        )
    matched = {}

    def assign(index, seen):
        for target in candidates[index]:
            if target in seen:
                continue
            seen.add(target)
            if target not in matched or assign(matched[target], seen):
                matched[target] = index
                return True
        return False

    for index in range(len(predicted)):
        assign(index, set())
    tp, fp, fn = len(matched), len(predicted) - len(matched), len(truth) - len(matched)
    delays = []
    for target_index, prediction_index in matched.items():
        target = truth[target_index]
        completions = [
            done
            for t, done in predicted[prediction_index]["points"]
            if done is not None and target["start_s"] <= t <= target["end_s"]
        ]
        if realtime and completions:
            delays.append(max(0.0, min(completions) - target["start_s"]))
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "false_alarms_per_hour": fp * 3600 / duration_s if duration_s > 0 else None,
        "event_to_verdict_p50_s": float(np.percentile(delays, 50)) if delays else None,
        "event_to_verdict_p95_s": float(np.percentile(delays, 95)) if delays else None,
        "matched_events": [
            {"annotation_id": truth[t]["id"], "prediction_index": p} for t, p in sorted(matched.items())
        ],
        "predicted_episodes": len(predicted),
    }


def video_metadata(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as video:
        for block in iter(lambda: video.read(1024 * 1024), b""):
            digest.update(block)
    cap = cv2.VideoCapture(str(path))
    try:
        fps, frames = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or not math.isfinite(frames) or frames <= 0:
            raise ValueError("Evaluation requires a readable constant-frame-rate video with FPS and frame count")
        return {
            "sha256": digest.hexdigest(),
            "fps": fps,
            "frames": int(frames),
            "duration_s": frames / fps,
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        }
    finally:
        cap.release()


def run_benchmark(
    manifest_path,
    output_dir,
    config=None,
    *,
    strategies=("interval", "motion", "cascade"),
    max_api_calls=0,
    allow_api=False,
    realtime=False,
    merge_gap_s=1.0,
    pipeline_factory=None,
):
    """Compare policies on identical local clips; real calls require explicit opt-in."""
    config = config or CascadeConfig.from_env()
    if not config.mock_vlm and (not allow_api or max_api_calls <= 0):
        raise ValueError("Real benchmark requires --allow-api and a positive --max-api-calls total cap")
    if (
        not strategies
        or len(set(strategies)) != len(strategies)
        or set(strategies) - {"interval", "motion", "cascade", "full"}
    ):
        raise ValueError("Choose unique interval, motion, cascade, or full strategies")
    if not math.isfinite(merge_gap_s) or merge_gap_s < 0:
        raise ValueError("merge_gap_s must be finite and non-negative")
    manifest = load_manifest(manifest_path)
    # Validate every file and annotation before admitting any paid request.
    metadata_by_id = {clip["id"]: video_metadata(clip["resolved_path"]) for clip in manifest["clips"]}
    for clip in manifest["clips"]:
        if any(event["end_s"] > metadata_by_id[clip["id"]]["duration_s"] + 1e-6 for event in clip.get("events", [])):
            raise ValueError(f"Annotation exceeds video duration in clip {clip['id']}")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    budget = CallBudget(max_api_calls)
    from . import __version__

    versions = {}
    for package in ("numpy", "opencv-python-headless", "openai", "pydantic", "ultralytics"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    # Source paths, URLs, keys and base URLs are deliberately not copied to reports.
    public_config = config.to_dict()
    public_config.pop("openai_api_key", None)
    public_config.pop("openai_base_url", None)
    source_commit = None
    source_tree_dirty = None
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file():
        try:
            commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=True
            )
            source_commit = commit.stdout.strip()
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, timeout=2, check=True
            )
            source_tree_dirty = bool(status.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    report = {
        "source_commit": source_commit,
        "source_tree_dirty": source_tree_dirty,
        "opencv_runtime": cv2.__version__,
        "schema_version": 1,
        "version": __version__,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": versions,
        "config": public_config,
        "realtime": realtime,
        "merge_gap_s": merge_gap_s,
        "max_api_calls": max_api_calls,
        "runs": [],
        "scientific_claims_validated": False,
    }
    factory = pipeline_factory or CascadePipeline
    for clip in manifest["clips"]:
        info = metadata_by_id[clip["id"]]
        for strategy in strategies:
            event_path = output / f"{clip['id']}-{strategy}.jsonl"
            events = []
            with JsonlEventWriter(event_path) as writer:

                def collect(event, writer=writer, events=events):
                    writer.write(event)
                    events.append(event)

                run_config = config.with_overrides(strategy=strategy, event_labels=manifest["event_labels"])
                pipeline = factory(run_config, event_callback=collect, call_budget=budget)
                error = None
                try:
                    pipeline.run_stream(clip["resolved_path"], realtime=realtime)
                except (ValueError, RuntimeError, TimeoutError) as exc:
                    error = type(exc).__name__
                metrics = pipeline.get_metrics_summary()
            quality_flags = [
                key
                for key in (
                    "failed_tasks",
                    "dropped_tasks",
                    "cancelled_tasks",
                    "expired_tasks",
                    "pending_tasks",
                    "budget_rejected_tasks",
                    "event_export_errors",
                    "usage_missing_calls",
                )
                if metrics[key]
            ]
            if metrics["total_frames"] != info["frames"]:
                quality_flags.append("incomplete_video")
            if error:
                quality_flags.append("run_error")
            if clip["footage_type"] == "synthetic":
                quality_flags.append("synthetic_footage")
            if config.mock_vlm or pipeline.tracker._use_fallback:
                quality_flags.append("simulation")
            run = {
                "clip_id": clip["id"],
                "strategy": strategy,
                "video": info,
                "license": clip["license"],
                "footage_type": clip["footage_type"],
                "events_file": event_path.name,
                "metrics": metrics,
                "error_type": error,
                "quality_flags": quality_flags,
                "comparison_ready": not quality_flags,
                "scores": score_events(
                    clip.get("events", []), events, info["duration_s"], merge_gap_s=merge_gap_s, realtime=realtime
                ),
            }
            report["runs"].append(run)
            (output / "benchmark.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
            )
    return report
