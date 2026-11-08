"""Structured VLM inspection with explicit simulation and response usage accounting."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from .config import CascadeConfig
from .detector import VLMTask

logger = logging.getLogger(__name__)

RiskLevel = Literal["NONE", "LOW", "MEDIUM", "HIGH"]
AnomalyLevel = RiskLevel


@dataclass(frozen=True)
class AuditResult:
    verdict: SemanticVerdict
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    simulated: bool = False


class VLMAuditError(RuntimeError):
    """A failed inspection, optionally carrying usage from a rejected response."""

    def __init__(self, message, prompt_tokens=None, completion_tokens=None):
        super().__init__(message)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


SYSTEM_PROMPT = (
    "You are an expert semantic video security and scene analysis engine serving as "
    "Stage 3 of a cascaded computer vision funnel. You receive a cropped Region of Interest "
    "(ROI) image along with Stage 2 YOLO + ByteTrack metadata (predicted class, confidence, "
    "and escalation trigger reason). Determine whether the detection is a valid real target "
    "or a background/lighting false positive, refine the category into a specific semantic "
    "role or object type (e.g., 'security_guard', 'courier', 'unattended_backpack'), provide "
    "a concise summary of the current activity or state, and rate the risk level."
)


class SemanticVerdict(BaseModel):
    """Structured output model returned by the Stage 3 VLM Judge."""

    model_config = ConfigDict(extra="forbid")

    is_valid_target: bool = Field(
        description="Whether the bounding box contains a real target rather than background/lighting false detection."
    )
    refined_category: str = Field(
        description="Fine-grained semantic category, e.g. refining 'person' into 'security_guard' or 'courier'."
    )
    activity_summary: str = Field(
        description="Concise summary describing the target's current behavior, posture, or state."
    )
    risk_level: RiskLevel = Field(description="Assessed risk level: NONE, LOW, MEDIUM, or HIGH.")

    # Backward-compatible property aliases
    @property
    def is_true_positive(self) -> bool:
        return self.is_valid_target

    @property
    def refined_label(self) -> str:
        return self.refined_category

    @property
    def scene_description(self) -> str:
        return self.activity_summary

    @property
    def anomaly_level(self) -> str:
        return self.risk_level


_MOCK_PROFILES: Dict[str, Tuple[str, str, RiskLevel]] = {
    "person": (
        "courier",
        "A courier is walking briskly across the monitored corridor carrying a parcel.",
        "NONE",
    ),
    "backpack": (
        "unattended_backpack",
        "A backpack is stationary near the walkway boundary requiring attention.",
        "MEDIUM",
    ),
    "suitcase": (
        "abandoned_suitcase",
        "A hard-shell suitcase is left unattended in the transit zone.",
        "HIGH",
    ),
    "cell phone": (
        "handheld_device",
        "A person is holding up a smartphone in the monitored area.",
        "LOW",
    ),
    "car": (
        "patrol_vehicle",
        "A service vehicle is moving slowly along the access lane.",
        "NONE",
    ),
}


class VLMJudge:
    """Stage 3 Vision-Language Model structured semantic judge."""

    def __init__(
        self,
        config: Optional[CascadeConfig] = None,
        *,
        mock: Optional[bool] = None,
        mock_mode: Optional[bool] = None,
        mock_latency_s: Optional[float] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        self.config = config or CascadeConfig()
        self.mock_latency_s = self.config.mock_latency_s if mock_latency_s is None else float(mock_latency_s)

        resolved_key = api_key or self.config.openai_api_key or os.environ.get("OPENAI_API_KEY")
        resolved_base_url = base_url or self.config.openai_base_url or os.environ.get("OPENAI_BASE_URL")
        self.api_key: Optional[str] = resolved_key
        self.base_url: Optional[str] = resolved_base_url

        explicit_mock = mock if mock is not None else mock_mode
        if explicit_mock is not None:
            self.mock = bool(explicit_mock)
        elif self.config.mock_vlm:
            self.mock = True
        else:
            self.mock = False

        if not self.mock and not self.api_key and client is None:
            raise ValueError("Set OPENAI_API_KEY or explicitly enable mock_vlm / --mock-vlm")

        self.mock_mode = self.mock
        self._client: Optional[Any] = client
        if not self.mock and self._client is None:
            from openai import OpenAI

            client_kwargs: Dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": self.config.api_timeout_s,
                "max_retries": 0,
            }
            if self.base_url:
                client_kwargs["base_url"] = self.base_url
            self._client = OpenAI(**client_kwargs)

    def audit(self, task: VLMTask) -> SemanticVerdict:
        """Return a verdict; failures raise VLMAuditError and never fabricate results."""
        return self.audit_with_usage(task).verdict

    def audit_with_usage(self, task: VLMTask) -> AuditResult:
        """Return the verdict and measured provider usage for this particular request."""
        if self.mock:
            return AuditResult(self._mock_audit(task, simulate_delay=True), 0, 0, simulated=True)

        user_prompt = (
            f"YOLO + ByteTrack Context:\n"
            f"- Frame Index: {task.frame_idx}\n"
            f"- Track ID: {task.track_id}\n"
            f"- Predicted Class: {task.yolo_class}\n"
            f"- Confidence: {task.yolo_conf:.2f}\n"
            f"- Bounding Box: {task.bbox}\n"
            f"- Escalation Trigger Reason: {task.trigger_reason}\n\n"
            "Inspect the cropped ROI image and return a structured SemanticVerdict."
        )
        data_uri = task.roi_b64 if task.roi_b64.startswith("data:image/") else f"data:image/jpeg;base64,{task.roi_b64}"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_uri,
                            "detail": self.config.vlm_detail,
                        },
                    },
                ],
            },
        ]

        try:
            assert self._client is not None
            completion = self._client.beta.chat.completions.parse(
                model=self.config.openai_model,
                messages=messages,
                response_format=SemanticVerdict,
            )
        except Exception as exc:
            # Avoid copying provider messages, image payloads or credentials into logs.
            raise VLMAuditError(f"VLM request failed ({type(exc).__name__})") from exc

        usage = getattr(completion, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        choices = getattr(completion, "choices", [])
        message = choices[0].message if choices else None
        parsed = getattr(message, "parsed", None)
        if parsed is None or getattr(message, "refusal", None):
            raise VLMAuditError("VLM response refused or contained no parsed verdict", prompt_tokens, completion_tokens)
        return AuditResult(parsed, prompt_tokens, completion_tokens)

    def judge_roi(
        self,
        roi_base64: str,
        yolo_class: str,
        confidence: float,
        trigger_reason: str,
        track_id: Optional[int] = None,
    ) -> SemanticVerdict:
        """Convenience wrapper around :meth:`audit` for direct ROI evaluation."""
        synthetic_task = VLMTask(
            frame_idx=0,
            track_id=track_id if track_id is not None else 0,
            yolo_class=yolo_class,
            yolo_conf=confidence,
            bbox=[0, 0, 100, 100],
            trigger_reason=trigger_reason,
            roi_b64=roi_base64,
        )
        return self.audit(synthetic_task)

    def _mock_audit(self, task: VLMTask, simulate_delay: bool = True) -> SemanticVerdict:
        if simulate_delay and self.mock_latency_s > 0:
            time.sleep(self.mock_latency_s)

        cls_key = task.yolo_class.lower().strip()
        reason = task.trigger_reason.upper().strip()

        base_cat, base_summary, base_risk = _MOCK_PROFILES.get(
            cls_key,
            (
                f"verified_{cls_key.replace(' ', '_')}",
                f"Target '{task.yolo_class}' (track #{task.track_id}) verified under {reason}.",
                "LOW",
            ),
        )

        if reason in ("OBJECT_INTERACTION", "OCCLUSION_INTERACTION"):
            return SemanticVerdict(
                is_valid_target=True,
                refined_category=f"interacting_{base_cat}",
                activity_summary=(
                    f"Target #{task.track_id} ({task.yolo_class}) is engaged in close-range "
                    "object handoff or occlusion interaction."
                ),
                risk_level="MEDIUM" if base_risk in ("NONE", "LOW") else base_risk,
            )

        if reason in ("LOW_CONF_HARD_EXAMPLE", "AMBIGUOUS_CONFIDENCE"):
            if task.yolo_conf < 0.32:
                return SemanticVerdict(
                    is_valid_target=False,
                    refined_category="lighting_shadow_false_positive",
                    activity_summary=(
                        f"Low-confidence '{task.yolo_class}' ({task.yolo_conf:.2f}) rejected "
                        "as a transient lighting or background artifact."
                    ),
                    risk_level="NONE",
                )
            return SemanticVerdict(
                is_valid_target=True,
                refined_category=base_cat,
                activity_summary=(
                    f"Ambiguous '{task.yolo_class}' ({task.yolo_conf:.2f}) confirmed as "
                    f"valid {base_cat} after VLM semantic inspection."
                ),
                risk_level="LOW" if base_risk == "NONE" else base_risk,
            )

        return SemanticVerdict(
            is_valid_target=True,
            refined_category=base_cat,
            activity_summary=base_summary,
            risk_level=base_risk,
        )
