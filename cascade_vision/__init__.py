"""Cascaded video inspection with explicit simulation and usage telemetry."""

__version__ = "0.3.0"

from .config import CascadeConfig
from .detector import (
    CascadeTracker,
    Stage2Result,
    TrackedObject,
    TriggerReason,
    VLMTask,
    compute_iou,
)
from .events import CallBudget, JsonlEventWriter
from .motion import MotionGate, compute_padded_bbox, encode_roi_base64
from .pipeline import CascadePipeline, TokenAuditReport
from .vlm_judge import AnomalyLevel, AuditResult, RiskLevel, SemanticVerdict, VLMAuditError, VLMJudge

__all__ = [
    "__version__",
    "AnomalyLevel",
    "AuditResult",
    "VLMAuditError",
    "CallBudget",
    "JsonlEventWriter",
    "CascadeConfig",
    "CascadePipeline",
    "CascadeTracker",
    "MotionGate",
    "RiskLevel",
    "SemanticVerdict",
    "Stage2Result",
    "TokenAuditReport",
    "TrackedObject",
    "TriggerReason",
    "VLMJudge",
    "VLMTask",
    "compute_iou",
    "compute_padded_bbox",
    "encode_roi_base64",
]
