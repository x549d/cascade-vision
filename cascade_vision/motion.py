"""Stage 1: OpenCV MOG2 Motion Gate and In-Memory ROI Base64 Encoder.

Filters out static or low-activity frames using Gaussian Mixture-based
Background/Foreground Segmentation (MOG2), shadow removal, and morphological
opening. Also provides zero-disk-I/O padded ROI cropping and JPEG Base64 encoding.
"""

from __future__ import annotations

import base64
import math
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np

from .config import CascadeConfig


class MotionGate:
    """Stage 1 traditional computer vision motion filter gate using OpenCV MOG2."""

    def __init__(
        self,
        config: Optional[CascadeConfig] = None,
        *,
        history: Optional[int] = None,
        var_threshold: Optional[float] = None,
        motion_ratio_threshold: Optional[float] = None,
    ) -> None:
        cfg = config or CascadeConfig()
        self.history: int = history if history is not None else cfg.mog2_history
        self.var_threshold: float = var_threshold if var_threshold is not None else cfg.mog2_var_threshold
        self.motion_ratio_threshold: float = (
            motion_ratio_threshold if motion_ratio_threshold is not None else cfg.motion_ratio_threshold
        )
        self.subtractor = cv2.createBackgroundSubtractorMOG2(
            history=self.history,
            varThreshold=self.var_threshold,
            detectShadows=True,
        )
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.last_mask: Optional[np.ndarray] = None
        self._initialized: bool = False

    def reset(self) -> None:
        """Reset the MOG2 background model."""
        self.subtractor = cv2.createBackgroundSubtractorMOG2(
            history=self.history,
            varThreshold=self.var_threshold,
            detectShadows=True,
        )
        self.last_mask = None
        self._initialized = False

    def check_motion(self, frame: np.ndarray) -> Tuple[bool, float]:
        """Evaluate whether a frame contains enough foreground motion to pass Stage 1.

        Pipeline:
        1. Apply MOG2 background subtractor to extract raw foreground mask
           (the very first frame seeds the background model and returns ``(False, 0.0)``).
        2. Threshold at 250 -> 255 (`cv2.THRESH_BINARY`) to strip grey shadows (127).
        3. Apply morphological opening (`cv2.MORPH_OPEN`) with a 3x3 elliptical
           structuring element to eliminate isolated salt-and-pepper noise.
        4. Compute the non-zero pixel ratio ``motion_ratio``.

        Args:
            frame: BGR or grayscale image as a NumPy array.

        Returns:
            A tuple ``(passed, motion_ratio)`` where ``passed`` is ``True`` when
            ``motion_ratio >= self.motion_ratio_threshold``.
        """
        if frame is None or frame.size == 0:
            raise ValueError("MotionGate.check_motion received an empty frame")

        if not self._initialized:
            self.subtractor.apply(frame, learningRate=1.0)
            self._initialized = True
            self.last_mask = np.zeros(frame.shape[:2], dtype=np.uint8)
            return False, 0.0

        raw_mask = self.subtractor.apply(frame)
        _, binary_mask = cv2.threshold(raw_mask, 250, 255, cv2.THRESH_BINARY)
        cleaned_mask = cv2.morphologyEx(binary_mask, cv2.MORPH_OPEN, self.kernel)
        self.last_mask = cleaned_mask

        total_pixels = float(cleaned_mask.shape[0] * cleaned_mask.shape[1])
        if total_pixels <= 0:
            return False, 0.0

        non_zero_pixels = float(cv2.countNonZero(cleaned_mask))
        motion_ratio = non_zero_pixels / total_pixels
        passed = bool(motion_ratio >= self.motion_ratio_threshold)
        return passed, motion_ratio

    @staticmethod
    def compute_padded_bbox(
        frame_shape: Sequence[int],
        bbox: Sequence[float],
        padding_ratio: float = 0.15,
    ) -> Tuple[int, int, int, int]:
        """Compute bounds-safe padded integer coordinates ``(x1, y1, x2, y2)``."""
        if len(frame_shape) < 2:
            raise ValueError(f"Invalid frame shape: {frame_shape}")
        h, w = int(frame_shape[0]), int(frame_shape[1])
        if h <= 0 or w <= 0:
            raise ValueError(f"Frame dimensions must be positive, got {w}x{h}")
        if len(bbox) != 4:
            raise ValueError(f"bbox must have 4 elements [x1, y1, x2, y2], got {len(bbox)}")

        raw_x1, raw_y1, raw_x2, raw_y2 = (float(v) for v in bbox)
        if not all(math.isfinite(v) for v in (raw_x1, raw_y1, raw_x2, raw_y2)):
            raise ValueError(f"bbox contains non-finite coordinates: {bbox}")

        x1, x2 = min(raw_x1, raw_x2), max(raw_x1, raw_x2)
        y1, y2 = min(raw_y1, raw_y2), max(raw_y1, raw_y2)

        box_w = max(1.0, x2 - x1)
        box_h = max(1.0, y2 - y1)
        pad_x = box_w * max(0.0, float(padding_ratio))
        pad_y = box_h * max(0.0, float(padding_ratio))

        px1 = max(0, min(w - 1, int(math.floor(x1 - pad_x))))
        py1 = max(0, min(h - 1, int(math.floor(y1 - pad_y))))
        px2 = max(px1 + 1, min(w, int(math.ceil(x2 + pad_x))))
        py2 = max(py1 + 1, min(h, int(math.ceil(y2 + pad_y))))
        return px1, py1, px2, py2

    @staticmethod
    def encode_roi_base64(
        frame: np.ndarray,
        bbox: Sequence[float],
        padding_ratio: float = 0.15,
        jpeg_quality: int = 80,
    ) -> str:
        """Crop a padded ROI from ``frame`` and encode it to a JPEG Base64 string in memory.

        Strictly bounds coordinates with ``max(0, ...)`` and ``min(w/h, ...)`` to
        prevent out-of-bounds slicing, then encodes directly via ``cv2.imencode``
        with zero disk I/O.
        """
        if frame is None or frame.size == 0:
            raise ValueError("Cannot encode ROI from an empty frame")

        x1, y1, x2, y2 = MotionGate.compute_padded_bbox(frame.shape, bbox, padding_ratio=padding_ratio)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            raise ValueError("Cropped ROI is empty after boundary clamping")

        roi_contiguous = np.ascontiguousarray(roi)
        quality = max(1, min(100, int(jpeg_quality)))
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        success, encoded_buf = cv2.imencode(".jpg", roi_contiguous, encode_params)
        if not success:
            raise RuntimeError("cv2.imencode failed to compress ROI to JPEG")

        return base64.b64encode(encoded_buf.tobytes()).decode("utf-8")


encode_roi_base64 = MotionGate.encode_roi_base64
compute_padded_bbox = MotionGate.compute_padded_bbox
