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
