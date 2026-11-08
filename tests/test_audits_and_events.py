"""Failure recovery, refresh, queue admission and event export regressions."""

import json
import threading
import time

import numpy as np

from cascade_vision import AuditResult, CascadeConfig, CascadePipeline, CascadeTracker, JsonlEventWriter, VLMAuditError
from cascade_vision.detector import VLMTask
from cascade_vision.vlm_judge import SemanticVerdict


def detection():
    return [(1, "person", 0.9, [0, 0, 20, 40])]


def verdict():
    return SemanticVerdict(
        is_valid_target=True, refined_category="person", activity_summary="walking", risk_level="NONE"
    )


def test_retry_backoff_uses_fresh_frame_and_stops_at_limit():
    now = [0.0]
    cfg = CascadeConfig(yolo_model="mock", min_stable_frames=1, audit_max_retries=2, retry_backoff_s=1)
    tracker = CascadeTracker(cfg, clock=lambda: now[0])
    first = tracker.evaluate_detections(detection(), 1).vlm_tasks[0]
    tracker.complete_audit(first, success=False)
    assert not tracker.evaluate_detections(detection(), 2).vlm_tasks
    now[0] = 1
    second = tracker.evaluate_detections(detection(), 3).vlm_tasks[0]
    assert (second.attempt, second.frame_idx, second.trigger_reason) == (2, 3, "AUDIT_RETRY")
    tracker.complete_audit(second, success=False)
    now[0] = 2.9
    assert not tracker.evaluate_detections(detection(), 4).vlm_tasks
    now[0] = 3
    third = tracker.evaluate_detections(detection(), 5).vlm_tasks[0]
    assert third.attempt == 3
    tracker.complete_audit(third, success=False)
    now[0] = 100
    assert not tracker.evaluate_detections(detection(), 100).vlm_tasks
    tracker.clear_audit_failure(1)
    assert tracker.evaluate_detections(detection(), 101).vlm_tasks[0].attempt == 1


def test_success_refresh_and_no_duplicate_pending_requests():
    cfg = CascadeConfig(yolo_model="mock", min_stable_frames=1, cooldown_frames=1, refresh_interval_frames=3)
    tracker = CascadeTracker(cfg)
    first = tracker.evaluate_detections(detection(), 1).vlm_tasks[0]
    assert not tracker.evaluate_detections(detection(), 2).vlm_tasks
    tracker.complete_audit(first, success=True)
    assert not tracker.evaluate_detections(detection(), 3).vlm_tasks
    refresh = tracker.evaluate_detections(detection(), 4).vlm_tasks[0]
    assert refresh.trigger_reason == "VERDICT_REFRESH"
    assert not tracker.evaluate_detections(detection(), 5).vlm_tasks


def test_refusal_never_retries_automatically():
    tracker = CascadeTracker(CascadeConfig(yolo_model="mock", min_stable_frames=1, retry_backoff_s=0))
    first = tracker.evaluate_detections(detection(), 1).vlm_tasks[0]
    tracker.complete_audit(first, success=False, retryable=False)
    assert not tracker.evaluate_detections(detection(), 100).vlm_tasks


def test_ttl_refreshes_even_when_frame_refresh_is_disabled():
    now = [0.0]
    cfg = CascadeConfig(
        yolo_model="mock", min_stable_frames=1, cooldown_frames=0, refresh_interval_frames=0, verdict_ttl_s=5
    )
    tracker = CascadeTracker(cfg, clock=lambda: now[0])
    first = tracker.evaluate_detections(detection(), 1).vlm_tasks[0]
    tracker.complete_audit(first, success=True)
    now[0] = 4.9
    assert not tracker.evaluate_detections(detection(), 2).vlm_tasks
    now[0] = 5
    assert tracker.evaluate_detections(detection(), 3).vlm_tasks[0].trigger_reason == "VERDICT_REFRESH"


