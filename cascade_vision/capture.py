"""Video capture with bounded RTSP reconnects and no credential-bearing errors."""

from __future__ import annotations

import math
import time

import cv2


def video_frames(source, config, *, on_disconnect=None, on_reconnect=None, capture_factory=None):
    """Yield (frame, media seconds). Local EOF never reconnects.

    Open/read timeouts are passed at open time to the FFmpeg RTSP backend.
    Backend timeout enforcement is provided by OpenCV, not Python cancellation.
    """
    factory = capture_factory or cv2.VideoCapture
    rtsp = isinstance(source, str) and source.lower().startswith(("rtsp://", "rtsps://"))
    src = int(source) if isinstance(source, str) and source.strip().isdigit() else source
    cap = None
    failures = 0
    disconnected = False
    index = 0
    try:
        while True:
            if cap is None:
                if rtsp:
                    params = [
                        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                        int(config.capture_timeout_ms),
                        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                        int(config.capture_timeout_ms),
                    ]
                    cap = factory(src, cv2.CAP_FFMPEG, params)
                else:
                    cap = factory(src)
                if not cap.isOpened() and not rtsp:
                    raise RuntimeError("Unable to open video source (source value omitted)")
            ok, frame = cap.read() if cap.isOpened() else (False, None)
            if ok and frame is not None:
                if disconnected:
                    if on_reconnect:
                        on_reconnect()
                    disconnected = False
                failures = 0
                fps = cap.get(cv2.CAP_PROP_FPS)
                media_time = index / fps if not rtsp and math.isfinite(fps) and fps > 0 else None
                index += 1
                yield frame, media_time
                continue
            if not rtsp:
                break
            cap.release()
            cap = None
            if not disconnected:
                disconnected = True
                if on_disconnect:
                    on_disconnect()
            if failures >= config.rtsp_reconnect_attempts:
                raise RuntimeError("RTSP reconnect attempts exhausted (source value omitted)")
            delay = config.rtsp_reconnect_backoff_s * 2 ** min(failures, 10)
            time.sleep(min(delay, 30.0))
            failures += 1
    finally:
        if cap is not None:
            cap.release()
