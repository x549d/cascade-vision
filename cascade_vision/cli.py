"""CLI and deterministic offline synthetic stream generator."""

from __future__ import annotations

import argparse
import json
import platform
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, Iterator

import cv2
import numpy as np
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import CascadeConfig
from .evaluation import run_benchmark
from .events import JsonlEventWriter
from .pipeline import CascadePipeline
from .vlm_judge import SemanticVerdict

console = Console()


def generate_synthetic_stream(
    total_frames: int = 300,
    width: int = 640,
    height: int = 480,
    seed: int = 42,
) -> Iterator[np.ndarray]:
    """Generate a 300-frame 640x480 synthetic test stream using NumPy and OpenCV.

    Timeline (for default 300 frames):
    - Frames 0..99 (first 100 frames): Static background scene to test Stage 1 MOG2 interception.
    - Frames 100..249 (middle 150 frames): Moving targets with sensor noise to test Stage 2
      tracking and Stage 3 asynchronous VLM triggers.
    - Frames 250..299 (final 50 frames): Returns to static background.
    """
    rng = np.random.default_rng(seed)
    base_bg = np.full((height, width, 3), 36, dtype=np.uint8)
    # Draw static corridor floor & reference lines
    floor_y = int(height * 0.70)
    cv2.rectangle(base_bg, (0, floor_y), (width, height), (52, 52, 52), -1)
    cv2.line(base_bg, (0, floor_y), (width, floor_y), (82, 82, 82), 2)

    static_head = min(100, total_frames // 3)
    active_span = min(150, max(1, total_frames - static_head - (total_frames // 6)))
    active_start = static_head
    active_end = active_start + active_span  # 100..250 for 300 frames

    for idx in range(total_frames):
        frame = base_bg.copy()

        if active_start <= idx < active_end:
            # Add subtle sensor noise during active motion window
            noise = rng.integers(-4, 5, size=(height, width, 1), dtype=np.int16)
            frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)

            rel = idx - active_start
            progress = rel / float(max(1, active_span - 1))

            # Moving Target 1: Priority "person" silhouette crossing left -> right
            px = int(50 + progress * (width - 160))
            py = int(height * 0.40)
            cv2.rectangle(frame, (px, py), (px + 48, py + 125), (30, 45, 220), -1)
            cv2.circle(frame, (px + 24, py - 18), 16, (30, 45, 220), -1)

            # Moving Target 2: Ambiguous / interacting "backpack" appearing in second half of active window
            if rel >= (active_span // 2):
                sub_prog = (rel - (active_span // 2)) / float(max(1, active_span // 2))
                bx = int(width - 110 - sub_prog * (width - 220))
                by = int(height * 0.52)
                cv2.rectangle(frame, (bx, by), (bx + 50, by + 54), (20, 210, 240), -1)

        yield frame


def print_efficiency_report(
    summary: Dict[str, Any],
    verdict_cache: Dict[int, SemanticVerdict],
    mock_mode: bool,
) -> None:
    """Clearly distinguish measured usage, simulation and hypothetical estimates."""
    table = Table(title="Cascade Vision — OFFLINE SIMULATION" if mock_mode else "Cascade Vision — API run")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for key in (
        "total_frames",
        "stage1_intercepted",
        "stage2_flagged_tasks",
        "vlm_calls",
        "vlm_completed_calls",
        "failed_tasks",
        "dropped_tasks",
        "cancelled_tasks",
        "pending_tasks",
        "expired_tasks",
        "budget_rejected_tasks",
        "event_export_errors",
        "reconnects",
        "simulated_calls",
        "observed_prompt_tokens",
        "observed_completion_tokens",
        "usage_missing_calls",
        "actual_tokens",
        "estimated_cost_usd",
        "average_fps",
        "mean_audit_latency_ms",
        "queue_wait_p95_ms",
        "request_e2e_p95_ms",
    ):
        value = summary[key]
        table.add_row(key, "unknown" if value is None else str(value))
    console.print(table)
    console.print("FPS includes final drain; queue/e2e percentiles use the last 4096 attempts.")
    console.print("Image-only estimates (exclude text/output; do not measure event recall):")
    console.print({key: summary[key] for key in ("estimated_low_image_tokens", "hypothetical_full_frame_low_tokens")})
    if mock_mode:
        console.print("SIMULATED verdicts; no real YOLO or VLM accuracy is measured by the demo.")
    for track_id, verdict in sorted(verdict_cache.items()):
        console.print({"track_id": track_id, "verdict": verdict.model_dump()})


def main() -> None:
    parser = argparse.ArgumentParser(description="Cascaded motion + YOLO + structured VLM video inspection")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("demo", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("--max-frames", "-n", type=int, default=300)
        sub.add_argument(
            "--mock-vlm",
            action="store_true",
            default=None,
            help="Explicitly simulate VLM responses; demo always simulates",
        )
        sub.add_argument("--headless", action="store_true", help="Use default terminal-only mode")
        sub.add_argument("--show-window", action="store_true", help="Requires OpenCV with GUI support")
        sub.add_argument("--report-json", type=Path, help="Save metrics and execution metadata")
        if command == "run":
            sub.add_argument("--source", "-s", default="0")
            sub.add_argument("--conf-min", type=float, default=None)
            sub.add_argument("--yolo-model", default=None, help="YOLO weights path, or mock for synthetic contours")
        sub.add_argument("--events-jsonl", type=Path, help="Write per-task events and verdicts")
        sub.add_argument("--max-api-calls", type=int, default=None, help="Cap total VLM attempts (including retries)")
        sub.add_argument("--event-labels", help="Comma-separated observable event vocabulary")
        sub.add_argument("--realtime", action="store_true", help="Pace local video at source FPS")
    benchmark = commands.add_parser("benchmark", help="Compare policies on annotated local clips")
    benchmark.add_argument("--manifest", type=Path, required=True)
    benchmark.add_argument("--output-dir", type=Path, required=True)
    benchmark.add_argument("--strategies", default="interval,motion,cascade")
    benchmark.add_argument("--mock-vlm", action="store_true", default=None)
    benchmark.add_argument("--yolo-model", default=None)
    benchmark.add_argument("--allow-api", action="store_true", help="Explicitly allow paid provider requests")
    benchmark.add_argument("--max-api-calls", type=int, default=0, help="Total cap shared by all clips/policies")
    benchmark.add_argument("--realtime", action="store_true")
    benchmark.add_argument("--merge-gap-s", type=float, default=1.0)
    args = parser.parse_args()
    if args.command == "benchmark":
        try:
            config = CascadeConfig.from_env(mock_vlm=args.mock_vlm, yolo_model=args.yolo_model)
            report = run_benchmark(
                args.manifest,
                args.output_dir,
                config,
                strategies=tuple(args.strategies.split(",")),
                max_api_calls=args.max_api_calls,
                allow_api=args.allow_api,
                realtime=args.realtime,
                merge_gap_s=args.merge_gap_s,
            )
            console.print(
                {
                    "report": str(args.output_dir / "benchmark.json"),
                    "runs": [
                        {
                            "clip": row["clip_id"],
                            "strategy": row["strategy"],
                            "quality_flags": row["quality_flags"],
                            "scores": row["scores"],
                        }
                        for row in report["runs"]
                    ],
                }
            )
            if any(set(row["quality_flags"]) - {"simulation", "synthetic_footage"} for row in report["runs"]):
                raise SystemExit(1)
        except (ValueError, RuntimeError, OSError) as exc:
            parser.exit(1, f"cascade-vision benchmark: {type(exc).__name__}: {exc}\n")
        return
    if args.max_frames < (30 if args.command == "demo" else 1):
        parser.error("max-frames must be >= 30 for demo, or >= 1 for run")
    if args.headless and args.show_window:
        parser.error("--headless and --show-window cannot be combined")
    try:
        config = CascadeConfig.from_env(
            mock_vlm=True if args.command == "demo" else args.mock_vlm,
            yolo_model="mock" if args.command == "demo" else args.yolo_model,
            conf_min=None if args.command == "demo" else args.conf_min,
            max_vlm_calls=args.max_api_calls,
            event_labels=args.event_labels.split(",") if args.event_labels else None,
        )
        with ExitStack() as stack:
            writer = stack.enter_context(JsonlEventWriter(args.events_jsonl)) if args.events_jsonl else None
            pipeline = CascadePipeline(config, event_callback=writer.write if writer else None)
            source = generate_synthetic_stream(total_frames=args.max_frames) if args.command == "demo" else args.source
            try:
                pipeline.run_stream(
                    source,
                    headless=not args.show_window,
                    max_frames=args.max_frames,
                    realtime=args.realtime,
                    source_fps=30 if args.command == "demo" else None,
                )
            finally:
                summary = pipeline.get_metrics_summary()
                print_efficiency_report(summary, pipeline.verdict_cache, pipeline.vlm_judge.mock)
                if args.report_json:
                    document = {
                        "schema_version": 2,
                        "version": __version__,
                        "python": platform.python_version(),
                        "platform": platform.platform(),
                        "model": config.openai_model,
                        "detail": config.vlm_detail,
                        "mock_vlm": pipeline.vlm_judge.mock,
                        "mock_detector": pipeline.tracker._use_fallback,
                        "metrics": summary,
                    }
                    args.report_json.parent.mkdir(parents=True, exist_ok=True)
                    args.report_json.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            if any(
                summary[key]
                for key in (
                    "failed_tasks",
                    "cancelled_tasks",
                    "expired_tasks",
                    "dropped_tasks",
                    "budget_rejected_tasks",
                    "event_export_errors",
                )
            ):
                raise SystemExit(1)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        parser.exit(1, f"cascade-vision: {exc}\n")


if __name__ == "__main__":
    main()
