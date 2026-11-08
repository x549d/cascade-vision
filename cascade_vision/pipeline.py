"""Bounded asynchronous video pipeline with measured usage and explicit failures."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union
from uuid import uuid4

import cv2
import numpy as np

from .capture import video_frames
from .config import CascadeConfig
from .detector import CascadeTracker, Stage2Result, TrackedObject, VLMTask
from .events import CallBudget
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
    expired_tasks: int = 0
    budget_rejected_tasks: int = 0
    event_export_errors: int = 0
    reconnects: int = 0
    detector_refresh_frames: int = 0
    queue_wait_p95_ms: float = 0.0
    request_e2e_p50_ms: float = 0.0
    request_e2e_p95_ms: float = 0.0
    latency_sample_count: int = 0
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
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        call_budget: Optional[CallBudget] = None,
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
        self._verdict_times: Dict[int, float] = {}
        self._stream_epoch = 0
        self._session_id = str(uuid4())
        self.event_callback = event_callback
        self.call_budget = call_budget or CallBudget(self.config.max_vlm_calls)
        self._queue_wait_samples = deque(maxlen=4096)
        self._e2e_samples = deque(maxlen=4096)
        self.expired_tasks = 0
        self.budget_rejected_tasks = 0
        self.event_export_errors = 0
        self.reconnects = 0
        self.detector_refresh_frames = 0
        self._next_sample_s = 0.0
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
                cancelled = self.task_queue.get_nowait()
            except queue.Empty:
                break
            else:
                with self._lock:
                    self.cancelled_tasks += 1
                if cancelled is not None:
                    self.tracker.release_audit(cancelled)
                    self._emit("cancelled", cancelled)
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

    def _emit(self, status, task=None, **details):
        if self.event_callback is None:
            return
        event = {
            "schema_version": 1,
            "session_id": self._session_id,
            "status": status,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        if task is not None:
            event.update(
                frame_idx=task.frame_idx,
                track_id=task.track_id,
                stream_epoch=task.stream_epoch,
                media_timestamp_s=task.media_timestamp_s,
                trigger_reason=task.trigger_reason,
                yolo_class=task.yolo_class,
                yolo_conf=task.yolo_conf,
                bbox=task.bbox,
                attempt=task.attempt,
                completion_elapsed_s=time.perf_counter() - self._start_time,
            )
        try:
            self.event_callback(event)
        except Exception:
            with self._lock:
                self.event_export_errors += 1
            logger.error("Event export failed; inspect event_export_errors")

    def get_verdict(self, track_id):
        """Return only a current, unexpired verdict."""
        with self._lock:
            completed = self._verdict_times.get(track_id)
            if completed is None or time.monotonic() - completed >= self.config.verdict_ttl_s:
                return None
            return self.verdict_cache.get(track_id)

    def _consumer_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self.task_queue.get(timeout=0.02)
            except queue.Empty:
                continue
            if task is None:
                self.task_queue.task_done()
                continue
            started = time.monotonic()
            wait = max(0.0, started - task.created_monotonic)
            if wait > self.config.max_task_age_s or task.stream_epoch != self._stream_epoch:
                with self._lock:
                    self.expired_tasks += 1
                if task.stream_epoch == self._stream_epoch:
                    self.tracker.release_audit(task)
                self._emit("expired", task, queue_wait_ms=1000 * wait)
                self.task_queue.task_done()
                continue
            if not self.call_budget.acquire():
                with self._lock:
                    self.budget_rejected_tasks += 1
                self.tracker.release_audit(task)
                self._emit("budget_rejected", task)
                self.task_queue.task_done()
                continue
            with self._lock:
                self.vlm_calls += 1
            try:
                result = self.vlm_judge.audit_with_usage(task)
                finished = time.monotonic()
                with self._lock:
                    self._record_usage(result)
                    self.vlm_completed_calls += 1
                    self.simulated_calls += int(result.simulated)
                    applied = task.stream_epoch == self._stream_epoch and task.frame_idx >= self._verdict_frames.get(
                        task.track_id, -1
                    )
                    if applied:
                        self.verdict_cache.pop(task.track_id, None)
                        self.verdict_cache[task.track_id] = result.verdict
                        self._verdict_frames[task.track_id] = task.frame_idx
                        self._verdict_times[task.track_id] = finished
                        while len(self.verdict_cache) > self.config.max_cached_tracks:
                            oldest = next(iter(self.verdict_cache))
                            self.verdict_cache.pop(oldest)
                            self._verdict_frames.pop(oldest, None)
                            self._verdict_times.pop(oldest, None)
                if applied:
                    self.tracker.complete_audit(task, success=True)
                self._emit(
                    "completed",
                    task,
                    verdict=result.verdict.model_dump(),
                    simulated=result.simulated,
                    applied=applied,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    queue_wait_ms=1000 * wait,
                    service_ms=1000 * (finished - started),
                    request_e2e_ms=1000 * (finished - task.created_monotonic),
                )
            except Exception as exc:
                retryable = isinstance(exc, VLMAuditError) and exc.retryable
                with self._lock:
                    self.failed_tasks += 1
                    if isinstance(exc, VLMAuditError):
                        self._record_usage(exc)
                    else:
                        self.usage_missing_calls += 1
                if task.stream_epoch == self._stream_epoch:
                    self.tracker.complete_audit(task, success=False, retryable=retryable)
                self._emit(
                    "failed",
                    task,
                    error_type=type(exc).__name__,
                    retryable=retryable,
                    prompt_tokens=getattr(exc, "prompt_tokens", None),
                    completion_tokens=getattr(exc, "completion_tokens", None),
                    queue_wait_ms=1000 * wait,
                    service_ms=1000 * (time.monotonic() - started),
                )
                logger.warning("VLM inspection failed for track #%s (%s)", task.track_id, type(exc).__name__)
            finally:
                latency = time.monotonic() - started
                with self._lock:
                    self._audit_latency_total += latency
                    self._audit_latency_max = max(self._audit_latency_max, latency)
                    self._queue_wait_samples.append(1000 * wait)
                    self._e2e_samples.append(1000 * (time.monotonic() - task.created_monotonic))
                self.task_queue.task_done()

    def process_frame(
        self, frame: np.ndarray, render_hud: bool = True, *, media_timestamp_s=None
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

        if motion_passed:
            self.stage1_passed += 1
        else:
            self.stage1_intercepted += 1
        periodic = (
            self.config.detector_refresh_interval_frames > 0
            and frame_idx % self.config.detector_refresh_interval_frames == 0
        )
        if self.config.strategy == "cascade":
            run_detector = motion_passed or periodic
            if periodic and not motion_passed:
                self.detector_refresh_frames += 1
            tracked_objects, vlm_tasks = self.tracker.process_frame(frame, frame_idx) if run_detector else ([], [])
        else:
            if self.config.strategy == "interval":
                if media_timestamp_s is None:
                    raise ValueError("Interval sampling requires video timestamps / source_fps")
                selected = media_timestamp_s + 1e-9 >= self._next_sample_s
                if selected:
                    self._next_sample_s = media_timestamp_s + self.config.sample_interval_s
            else:
                selected = self.config.strategy == "full" or motion_passed
            tracked_objects = []
            h, w = frame.shape[:2]
            vlm_tasks = (
                [VLMTask(frame_idx, 0, "scene", 1.0, [0, 0, w, h], self.config.strategy.upper())] if selected else []
            )
        stage2_result = Stage2Result(frame_idx, tracked_objects, vlm_tasks)
        if not vlm_tasks:
            self.stage2_intercepted += 1
        for task in vlm_tasks:
            task.media_timestamp_s = media_timestamp_s
            task.stream_epoch = self._stream_epoch
            if not task.roi_b64:
                task.roi_b64 = encode_roi_base64(
                    frame, task.bbox, padding_ratio=self.config.roi_padding_ratio, jpeg_quality=self.config.jpeg_quality
                )
            self.stage2_flagged_tasks += 1
            try:
                self.task_queue.put_nowait(task)
            except queue.Full:
                self.dropped_tasks += 1
                self.tracker.release_audit(task)
                self._emit("dropped", task)

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
            now = time.monotonic()
            verdict_snapshot = {
                tid: verdict
                for tid, verdict in self.verdict_cache.items()
                if now - self._verdict_times.get(tid, 0) < self.config.verdict_ttl_s
            }
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
                expired_tasks=self.expired_tasks,
                budget_rejected_tasks=self.budget_rejected_tasks,
                event_export_errors=self.event_export_errors,
                reconnects=self.reconnects,
                detector_refresh_frames=self.detector_refresh_frames,
                queue_wait_p95_ms=float(np.percentile(self._queue_wait_samples, 95)) if self._queue_wait_samples else 0,
                request_e2e_p50_ms=float(np.percentile(self._e2e_samples, 50)) if self._e2e_samples else 0,
                request_e2e_p95_ms=float(np.percentile(self._e2e_samples, 95)) if self._e2e_samples else 0,
                latency_sample_count=len(self._e2e_samples),
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

    def _on_disconnect(self):
        with self._lock:
            self._stream_epoch += 1
            self.verdict_cache.clear()
            self._verdict_frames.clear()
            self._verdict_times.clear()
        self.tracker.reset()
        self.motion_gate.reset()
        self._emit("stream_disconnected", stream_epoch=self._stream_epoch)

    def _on_reconnect(self):
        self.reconnects += 1
        self._emit("stream_reconnected", stream_epoch=self._stream_epoch)

    def run_stream(
        self,
        frame_source: Union[str, int, Iterable[np.ndarray]],
        *,
        headless=True,
        max_frames=None,
        on_step=None,
        drain_timeout=30.0,
        source_fps=None,
        realtime=False,
    ) -> TokenAuditReport:
        """Process video or an iterable; real-time pacing is opt-in for evaluation."""
        if self.total_frames:
            raise RuntimeError("Use a new CascadePipeline for each stream")
        self._start_time = time.perf_counter()
        self._end_time = None
        if isinstance(frame_source, (str, int)):
            iterator = video_frames(
                frame_source, self.config, on_disconnect=self._on_disconnect, on_reconnect=self._on_reconnect
            )
        else:
            if source_fps is not None and (not np.isfinite(source_fps) or source_fps <= 0):
                raise ValueError("source_fps must be finite and positive")
            iterator = ((frame, idx / source_fps if source_fps else None) for idx, frame in enumerate(frame_source))
        self.start_workers()
        playback_started = None
        try:
            # Check the limit before asking a live capture for another frame.
            while max_frames is None or self.total_frames < max_frames:
                try:
                    frame, timestamp = next(iterator)
                except StopIteration:
                    break
                if playback_started is None:
                    playback_started = time.perf_counter()
                    self._start_time = playback_started
                if realtime:
                    if timestamp is None:
                        raise ValueError("Real-time replay requires source timestamps")
                    time.sleep(max(0.0, playback_started + timestamp - time.perf_counter()))
                annotated, _passed, _ratio, s2 = self.process_frame(
                    frame, render_hud=not headless, media_timestamp_s=timestamp
                )
                if on_step:
                    on_step(self.compute_audit_report(), s2)
                if not headless:
                    cv2.imshow("cascade-vision HUD", annotated)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
        finally:
            if hasattr(iterator, "close"):
                iterator.close()
            if not headless:
                cv2.destroyAllWindows()
            self.stop_workers(drain=True, timeout=drain_timeout)
            self._end_time = time.perf_counter()
        return self.compute_audit_report()
