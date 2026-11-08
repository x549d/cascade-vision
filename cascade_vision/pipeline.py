"""Bounded asynchronous video pipeline with measured usage and explicit failures."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import cv2
import numpy as np

from .config import CascadeConfig
from .detector import CascadeTracker, Stage2Result, TrackedObject, VLMTask
from .motion import MotionGate, encode_roi_base64
from .vlm_judge import SemanticVerdict, VLMAuditError, VLMJudge

logger = logging.getLogger(__name__)


@dataclass
class TokenAuditReport:
    """Measured API usage and a separately labelled low-detail image estimate."""

    total_frames: int = 0
    stage1_passed: int = 0
    stage1_intercepted: int = 0
    stage2_intercepted: int = 0
    stage2_flagged_tasks: int = 0
    vlm_calls: int = 0
    vlm_completed_calls: int = 0
    failed_tasks: int = 0
    dropped_tasks: int = 0
    cancelled_tasks: int = 0
    pending_tasks: int = 0
    simulated_calls: int = 0
    observed_prompt_tokens: int = 0
    observed_completion_tokens: int = 0
    usage_missing_calls: int = 0
    actual_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    estimated_low_image_tokens: Optional[int] = None
    hypothetical_full_frame_low_tokens: Optional[int] = None
    stage1_filter_rate_pct: float = 0.0
    elapsed_seconds: float = 0.0
    average_fps: float = 0.0
    mean_audit_latency_ms: float = 0.0
    max_audit_latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CascadePipeline:
    """Producer-Consumer asynchronous three-stage cascaded video analytics pipeline."""

    def __init__(
        self,
        config: Optional[CascadeConfig] = None,
        *,
        mock_vlm: Optional[bool] = None,
        motion_gate: Optional[MotionGate] = None,
        tracker: Optional[CascadeTracker] = None,
        vlm_judge: Optional[VLMJudge] = None,
    ) -> None:
        self.config = config or CascadeConfig()
        if mock_vlm is not None:
            self.config = self.config.with_overrides(mock_vlm=mock_vlm)

        self.vlm_judge = vlm_judge or VLMJudge(
            self.config,
            mock=self.config.mock_vlm if self.config.mock_vlm else None,
        )

        self.motion_gate = motion_gate or MotionGate(self.config)
        self.tracker = tracker or CascadeTracker(self.config)

        # Bounded task queue & thread-safe verdict cache indexed by track_id
        self.task_queue: queue.Queue[Optional[VLMTask]] = queue.Queue(maxsize=self.config.max_queue_size)
        self.verdict_cache: Dict[int, SemanticVerdict] = {}
        # Alias for backward compatibility
        self.vlm_results = self.verdict_cache

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._verdict_frames: Dict[int, int] = {}
        self._workers: List[threading.Thread] = []

        # Funnel telemetry counters
        self.total_frames: int = 0
        self.stage1_passed: int = 0
        self.stage1_intercepted: int = 0
        self.stage2_intercepted: int = 0
        self.stage2_flagged_tasks: int = 0
        self.vlm_completed_calls: int = 0
        self.vlm_calls = 0
        self.failed_tasks = 0
        self.cancelled_tasks = 0
        self.simulated_calls = 0
        self.observed_prompt_tokens = 0
        self.observed_completion_tokens = 0
        self.usage_missing_calls = 0
        self._audit_latency_total = 0.0
        self._audit_latency_max = 0.0
        self.dropped_tasks: int = 0
        self._start_time: float = time.perf_counter()
        self._end_time: Optional[float] = None

    def start_workers(self) -> None:
        """Start consumers, refusing to reuse a pipeline that is still shutting down."""
        alive = [worker for worker in self._workers if worker.is_alive()]
        if alive:
            if self._stop_event.is_set():
                raise RuntimeError("Previous VLM workers are still stopping")
            return
        self._stop_event.clear()
        self._workers = []
        for idx in range(self.config.worker_threads):
            worker = threading.Thread(target=self._consumer_loop, name=f"cascade-vlm-{idx}", daemon=True)
            worker.start()
            self._workers.append(worker)

    def stop_workers(self, drain: bool = True, timeout: float = 30.0) -> None:
        """Drain both queued and in-flight tasks within one total deadline.

        Python cannot cancel an in-flight network request. If it outlives the
        deadline, retain its handle and raise rather than claiming clean shutdown.
        """
        deadline = time.perf_counter() + max(0.0, timeout)
        if drain:
            with self.task_queue.all_tasks_done:
                while self.task_queue.unfinished_tasks:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    self.task_queue.all_tasks_done.wait(remaining)
        self._stop_event.set()
        while True:
            try:
                self.task_queue.get_nowait()
            except queue.Empty:
                break
            else:
                with self._lock:
                    self.cancelled_tasks += 1
                self.task_queue.task_done()
        for worker in self._workers:
            worker.join(timeout=max(0.0, deadline - time.perf_counter()))
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        if self._workers:
            raise TimeoutError("VLM shutdown timed out; in-flight workers are still tracked")

    def _record_usage(self, result) -> None:
        if result.prompt_tokens is None or result.completion_tokens is None:
            self.usage_missing_calls += 1
        self.observed_prompt_tokens += result.prompt_tokens or 0
        self.observed_completion_tokens += result.completion_tokens or 0

    def _consumer_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self.task_queue.get(timeout=0.02)
            except queue.Empty:
                continue
            if task is None:
                self.task_queue.task_done()
                continue
            started = time.perf_counter()
            with self._lock:
                self.vlm_calls += 1
            try:
                result = self.vlm_judge.audit_with_usage(task)
                with self._lock:
                    self._record_usage(result)
                    self.vlm_completed_calls += 1
                    self.simulated_calls += int(result.simulated)
                    # A slow old response must never replace a newer verdict.
                    if task.frame_idx >= self._verdict_frames.get(task.track_id, -1):
                        self.verdict_cache.pop(task.track_id, None)
                        self.verdict_cache[task.track_id] = result.verdict
                        self._verdict_frames[task.track_id] = task.frame_idx
                        while len(self.verdict_cache) > self.config.max_cached_tracks:
                            oldest = next(iter(self.verdict_cache))
                            self.verdict_cache.pop(oldest)
                            self._verdict_frames.pop(oldest, None)
            except Exception as exc:
                with self._lock:
                    self.failed_tasks += 1
                    if isinstance(exc, VLMAuditError):
                        self._record_usage(exc)
                    else:
                        self.usage_missing_calls += 1
                logger.warning("VLM inspection failed for track #%s (%s)", task.track_id, type(exc).__name__)
            finally:
                latency = time.perf_counter() - started
                with self._lock:
                    self._audit_latency_total += latency
                    self._audit_latency_max = max(self._audit_latency_max, latency)
                self.task_queue.task_done()

    def process_frame(
        self, frame: np.ndarray, render_hud: bool = True
    ) -> Tuple[np.ndarray, bool, float, Optional[Stage2Result]]:
        """Producer step for a single frame: Stage 1 -> Stage 2 -> non-blocking Queue push -> HUD."""
        if self.total_frames == 0:
            self._start_time = time.perf_counter()
            self.start_workers()

        self.total_frames += 1
        frame_idx = self.total_frames

        # Stage 1: MotionGate
        motion_passed, motion_ratio = self.motion_gate.check_motion(frame)
        stage2_result: Optional[Stage2Result] = None

        if not motion_passed:
            self.stage1_intercepted += 1
        else:
            self.stage1_passed += 1
            # Stage 2: CascadeTracker
            tracked_objects, vlm_tasks = self.tracker.process_frame(frame, frame_idx=frame_idx)
            stage2_result = Stage2Result(
                frame_idx=frame_idx,
                tracked_objects=tracked_objects,
                vlm_tasks=vlm_tasks,
            )
            if not vlm_tasks:
                self.stage2_intercepted += 1
            else:
                for task in vlm_tasks:
                    if not task.roi_b64:
                        task.roi_b64 = encode_roi_base64(
                            frame,
                            task.bbox,
                            padding_ratio=self.config.roi_padding_ratio,
                            jpeg_quality=self.config.jpeg_quality,
                        )
                    self.stage2_flagged_tasks += 1
                    try:
                        self.task_queue.put_nowait(task)
                    except queue.Full:
                        self.dropped_tasks += 1
                        # A task that never entered the queue must not consume its cooldown.
                        if self.tracker.last_audited_frame.get(task.track_id) == task.frame_idx:
                            self.tracker.last_audited_frame.pop(task.track_id, None)

        annotated = (
            self.render_hud(
                frame,
                motion_passed=motion_passed,
                motion_ratio=motion_ratio,
                tracked_objects=stage2_result.tracked_objects if stage2_result else [],
            )
            if render_hud
            else frame
        )
        return annotated, motion_passed, motion_ratio, stage2_result

    def render_hud(
        self,
        frame: np.ndarray,
        *,
        motion_passed: bool,
        motion_ratio: float,
        tracked_objects: Iterable[TrackedObject],
    ) -> np.ndarray:
        """Render bounding boxes and ``[VLM: {refined_category} | {risk_level}]`` on the frame."""
        canvas = frame.copy()
        with self._lock:
            verdict_snapshot = dict(self.verdict_cache)
            completed_calls = self.vlm_completed_calls

        for obj in tracked_objects:
            x1, y1, x2, y2 = (int(v) for v in obj.bbox[:4])
            verdict = verdict_snapshot.get(obj.track_id)

            if verdict is not None:
                if not verdict.is_valid_target:
                    color = (140, 140, 140)
                elif verdict.risk_level in ("HIGH", "MEDIUM"):
                    color = (0, 50, 255)
                else:
                    color = (0, 230, 110)
                label = f"[VLM: {verdict.refined_category} | {verdict.risk_level}]"
            else:
                color = (0, 200, 255) if obj.trigger_reason else (255, 180, 40)
                label = f"#{obj.track_id} {obj.yolo_class} ({obj.yolo_conf:.2f})"

            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
            text_y = max(18, y1 - 8)
            cv2.putText(
                canvas,
                label,
                (max(2, x1), text_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                color,
                2,
                cv2.LINE_AA,
            )

        banner = (
            f"Frames:{self.total_frames} | S1-Skip:{self.stage1_intercepted} | "
            f"S2-Skip:{self.stage2_intercepted} | VLM:{completed_calls} | "
            f"Drop:{self.dropped_tasks} | Motion:{motion_ratio * 100:.2f}% "
            f"({'MOTION' if motion_passed else 'STATIC'})"
        )
        cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 28), (20, 20, 20), -1)
        cv2.putText(
            canvas,
            banner,
            (10, 19),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (240, 240, 240),
            1,
            cv2.LINE_AA,
        )
        return canvas

    def compute_audit_report(self) -> TokenAuditReport:
        """Return response usage; estimates are never labelled actual billed tokens."""
        elapsed = max(1e-6, (self._end_time or time.perf_counter()) - self._start_time)
        with self.task_queue.all_tasks_done:
            pending = self.task_queue.unfinished_tasks
        with self._lock:
            complete_usage = pending == 0 and self.usage_missing_calls == 0
            actual = (self.observed_prompt_tokens + self.observed_completion_tokens) if complete_usage else None
            cost = None
            if actual == 0:
                cost = 0.0
            elif complete_usage and (
                self.config.input_cost_per_million_tokens is not None
                and self.config.output_cost_per_million_tokens is not None
            ):
                cost = (
                    self.observed_prompt_tokens * self.config.input_cost_per_million_tokens
                    + self.observed_completion_tokens * self.config.output_cost_per_million_tokens
                ) / 1_000_000
            # Known tile-model low-detail base tokens only; unknown endpoints/models stay unknown.
            base = {"gpt-4o-mini": 2833, "gpt-4o": 85, "gpt-4.1": 85}.get(self.config.openai_model)
            if self.config.openai_base_url or self.config.vlm_detail != "low":
                base = None
            return TokenAuditReport(
                total_frames=self.total_frames,
                stage1_passed=self.stage1_passed,
                stage1_intercepted=self.stage1_intercepted,
                stage2_intercepted=self.stage2_intercepted,
                stage2_flagged_tasks=self.stage2_flagged_tasks,
                vlm_calls=self.vlm_calls,
                vlm_completed_calls=self.vlm_completed_calls,
                failed_tasks=self.failed_tasks,
                dropped_tasks=self.dropped_tasks,
                cancelled_tasks=self.cancelled_tasks,
                pending_tasks=pending,
                simulated_calls=self.simulated_calls,
                observed_prompt_tokens=self.observed_prompt_tokens,
                observed_completion_tokens=self.observed_completion_tokens,
                usage_missing_calls=self.usage_missing_calls,
                actual_tokens=actual,
                estimated_cost_usd=cost,
                estimated_low_image_tokens=self.stage2_flagged_tasks * base if base is not None else None,
                hypothetical_full_frame_low_tokens=self.total_frames * base if base is not None else None,
                stage1_filter_rate_pct=round(100 * self.stage1_intercepted / max(1, self.total_frames), 2),
                elapsed_seconds=round(elapsed, 3),
                average_fps=round(self.total_frames / elapsed, 2),
                mean_audit_latency_ms=round(1000 * self._audit_latency_total / max(1, self.vlm_calls), 3),
                max_audit_latency_ms=round(1000 * self._audit_latency_max, 3),
            )

    def get_metrics_summary(self) -> Dict[str, Any]:
        return self.compute_audit_report().to_dict()

    def run_stream(
        self,
        frame_source: Union[str, int, Iterable[np.ndarray]],
        *,
        headless: bool = True,
        max_frames: Optional[int] = None,
        on_step: Optional[Callable[[TokenAuditReport, Optional[Stage2Result]], None]] = None,
        drain_timeout: float = 30.0,
    ) -> TokenAuditReport:
        """Run the producer-consumer pipeline over a camera index, video file, or frame iterable."""
        if self.total_frames:
            raise RuntimeError("Use a new CascadePipeline for each stream")
        self.start_workers()
        self._start_time = time.perf_counter()
        self._end_time = None

        cap: Optional[cv2.VideoCapture] = None
        if isinstance(frame_source, (str, int)):
            src: Union[str, int] = (
                int(frame_source) if isinstance(frame_source, str) and frame_source.strip().isdigit() else frame_source
            )
            cap = cv2.VideoCapture(src)
            if not cap.isOpened():
                self.stop_workers(drain=False)
                raise RuntimeError(f"Unable to open video source: {frame_source!r}")

            def _iter_frames() -> Iterable[np.ndarray]:
                assert cap is not None
                while True:
                    ok, frm = cap.read()
                    if not ok or frm is None:
                        break
                    yield frm

            iterator: Iterable[np.ndarray] = _iter_frames()
        else:
            iterator = frame_source

        try:
            for frame in iterator:
                if max_frames is not None and self.total_frames >= max_frames:
                    break
                annotated, _passed, _ratio, s2 = self.process_frame(frame, render_hud=not headless)
                if on_step is not None:
                    on_step(self.compute_audit_report(), s2)
                if not headless:
                    cv2.imshow("cascade-vision HUD", annotated)
                    if (cv2.waitKey(1) & 0xFF) == ord("q"):
                        break
        finally:
            if cap is not None:
                cap.release()
            if not headless:
                try:
                    cv2.destroyAllWindows()
                except Exception:
                    pass
            self.stop_workers(drain=True, timeout=drain_timeout)
            self._end_time = time.perf_counter()

        return self.compute_audit_report()
