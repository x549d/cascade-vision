"""Global configuration dataclass for cascade-vision.

Supports environment variable overrides (prefixed with ``CASCADE_``) and
explicit keyword argument overrides.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Dict, List, Mapping, Optional

ENV_PREFIX = "CASCADE_"


def _default_priority_classes() -> List[str]:
    return ["person", "backpack", "suitcase", "cell phone", "car"]


@dataclass
class CascadeConfig:
    """Global configuration for the Three-Stage Cascaded Funnel."""

    # Stage 1: OpenCV MOG2 Motion Gate
    motion_ratio_threshold: float = 0.012
    mog2_history: int = 500
    mog2_var_threshold: float = 16.0

    # Stage 2: YOLO + ByteTrack & Cascade Router
    yolo_model: str = "yolo11n.pt"
    conf_min: float = 0.25
    conf_ambiguous_upper: float = 0.55
    priority_classes: List[str] = field(default_factory=_default_priority_classes)
    cooldown_frames: int = 90
    min_stable_frames: int = 3
    max_track_age_frames: int = 300
    occlusion_iou_threshold: float = 0.35

    # ROI Cropping & Base64 Encoding
    roi_padding_ratio: float = 0.15
    jpeg_quality: int = 80

    # Stage 3: VLM Semantic Judge
    openai_model: str = "gpt-4o-mini"
    vlm_detail: str = "low"
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    mock_vlm: bool = False
    mock_latency_s: float = 0.30
    api_timeout_s: float = 10.0

    # Pipeline Queue & Concurrency
    max_queue_size: int = 20
    worker_threads: int = 2
    max_cached_tracks: int = 1000

    # Optional user-supplied standard rates; never assume current provider pricing.
    input_cost_per_million_tokens: Optional[float] = None
    output_cost_per_million_tokens: Optional[float] = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.conf_min <= self.conf_ambiguous_upper <= 1.0:
            raise ValueError(
                f"Expected 0.0 <= conf_min ({self.conf_min}) <= "
                f"conf_ambiguous_upper ({self.conf_ambiguous_upper}) <= 1.0"
            )
        if not 0.0 <= self.motion_ratio_threshold <= 1.0:
            raise ValueError("motion_ratio_threshold must be between 0.0 and 1.0")
        if self.roi_padding_ratio < 0.0:
            raise ValueError("roi_padding_ratio must be non-negative")
        if self.max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        if self.worker_threads < 1:
            raise ValueError("worker_threads must be >= 1")
        if self.vlm_detail not in {"low", "high", "auto"}:
            raise ValueError("vlm_detail must be low, high, or auto")
        for name in ("min_stable_frames", "max_track_age_frames", "max_cached_tracks", "mog2_history"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        for name in ("cooldown_frames", "mock_latency_s"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not math.isfinite(self.mog2_var_threshold) or self.mog2_var_threshold <= 0:
            raise ValueError("mog2_var_threshold must be finite and positive")
        if not math.isfinite(self.api_timeout_s) or self.api_timeout_s <= 0:
            raise ValueError("api_timeout_s must be finite and positive")
        if not math.isfinite(self.roi_padding_ratio):
            raise ValueError("roi_padding_ratio must be finite")
        if not 0 <= self.occlusion_iou_threshold <= 1:
            raise ValueError("occlusion_iou_threshold must be between 0 and 1")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        for name in ("input_cost_per_million_tokens", "output_cost_per_million_tokens"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        **overrides: Any,
    ) -> CascadeConfig:
        """Create a :class:`CascadeConfig` from defaults, environment variables, and keyword overrides."""
        source_env = os.environ if env is None else env
        parsed: Dict[str, Any] = {}

        for f in fields(cls):
            env_key = f"{ENV_PREFIX}{f.name.upper()}"
            if env_key in source_env:
                raw_val = source_env[env_key]
                parsed[f.name] = _coerce_env_value(f.name, raw_val)

        if "openai_api_key" not in parsed and "OPENAI_API_KEY" in source_env:
            parsed["openai_api_key"] = source_env["OPENAI_API_KEY"]
        if "openai_base_url" not in parsed and "OPENAI_BASE_URL" in source_env:
            parsed["openai_base_url"] = source_env["OPENAI_BASE_URL"]

        for key, value in overrides.items():
            if value is not None:
                parsed[key] = value

        return cls(**parsed)

    def with_overrides(self, **overrides: Any) -> CascadeConfig:
        """Return a new :class:`CascadeConfig` with non-None overrides applied."""
        filtered = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **filtered)

    def to_dict(self, redact_secrets: bool = True) -> Dict[str, Any]:
        """Serialize configuration to a dictionary, redacting API keys by default."""
        data = asdict(self)
        if redact_secrets and data.get("openai_api_key"):
            data["openai_api_key"] = "***REDACTED***"
        return data


def _coerce_env_value(field_name: str, raw: str) -> Any:
    text = raw.strip()
    bool_fields = {"mock_vlm"}
    int_fields = {
        "mog2_history",
        "cooldown_frames",
        "min_stable_frames",
        "jpeg_quality",
        "max_queue_size",
        "worker_threads",
        "max_track_age_frames",
        "max_cached_tracks",
    }
    float_fields = {
        "motion_ratio_threshold",
        "mog2_var_threshold",
        "conf_min",
        "conf_ambiguous_upper",
        "occlusion_iou_threshold",
        "roi_padding_ratio",
        "mock_latency_s",
        "input_cost_per_million_tokens",
        "output_cost_per_million_tokens",
        "api_timeout_s",
    }
    list_fields = {"priority_classes"}

    if field_name in bool_fields:
        if text.lower() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ValueError(f"Invalid boolean for {field_name}")
        return text.lower() in {"1", "true", "yes", "on"}
    if field_name in int_fields:
        return int(text)
    if field_name in float_fields:
        return float(text)
    if field_name in list_fields:
        return [item.strip() for item in text.split(",") if item.strip()]
    return text
