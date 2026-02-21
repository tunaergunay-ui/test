from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Role(str, Enum):
    AUDITOR = "ROLE_AUDITOR"
    AUDIT_MANAGER = "ROLE_AUDIT_MANAGER"
    CAE = "ROLE_CAE"
    CONTROL_OWNER = "ROLE_CONTROL_OWNER"


class EngagementState(str, Enum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    IN_FIELDWORK = "IN_FIELDWORK"
    REVIEW_PENDING = "REVIEW_PENDING"
    REPORTED = "REPORTED"
    FOLLOW_UP = "FOLLOW_UP"
    CLOSED = "CLOSED"


class FindingState(str, Enum):
    OPEN = "OPEN"
    VALIDATED = "VALIDATED"
    AGREED_ACTION = "AGREED_ACTION"
    IN_REMEDIATION = "IN_REMEDIATION"
    RETEST = "RETEST"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    OVERDUE = "OVERDUE"


@dataclass
class RiskScore:
    inherent_risk: float
    residual_risk: float
    rating_band: str


@dataclass
class Risk:
    risk_id: str
    entity_id: str
    likelihood: int
    impact: int
    velocity: int
    regulatory_sensitivity: int
    control_effectiveness_index: float
    score: Optional[RiskScore] = None


@dataclass
class Engagement:
    engagement_id: str
    entity_id: str
    created_by: str
    created_role: Role
    state: EngagementState = EngagementState.DRAFT


@dataclass
class Finding:
    finding_id: str
    engagement_id: str
    created_by: str
    created_role: Role
    severity: str
    due_date: datetime
    state: FindingState = FindingState.OPEN
    final_approved_by: Optional[str] = None
    action_plan: Optional[str] = None
    action_owner: Optional[str] = None
    action_due_date: Optional[datetime] = None


@dataclass
class ImmutableAuditEvent:
    event_id: str
    event_type: str
    aggregate_id: str
    actor_id: str
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)
    previous_hash: str = ""
    current_hash: str = ""