def test_live_pipeline_recovers_from_failed_first_priority_request(tmp_path):
    class FailOnce:
        def __init__(self):
            self.calls = 0

        def audit_with_usage(self, task):
            self.calls += 1
            if self.calls == 1:
                raise VLMAuditError("offline injected transport error", retryable=True)
            return AuditResult(verdict(), 10, 5)

    output = tmp_path / "events.jsonl"
    cfg = CascadeConfig(yolo_model="mock", min_stable_frames=1, retry_backoff_s=0)
    with JsonlEventWriter(output) as writer:
        pipeline = CascadePipeline(cfg, vlm_judge=FailOnce(), event_callback=writer.write)
        pipeline.motion_gate.check_motion = lambda frame: (True, 1.0)
        pipeline.tracker._extract_raw_tracks = lambda frame: detection()
        frames = [np.zeros((50, 30, 3), dtype=np.uint8) for _ in range(3)]
        pipeline.run_stream(frames, source_fps=10, on_step=lambda *args: pipeline.task_queue.join())
    events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [e["status"] for e in events] == ["failed", "completed"]
    assert events[1]["trigger_reason"] == "AUDIT_RETRY"
    assert events[1]["frame_idx"] == 2
    assert events[1]["media_timestamp_s"] == 0.1
    assert events[1]["attempt"] == 2
    assert events[1]["request_e2e_ms"] >= events[1]["service_ms"] >= 0
    assert pipeline.get_verdict(1) is not None
    assert pipeline.get_metrics_summary()["actual_tokens"] is None  # failed request's billing is unknown
    pipeline._verdict_times[1] = time.monotonic() - cfg.verdict_ttl_s - 1
    assert pipeline.get_verdict(1) is None
    assert "roi_b64" not in output.read_text(encoding="utf-8")


def test_expired_task_releases_reservation_without_an_api_call():
    cfg = CascadeConfig(mock_vlm=True, yolo_model="mock", min_stable_frames=1, max_task_age_s=1)
    pipeline = CascadePipeline(cfg)
    task = pipeline.tracker.evaluate_detections(detection(), 1).vlm_tasks[0]
    task.created_monotonic -= 10
    pipeline.task_queue.put(task)
    pipeline.start_workers()
    pipeline.stop_workers()
    assert pipeline.expired_tasks == 1
    assert pipeline.vlm_calls == 0
    assert pipeline.tracker.evaluate_detections(detection(), 2).vlm_tasks


def test_request_cap_and_export_failure_are_visible():
    cfg = CascadeConfig(mock_vlm=True, yolo_model="mock", max_vlm_calls=1, mock_latency_s=0)

    def fail_export(event):
        raise OSError("test disk failure")

    pipeline = CascadePipeline(cfg, event_callback=fail_export)
    for tid in (1, 2):
        pipeline.task_queue.put(VLMTask(1, tid, "person", 0.9, [0, 0, 10, 20], "TEST"))
    pipeline.start_workers()
    pipeline.stop_workers()
    assert pipeline.vlm_calls == 1
    assert pipeline.budget_rejected_tasks == 1
    assert pipeline.event_export_errors == 2


def test_old_epoch_response_cannot_repopulate_cache_after_disconnect():
    started, release = threading.Event(), threading.Event()

    class SlowJudge:
        def audit_with_usage(self, task):
            started.set()
            assert release.wait(3)
            return AuditResult(verdict(), 0, 0, simulated=True)

    pipeline = CascadePipeline(CascadeConfig(yolo_model="mock"), vlm_judge=SlowJudge())
    pipeline.task_queue.put(VLMTask(1, 1, "person", 0.9, [0, 0, 10, 20], "TEST"))
    pipeline.start_workers()
    try:
        assert started.wait(2)
        pipeline._on_disconnect()
    finally:
        release.set()
        pipeline.stop_workers()
    assert not pipeline.verdict_cache
    assert pipeline._stream_epoch == 1
