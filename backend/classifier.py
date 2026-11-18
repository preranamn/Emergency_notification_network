"""Emergency message classification and QoS parameter mapping."""

from dataclasses import asdict, dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class EmergencyProfile:
    category: str
    priority: int
    priority_label: str
    dscp: int
    dscp_name: str
    tos: int  # IPv4 ToS byte = dscp << 2 (ECN bits = 0)
    queue_id: int
    queue_label: str
    htb_priority: int
    of_priority: int
    color: str
    default_message: str

    def to_dict(self) -> Dict:
        return asdict(self)


PROFILES: Dict[str, EmergencyProfile] = {
    "MEDICAL": EmergencyProfile(
        category="MEDICAL",
        priority=1,
        priority_label="HIGHEST",
        dscp=46,
        dscp_name="EF",
        tos=46 << 2,  # 184 (0xB8)
        queue_id=1,
        queue_label="Queue 1 (Highest Priority)",
        htb_priority=0,
        of_priority=300,
        color="#f97316",  # Orange
        default_message="Urgent medical assistance required in Coach B2.",
    ),
    "FIRE": EmergencyProfile(
        category="FIRE",
        priority=2,
        priority_label="MEDIUM",
        dscp=34,
        dscp_name="AF41",
        tos=34 << 2,  # 136 (0x88)
        queue_id=2,
        queue_label="Queue 2 (Second-Highest Priority)",
        htb_priority=1,
        of_priority=200,
        color="#ef4444",  # Red
        default_message="Fire detected in Coach A3. Immediate attention required.",
    ),
    "GENERAL": EmergencyProfile(
        category="GENERAL",
        priority=3,
        priority_label="NORMAL",
        dscp=0,
        dscp_name="BE",
        tos=0,  # 0 (0x00)
        queue_id=3,
        queue_label="Queue 3 (Normal / Best-Effort)",
        htb_priority=3,
        of_priority=100,
        color="#eab308",  # Yellow
        default_message="General emergency announcement. Contact railway security.",
    ),
}

MEDICAL_KEYWORDS = (
    "medical", "doctor", "ambulance", "cardiac", "heart", "injury", "injured",
    "bleeding", "unconscious", "seizure", "paramedic", "health", "hospital",
    "breathing", "stroke", "patient", "first aid", "cpr",
)

FIRE_KEYWORDS = (
    "fire", "smoke", "flame", "flames", "burn", "burning", "explosion",
    "extinguisher", "blaze", "ignite", "spark", "hazard", "evacuate", "heat",
)


class EmergencyClassifier:
    """Classifies emergency notifications and assigns QoS DSCP/Queue profiles."""

    @staticmethod
    def get_profile(category: str) -> EmergencyProfile:
        key = (category or "").strip().upper()
        if key not in PROFILES:
            raise ValueError(f"Unsupported emergency category: {category!r}. Must be one of {list(PROFILES)}.")
        return PROFILES[key]

    @staticmethod
    def all_profiles() -> Dict[str, Dict]:
        return {k: v.to_dict() for k, v in PROFILES.items()}

    @classmethod
    def classify(cls, message: str, explicit_category: Optional[str] = None) -> EmergencyProfile:
        """Return an EmergencyProfile using explicit selection or keyword classification."""
        if explicit_category and explicit_category.strip().upper() != "AUTO":
            return cls.get_profile(explicit_category)

        text = (message or "").lower()
        if any(kw in text for kw in MEDICAL_KEYWORDS):
            return PROFILES["MEDICAL"]
        if any(kw in text for kw in FIRE_KEYWORDS):
            return PROFILES["FIRE"]
        return PROFILES["GENERAL"]

    @staticmethod
    def profile_from_dscp(dscp: int) -> EmergencyProfile:
        for profile in PROFILES.values():
            if profile.dscp == dscp:
                return profile
        return PROFILES["GENERAL"]
