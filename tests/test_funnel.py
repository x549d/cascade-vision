"""Pytest unit test suite for cascade-vision.

Covers:
1. 100% interception of pure black static frames by Stage 1 ``MotionGate``.
2. Bounds-safe cropping & Base64 JPEG encoding via ``encode_roi_base64`` when
   bounding box coordinates exceed frame dimensions.
3. Geometric precision of ``compute_iou`` across identical, disjoint, and partially
   overlapping boxes.
4. Stage 2 three-rule cascade state machine (``FIRST_SEEN_PRIORITY``,
   ``LOW_CONF_HARD_EXAMPLE``, ``OBJECT_INTERACTION``) and cooldown enforcement.
5. ``VLMJudge(mock=True)`` returning valid ``SemanticVerdict`` Pydantic instances.
6. End-to-end 300-frame synthetic pipeline execution and token savings calculation.
"""

from __future__ import annotations

import base64

import cv2
import numpy as np
import pytest

from cascade_vision.cli import generate_synthetic_stream
from cascade_vision.config import CascadeConfig
from cascade_vision.detector import CascadeTracker, TriggerReason, VLMTask, compute_iou
from cascade_vision.motion import MotionGate, encode_roi_base64
from cascade_vision.pipeline import CascadePipeline
from cascade_vision.vlm_judge import SemanticVerdict, VLMJudge


# ---------------------------------------------------------------------------
# 1. Stage 1: Pure Black Static Frames 100% Intercepted by MotionGate
# ---------------------------------------------------------------------------
def test_pure_black_static_frames_are_100_percent_intercepted() -> None:
    cfg = CascadeConfig(motion_ratio_threshold=0.012, mog2_history=500, mog2_var_threshold=16.0)
    gate = MotionGate(cfg)

    black_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    passed_flags = []
    for _ in range(50):
        passed, ratio = gate.check_motion(black_frame)
        passed_flags.append(passed)
        assert ratio == pytest.approx(0.0)

    # Every pure black static frame must be intercepted (0% pass rate -> 100% intercepted)
    assert not any(passed_flags)


def test_motion_gate_passes_frame_with_moving_foreground() -> None:
    cfg = CascadeConfig(motion_ratio_threshold=0.012)
    gate = MotionGate(cfg)

    black_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for _ in range(10):
        gate.check_motion(black_frame)

    active_frame = black_frame.copy()
    cv2.rectangle(active_frame, (150, 120), (350, 320), (255, 255, 255), -1)
    passed, ratio = gate.check_motion(active_frame)
    assert passed is True
    assert ratio >= cfg.motion_ratio_threshold


# ---------------------------------------------------------------------------
# 2. Stage 1: Out-of-Bounds Bounding Box Cropping & Base64 Encoding
# ---------------------------------------------------------------------------
def test_encode_roi_base64_handles_out_of_bounds_bbox_without_error() -> None:
    frame = np.full((240, 320, 3), (120, 60, 200), dtype=np.uint8)

    # Coordinates extends far beyond negative and positive frame boundaries
    out_of_bounds_bbox = [-150, -90, 520, 480]
    b64_str = encode_roi_base64(frame, out_of_bounds_bbox, padding_ratio=0.15, jpeg_quality=80)

    assert isinstance(b64_str, str)
    assert len(b64_str) > 32

    decoded_bytes = base64.b64decode(b64_str)
    np_buf = np.frombuffer(decoded_bytes, dtype=np.uint8)
    decoded_img = cv2.imdecode(np_buf, cv2.IMREAD_COLOR)
    assert decoded_img is not None
    assert decoded_img.shape == (240, 320, 3)


# ---------------------------------------------------------------------------
# 3. Stage 2: compute_iou Geometric Precision
# ---------------------------------------------------------------------------
def test_compute_iou_geometric_precision() -> None:
    # Identical boxes -> IoU = 1.0
    assert CascadeTracker.compute_iou([10, 10, 110, 110], [10, 10, 110, 110]) == pytest.approx(1.0)
    # Non-overlapping boxes -> IoU = 0.0
    assert CascadeTracker.compute_iou([0, 0, 50, 50], [60, 60, 120, 120]) == pytest.approx(0.0)
    # Partial overlap: two 100x100 boxes overlapping by 50x100 (inter=5000, union=15000 -> 1/3)
    assert compute_iou([0, 0, 100, 100], [50, 0, 150, 100]) == pytest.approx(1.0 / 3.0, rel=1e-6)
    # Contained box: 50x50 inside 100x100 (inter=2500, union=10000 -> 0.25)
    assert compute_iou([0, 0, 100, 100], [25, 25, 75, 75]) == pytest.approx(0.25, rel=1e-6)


