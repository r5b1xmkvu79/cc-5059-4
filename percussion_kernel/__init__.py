"""打击乐节奏创作内核：约束求解 + 候选排序 + 时移/力度 + 音频渲染。"""

from .model import (
    ACCENT_SOFT,
    ACCENT_LOCKED,
    VoiceSpec,
    Relation,
    Config,
    Event,
    config_from_dict,
    config_to_dict,
    default_config,
)
from .solver import SolveFailure, generate

__all__ = [
    "ACCENT_SOFT",
    "ACCENT_LOCKED",
    "VoiceSpec",
    "Relation",
    "Config",
    "Event",
    "config_from_dict",
    "config_to_dict",
    "default_config",
    "SolveFailure",
    "generate",
]
