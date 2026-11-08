"""User-visible CLI behavior and machine-readable reporting."""

import json
import sys

import pytest

from cascade_vision.cli import main


def test_demo_exports_explicit_simulation_without_secrets(tmp_path, monkeypatch):
    output = tmp_path / "demo.json"
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-do-not-export")
    monkeypatch.setenv("CASCADE_MOCK_LATENCY_S", "0")
    monkeypatch.setattr(sys, "argv", ["cascade-vision", "demo", "--report-json", str(output)])
    main()
    text = output.read_text(encoding="utf-8")
    document = json.loads(text)
    assert "test-secret-do-not-export" not in text
    assert document["mock_vlm"] is document["mock_detector"] is True
    assert document["metrics"]["actual_tokens"] == 0
    assert document["metrics"]["total_frames"] == 300
    assert document["metrics"]["vlm_completed_calls"] >= 1


def test_run_without_credentials_fails_before_detector_load(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CASCADE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CASCADE_MOCK_VLM", "false")
    monkeypatch.setattr(sys, "argv", ["cascade-vision", "run", "--source", "not-a-real-video"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["demo", "--max-frames", "1"],
        ["demo", "--headless", "--show-window"],
    ],
)
def test_invalid_arguments_fail_before_processing(arguments, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cascade-vision", *arguments])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_capped_demo_exports_loss_report_and_exits_unsuccessfully(tmp_path, monkeypatch):
    output, events = tmp_path / "run.json", tmp_path / "events.jsonl"
    monkeypatch.setenv("CASCADE_MOCK_LATENCY_S", "0")
    monkeypatch.setattr(
        sys,
        "argv",
        ["cascade-vision", "demo", "--max-api-calls", "1", "--report-json", str(output), "--events-jsonl", str(events)],
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["metrics"]["vlm_calls"] == 1
    assert report["metrics"]["budget_rejected_tasks"] > 0
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert "budget_rejected" in {record["status"] for record in records}


def test_paid_benchmark_cli_requires_opt_in_before_opening_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CASCADE_MOCK_VLM", "false")
    monkeypatch.setattr(
        sys,
        "argv",
        ["cascade-vision", "benchmark", "--manifest", "not-a-real-file.json", "--output-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert "allow-api" in capsys.readouterr().err