# ---------------------------------------------------------------------------
# 4. Stage 2: Three Cascade Trigger Rules & Cooldown State Machine
# ---------------------------------------------------------------------------
def test_cascade_tracker_three_trigger_rules_and_cooldown() -> None:
    cfg = CascadeConfig(cooldown_frames=90, yolo_model="mock")
    tracker = CascadeTracker(cfg)

    # Condition A: FIRST_SEEN_PRIORITY on streak_count == 3
    det_person = [(1, "person", 0.88, [20, 20, 100, 180])]
    assert len(tracker.evaluate_detections(det_person, frame_idx=1).vlm_tasks) == 0
    assert len(tracker.evaluate_detections(det_person, frame_idx=2).vlm_tasks) == 0
    r3 = tracker.evaluate_detections(det_person, frame_idx=3)
    assert len(r3.vlm_tasks) == 1
    assert r3.vlm_tasks[0].trigger_reason == TriggerReason.FIRST_SEEN_PRIORITY.value

    # Cooldown suppresses re-trigger at frame 10
    assert len(tracker.evaluate_detections(det_person, frame_idx=10).vlm_tasks) == 0

    # Condition B: LOW_CONF_HARD_EXAMPLE for confidence in [0.25, 0.55]
    det_hard = [(2, "backpack", 0.42, [150, 150, 210, 220])]
    r_hard = tracker.evaluate_detections(det_hard, frame_idx=15)
    assert len(r_hard.vlm_tasks) == 1
    assert r_hard.vlm_tasks[0].trigger_reason == TriggerReason.LOW_CONF_HARD_EXAMPLE.value

    # Condition C: OBJECT_INTERACTION when two priority targets overlap with IoU > 0.35
    tracker.last_audited_frame[10] = 1
    tracker.last_audited_frame[11] = 1
    det_interact = [
        (10, "person", 0.91, [0, 0, 100, 100]),
        (11, "suitcase", 0.89, [20, 0, 100, 100]),  # IoU = 0.80 > 0.35
    ]
    r_inter = tracker.evaluate_detections(det_interact, frame_idx=100)
    assert len(r_inter.vlm_tasks) == 2
    assert all(t.trigger_reason == TriggerReason.OBJECT_INTERACTION.value for t in r_inter.vlm_tasks)


# ---------------------------------------------------------------------------
# 5. Stage 3: VLMJudge(mock=True) Returns Valid SemanticVerdict
# ---------------------------------------------------------------------------
def test_vlm_judge_mock_returns_valid_semantic_verdict() -> None:
    judge = VLMJudge(mock=True, mock_latency_s=0.01)
    task = VLMTask(
        frame_idx=12,
        track_id=7,
        yolo_class="person",
        yolo_conf=0.84,
        bbox=[30, 40, 110, 220],
        trigger_reason="FIRST_SEEN_PRIORITY",
        roi_b64="dGVzdA==",
    )
    verdict = judge.audit(task)

    assert isinstance(verdict, SemanticVerdict)
    assert isinstance(verdict.is_valid_target, bool) and verdict.is_valid_target is True
    assert isinstance(verdict.refined_category, str) and len(verdict.refined_category) > 0
    assert isinstance(verdict.activity_summary, str) and len(verdict.activity_summary) > 0
    assert verdict.risk_level in {"NONE", "LOW", "MEDIUM", "HIGH"}


# ---------------------------------------------------------------------------
# 6. End-to-End 300-Frame Synthetic Stream Pipeline & Token Savings Summary
# ---------------------------------------------------------------------------
def test_end_to_end_300_frame_synthetic_pipeline_metrics() -> None:
    cfg = CascadeConfig(mock_vlm=True, yolo_model="mock")
    judge = VLMJudge(cfg, mock=True, mock_latency_s=0.01)
    pipeline = CascadePipeline(cfg, vlm_judge=judge)

    stream = generate_synthetic_stream(total_frames=300, width=640, height=480)
    pipeline.run_stream(stream, headless=True, max_frames=300)
    metrics = pipeline.get_metrics_summary()

    assert metrics["total_frames"] == 300
    assert metrics["stage1_intercepted"] >= 140  # >=100 head + ~50 tail static frames
    assert metrics["vlm_calls"] >= 1
    assert metrics["actual_tokens"] == 0
    assert metrics["simulated_calls"] == metrics["vlm_calls"]
    assert metrics["estimated_low_image_tokens"] == metrics["stage2_flagged_tasks"] * 2833
    assert metrics["hypothetical_full_frame_low_tokens"] == 300 * 2833
    assert metrics["failed_tasks"] == metrics["pending_tasks"] == 0
    assert len(pipeline.verdict_cache) >= 1
