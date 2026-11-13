import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

MARKETPLACE = Path(__file__).resolve().parents[1] / "codex-plugin"


def run_plugin(video, output, *options):
    # Follow the marketplace and manifest paths as an installation would.
    marketplace = json.loads((MARKETPLACE / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    plugin = MARKETPLACE / marketplace["plugins"][0]["source"]["path"]
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    skill = plugin / manifest["skills"] / "video-triage"
    assert (skill / "SKILL.md").is_file()
    return subprocess.run(
        [sys.executable, str(skill / "scripts/triage_video.py"), str(video), "--output-dir", str(output), *options],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def make_clip(tmp_path, scenes):
    # Write to an ASCII basename first for OpenCV builds with narrow Windows paths.
    clip = tmp_path / "source.avi"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"MJPG"), 12, (160, 90))
    assert writer.isOpened()
    try:
        for value in scenes:
            frame = np.full((90, 160, 3), value, dtype=np.uint8)
            for _ in range(24):
                writer.write(frame)
    finally:
        writer.release()
    return clip.rename(tmp_path / "视频 test.avi")


@pytest.mark.parametrize("limit", [1, 3])
def test_detects_known_visual_changes_and_exports_images(tmp_path, limit):
    clip = make_clip(tmp_path, [24, 208, 56])
    result = run_plugin(clip, tmp_path / "筛选结果", "--max-frames", str(limit))
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    path = Path(summary["manifest"])
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["frames_read"] == 72
    assert report["frames_sampled"] == 12
    assert len(report["frames"]) == summary["selected_frames"] == limit
    if limit == 3:
        assert [f["time_seconds"] for f in report["frames"]] == [0.0, 2.0, 4.0]
        assert report["frames"][0]["reason"] == "opening_context"
    for frame in report["frames"]:
        image = cv2.imdecode(
            np.frombuffer((path.parent / frame["file"]).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if frame["reason"] == "visual_change":
            assert frame["time_seconds"] in (2.0, 4.0)
            assert np.median(image) == pytest.approx({2.0: 208, 4.0: 56}[frame["time_seconds"]], abs=3)
    sheet = cv2.imdecode(np.frombuffer(Path(summary["contact_sheet"]).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert sheet is not None and sheet.size


def test_static_video_returns_context_only(tmp_path):
    clip = make_clip(tmp_path, [80, 80])
    result = run_plugin(clip, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    path = Path(json.loads(result.stdout)["manifest"])
    frames = json.loads(path.read_text(encoding="utf-8"))["frames"]
    assert len(frames) == 1
    assert frames[0]["reason"] == "opening_context"


def test_remote_url_is_rejected_before_writing_output(tmp_path):
    output = tmp_path / "out"
    result = run_plugin("https://example.com/video.mp4", output)
    assert result.returncode != 0
    assert "existing local" in result.stderr
    assert not output.exists()
