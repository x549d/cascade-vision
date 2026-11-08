"""Episode scoring and comparisons on generated, explicitly synthetic footage."""

import json

import cv2
import numpy as np
import pytest

from cascade_vision import CascadeConfig
from cascade_vision.evaluation import load_manifest, run_benchmark, score_events


def prediction(timestamp, label="person", completed=None, **extra):
    return {
        "status": "completed",
        "applied": True,
        "media_timestamp_s": timestamp,
        "completion_elapsed_s": completed,
        "verdict": {"observed_events": [label]},
        **extra,
    }


def test_duplicate_points_form_one_episode_and_failed_calls_do_not_count():
    truth = [
        {"id": "one", "label": "person", "start_s": 1, "end_s": 3},
        {"id": "two", "label": "person", "start_s": 5, "end_s": 7},
    ]
    events = [prediction(1.2), prediction(2), prediction(9), prediction(6, status="failed")]
    scores = score_events(truth, events, 10)
    assert (scores["true_positives"], scores["false_positives"], scores["false_negatives"]) == (1, 1, 1)
    assert scores["precision"] == scores["recall"] == scores["f1"] == 0.5
    assert scores["false_alarms_per_hour"] == 360
    assert scores["event_to_verdict_p50_s"] is None


def test_one_episode_cannot_satisfy_two_annotations():
    truth = [
        {"id": "one", "label": "person", "start_s": 1, "end_s": 2},
        {"id": "two", "label": "person", "start_s": 2.5, "end_s": 3.5},
    ]
    scores = score_events(truth, [prediction(1.5), prediction(3)], 4, merge_gap_s=2)
    assert scores["true_positives"] == 1
    assert scores["false_negatives"] == 1


def test_bridged_gap_without_observed_point_does_not_match():
    truth = [{"id": "one", "label": "person", "start_s": 2, "end_s": 3}]
    scores = score_events(truth, [prediction(1), prediction(4)], 5, merge_gap_s=4)
    assert scores["true_positives"] == 0


def test_event_latency_includes_completion_time_only_for_paced_replay():
    truth = [{"id": "one", "label": "person", "start_s": 1, "end_s": 3}]
    events = [prediction(1.2, completed=1.8), prediction(2, completed=2.2)]
    assert score_events(truth, events, 4, realtime=True)["event_to_verdict_p95_s"] == pytest.approx(0.8)
    assert score_events(truth, events, 4)["event_to_verdict_p95_s"] is None


@pytest.fixture
def generated_manifest(tmp_path):
    video_path = tmp_path / "fixture.avi"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (80, 64))
    assert writer.isOpened()
    for index in range(20):
        frame = np.zeros((64, 80, 3), dtype=np.uint8)
        cv2.rectangle(frame, (10 + index, 10), (30 + index, 55), (0, 0, 255), -1)
        writer.write(frame)
    writer.release()
    manifest = {
        "schema_version": 1,
        "event_labels": ["person"],
        "clips": [
            {
                "id": "generated",
                "footage_type": "synthetic",
                "path": "fixture.avi",
                "license": "Generated synthetic test fixture; no real footage",
                "events": [{"id": "person-1", "label": "person", "start_s": 0.2, "end_s": 1.5}],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_three_policy_comparison_exports_hashes_raw_events_and_quality_flags(generated_manifest, tmp_path):
    cfg = CascadeConfig(
        mock_vlm=True,
        yolo_model="mock",
        mock_latency_s=0,
        max_queue_size=100,
        openai_api_key="private-test-key",
        openai_base_url="https://private.example",
    )
    result = run_benchmark(generated_manifest, tmp_path / "out", cfg)
    assert [run["strategy"] for run in result["runs"]] == ["interval", "motion", "cascade"]
    for run in result["runs"]:
        assert len(run["video"]["sha256"]) == 64
        assert run["metrics"]["total_frames"] == 20
        assert run["metrics"]["actual_tokens"] == 0
        assert run["quality_flags"] == ["synthetic_footage", "simulation"]
        assert run["comparison_ready"] is False
        assert (tmp_path / "out" / run["events_file"]).is_file()
    text = (tmp_path / "out" / "benchmark.json").read_text(encoding="utf-8")
    assert "private-test-key" not in text
    assert "private.example" not in text
    assert "resolved_path" not in text


def test_api_benchmark_requires_explicit_opt_in_and_shared_cap(generated_manifest, tmp_path):
    with pytest.raises(ValueError, match="allow-api"):
        run_benchmark(generated_manifest, tmp_path / "out", CascadeConfig())
    cfg = CascadeConfig(mock_vlm=True, yolo_model="mock", mock_latency_s=0, max_queue_size=100)
    result = run_benchmark(generated_manifest, tmp_path / "capped", cfg, max_api_calls=1)
    assert sum(run["metrics"]["vlm_calls"] for run in result["runs"]) == 1
    assert any("budget_rejected_tasks" in run["quality_flags"] for run in result["runs"])


def test_invalid_license_or_annotations_rejected_before_running(generated_manifest):
    document = json.loads(generated_manifest.read_text(encoding="utf-8"))
    document["clips"][0]["license"] = ""
    generated_manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="license"):
        load_manifest(generated_manifest)
