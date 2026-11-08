"""Exercise live reconnection with injected captures, never real cameras."""

import cv2
import numpy as np
import pytest

from cascade_vision import CascadeConfig
from cascade_vision.capture import video_frames


class Capture:
    def __init__(self, frames, opened=True):
        self.frames = iter(frames)
        self.opened = opened
        self.released = False
        self.reads = 0

    def isOpened(self):
        return self.opened

    def read(self):
        self.reads += 1
        frame = next(self.frames, None)
        return frame is not None, frame

    def get(self, key):
        return 10 if key == cv2.CAP_PROP_FPS else 0

    def release(self):
        self.released = True


def test_rtsp_reconnect_resets_session_and_passes_open_time_timeouts():
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    caps = [Capture([frame]), Capture([frame, frame])]
    remaining = iter(caps)
    arguments, states = [], []

    def factory(*args):
        arguments.append(args)
        return next(remaining)

    stream = video_frames(
        "rtsp://user:private-password@example/video",
        CascadeConfig(rtsp_reconnect_backoff_s=0),
        capture_factory=factory,
        on_disconnect=lambda: states.append("down"),
        on_reconnect=lambda: states.append("up"),
    )
    try:
        assert next(stream)[1] is None
        assert next(stream)[1] is None
    finally:
        stream.close()
    assert states == ["down", "up"]
    assert len(arguments) == 2
    assert arguments[0][1] == cv2.CAP_FFMPEG
    assert cv2.CAP_PROP_READ_TIMEOUT_MSEC in arguments[0][2]
    assert all(cap.released for cap in caps)


def test_rtsp_retries_are_bounded_and_error_omits_credentials():
    caps = []

    def factory(*args):
        cap = Capture([], opened=False)
        caps.append(cap)
        return cap

    stream = video_frames(
        "rtsp://private-user:private-password@example/video",
        CascadeConfig(rtsp_reconnect_attempts=2, rtsp_reconnect_backoff_s=0),
        capture_factory=factory,
    )
    with pytest.raises(RuntimeError, match="exhausted") as caught:
        next(stream)
    assert len(caps) == 3
    assert "private" not in str(caught.value)
    assert all(cap.released for cap in caps)


def test_local_eof_does_not_reconnect():
    cap = Capture([np.zeros((10, 10, 3), dtype=np.uint8)])
    calls = []

    def factory(*args):
        calls.append(args)
        return cap

    assert len(list(video_frames("example.avi", CascadeConfig(), capture_factory=factory))) == 1
    assert len(calls) == 1
    assert cap.released
