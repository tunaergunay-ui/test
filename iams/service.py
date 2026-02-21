from __future__ import annotations

import hashlib
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List

from .domain import (
    Engagement,
    EngagementState,
    Finding,
    FindingState,
    ImmutableAuditEvent,
    Risk,
    RiskScore,
    Role,
)


class AuthorizationError(Exception):
    pass


class WorkflowError(Exception):
    pass


class ValidationError(Exception):
    pass


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class IAMSService:
    ALLOWED_FINDING_SEVERITIES = {"Low", "Medium", "High", "Critical"}
    FINDING_CREATION_ALLOWED_ENGAGEMENT_STATES = {
        EngagementState.IN_FIELDWORK,
        EngagementState.REVIEW_PENDING,
        EngagementState.REPORTED,
        EngagementState.FOLLOW_UP,
    }
    MAX_TIMELINE_PAGE_LIMIT = 200
    EMITTED_AUDIT_EVENT_TYPES = frozenset(
        {
            "RISK_CREATED",
            "RISK_SCORED",
            "ENGAGEMENT_CREATED",
            "ENGAGEMENT_TRANSITIONED",
            "FINDING_CREATED",
            "FINDING_TRANSITIONED",
            "FINDING_ACTION_PLAN_ASSIGNED",
            "FINDING_DUE_DATE_RESCHEDULED",
            "FINDING_OVERDUE",
        }
    )
    ALLOWED_TIMELINE_EVENT_TYPES = EMITTED_AUDIT_EVENT_TYPES
    ENGAGEMENT_TRANSITIONS = {
        EngagementState.DRAFT: {EngagementState.PLANNED},
        EngagementState.PLANNED: {EngagementState.IN_FIELDWORK},
        EngagementState.IN_FIELDWORK: {EngagementState.REVIEW_PENDING},
        EngagementState.REVIEW_PENDING: {EngagementState.REPORTED},
        EngagementState.REPORTED: {EngagementState.FOLLOW_UP},
        EngagementState.FOLLOW_UP: {EngagementState.CLOSED},
    }
    FINDING_TRANSITIONS = {
        FindingState.OPEN: {FindingState.VALIDATED, FindingState.REJECTED},
        FindingState.VALIDATED: {FindingState.AGREED_ACTION},
        FindingState.AGREED_ACTION: {FindingState.IN_REMEDIATION, FindingState.OVERDUE},
        FindingState.IN_REMEDIATION: {FindingState.RETEST, FindingState.OVERDUE},
        FindingState.RETEST: {FindingState.CLOSED},
        FindingState.OVERDUE: {FindingState.IN_REMEDIATION},
    }

    def __init__(self, now_provider: Callable[[], datetime] | None = None) -> None:
        self.risks: Dict[str, Risk] = {}
        self.engagements: Dict[str, Engagement] = {}
        self.findings: Dict[str, Finding] = {}
        self.audit_events: List[ImmutableAuditEvent] = []
        self._now_provider = now_provider or datetime.utcnow

    def _now(self) -> datetime:
        return self._now_provider()

    @classmethod
    def get_allowed_timeline_event_types(cls) -> List[str]:
        return sorted(cls.ALLOWED_TIMELINE_EVENT_TYPES)

    @classmethod
    def get_allowed_engagement_transitions(cls, from_state: EngagementState) -> List[str]:
        return sorted(state.value for state in cls.ENGAGEMENT_TRANSITIONS.get(from_state, set()))

    @classmethod
    def get_allowed_finding_transitions(cls, from_state: FindingState) -> List[str]:
        return sorted(state.value for state in cls.FINDING_TRANSITIONS.get(from_state, set()))

    def _append_event(self, event_type: str, aggregate_id: str, actor_id: str) -> ImmutableAuditEvent:
        if event_type not in self.EMITTED_AUDIT_EVENT_TYPES:
            allowed = ", ".join(self.get_allowed_timeline_event_types())
            raise ValidationError(f"event_type must be one of: {allowed}")
        event = ImmutableAuditEvent(
            event_id=f"evt-{len(self.audit_events)+1}",
            event_type=event_type,
            aggregate_id=aggregate_id,
            actor_id=actor_id,
            occurred_at=self._now(),
        )
        previous_hash = self.audit_events[-1].current_hash if self.audit_events else "GENESIS"
        payload = f"{event.event_id}|{event.event_type}|{event.aggregate_id}|{event.actor_id}|{event.occurred_at.isoformat()}|{previous_hash}"
        event.previous_hash = previous_hash
        event.current_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.audit_events.append(event)
        return event

    @staticmethod
    def _score_band(residual_risk: float) -> str:
        if residual_risk >= 75:
            return "Critical"
        if residual_risk >= 50:
            return "High"
        if residual_risk >= 25:
            return "Medium"
        return "Low"

    @staticmethod
    def _validate_risk(risk: Risk) -> None:
        for field_name in ["likelihood", "impact", "velocity", "regulatory_sensitivity"]:
            value = getattr(risk, field_name)
            if not 1 <= value <= 5:
                raise ValidationError(f"{field_name} must be in range 1..5")
        if not 0 <= risk.control_effectiveness_index <= 1:
            raise ValidationError("control_effectiveness_index must be between 0 and 1")

    def create_risk(self, risk: Risk, actor_id: str) -> Risk:
        self._validate_risk(risk)
        if risk.risk_id in self.risks:
            raise ConflictError(f"risk_id already exists: {risk.risk_id}")
        self.risks[risk.risk_id] = risk
        self._append_event("RISK_CREATED", risk.risk_id, actor_id)
        return risk

    def score_risk(self, risk_id: str, actor_id: str) -> RiskScore:
        risk = self.risks.get(risk_id)
        if risk is None:
            raise NotFoundError(f"risk_id not found: {risk_id}")
        inherent = (risk.likelihood * risk.impact * risk.velocity * risk.regulatory_sensitivity) / 4
        residual = inherent * (1 - risk.control_effectiveness_index)
        score = RiskScore(
            inherent_risk=round(inherent, 2),
            residual_risk=round(residual, 2),
            rating_band=self._score_band(residual),
        )
        risk.score = score
        self._append_event("RISK_SCORED", risk_id, actor_id)
        return score

    def create_engagement(self, engagement: Engagement) -> Engagement:
        if engagement.engagement_id in self.engagements:
            raise ConflictError(f"engagement_id already exists: {engagement.engagement_id}")
        self.engagements[engagement.engagement_id] = engagement
        self._append_event("ENGAGEMENT_CREATED", engagement.engagement_id, engagement.created_by)
        return engagement

    def transition_engagement(self, engagement_id: str, to_state: EngagementState, actor_id: str, actor_role: Role) -> Engagement:
        engagement = self.engagements.get(engagement_id)
        if engagement is None:
            raise NotFoundError(f"engagement_id not found: {engagement_id}")
        if to_state not in self.ENGAGEMENT_TRANSITIONS.get(engagement.state, set()):
            allowed = self.get_allowed_engagement_transitions(engagement.state)
            raise WorkflowError(
                f"Invalid engagement transition: {engagement.state.value} -> {to_state.value}. "
                f"Allowed: {allowed or 'none'}"
            )

        if engagement.state == EngagementState.DRAFT and to_state == EngagementState.PLANNED:
            if actor_role not in {Role.AUDIT_MANAGER, Role.CAE}:
                raise AuthorizationError("Only manager/CAE can move DRAFT to PLANNED.")

        engagement.state = to_state
        self._append_event("ENGAGEMENT_TRANSITIONED", engagement_id, actor_id)
        return engagement

    @classmethod
    def _validate_finding_severity(cls, severity: str) -> None:
        if severity not in cls.ALLOWED_FINDING_SEVERITIES:
            allowed = ", ".join(sorted(cls.ALLOWED_FINDING_SEVERITIES))
            raise ValidationError(f"severity must be one of: {allowed}")

    def create_finding(self, finding: Finding) -> Finding:
        engagement = self.engagements.get(finding.engagement_id)
        if engagement is None:
            raise NotFoundError("engagement_id not found for finding creation")
        if engagement.state not in self.FINDING_CREATION_ALLOWED_ENGAGEMENT_STATES:
            raise WorkflowError("finding creation requires engagement in active audit lifecycle state")
        self._validate_finding_severity(finding.severity)
        if finding.due_date <= self._now():
            raise ValidationError("finding due_date must be in the future")

        if finding.finding_id in self.findings:
            raise ConflictError(f"finding_id already exists: {finding.finding_id}")
        self.findings[finding.finding_id] = finding
        self._append_event("FINDING_CREATED", finding.finding_id, finding.created_by)
        return finding

    @staticmethod
    def _has_remediation_plan(finding: Finding) -> bool:
        return bool(
            finding.action_plan
            and finding.action_plan.strip()
            and finding.action_owner
            and finding.action_owner.strip()
            and finding.action_due_date is not None
        )

    def transition_finding(self, finding_id: str, to_state: FindingState, actor_id: str, actor_role: Role) -> Finding:
        finding = self.findings.get(finding_id)
        if finding is None:
            raise NotFoundError(f"finding_id not found: {finding_id}")
        if to_state not in self.FINDING_TRANSITIONS.get(finding.state, set()):
            allowed = self.get_allowed_finding_transitions(finding.state)
            raise WorkflowError(
                f"Invalid finding transition: {finding.state.value} -> {to_state.value}. "
                f"Allowed: {allowed or 'none'}"
            )

        if to_state == FindingState.CLOSED and actor_role not in {Role.AUDIT_MANAGER, Role.CAE}:
            raise AuthorizationError("Only manager/CAE may close a finding.")

        # SoD: creator cannot final close their own finding
        if to_state == FindingState.CLOSED and finding.created_by == actor_id:
            raise AuthorizationError("SoD violation: creator cannot close same finding.")

        if to_state == FindingState.IN_REMEDIATION and not self._has_remediation_plan(finding):
            raise WorkflowError("Cannot enter IN_REMEDIATION without assigned action plan, owner, and due date.")

        finding.state = to_state
        if to_state == FindingState.CLOSED:
            finding.final_approved_by = actor_id
        self._append_event("FINDING_TRANSITIONED", finding_id, actor_id)
        return finding

    def assign_finding_action_plan(
        self,
        finding_id: str,
        action_plan: str,
        action_owner: str,
        action_due_date: datetime,
        actor_id: str,
        actor_role: Role,
    ) -> Finding:
        finding = self.findings.get(finding_id)
        if finding is None:
            raise NotFoundError(f"finding_id not found: {finding_id}")
        if actor_role not in {Role.AUDIT_MANAGER, Role.CAE}:
            raise AuthorizationError("Only manager/CAE may assign remediation action plans.")
        if finding.state not in {FindingState.VALIDATED, FindingState.AGREED_ACTION, FindingState.IN_REMEDIATION, FindingState.OVERDUE}:
            raise WorkflowError("Action plan can only be assigned on validated or active remediation findings.")
        if not action_plan.strip():
            raise ValidationError("action_plan cannot be empty")
        if not action_owner.strip():
            raise ValidationError("action_owner cannot be empty")
        if action_due_date <= self._now():
            raise ValidationError("action_due_date must be in the future")
        if action_due_date > finding.due_date:
            raise ValidationError("action_due_date cannot exceed finding due_date")

        finding.action_plan = action_plan
        finding.action_owner = action_owner
        finding.action_due_date = action_due_date
        self._append_event("FINDING_ACTION_PLAN_ASSIGNED", finding_id, actor_id)
        return finding

    def reschedule_finding_due_date(
        self,
        finding_id: str,
        new_due_date: datetime,
        actor_id: str,
        actor_role: Role,
    ) -> Finding:
        finding = self.findings.get(finding_id)
        if finding is None:
            raise NotFoundError(f"finding_id not found: {finding_id}")
        if actor_role not in {Role.AUDIT_MANAGER, Role.CAE}:
            raise AuthorizationError("Only manager/CAE may reschedule finding due dates.")
        if finding.state in {FindingState.CLOSED, FindingState.REJECTED}:
            raise WorkflowError("Cannot reschedule due date for closed/rejected findings.")
        if new_due_date <= self._now():
            raise ValidationError("new_due_date must be in the future")
        if finding.action_due_date and new_due_date < finding.action_due_date:
            raise ValidationError("new_due_date cannot be earlier than action_due_date")

        finding.due_date = new_due_date
        if finding.state == FindingState.OVERDUE:
            finding.state = FindingState.IN_REMEDIATION
        self._append_event("FINDING_DUE_DATE_RESCHEDULED", finding_id, actor_id)
        return finding

    def mark_overdue_findings(self, now: datetime, actor_id: str = "system") -> int:
        transitioned = 0
        for finding in self.findings.values():
            if finding.state in {FindingState.CLOSED, FindingState.REJECTED, FindingState.OVERDUE}:
                continue
            if finding.due_date < now and finding.state in {FindingState.AGREED_ACTION, FindingState.IN_REMEDIATION}:
                finding.state = FindingState.OVERDUE
                transitioned += 1
                self._append_event("FINDING_OVERDUE", finding.finding_id, actor_id)
        return transitioned

    def get_audit_chain_valid(self) -> bool:
        prev = "GENESIS"
        for event in self.audit_events:
            payload = f"{event.event_id}|{event.event_type}|{event.aggregate_id}|{event.actor_id}|{event.occurred_at.isoformat()}|{prev}"
            current = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if event.previous_hash != prev or event.current_hash != current:
                return False
            prev = current
        return True

    def summarize_kpis(self) -> dict:
        total_findings = len(self.findings)
        by_state = {state.value: 0 for state in FindingState}
        by_severity = {severity: 0 for severity in sorted(self.ALLOWED_FINDING_SEVERITIES)}

        for finding in self.findings.values():
            by_state[finding.state.value] += 1
            if finding.severity in by_severity:
                by_severity[finding.severity] += 1

        return {
            "total_risks": len(self.risks),
            "total_engagements": len(self.engagements),
            "total_findings": total_findings,
            "open_findings": total_findings - by_state[FindingState.CLOSED.value] - by_state[FindingState.REJECTED.value],
            "overdue_findings": by_state[FindingState.OVERDUE.value],
            "findings_by_state": by_state,
            "findings_by_severity": by_severity,
        }


    def _serialize_value(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        if is_dataclass(value):
            return {k: self._serialize_value(v) for k, v in asdict(value).items()}
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._serialize_value(v) for v in value]
        return value

    @classmethod
    def _normalize_event_type(cls, event_type: str | None) -> str | None:
        if event_type is None:
            return None
        if not isinstance(event_type, str):
            raise ValidationError("event_type must be a string when provided")
        normalized_event_type = event_type.strip()
        if not normalized_event_type:
            raise ValidationError("event_type cannot be blank when provided")
        if normalized_event_type not in cls.ALLOWED_TIMELINE_EVENT_TYPES:
            allowed = ", ".join(cls.get_allowed_timeline_event_types())
            raise ValidationError(f"event_type must be one of: {allowed}")
        return normalized_event_type

    @staticmethod
    def _validate_limit(limit: int | None, max_limit: int | None = None) -> None:
        if limit is None:
            return
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError("limit must be a positive integer")
        if max_limit is not None and limit > max_limit:
            raise ValidationError(f"limit cannot exceed {max_limit}")

    @staticmethod
    def _validate_offset(offset: int) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be a non-negative integer")

    def _get_filtered_finding_events(self, finding_id: str, event_type: str | None) -> tuple[List[ImmutableAuditEvent], str | None]:
        if finding_id not in self.findings:
            raise NotFoundError(f"finding_id not found: {finding_id}")

        normalized_event_type = self._normalize_event_type(event_type)
        events = [e for e in self.audit_events if e.aggregate_id == finding_id]
        events.sort(key=lambda e: (e.occurred_at, e.event_id))

        if normalized_event_type is None:
            return events, None
        return [e for e in events if e.event_type == normalized_event_type], normalized_event_type

    @staticmethod
    def _normalize_sort_order(sort_order: str) -> str:
        if not isinstance(sort_order, str):
            raise ValidationError("sort_order must be a string: 'asc' or 'desc'")
        normalized_sort_order = sort_order.strip().lower()
        if normalized_sort_order not in {"asc", "desc"}:
            raise ValidationError("sort_order must be either 'asc' or 'desc'")
        return normalized_sort_order

    def get_finding_timeline(
        self,
        finding_id: str,
        event_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort_order: str = "asc",
    ) -> List[dict]:
        self._validate_limit(limit)
        self._validate_offset(offset)
        normalized_sort_order = self._normalize_sort_order(sort_order)

        events, _ = self._get_filtered_finding_events(finding_id=finding_id, event_type=event_type)
        if normalized_sort_order == "desc":
            events = list(reversed(events))
        events = events[offset:]
        if limit is not None:
            events = events[:limit]

        return [self._serialize_value(e) for e in events]

    def get_finding_timeline_page(
        self,
        finding_id: str,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_order: str = "asc",
    ) -> dict:
        self._validate_limit(limit, max_limit=self.MAX_TIMELINE_PAGE_LIMIT)
        self._validate_offset(offset)
        normalized_sort_order = self._normalize_sort_order(sort_order)

        filtered_events, normalized_event_type = self._get_filtered_finding_events(
            finding_id=finding_id,
            event_type=event_type,
        )
        if normalized_sort_order == "desc":
            filtered_events = list(reversed(filtered_events))
        total = len(filtered_events)
        page_events = filtered_events[offset : offset + limit]
        paged_items = [self._serialize_value(e) for e in page_events]

        has_more = (offset + len(paged_items)) < total
        return {
            "finding_id": finding_id,
            "event_type": normalized_event_type,
            "sort_order": normalized_sort_order,
            "offset": offset,
            "limit": limit,
            "max_limit": self.MAX_TIMELINE_PAGE_LIMIT,
            "total": total,
            "count": len(paged_items),
            "has_more": has_more,
            "next_offset": (offset + len(paged_items)) if has_more else None,
            "items": paged_items,
        }

    def export_snapshot(self) -> dict:
        return {
            "risks": [self._serialize_value(v) for v in self.risks.values()],
            "engagements": [self._serialize_value(v) for v in self.engagements.values()],
            "findings": [self._serialize_value(v) for v in self.findings.values()],
            "audit_events": [self._serialize_value(v) for v in self.audit_events],
            "generated_at": self._now().isoformat(),
        }
