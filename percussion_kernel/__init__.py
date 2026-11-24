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
from .variation import (
    VariationFailure,
    VariationSpec,
    variate,
    variation_spec_from_dict,
    spec_to_dict,
)

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
    "VariationFailure",
    "VariationSpec",
    "variate",
    "variation_spec_from_dict",
    "spec_to_dict",
]
