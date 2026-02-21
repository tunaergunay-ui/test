import unittest
from datetime import datetime, timedelta

from iams.domain import Engagement, EngagementState, Finding, FindingState, Risk, Role
from iams.service import (
    AuthorizationError,
    ConflictError,
    IAMSService,
    NotFoundError,
    ValidationError,
    WorkflowError,
)


class IAMSServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixed_now = datetime(2025, 1, 1, 9, 0, 0)
        self.svc = IAMSService(now_provider=lambda: self.fixed_now)

    def _create_active_engagement(self, engagement_id: str) -> None:
        self.svc.create_engagement(Engagement(engagement_id, "ent-1", "u-aud", Role.AUDITOR))
        self.svc.transition_engagement(engagement_id, EngagementState.PLANNED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_engagement(engagement_id, EngagementState.IN_FIELDWORK, "u-mgr", Role.AUDIT_MANAGER)

    def test_risk_scoring(self):
        risk = Risk(
            risk_id="risk-1",
            entity_id="ent-1",
            likelihood=4,
            impact=5,
            velocity=3,
            regulatory_sensitivity=5,
            control_effectiveness_index=0.60,
        )
        self.svc.create_risk(risk, actor_id="u1")
        score = self.svc.score_risk("risk-1", actor_id="u1")
        self.assertAlmostEqual(score.inherent_risk, 75.0)
        self.assertAlmostEqual(score.residual_risk, 30.0)
        self.assertEqual(score.rating_band, "Medium")

    def test_risk_validation(self):
        invalid = Risk("r-bad", "ent-1", 9, 5, 3, 2, 0.2)
        with self.assertRaises(ValidationError):
            self.svc.create_risk(invalid, actor_id="u1")

    def test_engagement_transition_guard(self):
        self.svc.create_engagement(Engagement("eng-1", "ent-1", "u-aud", Role.AUDITOR))
        with self.assertRaises(AuthorizationError):
            self.svc.transition_engagement("eng-1", EngagementState.PLANNED, "u-aud", Role.AUDITOR)
        self.svc.transition_engagement("eng-1", EngagementState.PLANNED, "u-mgr", Role.AUDIT_MANAGER)
        self.assertEqual(self.svc.engagements["eng-1"].state, EngagementState.PLANNED)

    def test_create_finding_requires_valid_engagement_and_due_date(self):
        with self.assertRaises(NotFoundError):
            self.svc.create_finding(
                Finding("f-x", "eng-missing", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=5))
            )

        self._create_active_engagement("eng-2")
        with self.assertRaises(ValidationError):
            self.svc.create_finding(
                Finding("f-y", "eng-2", "u-aud", Role.AUDITOR, "High", self.fixed_now - timedelta(days=1))
            )

        with self.assertRaises(ValidationError):
            self.svc.create_finding(
                Finding("f-z", "eng-2", "u-aud", Role.AUDITOR, "Sev-1", self.fixed_now + timedelta(days=2))
            )


    def test_create_finding_requires_active_engagement_state(self):
        self.svc.create_engagement(Engagement("eng-s", "ent-1", "u-aud", Role.AUDITOR))
        with self.assertRaises(WorkflowError):
            self.svc.create_finding(
                Finding("f-s", "eng-s", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=5))
            )

        self.svc.transition_engagement("eng-s", EngagementState.PLANNED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_engagement("eng-s", EngagementState.IN_FIELDWORK, "u-mgr", Role.AUDIT_MANAGER)
        created = self.svc.create_finding(
            Finding("f-s", "eng-s", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=5))
        )
        self.assertEqual(created.finding_id, "f-s")

    def test_finding_sod_close_block(self):
        self._create_active_engagement("eng-3")
        f = Finding(
            finding_id="f-1",
            engagement_id="eng-3",
            created_by="u-aud",
            created_role=Role.AUDITOR,
            severity="High",
            due_date=self.fixed_now + timedelta(days=30),
        )
        self.svc.create_finding(f)
        self.svc.transition_finding("f-1", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-1", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.assign_finding_action_plan(
            finding_id="f-1",
            action_plan="Harden IAM role provisioning approvals",
            action_owner="owner-iam",
            action_due_date=self.fixed_now + timedelta(days=14),
            actor_id="u-mgr",
            actor_role=Role.AUDIT_MANAGER,
        )
        self.svc.transition_finding("f-1", FindingState.IN_REMEDIATION, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-1", FindingState.RETEST, "u-mgr", Role.AUDIT_MANAGER)

        with self.assertRaises(AuthorizationError):
            self.svc.transition_finding("f-1", FindingState.CLOSED, "u-aud", Role.AUDIT_MANAGER)

        self.svc.transition_finding("f-1", FindingState.CLOSED, "u-cae", Role.CAE)
        self.assertEqual(self.svc.findings["f-1"].final_approved_by, "u-cae")

    def test_mark_overdue_findings(self):
        self._create_active_engagement("eng-4")
        finding = Finding(
            finding_id="f-over",
            engagement_id="eng-4",
            created_by="u-aud",
            created_role=Role.AUDITOR,
            severity="Medium",
            due_date=self.fixed_now + timedelta(days=1),
        )
        self.svc.create_finding(finding)
        self.svc.transition_finding("f-over", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-over", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)

        transitioned = self.svc.mark_overdue_findings(self.fixed_now + timedelta(days=2))
        self.assertEqual(transitioned, 1)
        self.assertEqual(self.svc.findings["f-over"].state, FindingState.OVERDUE)

    def test_audit_chain_integrity(self):
        risk = Risk("risk-2", "ent-1", 3, 3, 3, 3, 0.5)
        self.svc.create_risk(risk, actor_id="u1")
        self.svc.score_risk("risk-2", actor_id="u1")
        self.assertTrue(self.svc.get_audit_chain_valid())


    def test_duplicate_ids_and_not_found_guards(self):
        risk = Risk("risk-dup", "ent-1", 3, 3, 3, 3, 0.4)
        self.svc.create_risk(risk, actor_id="u1")
        with self.assertRaises(ConflictError):
            self.svc.create_risk(risk, actor_id="u1")

        with self.assertRaises(NotFoundError):
            self.svc.score_risk("risk-missing", actor_id="u1")

        self._create_active_engagement("eng-dup")
        with self.assertRaises(ConflictError):
            self._create_active_engagement("eng-dup")

        self.svc.create_finding(Finding("f-dup", "eng-dup", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=5)))
        with self.assertRaises(ConflictError):
            self.svc.create_finding(Finding("f-dup", "eng-dup", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=10)))

    def test_assign_action_plan_authorization_and_validation(self):
        self._create_active_engagement("eng-5")
        self.svc.create_finding(Finding("f-ap", "eng-5", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=20)))

        with self.assertRaises(AuthorizationError):
            self.svc.assign_finding_action_plan(
                finding_id="f-ap",
                action_plan="Rotate privileged credentials",
                action_owner="owner-1",
                action_due_date=self.fixed_now + timedelta(days=10),
                actor_id="u-aud",
                actor_role=Role.AUDITOR,
            )

        self.svc.transition_finding("f-ap", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        updated = self.svc.assign_finding_action_plan(
            finding_id="f-ap",
            action_plan="Rotate privileged credentials",
            action_owner="owner-1",
            action_due_date=self.fixed_now + timedelta(days=10),
            actor_id="u-mgr",
            actor_role=Role.AUDIT_MANAGER,
        )
        self.assertEqual(updated.action_owner, "owner-1")

        with self.assertRaises(ValidationError):
            self.svc.assign_finding_action_plan(
                finding_id="f-ap",
                action_plan="   ",
                action_owner="owner-1",
                action_due_date=self.fixed_now + timedelta(days=10),
                actor_id="u-mgr",
                actor_role=Role.AUDIT_MANAGER,
            )

        with self.assertRaises(ValidationError):
            self.svc.assign_finding_action_plan(
                finding_id="f-ap",
                action_plan="Rotate privileged credentials",
                action_owner="  ",
                action_due_date=self.fixed_now + timedelta(days=10),
                actor_id="u-mgr",
                actor_role=Role.AUDIT_MANAGER,
            )

        with self.assertRaises(ValidationError):
            self.svc.assign_finding_action_plan(
                finding_id="f-ap",
                action_plan="Rotate privileged credentials",
                action_owner="owner-1",
                action_due_date=self.fixed_now + timedelta(days=25),
                actor_id="u-mgr",
                actor_role=Role.AUDIT_MANAGER,
            )

    def test_in_remediation_requires_assigned_action_plan(self):
        self._create_active_engagement("eng-rem")
        self.svc.create_finding(Finding("f-rem", "eng-rem", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=10)))
        self.svc.transition_finding("f-rem", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-rem", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)

        with self.assertRaises(WorkflowError):
            self.svc.transition_finding("f-rem", FindingState.IN_REMEDIATION, "u-mgr", Role.AUDIT_MANAGER)

        self.svc.assign_finding_action_plan(
            finding_id="f-rem",
            action_plan="Implement quarterly privileged access recertification",
            action_owner="owner-rem",
            action_due_date=self.fixed_now + timedelta(days=7),
            actor_id="u-mgr",
            actor_role=Role.AUDIT_MANAGER,
        )
        updated = self.svc.transition_finding("f-rem", FindingState.IN_REMEDIATION, "u-mgr", Role.AUDIT_MANAGER)
        self.assertEqual(updated.state, FindingState.IN_REMEDIATION)

    def test_reschedule_due_date_rules_and_overdue_recovery(self):
        self._create_active_engagement("eng-rd")
        self.svc.create_finding(Finding("f-rd", "eng-rd", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=3)))
        self.svc.transition_finding("f-rd", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-rd", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.assign_finding_action_plan(
            finding_id="f-rd",
            action_plan="Enforce quarterly access review",
            action_owner="owner-rd",
            action_due_date=self.fixed_now + timedelta(days=2),
            actor_id="u-mgr",
            actor_role=Role.AUDIT_MANAGER,
        )
        self.svc.transition_finding("f-rd", FindingState.IN_REMEDIATION, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.mark_overdue_findings(self.fixed_now + timedelta(days=4))
        self.assertEqual(self.svc.findings["f-rd"].state, FindingState.OVERDUE)

        with self.assertRaises(AuthorizationError):
            self.svc.reschedule_finding_due_date("f-rd", self.fixed_now + timedelta(days=6), "u-aud", Role.AUDITOR)

        with self.assertRaises(ValidationError):
            self.svc.reschedule_finding_due_date("f-rd", self.fixed_now + timedelta(days=1), "u-mgr", Role.AUDIT_MANAGER)

        updated = self.svc.reschedule_finding_due_date("f-rd", self.fixed_now + timedelta(days=8), "u-mgr", Role.AUDIT_MANAGER)
        self.assertEqual(updated.state, FindingState.IN_REMEDIATION)
        self.assertEqual(updated.due_date, self.fixed_now + timedelta(days=8))

    def test_reschedule_blocked_for_closed_finding(self):
        self._create_active_engagement("eng-cl")
        self.svc.create_finding(Finding("f-cl", "eng-cl", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=20)))
        self.svc.transition_finding("f-cl", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-cl", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.assign_finding_action_plan(
            finding_id="f-cl",
            action_plan="Apply hardening baseline",
            action_owner="owner-cl",
            action_due_date=self.fixed_now + timedelta(days=10),
            actor_id="u-mgr",
            actor_role=Role.AUDIT_MANAGER,
        )
        self.svc.transition_finding("f-cl", FindingState.IN_REMEDIATION, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-cl", FindingState.RETEST, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-cl", FindingState.CLOSED, "u-cae", Role.CAE)

        with self.assertRaises(WorkflowError):
            self.svc.reschedule_finding_due_date("f-cl", self.fixed_now + timedelta(days=30), "u-mgr", Role.AUDIT_MANAGER)

    def test_kpi_summary(self):
        self._create_active_engagement("eng-kpi")
        self.svc.create_finding(Finding("f-k1", "eng-kpi", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=2)))
        self.svc.create_finding(Finding("f-k2", "eng-kpi", "u-aud", Role.AUDITOR, "Medium", self.fixed_now + timedelta(days=2)))
        self.svc.transition_finding("f-k1", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-k1", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.mark_overdue_findings(self.fixed_now + timedelta(days=5))

        summary = self.svc.summarize_kpis()
        self.assertEqual(summary["total_findings"], 2)
        self.assertEqual(summary["overdue_findings"], 1)
        self.assertEqual(summary["findings_by_severity"]["High"], 1)

    def test_export_snapshot_uses_time_provider(self):
        self._create_active_engagement("eng-exp")
        self.svc.create_finding(Finding("f-exp", "eng-exp", "u-aud", Role.AUDITOR, "Low", self.fixed_now + timedelta(days=2)))
        snapshot = self.svc.export_snapshot()
        self.assertEqual(snapshot["generated_at"], self.fixed_now.isoformat())
        self.assertEqual(snapshot["engagements"][0]["state"], EngagementState.IN_FIELDWORK.value)
        self.assertIsInstance(snapshot["findings"][0]["due_date"], str)
        self.assertEqual(snapshot["findings"][0]["created_role"], Role.AUDITOR.value)

    def test_finding_timeline_returns_serialized_events(self):
        self._create_active_engagement("eng-tl")
        self.svc.create_finding(Finding("f-tl", "eng-tl", "u-aud", Role.AUDITOR, "Medium", self.fixed_now + timedelta(days=10)))
        self.svc.transition_finding("f-tl", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)

        timeline = self.svc.get_finding_timeline("f-tl")
        self.assertGreaterEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["event_type"], "FINDING_CREATED")
        self.assertIsInstance(timeline[0]["occurred_at"], str)

        with self.assertRaises(NotFoundError):
            self.svc.get_finding_timeline("f-missing")


    def test_finding_timeline_supports_filtering_and_limit(self):
        self._create_active_engagement("eng-tlf")
        self.svc.create_finding(Finding("f-tlf", "eng-tlf", "u-aud", Role.AUDITOR, "Medium", self.fixed_now + timedelta(days=10)))
        self.svc.transition_finding("f-tlf", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-tlf", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)

        transitioned_only = self.svc.get_finding_timeline("f-tlf", event_type="FINDING_TRANSITIONED")
        self.assertEqual(len(transitioned_only), 2)
        self.assertTrue(all(e["event_type"] == "FINDING_TRANSITIONED" for e in transitioned_only))

        latest_one = self.svc.get_finding_timeline("f-tlf", limit=1)
        self.assertEqual(len(latest_one), 1)
        self.assertEqual(latest_one[0]["event_type"], "FINDING_CREATED")

        paged = self.svc.get_finding_timeline("f-tlf", offset=1, limit=2)
        self.assertEqual(len(paged), 2)
        self.assertTrue(all(e["event_type"] == "FINDING_TRANSITIONED" for e in paged))

    def test_finding_timeline_rejects_invalid_filters(self):
        self._create_active_engagement("eng-tlv")
        self.svc.create_finding(Finding("f-tlv", "eng-tlv", "u-aud", Role.AUDITOR, "Low", self.fixed_now + timedelta(days=10)))

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tlv", event_type="   ")

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tlv", event_type=True)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page("f-tlv", event_type=123)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tlv", event_type="FINDING_UNKNOWN")

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page("f-tlv", event_type="FINDING_UNKNOWN")

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tlv", limit=0)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tlv", offset=-1)


    def test_finding_timeline_normalizes_event_type_and_limit_type(self):
        self._create_active_engagement("eng-tln")
        self.svc.create_finding(Finding("f-tln", "eng-tln", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=7)))
        self.svc.transition_finding("f-tln", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)

        timeline = self.svc.get_finding_timeline("f-tln", event_type="  FINDING_TRANSITIONED  ")
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["event_type"], "FINDING_TRANSITIONED")

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tln", limit=True)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tln", offset=True)


    def test_finding_timeline_page_includes_total_and_count(self):
        self._create_active_engagement("eng-tlp")
        self.svc.create_finding(Finding("f-tlp", "eng-tlp", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=8)))
        self.svc.transition_finding("f-tlp", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-tlp", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)

        page = self.svc.get_finding_timeline_page("f-tlp", offset=1, limit=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["count"], 2)
        self.assertEqual(page["offset"], 1)
        self.assertEqual(page["limit"], 2)
        self.assertEqual(page["max_limit"], self.svc.MAX_TIMELINE_PAGE_LIMIT)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(len(page["items"]), 2)

        early_page = self.svc.get_finding_timeline_page("f-tlp", offset=0, limit=2)
        self.assertTrue(early_page["has_more"])
        self.assertEqual(early_page["next_offset"], 2)

        tail_page = self.svc.get_finding_timeline_page("f-tlp", offset=3, limit=2)
        self.assertEqual(tail_page["count"], 0)
        self.assertFalse(tail_page["has_more"])
        self.assertIsNone(tail_page["next_offset"])


    def test_finding_timeline_page_applies_event_type_filter(self):
        self._create_active_engagement("eng-tlpf")
        self.svc.create_finding(Finding("f-tlpf", "eng-tlpf", "u-aud", Role.AUDITOR, "High", self.fixed_now + timedelta(days=8)))
        self.svc.transition_finding("f-tlpf", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-tlpf", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)

        page = self.svc.get_finding_timeline_page(
            "f-tlpf",
            event_type="  FINDING_TRANSITIONED  ",
            offset=0,
            limit=10,
        )
        self.assertEqual(page["event_type"], "FINDING_TRANSITIONED")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["count"], 2)
        self.assertTrue(all(item["event_type"] == "FINDING_TRANSITIONED" for item in page["items"]))

    def test_finding_timeline_supports_desc_sort_order(self):
        self._create_active_engagement("eng-tls")
        self.svc.create_finding(Finding("f-tls", "eng-tls", "u-aud", Role.AUDITOR, "Medium", self.fixed_now + timedelta(days=10)))
        self.svc.transition_finding("f-tls", FindingState.VALIDATED, "u-mgr", Role.AUDIT_MANAGER)
        self.svc.transition_finding("f-tls", FindingState.AGREED_ACTION, "u-mgr", Role.AUDIT_MANAGER)

        timeline = self.svc.get_finding_timeline("f-tls", sort_order="desc")
        self.assertEqual(timeline[0]["event_type"], "FINDING_TRANSITIONED")
        self.assertEqual(timeline[-1]["event_type"], "FINDING_CREATED")

        page = self.svc.get_finding_timeline_page("f-tls", sort_order=" DESC ", limit=2)
        self.assertEqual(page["sort_order"], "desc")
        self.assertEqual(page["items"][0]["event_type"], "FINDING_TRANSITIONED")

    def test_finding_timeline_rejects_invalid_sort_order(self):
        self._create_active_engagement("eng-tlsv")
        self.svc.create_finding(Finding("f-tlsv", "eng-tlsv", "u-aud", Role.AUDITOR, "Low", self.fixed_now + timedelta(days=10)))

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tlsv", sort_order="latest")

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page("f-tlsv", sort_order=" ")

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline("f-tlsv", sort_order=None)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page("f-tlsv", sort_order=True)

    def test_finding_timeline_page_reuses_timeline_validations(self):
        self._create_active_engagement("eng-tlpv")
        self.svc.create_finding(Finding("f-tlpv", "eng-tlpv", "u-aud", Role.AUDITOR, "Medium", self.fixed_now + timedelta(days=9)))

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page("f-tlpv", limit=0)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page("f-tlpv", offset=-1)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page("f-tlpv", offset=True)

        with self.assertRaises(ValidationError):
            self.svc.get_finding_timeline_page(
                "f-tlpv",
                limit=self.svc.MAX_TIMELINE_PAGE_LIMIT + 1,
            )

    def test_audit_event_timestamp_uses_time_provider(self):
        self.svc.create_engagement(Engagement("eng-time", "ent-1", "u-aud", Role.AUDITOR))
        self.assertEqual(self.svc.audit_events[-1].occurred_at, self.fixed_now)


if __name__ == "__main__":
    unittest.main()
