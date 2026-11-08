"""Regression tests for real-provider boundaries, accounting and concurrency."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from cascade_vision import (
    AuditResult,
    CascadeConfig,
    CascadePipeline,
    CascadeTracker,
    SemanticVerdict,
    VLMAuditError,
    VLMJudge,
    VLMTask,
)


def task(frame=1, track=1):
    return VLMTask(frame, track, "person", 0.9, [0, 0, 10, 10], "FIRST_SEEN_PRIORITY", "fake-image")


def verdict(label="person"):
    return SemanticVerdict(is_valid_target=True, refined_category=label, activity_summary="test", risk_level="NONE")


def client_for(parse):
    return SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))))


def test_missing_credentials_never_enable_mock(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="explicitly enable"):
        VLMJudge()


def test_api_failure_never_fabricates_verdict():
    def fail(**kwargs):
        raise ConnectionError("test")

    judge = VLMJudge(client=client_for(fail), mock=False)
    with pytest.raises(VLMAuditError, match="ConnectionError"):
        judge.audit(task())
    assert judge.mock is False


def test_detail_and_measured_usage_are_preserved():
    def parse(**kwargs):
        assert kwargs["messages"][1]["content"][1]["image_url"]["detail"] == "high"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=verdict()))],
            usage=SimpleNamespace(prompt_tokens=4000, completion_tokens=100),
        )

    cfg = CascadeConfig(
        yolo_model="mock", vlm_detail="high", input_cost_per_million_tokens=1, output_cost_per_million_tokens=2
    )
    pipeline = CascadePipeline(cfg, vlm_judge=VLMJudge(cfg, client=client_for(parse)))
    pipeline.task_queue.put(task())
    pipeline.start_workers()
    pipeline.stop_workers()
    report = pipeline.get_metrics_summary()
    assert report["actual_tokens"] == 4100
    assert report["estimated_cost_usd"] == pytest.approx(0.0042)
    assert report["estimated_low_image_tokens"] is None


@pytest.mark.parametrize("refused", [False, True])
def test_missing_usage_and_refusal_are_not_reported_as_zero(refused):
    def parse(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=None if refused else verdict(), refusal="refused" if refused else None
                    )
                )
            ],
            usage=None,
        )

    cfg = CascadeConfig(yolo_model="mock")
    pipeline = CascadePipeline(cfg, vlm_judge=VLMJudge(cfg, client=client_for(parse)))
    pipeline.task_queue.put(task())
    pipeline.start_workers()
    pipeline.stop_workers()
    report = pipeline.get_metrics_summary()
    assert report["usage_missing_calls"] == 1
    assert report["actual_tokens"] is None
    assert report["estimated_cost_usd"] is None
    assert report["failed_tasks"] == int(refused)
    assert bool(pipeline.verdict_cache) is not refused


def test_refusal_still_accounts_for_billed_response():
    def parse(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=None, refusal="no"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    with pytest.raises(VLMAuditError) as caught:
        VLMJudge(client=client_for(parse)).audit(task())
    assert caught.value.prompt_tokens == 10
    assert caught.value.completion_tokens == 5


def test_stage1_skips_do_not_reset_stability_but_missing_detections_do():
    tracker = CascadeTracker(CascadeConfig(yolo_model="mock"))
    detection = [(1, "person", 0.9, [0, 0, 10, 10])]
    assert not tracker.evaluate_detections(detection, 1).vlm_tasks
    assert not tracker.evaluate_detections(detection, 3).vlm_tasks
    assert len(tracker.evaluate_detections(detection, 5).vlm_tasks) == 1
    tracker.evaluate_detections([], 6)
    tracker.evaluate_detections(detection, 7)
    assert tracker.streak_count[1] == 1


def test_empty_real_detection_does_not_use_synthetic_contours():
    model = SimpleNamespace(track=lambda *args, **kwargs: [])
    tracker = CascadeTracker(CascadeConfig(), model=model)
    tracker._fallback_tracker.track = lambda frame: pytest.fail("Synthetic detector must not run")
    assert tracker.process_frame(np.zeros((20, 20, 3), dtype=np.uint8), 1) == ([], [])


def test_overflow_can_retry_first_priority_audit():
    cfg = CascadeConfig(mock_vlm=True, yolo_model="mock", min_stable_frames=1, max_queue_size=1)
    pipeline = CascadePipeline(cfg)
    pipeline.start_workers = lambda: None
    pipeline.motion_gate.check_motion = lambda frame: (True, 1.0)
    pipeline.tracker._extract_raw_tracks = lambda frame: [(1, "person", 0.9, [0, 0, 10, 10])]
    pipeline.task_queue.put(task(track=99))
    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    pipeline.process_frame(frame, render_hud=False)
    assert pipeline.dropped_tasks == 1
    assert 1 not in pipeline.tracker.last_audited_frame
    pipeline.task_queue.get_nowait()
    pipeline.task_queue.task_done()
    pipeline.process_frame(frame, render_hud=False)
    assert pipeline.task_queue.get_nowait().track_id == 1
    pipeline.task_queue.task_done()


def test_shutdown_tracks_inflight_worker_and_prevents_restart():
    started, release = threading.Event(), threading.Event()

    class BlockingJudge:
        def audit_with_usage(self, item):
            started.set()
            assert release.wait(3)
            return AuditResult(verdict(), 0, 0, simulated=True)

    pipeline = CascadePipeline(CascadeConfig(yolo_model="mock", worker_threads=1), vlm_judge=BlockingJudge())
    pipeline.task_queue.put(task())
    pipeline.task_queue.put(task(track=2))
    pipeline.start_workers()
    try:
        assert started.wait(2)
        with pytest.raises(TimeoutError):
            pipeline.stop_workers(timeout=0.01)
        assert pipeline._workers
        assert pipeline.cancelled_tasks == 1
        with pytest.raises(RuntimeError, match="still stopping"):
            pipeline.start_workers()
    finally:
        release.set()
        pipeline.stop_workers()
    assert pipeline.task_queue.unfinished_tasks == 0


def test_slow_old_response_does_not_replace_new_verdict():
    started, release = threading.Event(), threading.Event()

    class OutOfOrderJudge:
        def audit_with_usage(self, item):
            if item.frame_idx == 1:
                started.set()
                assert release.wait(3)
            else:
                assert started.wait(2)
            return AuditResult(verdict(str(item.frame_idx)), 0, 0, simulated=True)

    pipeline = CascadePipeline(CascadeConfig(yolo_model="mock", worker_threads=2), vlm_judge=OutOfOrderJudge())
    pipeline.task_queue.put(task(1))
    pipeline.task_queue.put(task(2))
    pipeline.start_workers()
    try:
        assert started.wait(2)
        import time

        deadline = time.monotonic() + 2
        while 1 not in pipeline.verdict_cache and time.monotonic() < deadline:
            time.sleep(0.005)
        assert pipeline.verdict_cache[1].refined_category == "2"
    finally:
        release.set()
        pipeline.stop_workers()
    assert pipeline.verdict_cache[1].refined_category == "2"


def test_track_state_expires_and_cache_is_bounded():
    cfg = CascadeConfig(mock_vlm=True, yolo_model="mock", max_track_age_frames=2, max_cached_tracks=1, mock_latency_s=0)
    pipeline = CascadePipeline(cfg)
    pipeline.tracker.evaluate_detections([(1, "person", 0.9, [0, 0, 10, 10])], 1)
    pipeline.tracker.evaluate_detections([], 4)
    assert not pipeline.tracker._last_seen_frame
    pipeline.task_queue.put(task(track=1))
    pipeline.task_queue.put(task(track=2))
    pipeline.start_workers()
    pipeline.stop_workers()
    assert len(pipeline.verdict_cache) == len(pipeline._verdict_frames) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_timeout_s": 0},
        {"mock_latency_s": -1},
        {"max_cached_tracks": 0},
        {"vlm_detail": "invalid"},
        {"roi_padding_ratio": float("nan")},
        {"input_cost_per_million_tokens": float("inf")},
    ],
)
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        CascadeConfig(**kwargs)


def test_invalid_boolean_environment_rejected():
    with pytest.raises(ValueError):
        CascadeConfig.from_env(env={"CASCADE_MOCK_VLM": "flase"})
