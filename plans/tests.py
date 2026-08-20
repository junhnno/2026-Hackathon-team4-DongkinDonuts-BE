import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from common.models import ActivityTag, StateOption
from context.models import (
    NextActivityPlan,
    NextActivityPlanActivityTag,
    UserContextSnapshot,
    UserContextSnapshotState,
)
from context.utils import today_for_user
from digital_state.models import PcUsagePattern
from routines.models import (
    ActivityType,
    RoutineInstance,
    RoutineInstanceStatus,
    StageType,
)
from sessions_app.models import (
    DifficultyFeedback,
    RecoveryFeeling,
    Session,
    SessionFeedback,
    SessionStatus,
)

from .models import (
    AIInsight,
    AIPlanRun,
    InsightType,
    Notification,
    NotificationKind,
    NotificationStatus,
    PlanStatus,
    RecoveryPlan,
    RecoverySlot,
    SlotNotificationBasis,
    SlotStatus,
    WebPushSubscription,
)
from .services import (
    OPEN_SLOT_STATUSES,
    build_policy_recommended_slots,
    create_or_replace_today_plan,
    has_today_pc_usage_pattern,
    reset_next_activity_and_slot,
    schedule_next_slot_after_completed_slot,
    set_slot_notification,
    today_day_of_week_for_user,
)
from .web_push import send_due_notifications


class RecoveryPlanServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(id=uuid.uuid4())
        self.state, _ = StateOption.objects.get_or_create(
            code="EYE_TIRED",
            defaults={"label": "눈이 피곤해요"},
        )
        self.activity_tag = ActivityTag.objects.create(code="CODING", name="코딩")
        self.context_snapshot = UserContextSnapshot.objects.create(
            user=self.user,
            service_date=today_for_user(self.user),
        )
        UserContextSnapshotState.objects.create(
            context_snapshot=self.context_snapshot,
            state=self.state,
            priority=1,
        )
        self.next_activity_plan = NextActivityPlan.objects.create(
            user=self.user,
            context_snapshot=self.context_snapshot,
            service_date=today_for_user(self.user),
            expected_activity_minutes=60,
        )
        NextActivityPlanActivityTag.objects.create(
            next_activity_plan=self.next_activity_plan,
            activity_tag=self.activity_tag,
        )

    def test_plan_without_pc_pattern_preserves_snapshot_slots_for_activity_window(self):
        first_time = timezone.now() + timedelta(minutes=60)
        second_time = timezone.now() + timedelta(minutes=120)

        plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_times=[first_time, second_time],
        )

        self.assertEqual(plan.status, PlanStatus.ACTIVE)
        self.assertFalse(plan.generation_snapshot_json["has_today_pc_usage_pattern"])
        self.assertEqual(plan.slots.count(), 2)
        self.assertTrue(
            all(
                slot.notification_basis == SlotNotificationBasis.SNAPSHOT
                for slot in plan.slots.all()
            )
        )

        PcUsagePattern.objects.create(
            user=self.user,
            day_of_week=today_day_of_week_for_user(self.user),
            hour=15,
            is_used=True,
        )
        self.assertTrue(has_today_pc_usage_pattern(self.user))

        second_slot = schedule_next_slot_after_completed_slot(
            completed_slot=plan.slots.order_by("sequence_no").first(),
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_at=second_time,
        )

        self.assertIsNotNone(second_slot)
        self.assertEqual(second_slot.sequence_no, 3)
        self.assertEqual(second_slot.notification_basis, SlotNotificationBasis.SNAPSHOT)
        self.assertEqual(plan.slots.count(), 3)

    def test_policy_slots_split_next_activity_duration_by_state_interval(self):
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        NextActivityPlan.objects.filter(id=self.next_activity_plan.id).update(
            created_at=fixed_now,
            expected_activity_minutes=120,
        )
        self.next_activity_plan.refresh_from_db()

        slots = build_policy_recommended_slots(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            base_time=fixed_now,
        )

        self.assertEqual(
            [
                slot["recommended_at"]
                for slot in slots
                if slot["notification_basis"] == SlotNotificationBasis.SNAPSHOT
            ],
            [
                fixed_now + timedelta(minutes=20),
                fixed_now + timedelta(minutes=40),
                fixed_now + timedelta(minutes=60),
                fixed_now + timedelta(minutes=80),
                fixed_now + timedelta(minutes=100),
                fixed_now + timedelta(minutes=120),
            ],
        )

    def test_policy_slots_include_previous_session_frequency_times(self):
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        NextActivityPlan.objects.filter(id=self.next_activity_plan.id).update(
            created_at=fixed_now,
            expected_activity_minutes=60,
        )
        self.next_activity_plan.refresh_from_db()
        plan = RecoveryPlan.objects.create(user=self.user, plan_date=today_for_user(self.user))
        slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            sequence_no=1,
            recommended_at=fixed_now + timedelta(minutes=20),
        )
        activity = ActivityType.objects.create(
            code="history_eye_shift",
            stage_type=StageType.BRAIN_SHIFT,
            target_state=self.state,
            name="눈 이완",
            default_duration_sec=90,
        )
        routine = RoutineInstance.objects.create(
            recovery_slot=slot,
            activity=activity,
            sequence_no=1,
            difficulty_level=1,
            planned_duration_sec=90,
        )
        frequent_time = fixed_now.replace(hour=16, minute=30)
        PcUsagePattern.objects.create(
            user=self.user,
            day_of_week=today_day_of_week_for_user(self.user),
            hour=16,
            is_used=True,
        )
        for weeks_ago, minute in enumerate([10, 30, 50], start=1):
            started_at = frequent_time.replace(minute=minute) - timedelta(days=7 * weeks_ago)
            Session.objects.create(
                user=self.user,
                recovery_slot=slot,
                routine_instance=routine,
                activity=activity,
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=2),
                duration_sec=120,
                accuracy=100,
                status=SessionStatus.COMPLETED,
            )

        slots = build_policy_recommended_slots(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            base_time=fixed_now,
        )

        self.assertIn(
            {
                "recommended_at": frequent_time,
                "notification_basis": SlotNotificationBasis.FREQUENCY,
                "reason": "주간 PC 사용 패턴과 최근 회복 세션 기록이 함께 몰린 시간대에 배치했습니다.",
                "data_sources": ["pc_usage_patterns", "previous_sessions", "time_policy"],
            },
            slots,
        )

    def test_plan_with_pc_pattern_does_not_mark_manual_slots_as_frequency(self):
        PcUsagePattern.objects.create(
            user=self.user,
            day_of_week=today_day_of_week_for_user(self.user),
            hour=10,
            is_used=True,
        )
        first_time = timezone.now() + timedelta(minutes=60)
        second_time = timezone.now() + timedelta(minutes=120)

        plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_times=[first_time, second_time],
        )

        self.assertTrue(plan.generation_snapshot_json["has_today_pc_usage_pattern"])
        self.assertEqual(plan.slots.count(), 2)
        self.assertTrue(
            all(
                slot.notification_basis == SlotNotificationBasis.SNAPSHOT
                for slot in plan.slots.all()
            )
        )

        next_slot = schedule_next_slot_after_completed_slot(completed_slot=plan.slots.order_by("sequence_no").first())

        self.assertIsNone(next_slot)
        self.assertEqual(plan.slots.count(), 2)

    def test_notification_setting_is_slot_level_and_defaults_to_enabled(self):
        plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
        )
        slot = plan.slots.get()

        self.assertTrue(slot.notification_enabled)
        self.assertEqual(slot.notifications.filter(status=NotificationStatus.PENDING).count(), 1)

        set_slot_notification(slot=slot, enabled=False, repeat_rule="FREQ=DAILY")
        slot.refresh_from_db()

        self.assertFalse(slot.notification_enabled)
        self.assertEqual(slot.repeat_rule, "")
        self.assertEqual(slot.notifications.filter(status=NotificationStatus.PENDING).count(), 0)
        reengagement = Notification.objects.get(kind=NotificationKind.REENGAGEMENT)
        self.assertIsNone(reengagement.recovery_slot_id)
        self.assertEqual(reengagement.user, self.user)
        self.assertEqual(reengagement.scheduled_at, slot.effective_time + timedelta(days=7))

        set_slot_notification(slot=slot, enabled=True, repeat_rule="FREQ=DAILY")
        slot.refresh_from_db()
        reengagement.refresh_from_db()

        self.assertTrue(slot.notification_enabled)
        self.assertEqual(slot.repeat_rule, "FREQ=DAILY")
        self.assertEqual(slot.notifications.filter(status=NotificationStatus.PENDING).count(), 1)
        self.assertEqual(reengagement.status, NotificationStatus.CANCELED)

    def test_replacing_today_plan_cancels_previous_open_slots_of_the_same_flow(self):
        # 같은 흐름(둘 다 use_ai_decision 생략 = None)으로 다시 생성하면, 예전
        # 열린 슬롯은 FREQUENCY든 SNAPSHOT이든 basis 상관없이 전부 취소되고,
        # 더 이상 "FREQUENCY만 예외로 부활"시키지 않는다 — 그 "부활" 로직이
        # 바로 "PC 사용 패턴에서 블록을 지웠는데 그 블록 알림이 안 사라지는"
        # 버그의 원인이었다.
        frequency_time = timezone.now() + timedelta(minutes=70)
        old_plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_slots=[
                {
                    "recommended_at": timezone.now() + timedelta(minutes=60),
                    "notification_basis": SlotNotificationBasis.SNAPSHOT,
                },
                {
                    "recommended_at": frequency_time,
                    "notification_basis": SlotNotificationBasis.FREQUENCY,
                },
            ],
        )
        old_snapshot = old_plan.slots.get(notification_basis=SlotNotificationBasis.SNAPSHOT)
        old_frequency = old_plan.slots.get(notification_basis=SlotNotificationBasis.FREQUENCY)

        new_plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_times=[timezone.now() + timedelta(minutes=90)],
        )

        old_plan.refresh_from_db()
        old_snapshot.refresh_from_db()
        old_frequency.refresh_from_db()
        # 같은 흐름은 새로 만들지 않고 오늘의 active plan을 그대로 재사용한다.
        self.assertEqual(new_plan.id, old_plan.id)
        self.assertEqual(new_plan.status, PlanStatus.ACTIVE)
        self.assertEqual(old_snapshot.status, SlotStatus.CANCELED)
        self.assertEqual(old_frequency.status, SlotStatus.CANCELED)
        self.assertEqual(old_snapshot.notifications.filter(status=NotificationStatus.PENDING).count(), 0)
        self.assertEqual(old_frequency.notifications.filter(status=NotificationStatus.PENDING).count(), 0)
        # 예전 FREQUENCY 슬롯 시각에 "새로" 만들어진 슬롯이 없다 — 취소된
        # old_frequency 자기 자신은 여전히 plan에 남아있으니 그건 제외하고 본다.
        self.assertFalse(
            new_plan.slots.exclude(id=old_frequency.id)
            .filter(
                notification_basis=SlotNotificationBasis.FREQUENCY,
                recommended_at=old_frequency.effective_time,
            )
            .exists()
        )

    def test_regenerating_other_flow_does_not_cancel_this_flows_open_slots(self):
        # 상태 선택 모달(use_ai_decision=False)로 만든 알림은, My Digital State
        # 흐름(use_ai_decision=True)이 재생성돼도 건드리면 안 된다 — 두 흐름은
        # 완전히 독립적이어야 한다.
        modal_plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_times=[timezone.now() + timedelta(minutes=30)],
            use_ai_decision=False,
        )
        modal_slot = modal_plan.slots.get()

        digital_plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_times=[timezone.now() + timedelta(minutes=45)],
            use_ai_decision=True,
        )

        modal_slot.refresh_from_db()
        # 하루 한 plan을 공유해서 재사용하지만, 모달 슬롯은 취소되지 않는다.
        self.assertEqual(digital_plan.id, modal_plan.id)
        self.assertIn(modal_slot.status, OPEN_SLOT_STATUSES)
        self.assertEqual(modal_slot.notifications.filter(status=NotificationStatus.PENDING).count(), 1)

        # 이번엔 반대로 모달을 다시 완료해도, PC 패턴 흐름이 만든 슬롯은 그대로.
        digital_slot = digital_plan.slots.exclude(id=modal_slot.id).get()
        create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_times=[timezone.now() + timedelta(minutes=20)],
            use_ai_decision=False,
        )
        digital_slot.refresh_from_db()
        modal_slot.refresh_from_db()
        self.assertEqual(modal_slot.status, SlotStatus.CANCELED)
        self.assertIn(digital_slot.status, OPEN_SLOT_STATUSES)

    @patch("plans.web_push.send_web_push")
    def test_send_due_notifications_sends_pending_notification_to_active_subscriptions(self, mock_send_web_push):
        scheduled_at = timezone.now().replace(microsecond=0)
        notification = Notification.objects.create(
            user=self.user,
            kind=NotificationKind.REENGAGEMENT,
            message="회복 루틴을 다시 시작해볼까요?",
            scheduled_at=scheduled_at,
        )
        subscription = WebPushSubscription.objects.create(
            user=self.user,
            endpoint="https://push.example.test/subscriptions/worker",
            p256dh="p256dh-key",
            auth="auth-key",
            is_active=True,
        )

        result = send_due_notifications(now=scheduled_at)

        notification.refresh_from_db()
        self.assertEqual(result.processed_count, 1)
        self.assertEqual(result.sent_count, 1)
        self.assertEqual(notification.status, NotificationStatus.SENT)
        self.assertEqual(notification.sent_at, scheduled_at)
        self.assertEqual(mock_send_web_push.call_count, 1)
        sent_subscription, payload = mock_send_web_push.call_args.args
        self.assertEqual(sent_subscription.id, subscription.id)
        self.assertEqual(payload["data"]["notification_id"], str(notification.id))
        self.assertEqual(payload["data"]["kind"], NotificationKind.REENGAGEMENT)

    def test_send_due_notifications_marks_failed_without_active_subscription(self):
        scheduled_at = timezone.now().replace(microsecond=0)
        notification = Notification.objects.create(
            user=self.user,
            kind=NotificationKind.REENGAGEMENT,
            message="회복 루틴을 다시 시작해볼까요?",
            scheduled_at=scheduled_at,
        )

        result = send_due_notifications(now=scheduled_at)

        notification.refresh_from_db()
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(notification.status, NotificationStatus.FAILED)
        self.assertEqual(notification.delivery_error, "활성 Web Push 구독이 없습니다.")

    def test_default_recovery_time_uses_shortest_state_policy_interval(self):
        body_state, _ = StateOption.objects.get_or_create(
            code="BODY_STIFF",
            defaults={"label": "몸이 굳었어요"},
        )
        UserContextSnapshotState.objects.create(
            context_snapshot=self.context_snapshot,
            state=body_state,
            priority=2,
        )
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)

        with patch("plans.services.timezone.now", return_value=fixed_now):
            plan = create_or_replace_today_plan(
                user=self.user,
                context_snapshot=self.context_snapshot,
                next_activity_plan=self.next_activity_plan,
            )

        slot = plan.slots.get()
        self.assertEqual(slot.recommended_at, fixed_now + timedelta(minutes=20))
        self.assertEqual(slot.interval_minutes, 20)
        self.assertEqual(plan.generation_snapshot_json["time_policy"]["interval_minutes"], 20)
        self.assertEqual(
            plan.generation_snapshot_json["time_policy"]["selected_state_codes"],
            ["EYE_TIRED"],
        )

    def test_reset_next_activity_replaces_open_snapshot_slots_and_keeps_frequency(self):
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        plan = create_or_replace_today_plan(
            user=self.user,
            context_snapshot=self.context_snapshot,
            next_activity_plan=self.next_activity_plan,
            recommended_slots=[
                {
                    "recommended_at": fixed_now + timedelta(minutes=20),
                    "notification_basis": SlotNotificationBasis.SNAPSHOT,
                },
                {
                    "recommended_at": fixed_now + timedelta(minutes=30),
                    "notification_basis": SlotNotificationBasis.FREQUENCY,
                },
                {
                    "recommended_at": fixed_now + timedelta(minutes=40),
                    "notification_basis": SlotNotificationBasis.SNAPSHOT,
                },
            ],
        )
        target_slot = plan.slots.filter(notification_basis=SlotNotificationBasis.SNAPSHOT).order_by("sequence_no").first()
        frequency_slot = plan.slots.get(notification_basis=SlotNotificationBasis.FREQUENCY)
        replacement_activity_plan = NextActivityPlan.objects.create(
            user=self.user,
            context_snapshot=self.context_snapshot,
            service_date=today_for_user(self.user),
            expected_activity_minutes=60,
        )
        NextActivityPlan.objects.filter(id=replacement_activity_plan.id).update(created_at=fixed_now)
        replacement_activity_plan.refresh_from_db()

        with patch("plans.services.timezone.now", return_value=fixed_now):
            replacement_slot = reset_next_activity_and_slot(
                user=self.user,
                target_slot=target_slot,
                context_snapshot=self.context_snapshot,
                next_activity_plan=replacement_activity_plan,
            )

        old_snapshot_statuses = list(
            plan.slots.filter(
                notification_basis=SlotNotificationBasis.SNAPSHOT,
                next_activity_plan=self.next_activity_plan,
            ).values_list("status", flat=True)
        )
        replacement_snapshot_times = list(
            plan.slots.filter(
                notification_basis=SlotNotificationBasis.SNAPSHOT,
                next_activity_plan=replacement_activity_plan,
                status__in=[SlotStatus.RECOMMENDED, SlotStatus.SCHEDULED, SlotStatus.CHANGED],
            )
            .order_by("recommended_at")
            .values_list("recommended_at", flat=True)
        )
        frequency_slot.refresh_from_db()

        self.assertTrue(all(status == SlotStatus.CANCELED for status in old_snapshot_statuses))
        self.assertEqual(frequency_slot.status, SlotStatus.RECOMMENDED)
        self.assertEqual(replacement_slot.sequence_no, 4)
        self.assertEqual(replacement_slot.next_activity_plan_id, replacement_activity_plan.id)
        self.assertEqual(
            replacement_snapshot_times,
            [
                fixed_now + timedelta(minutes=20),
                fixed_now + timedelta(minutes=40),
                fixed_now + timedelta(minutes=60),
            ],
        )
        self.assertEqual(
            plan.slots.filter(
                status__in=[SlotStatus.RECOMMENDED, SlotStatus.SCHEDULED, SlotStatus.CHANGED],
            ).count(),
            4,
        )


@override_settings(OPENAI_API_KEY="")
class RecoveryPlanApiTests(APITestCase):
    """
    기본적으로 OPENAI_API_KEY를 비워서 create_structured_response를 안 거치는
    테스트는 전부 정책 엔진(폴백) 경로로만 돈다 — .env에 실제 키가 들어있어도
    테스트가 실제 OpenAI 네트워크 호출을 하지 않도록 하는 안전장치. 실제 LLM
    경로를 검증하는 테스트는 메서드 단위로 OPENAI_API_KEY를 다시 채우고
    create_structured_response를 목(mock)으로 대체한다.
    """

    def setUp(self):
        self.device_code = uuid.uuid4()
        self.client.credentials(HTTP_X_DEVICE_CODE=str(self.device_code))
        self.state = StateOption.objects.create(code="NECK_STIFF", label="목이 뻐근해요")
        self.activity_tag = ActivityTag.objects.create(code="ASSIGNMENT", name="과제")

    def _ensure_reentry_activity_catalog(self, *, eye_state, body_state):
        ActivityType.objects.update_or_create(
            code="WAKE_HAND_ROUTINE",
            defaults={
                "stage_type": StageType.BRAIN_WAKE,
                "target_state": None,
                "name": "손 깨우기",
                "purpose": "감각을 깨우는 루틴",
                "default_duration_sec": 60,
                "is_active": True,
            },
        )
        ActivityType.objects.update_or_create(
            code="RESET_BREATH",
            defaults={
                "stage_type": StageType.BRAIN_RESET,
                "target_state": None,
                "name": "호흡 정리",
                "purpose": "호흡으로 마무리하는 루틴",
                "default_duration_sec": 60,
                "is_active": True,
            },
        )
        ActivityType.objects.update_or_create(
            code="SHIFT_EYE_RELAX",
            defaults={
                "stage_type": StageType.BRAIN_SHIFT,
                "target_state": eye_state,
                "name": "눈 이완",
                "purpose": "눈 피로를 낮추는 루틴",
                "default_duration_sec": 90,
                "is_active": True,
            },
        )
        ActivityType.objects.update_or_create(
            code="SHIFT_BODY_STRETCH",
            defaults={
                "stage_type": StageType.BRAIN_SHIFT,
                "target_state": body_state,
                "name": "목 어깨 스트레칭",
                "purpose": "굳은 목과 어깨를 푸는 루틴",
                "default_duration_sec": 90,
                "is_active": True,
            },
        )

    def test_create_today_plan_from_context_snapshot_and_next_activity_plan(self):
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 45,
            },
            format="json",
        )

        response = self.client.post(
            "/api/v1/plans/recovery-plans/today/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "next_activity_plan": activity_plan_response.data["data"]["id"],
                "recommended_times": [(timezone.now() + timedelta(minutes=45)).isoformat()],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["success"])
        self.assertFalse(response.data["data"]["generation_snapshot_json"]["has_today_pc_usage_pattern"])
        self.assertEqual(len(response.data["data"]["slots"]), 1)
        self.assertTrue(response.data["data"]["slots"][0]["notification_enabled"])

    def test_recovery_slot_detail_next_feedback_and_notification_endpoints(self):
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 45,
            },
            format="json",
        )
        self.client.post(
            "/api/v1/plans/recovery-plans/today/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "next_activity_plan": activity_plan_response.data["data"]["id"],
                "recommended_times": [(timezone.now() + timedelta(minutes=45)).isoformat()],
            },
            format="json",
        )

        next_response = self.client.get("/api/v1/plans/recovery-slots/next/")
        slot_id = next_response.data["data"]["id"]

        detail_response = self.client.get(f"/api/v1/plans/recovery-slots/{slot_id}/")
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data["data"]["id"], slot_id)

        reset_time_response = self.client.get("/api/v1/plans/recovery-slots/next-reset-time/")
        self.assertEqual(reset_time_response.status_code, status.HTTP_200_OK)
        self.assertEqual(reset_time_response.data["data"]["recovery_slot"], slot_id)

        RecoverySlot.objects.filter(id=slot_id).update(status=SlotStatus.STARTED)
        future_slot = RecoverySlot.objects.create(
            recovery_plan_id=detail_response.data["data"]["recovery_plan"],
            sequence_no=2,
            recommended_at=timezone.now() + timedelta(minutes=90),
        )

        runnable_response = self.client.get("/api/v1/plans/recovery-slots/next/")
        reset_time_response = self.client.get("/api/v1/plans/recovery-slots/next-reset-time/")

        self.assertEqual(runnable_response.status_code, status.HTTP_200_OK)
        self.assertEqual(runnable_response.data["data"]["id"], slot_id)
        self.assertEqual(reset_time_response.status_code, status.HTTP_200_OK)
        self.assertEqual(reset_time_response.data["data"]["recovery_slot"], str(future_slot.id))

        notification_response = self.client.patch(
            f"/api/v1/plans/recovery-slots/{slot_id}/notification/",
            {"notification_enabled": True, "repeat_rule": "FREQ=DAILY"},
            format="json",
        )
        self.assertEqual(notification_response.status_code, status.HTTP_200_OK)
        self.assertTrue(notification_response.data["data"]["notification_enabled"])

        notifications_response = self.client.get("/api/v1/plans/notifications/")
        notification_id = notifications_response.data["data"][0]["id"]
        click_response = self.client.post(f"/api/v1/plans/notifications/{notification_id}/click/")
        self.assertEqual(click_response.status_code, status.HTTP_200_OK)
        self.assertEqual(click_response.data["data"]["status"], NotificationStatus.CLICKED)

        feedback_response = self.client.post(
            f"/api/v1/plans/recovery-slots/{slot_id}/feedback/",
            {
                "recovery_feeling": "MUCH_BETTER",
                "difficulty_feedback": "JUST_RIGHT",
            },
            format="json",
        )
        self.assertEqual(feedback_response.status_code, status.HTTP_201_CREATED)

        history_response = self.client.get("/api/v1/plans/recovery-slots/history/")
        self.assertEqual(history_response.status_code, status.HTTP_200_OK)
        self.assertEqual(history_response.data["data"][0]["id"], slot_id)

    def test_today_slot_list_does_not_cancel_freshly_created_notifications(self):
        """
        cleanup_nearby_pattern_notifications_on_entry(진입 시점 30분 임계값)는
        GET /plans/recovery-slots/today/가 "사용자가 진짜로 들어와서 확인하는
        순간"에만 불리는 게 아니라 useRoutineHome이 마운트될 때마다(=생성 직후
        페이지가 다시 렌더될 때도) 불려서, 방금 막 생성한 알림의 첫 슬롯이
        우연히 30분 이내에 있으면 만들어지자마자 스스로 취소해버리는 버그가
        있었다 — 그래서 이 정리 로직 자체를 뺐다. 오늘 슬롯 조회는 이제
        기존 슬롯 상태를 그대로 보여줘야 한다(방금 만든 가까운 슬롯도 유지).
        """
        user = User.objects.create(id=self.device_code, timezone="Asia/Seoul")
        plan = RecoveryPlan.objects.create(
            user=user,
            plan_date=today_for_user(user),
            status=PlanStatus.ACTIVE,
        )
        nearby_slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            sequence_no=1,
            recommended_at=timezone.now() + timedelta(minutes=10),
            notification_basis=SlotNotificationBasis.FREQUENCY,
        )
        far_slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            sequence_no=2,
            recommended_at=timezone.now() + timedelta(hours=3),
            notification_basis=SlotNotificationBasis.FREQUENCY,
        )

        response = self.client.get("/api/v1/plans/recovery-slots/today/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        nearby_slot.refresh_from_db()
        far_slot.refresh_from_db()
        self.assertEqual(nearby_slot.status, SlotStatus.RECOMMENDED)
        self.assertEqual(far_slot.status, SlotStatus.RECOMMENDED)

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("plans.ai_planner.create_structured_response")
    def test_ai_generate_trusts_llm_slot_count_and_drops_out_of_pattern_slots(
        self, mock_create_structured_response
    ):
        """
        LLM이 (정책 엔진이라면 절대 안 나올) 3개의 슬롯을 자유롭게 제안하면, 그중
        PC 사용 패턴 밖 시각인 1개는 서버가 걸러내고 패턴 안에 있는 2개만 실제로
        생성돼야 한다 — "AI 자율 판단"이 진짜로 개수/시각을 결정한다는 걸 증명.
        """
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 90,
            },
            format="json",
        )

        user = User.objects.get(id=self.device_code)
        today_day = today_day_of_week_for_user(user)
        # PC 사용 패턴: 14시, 15시만 사용 중 — 16시는 패턴 밖.
        PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=14, is_used=True)
        PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=15, is_used=True)

        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        in_pattern_first = fixed_now.replace(hour=14, minute=10)
        in_pattern_second = fixed_now.replace(hour=15, minute=20)
        out_of_pattern = fixed_now.replace(hour=16, minute=0)

        shift = ActivityType.objects.create(
            code="shift_neck",
            stage_type=StageType.BRAIN_SHIFT,
            target_state=self.state,
            name="목 이완",
            default_duration_sec=90,
        )

        def shift_recommendation(reason):
            return [
                {
                    "activity_code": shift.code,
                    "difficulty_level": 3,
                    "planned_duration_sec": 90,
                    "reason": reason,
                }
            ]

        mock_create_structured_response.return_value = (
            {
                "summary": "PC 사용 밀집 구간을 고려해 3개의 슬롯을 제안합니다.",
                "slots": [
                    {
                        "recommended_at": in_pattern_first.isoformat(),
                        "interval_minutes": 40,
                        "reason": "첫 번째 밀집 구간",
                        "shift_recommendations": shift_recommendation("목 뻐근함을 줄입니다."),
                    },
                    {
                        "recommended_at": in_pattern_second.isoformat(),
                        "interval_minutes": 30,
                        "reason": "두 번째 밀집 구간",
                        "shift_recommendations": shift_recommendation("다시 한 번 이완합니다."),
                    },
                    {
                        # PC 사용 패턴 밖(16시) — 서버가 걸러내야 함
                        "recommended_at": out_of_pattern.isoformat(),
                        "interval_minutes": 30,
                        "reason": "패턴 밖 슬롯(걸러져야 함)",
                        "shift_recommendations": shift_recommendation("걸러짐"),
                    },
                ],
                "insights": [],
            },
            {"id": "resp_mock"},
        )

        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                    "use_ai_decision": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["data"]["slots"]), 2)

        plan_id = response.data["data"]["id"]
        slot_hours = sorted(
            RecoverySlot.objects.filter(recovery_plan_id=plan_id).values_list(
                "recommended_at__hour", flat=True
            )
        )
        self.assertEqual(slot_hours, [14, 15])

        ai_run = AIPlanRun.objects.get(id=response.data["data"]["ai_plan_run"])
        self.assertTrue(ai_run.model_name.startswith("openai:"))

    def test_ai_generate_falls_back_to_policy_engine_when_openai_key_missing(self):
        """
        use_ai_decision=True를 보내도 OPENAI_API_KEY가 없으면(클래스 기본값)
        create_structured_response가 OpenAIConfigurationError를 내고, 곧바로
        정책 엔진으로 생성돼야 한다 — LLM 장애/미설정이 회복 계획 생성 자체를
        막지 않는다는 안전장치 확인.
        """
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 45,
            },
            format="json",
        )

        response = self.client.post(
            "/api/v1/plans/recovery-plans/today/ai-generate/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "next_activity_plan": activity_plan_response.data["data"]["id"],
                "use_ai_decision": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ai_run = AIPlanRun.objects.get(id=response.data["data"]["ai_plan_run"])
        self.assertEqual(ai_run.model_name, "server_policy")
        self.assertFalse(ai_run.output_snapshot_json["raw_response"]["external_api_called"])

    def test_ai_generate_policy_fallback_repeats_within_each_pc_usage_window(self):
        """
        LLM이 없을 때(OPENAI_API_KEY 없음, 클래스 기본값) My Digital State
        흐름(use_ai_decision=True)의 정책 폴백은 PC 사용 패턴의 연속된 시간
        블록(윈도우)마다 그 안에서 상태 기반 interval 간격으로 반복되는 알림을
        배치해야 한다 — 블록 중간 지점 딱 1개로 뭉개지면 안 되고(긴 블록일수록
        더 자주 와야 함), 상태 인터벌 반복(SNAPSHOT)으로도 뭉개지면 안 된다.
        self.state(NECK_STIFF)는 RECOVERY_INTERVAL_MINUTES_BY_STATE에 없어서
        기본 간격(45분)이 적용된다.
        """
        user, _ = User.objects.get_or_create(
            id=self.device_code, defaults={"timezone": "Asia/Seoul"}
        )
        today_day = today_day_of_week_for_user(user)
        # 연속된 두 개의 블록: 06~08시, 10~12시 — 서로 떨어져 있어 별도 윈도우.
        for hour in [6, 7, 10, 11]:
            PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=hour, is_used=True)

        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 90,
            },
            format="json",
        )

        fixed_now = timezone.now().replace(hour=5, minute=0, second=0, microsecond=0)
        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                    "use_ai_decision": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ai_run = AIPlanRun.objects.get(id=response.data["data"]["ai_plan_run"])
        self.assertEqual(ai_run.model_name, "server_policy")

        plan_id = response.data["data"]["id"]
        created_slots = list(
            RecoverySlot.objects.filter(recovery_plan_id=plan_id).order_by("sequence_no")
        )
        self.assertEqual(len(created_slots), 4)
        self.assertEqual(
            [slot.recommended_at for slot in created_slots],
            [
                fixed_now.replace(hour=6, minute=45),
                fixed_now.replace(hour=7, minute=30),
                fixed_now.replace(hour=10, minute=45),
                fixed_now.replace(hour=11, minute=30),
            ],
        )
        self.assertTrue(all(slot.notification_basis == SlotNotificationBasis.FREQUENCY for slot in created_slots))

    def test_ai_generate_prefers_actual_session_history_over_uniform_interval(self):
        """
        My Digital State 흐름은 상태 선택 모달과 달리 "그냥 인터벌 기계적 반복"이면
        안 되고, 실제 과거 세션 기록을 분석해서 자주 활동했던 시간대를 우선 써야
        한다(간격이 균일할 필요 없음). PC 사용 블록(10~14시) 안에서 사용자가
        지난 3주간 매주 같은 요일 11:15 즈음 세션을 완료해왔다면, 그 블록 안의
        상태 interval 균일 반복(45분 간격 여러 개) 대신 11:15 하나로 배치돼야
        한다.
        """
        user, _ = User.objects.get_or_create(
            id=self.device_code, defaults={"timezone": "Asia/Seoul"}
        )
        today_day = today_day_of_week_for_user(user)
        for hour in [10, 11, 12, 13]:
            PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=hour, is_used=True)

        shift = ActivityType.objects.create(
            code="shift_history_test",
            stage_type=StageType.BRAIN_SHIFT,
            target_state=self.state,
            name="테스트용 이완",
            default_duration_sec=90,
        )
        fixed_now = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        # history_plan/history_slot은 "3주 전 완료했던 세션"을 매달아두기 위한
        # 껍데기일 뿐, 오늘의 실제 active plan을 흉내내려는 게 아니다. status를
        # 그대로 ACTIVE로 두면(기본값) 뒤에서 today/ai-generate가 오늘의 plan을
        # 재사용하는 로직과 충돌해서 이 더미 슬롯까지 오늘 plan에 섞여버린다.
        history_plan = RecoveryPlan.objects.create(
            user=user, plan_date=today_for_user(user), status=PlanStatus.REPLACED
        )
        history_slot = RecoverySlot.objects.create(
            recovery_plan=history_plan,
            sequence_no=1,
            recommended_at=fixed_now.replace(hour=11, minute=15),
        )
        history_routine = RoutineInstance.objects.create(
            recovery_slot=history_slot,
            activity=shift,
            sequence_no=1,
            difficulty_level=1,
            planned_duration_sec=90,
        )
        for weeks_ago in [1, 2, 3]:
            started_at = fixed_now.replace(hour=11, minute=15) - timedelta(days=7 * weeks_ago)
            Session.objects.create(
                user=user,
                recovery_slot=history_slot,
                routine_instance=history_routine,
                activity=shift,
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=2),
                duration_sec=120,
                accuracy=100,
                status=SessionStatus.COMPLETED,
            )

        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 90,
            },
            format="json",
        )

        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                    "use_ai_decision": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        plan_id = response.data["data"]["id"]
        created_slots = list(
            RecoverySlot.objects.filter(recovery_plan_id=plan_id).order_by("sequence_no")
        )
        self.assertEqual(len(created_slots), 1)
        self.assertEqual(created_slots[0].recommended_at, fixed_now.replace(hour=11, minute=15))
        self.assertEqual(created_slots[0].notification_basis, SlotNotificationBasis.FREQUENCY)

    def test_modal_generate_keeps_existing_frequency_notifications(self):
        """
        My Digital State로 06~08시/10~12시 두 블록에 알림을 미리 만들어둔 뒤,
        06시 되기 전(05:00)에 사용자가 일반 상태+활동 모달로 알림을 직접 설정해도
        기존 빈도 기반 알림은 조회/생성 부작용으로 취소되면 안 된다.
        """
        user, _ = User.objects.get_or_create(
            id=self.device_code, defaults={"timezone": "Asia/Seoul"}
        )
        today_day = today_day_of_week_for_user(user)
        for hour in [6, 7, 10, 11]:
            PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=hour, is_used=True)

        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 90,
            },
            format="json",
        )
        fixed_now = timezone.now().replace(hour=5, minute=0, second=0, microsecond=0)
        NextActivityPlan.objects.filter(id=activity_plan_response.data["data"]["id"]).update(
            created_at=fixed_now,
        )

        # 1) My Digital State 흐름 — 06~08시/10~12시 블록마다 45분 간격 알림 미리
        # 생성(06:45,07:30 / 10:45,11:30)
        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                    "use_ai_decision": True,
                },
                format="json",
            )

        # 2) 06시 되기 전, 상태 선택 모달로 알림 설정(use_ai_decision 안 보냄)
        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            modal_response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                },
                format="json",
            )

        self.assertEqual(modal_response.status_code, status.HTTP_201_CREATED)
        final_plan_id = modal_response.data["data"]["id"]
        frequency_slots = {
            slot.recommended_at: slot.status
            for slot in RecoverySlot.objects.filter(
                recovery_plan_id=final_plan_id,
                notification_basis=SlotNotificationBasis.FREQUENCY,
            )
        }
        self.assertEqual(frequency_slots[fixed_now.replace(hour=6, minute=45)], SlotStatus.RECOMMENDED)
        self.assertEqual(frequency_slots[fixed_now.replace(hour=7, minute=30)], SlotStatus.RECOMMENDED)
        self.assertEqual(frequency_slots[fixed_now.replace(hour=10, minute=45)], SlotStatus.RECOMMENDED)
        self.assertEqual(frequency_slots[fixed_now.replace(hour=11, minute=30)], SlotStatus.RECOMMENDED)

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("plans.ai_planner.create_structured_response")
    def test_ai_generate_never_calls_llm_when_use_ai_decision_omitted(
        self, mock_create_structured_response
    ):
        """
        use_ai_decision을 아예 안 보내면(상태 선택 모달 흐름 등) OPENAI_API_KEY가
        멀쩡히 설정돼 있어도 create_structured_response 자체를 호출하지 않아야
        한다 — My Digital State의 PC 사용 패턴 흐름에서만 LLM을 쓴다는 제약을
        코드로 검증.
        """
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 45,
            },
            format="json",
        )

        response = self.client.post(
            "/api/v1/plans/recovery-plans/today/ai-generate/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "next_activity_plan": activity_plan_response.data["data"]["id"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_create_structured_response.assert_not_called()
        ai_run = AIPlanRun.objects.get(id=response.data["data"]["ai_plan_run"])
        self.assertEqual(ai_run.model_name, "server_policy")

    def test_ai_generate_ignores_pc_usage_pattern_when_use_ai_decision_omitted(self):
        """
        상태 선택 모달 흐름(use_ai_decision 안 보냄)은 PC 사용 패턴/과거 세션
        빈도(digital_state)와 완전히 무관해야 한다 — 사용자가 PC 패턴과 그와
        겹치는 세션 기록을 잔뜩 갖고 있어도, 빈도 기반(FREQUENCY) 슬롯이 하나도
        섞여 들어가면 안 되고 상태 스냅샷 기반(SNAPSHOT) 슬롯만 나와야 한다.
        """
        user, _ = User.objects.get_or_create(
            id=self.device_code, defaults={"timezone": "Asia/Seoul"}
        )
        today_day = today_day_of_week_for_user(user)
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)

        # PC 사용 패턴 + 그 시간대에 몰린 과거 세션 기록(전형적인 빈도 기반 후보)
        PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=16, is_used=True)
        shift = ActivityType.objects.create(
            code="shift_freq_test",
            stage_type=StageType.BRAIN_SHIFT,
            target_state=self.state,
            name="테스트용 이완",
            default_duration_sec=90,
        )
        plan = RecoveryPlan.objects.create(user=user, plan_date=today_for_user(user))
        slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            sequence_no=1,
            recommended_at=fixed_now.replace(hour=16, minute=20),
        )
        routine = RoutineInstance.objects.create(
            recovery_slot=slot,
            activity=shift,
            sequence_no=1,
            difficulty_level=1,
            planned_duration_sec=90,
        )
        for weeks_ago, minute in enumerate([10, 30, 50], start=1):
            started_at = fixed_now.replace(hour=16, minute=minute) - timedelta(days=7 * weeks_ago)
            Session.objects.create(
                user=user,
                recovery_slot=slot,
                routine_instance=routine,
                activity=shift,
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=2),
                duration_sec=120,
                accuracy=100,
                status=SessionStatus.COMPLETED,
            )

        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 90,
            },
            format="json",
        )

        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        plan_id = response.data["data"]["id"]
        bases = set(
            RecoverySlot.objects.filter(recovery_plan_id=plan_id).values_list(
                "notification_basis", flat=True
            )
        )
        self.assertNotIn(SlotNotificationBasis.FREQUENCY, bases)

    def test_ai_generate_uses_fixed_wake_shift_groups_and_reset(self):
        eye_state, _ = StateOption.objects.get_or_create(
            code="EYE_TIRED",
            defaults={"label": "눈이 피로해요"},
        )
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [eye_state.code]},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 40,
            },
            format="json",
        )
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        NextActivityPlan.objects.filter(id=activity_plan_response.data["data"]["id"]).update(
            created_at=fixed_now,
        )

        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        first_slot = response.data["data"]["slots"][0]
        self.assertEqual(
            [routine["activity"]["code"] for routine in first_slot["routine_instances"]],
            [
                "WAKE_HAND_ROUTINE",
                "SHIFT_EYE_RELAX",
                "SHIFT_EYE_TRACKING",
                "RESET_BREATH",
            ],
        )
        body_state, _ = StateOption.objects.get_or_create(
            code="BODY_STIFF",
            defaults={"label": "목과 어깨가 굳었어요"},
        )
        body_snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [body_state.code]},
            format="json",
        )
        body_activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": body_snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 30,
            },
            format="json",
        )
        NextActivityPlan.objects.filter(id=body_activity_plan_response.data["data"]["id"]).update(
            created_at=fixed_now,
        )

        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            body_response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": body_snapshot_response.data["data"]["id"],
                    "next_activity_plan": body_activity_plan_response.data["data"]["id"],
                },
                format="json",
            )

        self.assertEqual(body_response.status_code, status.HTTP_201_CREATED)
        # 두 흐름 독립성 때문에 오늘의 plan을 재사용하므로, 이전(EYE) 호출의
        # 슬롯이 앞쪽 sequence_no로 여전히 남아있다 — 방금 새로 만든 슬롯은
        # 항상 마지막(가장 큰 sequence_no)이다.
        body_first_slot = body_response.data["data"]["slots"][-1]
        self.assertEqual(
            [routine["activity"]["code"] for routine in body_first_slot["routine_instances"]],
            [
                "WAKE_HAND_ROUTINE",
                "SHIFT_BODY_STRETCH",
                "SHIFT_SHOULDER_PMR",
                "RESET_BREATH",
            ],
        )

    def test_cancel_before_keeps_frequency_and_selected_time_slots(self):
        user = User.objects.create(id=self.device_code, timezone="Asia/Seoul")
        snapshot = UserContextSnapshot.objects.create(user=user, service_date=today_for_user(user))
        UserContextSnapshotState.objects.create(context_snapshot=snapshot, state=self.state, priority=1)
        activity_plan = NextActivityPlan.objects.create(
            user=user,
            context_snapshot=snapshot,
            service_date=today_for_user(user),
            expected_activity_minutes=90,
        )
        plan = RecoveryPlan.objects.create(user=user, plan_date=today_for_user(user))
        now = timezone.now().replace(microsecond=0)
        selected_time = now + timedelta(minutes=40)

        snapshot_before = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=1,
            recommended_at=now + timedelta(minutes=20),
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )
        frequency_before = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=2,
            recommended_at=now + timedelta(minutes=25),
            notification_basis=SlotNotificationBasis.FREQUENCY,
        )
        selected_slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=3,
            recommended_at=selected_time,
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )
        snapshot_after = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=4,
            recommended_at=now + timedelta(minutes=80),
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )

        response = self.client.post(
            "/api/v1/plans/recovery-slots/cancel-before/",
            {
                "before": selected_time.isoformat(),
                "exclude_slot": str(selected_slot.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["canceled_count"], 1)
        snapshot_before.refresh_from_db()
        frequency_before.refresh_from_db()
        selected_slot.refresh_from_db()
        snapshot_after.refresh_from_db()
        self.assertEqual(snapshot_before.status, SlotStatus.CANCELED)
        self.assertEqual(frequency_before.status, SlotStatus.RECOMMENDED)
        self.assertEqual(selected_slot.status, SlotStatus.RECOMMENDED)
        self.assertEqual(snapshot_after.status, SlotStatus.RECOMMENDED)

    def test_reentry_consumes_only_nearest_future_snapshot_slot(self):
        user = User.objects.create(id=self.device_code, timezone="Asia/Seoul")
        plan = RecoveryPlan.objects.create(user=user, plan_date=today_for_user(user))
        now = timezone.now().replace(microsecond=0)
        frequency_first = RecoverySlot.objects.create(
            recovery_plan=plan,
            sequence_no=1,
            recommended_at=now + timedelta(minutes=5),
            notification_basis=SlotNotificationBasis.FREQUENCY,
        )
        first_snapshot = RecoverySlot.objects.create(
            recovery_plan=plan,
            sequence_no=2,
            recommended_at=now + timedelta(minutes=10),
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )
        second_snapshot = RecoverySlot.objects.create(
            recovery_plan=plan,
            sequence_no=3,
            recommended_at=now + timedelta(minutes=20),
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )

        response = self.client.post("/api/v1/plans/recovery-slots/consume-nearest-snapshot/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["canceled_count"], 1)
        frequency_first.refresh_from_db()
        first_snapshot.refresh_from_db()
        second_snapshot.refresh_from_db()
        self.assertEqual(frequency_first.status, SlotStatus.RECOMMENDED)
        self.assertEqual(first_snapshot.status, SlotStatus.CANCELED)
        self.assertEqual(second_snapshot.status, SlotStatus.RECOMMENDED)

    def test_reentry_creates_immediate_slot_without_notification_and_two_shift_states(self):
        user = User.objects.create(id=self.device_code, timezone="Asia/Seoul")
        active_state, _ = StateOption.objects.update_or_create(
            code="BODY_STIFF",
            defaults={"label": "목과 어깨가 굳었어요"},
        )
        current_state, _ = StateOption.objects.update_or_create(
            code="EYE_TIRED",
            defaults={"label": "눈이 피로해요"},
        )
        self._ensure_reentry_activity_catalog(
            eye_state=current_state,
            body_state=active_state,
        )
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        active_snapshot = UserContextSnapshot.objects.create(user=user, service_date=today_for_user(user))
        UserContextSnapshotState.objects.create(
            context_snapshot=active_snapshot,
            state=active_state,
            priority=1,
        )
        current_snapshot = UserContextSnapshot.objects.create(user=user, service_date=today_for_user(user))
        UserContextSnapshotState.objects.create(
            context_snapshot=current_snapshot,
            state=current_state,
            priority=1,
        )
        activity_plan = NextActivityPlan.objects.create(
            user=user,
            context_snapshot=active_snapshot,
            service_date=today_for_user(user),
            expected_activity_minutes=90,
        )
        NextActivityPlan.objects.filter(id=activity_plan.id).update(
            created_at=fixed_now - timedelta(minutes=10),
        )
        activity_plan.refresh_from_db()

        plan = RecoveryPlan.objects.create(user=user, plan_date=today_for_user(user))
        frequency_slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=active_snapshot,
            next_activity_plan=activity_plan,
            sequence_no=1,
            recommended_at=fixed_now + timedelta(minutes=5),
            notification_basis=SlotNotificationBasis.FREQUENCY,
        )
        nearest_snapshot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=active_snapshot,
            next_activity_plan=activity_plan,
            sequence_no=2,
            recommended_at=fixed_now + timedelta(minutes=10),
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )
        later_snapshot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=active_snapshot,
            next_activity_plan=activity_plan,
            sequence_no=3,
            recommended_at=fixed_now + timedelta(minutes=20),
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )
        pending_notification = Notification.objects.create(
            user=user,
            recovery_slot=nearest_snapshot,
            kind=NotificationKind.RECOVERY_SLOT,
            message="회복 세션을 시작할 시간입니다.",
            scheduled_at=nearest_snapshot.recommended_at,
            status=NotificationStatus.PENDING,
        )

        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-slots/reentry/",
                {
                    "context_snapshot": str(current_snapshot.id),
                    "next_activity_plan": str(activity_plan.id),
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        frequency_slot.refresh_from_db()
        nearest_snapshot.refresh_from_db()
        later_snapshot.refresh_from_db()
        pending_notification.refresh_from_db()
        self.assertEqual(frequency_slot.status, SlotStatus.RECOMMENDED)
        self.assertEqual(nearest_snapshot.status, SlotStatus.CANCELED)
        self.assertEqual(later_snapshot.status, SlotStatus.RECOMMENDED)
        self.assertEqual(pending_notification.status, NotificationStatus.CANCELED)

        created_slot = response.data["data"]
        self.assertFalse(created_slot["notification_enabled"])
        self.assertEqual(created_slot["notifications"], [])
        self.assertEqual(
            [routine["activity"]["code"] for routine in created_slot["routine_instances"]],
            [
                "WAKE_HAND_ROUTINE",
                "SHIFT_EYE_RELAX",
                "SHIFT_BODY_STRETCH",
                "RESET_BREATH",
            ],
        )

    def test_reentry_deduplicates_same_active_and_current_state(self):
        user = User.objects.create(id=self.device_code, timezone="Asia/Seoul")
        body_state, _ = StateOption.objects.update_or_create(
            code="BODY_STIFF",
            defaults={"label": "목과 어깨가 굳었어요"},
        )
        eye_state, _ = StateOption.objects.update_or_create(
            code="EYE_TIRED",
            defaults={"label": "눈이 피로해요"},
        )
        self._ensure_reentry_activity_catalog(
            eye_state=eye_state,
            body_state=body_state,
        )
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        active_snapshot = UserContextSnapshot.objects.create(user=user, service_date=today_for_user(user))
        UserContextSnapshotState.objects.create(
            context_snapshot=active_snapshot,
            state=body_state,
            priority=1,
        )
        current_snapshot = UserContextSnapshot.objects.create(user=user, service_date=today_for_user(user))
        UserContextSnapshotState.objects.create(
            context_snapshot=current_snapshot,
            state=body_state,
            priority=1,
        )
        activity_plan = NextActivityPlan.objects.create(
            user=user,
            context_snapshot=active_snapshot,
            service_date=today_for_user(user),
            expected_activity_minutes=90,
        )
        NextActivityPlan.objects.filter(id=activity_plan.id).update(
            created_at=fixed_now - timedelta(minutes=10),
        )
        RecoveryPlan.objects.create(user=user, plan_date=today_for_user(user))

        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-slots/reentry/",
                {
                    "context_snapshot": str(current_snapshot.id),
                    "next_activity_plan": str(activity_plan.id),
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created_slot = response.data["data"]
        stage_types = [routine["stage_type"] for routine in created_slot["routine_instances"]]
        self.assertEqual(stage_types.count(StageType.BRAIN_SHIFT), 1)
        self.assertEqual(
            [routine["activity"]["code"] for routine in created_slot["routine_instances"]],
            [
                "WAKE_HAND_ROUTINE",
                "SHIFT_BODY_STRETCH",
                "RESET_BREATH",
            ],
        )

    def test_history_supports_date_filters_and_table_fields(self):
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code], "note": "오전 내내 모니터를 봄"},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 45,
            },
            format="json",
        )
        user = User.objects.get(id=self.device_code)
        snapshot = UserContextSnapshot.objects.get(id=snapshot_response.data["data"]["id"])
        activity_plan = NextActivityPlan.objects.get(id=activity_plan_response.data["data"]["id"])
        today = today_for_user(user)
        tomorrow = today + timedelta(days=1)
        now = timezone.now().replace(microsecond=0)

        plan = RecoveryPlan.objects.create(
            user=user,
            plan_date=today,
            generation_snapshot_json={
                "service_date": str(today),
                "has_today_pc_usage_pattern": True,
                "has_pc_usage_pattern": True,
                "pc_usage_pattern_count": 1,
                "pc_usage_patterns": [{"day_of_week": today_day_of_week_for_user(user), "hour": 9, "is_used": True}],
                "context_snapshot": {"id": str(snapshot.id)},
                "next_activity_plan": {"id": str(activity_plan.id)},
            },
        )
        missed_slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=1,
            recommended_at=now - timedelta(minutes=30),
            status=SlotStatus.RECOMMENDED,
        )
        completed_slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=2,
            recommended_at=now - timedelta(minutes=90),
            status=SlotStatus.COMPLETED,
        )
        upcoming_slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=3,
            recommended_at=now + timedelta(minutes=60),
            status=SlotStatus.RECOMMENDED,
        )
        tomorrow_plan = RecoveryPlan.objects.create(
            user=user,
            plan_date=tomorrow,
            generation_snapshot_json={"service_date": str(tomorrow)},
        )
        RecoverySlot.objects.create(
            recovery_plan=tomorrow_plan,
            sequence_no=1,
            recommended_at=now + timedelta(days=1),
        )

        activity = ActivityType.objects.create(
            code="history_neck_shift",
            stage_type=StageType.BRAIN_SHIFT,
            target_state=self.state,
            name="목 이완",
            default_duration_sec=90,
        )
        routine = RoutineInstance.objects.create(
            recovery_slot=missed_slot,
            activity=activity,
            sequence_no=1,
            difficulty_level=2,
            planned_duration_sec=90,
        )
        AIInsight.objects.create(
            recovery_plan=plan,
            recovery_slot=missed_slot,
            insight_type=InsightType.RECOMMENDATION_REASON,
            body="연속 사용 전에 짧은 휴식이 필요합니다.",
            data_sources_json=["context_snapshot", "next_activity_plan", "pc_usage_patterns"],
        )
        AIInsight.objects.create(
            recovery_plan=plan,
            recovery_slot=missed_slot,
            routine_instance=routine,
            insight_type=InsightType.ROUTINE_REASON,
            body="목 긴장을 낮추기 위한 루틴입니다.",
            data_sources_json=["llm_routine_reason"],
        )

        SessionFeedback.objects.create(
            recovery_slot=completed_slot,
            user=user,
            recovery_feeling="MUCH_BETTER",
            difficulty_feedback="JUST_RIGHT",
            skipped=False,
        )

        date_response = self.client.get(f"/api/v1/plans/recovery-slots/history/?date={today}")
        self.assertEqual(date_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(date_response.data["data"]), 3)

        history_statuses = {item["id"]: item["history_status"] for item in date_response.data["data"]}
        self.assertEqual(history_statuses[str(missed_slot.id)], "UPCOMING")
        self.assertEqual(history_statuses[str(completed_slot.id)], "COMPLETED")
        self.assertEqual(history_statuses[str(upcoming_slot.id)], "UPCOMING")

        completed_item = next(item for item in date_response.data["data"] if item["id"] == str(completed_slot.id))
        self.assertEqual(completed_item["remark"], "훨씬 나아졌어요")

        missed_item = next(item for item in date_response.data["data"] if item["id"] == str(missed_slot.id))
        self.assertEqual(missed_item["history_status_label"], "진행 예정")

        self.assertIn("목이 뻐근해요", missed_item["input_summary"])
        self.assertIn("과제", missed_item["input_summary"])
        self.assertIn("45분 예정", missed_item["input_summary"])
        self.assertEqual(missed_item["recommended_routines"][0]["activity"]["code"], "history_neck_shift")
        self.assertEqual(missed_item["recommended_routines"][0]["reason"], "목 긴장을 낮추기 위한 루틴입니다.")
        self.assertEqual(missed_item["remark"], "brainfit의 추천 시간")
        self.assertIn("디지털 사용 패턴", missed_item["data_source_summary"]["labels"])
        self.assertIn("디지털 사용 패턴", missed_item["data_notice"])

        range_response = self.client.get(
            f"/api/v1/plans/recovery-slots/history/?from_date={today}&to_date={today}"
        )
        self.assertEqual(range_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(range_response.data["data"]), 3)

        invalid_range_response = self.client.get(
            f"/api/v1/plans/recovery-slots/history/?start_date={tomorrow}&end_date={today}"
        )
        self.assertEqual(invalid_range_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_marks_unanswered_sent_notification_as_canceled_after_grace_period(self):
        user = User.objects.create(id=self.device_code, timezone="Asia/Seoul")
        snapshot = UserContextSnapshot.objects.create(user=user, service_date=today_for_user(user))
        UserContextSnapshotState.objects.create(context_snapshot=snapshot, state=self.state, priority=1)
        activity_plan = NextActivityPlan.objects.create(
            user=user,
            context_snapshot=snapshot,
            service_date=today_for_user(user),
            expected_activity_minutes=45,
        )
        plan = RecoveryPlan.objects.create(user=user, plan_date=today_for_user(user))
        sent_at = timezone.now().replace(microsecond=0) - timedelta(minutes=11)
        slot = RecoverySlot.objects.create(
            recovery_plan=plan,
            context_snapshot=snapshot,
            next_activity_plan=activity_plan,
            sequence_no=1,
            recommended_at=sent_at,
            notification_basis=SlotNotificationBasis.SNAPSHOT,
        )
        Notification.objects.create(
            user=user,
            recovery_slot=slot,
            kind=NotificationKind.RECOVERY_SLOT,
            message="회복 세션을 시작할 시간입니다.",
            scheduled_at=sent_at,
            sent_at=sent_at,
            status=NotificationStatus.SENT,
        )

        response = self.client.get(f"/api/v1/plans/recovery-slots/history/?date={today_for_user(user)}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        slot.refresh_from_db()
        self.assertEqual(slot.status, SlotStatus.CANCELED)
        item = response.data["data"][0]
        self.assertEqual(item["history_status"], "CANCELED")
        self.assertEqual(item["history_status_label"], "취소")

    def test_ai_generate_creates_plan_slots_routines_and_logs(self):
        snapshot_response = self.client.post(
            "/api/v1/context/context-snapshots/",
            {"state_options": [self.state.code], "note": "잠을 적게 잠"},
            format="json",
        )
        activity_plan_response = self.client.post(
            "/api/v1/context/next-activity-plans/",
            {
                "context_snapshot": snapshot_response.data["data"]["id"],
                "activity_tags": [self.activity_tag.code],
                "expected_activity_minutes": 90,
            },
            format="json",
        )
        user = User.objects.get(id=self.device_code)
        today_day = today_day_of_week_for_user(user)
        PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=14, is_used=True)
        PcUsagePattern.objects.create(user=user, day_of_week=today_day, hour=15, is_used=True)
        fixed_now = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        NextActivityPlan.objects.filter(id=activity_plan_response.data["data"]["id"]).update(
            created_at=fixed_now,
        )

        shift = ActivityType.objects.create(
            code="shift_neck",
            stage_type=StageType.BRAIN_SHIFT,
            target_state=self.state,
            name="목 이완",
            default_duration_sec=90,
        )
        second_shift = ActivityType.objects.create(
            code="shift_neck_focus",
            stage_type=StageType.BRAIN_SHIFT,
            target_state=self.state,
            name="목 이완 후 집중 전환",
            default_duration_sec=60,
        )
        ActivityType.objects.filter(
            target_state=self.state,
            stage_type=StageType.BRAIN_SHIFT,
        ).exclude(
            code__in=[shift.code, second_shift.code],
        ).update(is_active=False)
        with patch("plans.ai_planner.timezone.now", return_value=fixed_now):
            response = self.client.post(
                "/api/v1/plans/recovery-plans/today/ai-generate/",
                {
                    "context_snapshot": snapshot_response.data["data"]["id"],
                    "next_activity_plan": activity_plan_response.data["data"]["id"],
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["success"])
        self.assertIsNotNone(response.data["data"]["ai_plan_run"])
        self.assertEqual(len(response.data["data"]["slots"]), 2)
        self.assertEqual(response.data["data"]["slots"][0]["interval_minutes"], 45)
        for slot in response.data["data"]["slots"]:
            stage_types = [routine["stage_type"] for routine in slot["routine_instances"]]
            self.assertEqual(stage_types[0], StageType.BRAIN_WAKE)
            self.assertEqual(stage_types[-1], StageType.BRAIN_RESET)
            self.assertEqual(len(slot["routine_instances"]), 4)
            self.assertEqual(stage_types.count(StageType.BRAIN_SHIFT), 2)
            self.assertEqual(
                slot["notification_basis"],
                SlotNotificationBasis.SNAPSHOT,
            )
            self.assertEqual(slot["routine_instances"][0]["activity"]["code"], "WAKE_HAND_ROUTINE")
            self.assertEqual(slot["routine_instances"][-1]["activity"]["code"], "RESET_BREATH")

        first_slot_routines = response.data["data"]["slots"][0]["routine_instances"]
        second_slot_routines = response.data["data"]["slots"][1]["routine_instances"]
        self.assertEqual(first_slot_routines[0]["activity"]["code"], second_slot_routines[0]["activity"]["code"])
        self.assertEqual(first_slot_routines[1]["activity"]["code"], shift.code)
        self.assertEqual(first_slot_routines[2]["activity"]["code"], second_shift.code)
        self.assertEqual(second_slot_routines[1]["activity"]["code"], shift.code)
        self.assertEqual(first_slot_routines[3]["stage_type"], StageType.BRAIN_RESET)
        self.assertEqual(second_slot_routines[3]["stage_type"], StageType.BRAIN_RESET)
        self.assertEqual(AIPlanRun.objects.count(), 1)
        ai_run = AIPlanRun.objects.get()
        self.assertEqual(ai_run.model_name, "server_policy")
        self.assertFalse(ai_run.output_snapshot_json["raw_response"]["external_api_called"])
        self.assertEqual(
            [activity["code"] for activity in ai_run.input_snapshot_json["shift_activity_catalog"]],
            [shift.code, second_shift.code],
        )
        self.assertEqual(ai_run.input_snapshot_json["time_policy"]["interval_minutes"], 45)
        # use_ai_decision을 안 보낸 흐름이라 정책 엔진이 PC 사용 패턴을 참고하지
        # 않고, "PC 사용 패턴이 있는 시간대 안에 배치했다"는 인사이트도 안 붙는다
        # (원래 9개 슬롯 인사이트 + summary 인사이트였다면 여기서 1개 빠짐).
        self.assertEqual(AIInsight.objects.count(), 7)
        self.assertEqual(RoutineInstance.objects.count(), 8)
        created_slots = list(
            RecoverySlot.objects.filter(
                recovery_plan_id=response.data["data"]["id"],
            ).order_by("sequence_no")
        )
        self.assertEqual(
            [slot.recommended_at for slot in created_slots],
            [
                fixed_now.replace(hour=13, minute=45),
                fixed_now.replace(hour=14, minute=30),
            ],
        )

        today_slots_response = self.client.get("/api/v1/plans/recovery-slots/today/")
        self.assertEqual(today_slots_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(today_slots_response.data["data"]), 2)

    def test_web_push_subscription_create_list_and_delete(self):
        with override_settings(WEB_PUSH_VAPID_PUBLIC_KEY="public-key"):
            key_response = self.client.get("/api/v1/plans/notification-subscriptions/vapid-public-key/")
        self.assertEqual(key_response.status_code, status.HTTP_200_OK)
        self.assertEqual(key_response.data["data"]["public_key"], "public-key")

        create_response = self.client.post(
            "/api/v1/plans/notification-subscriptions/",
            {
                "endpoint": "https://push.example.test/subscriptions/abc",
                "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
                "user_agent": "test-browser",
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        subscription_id = create_response.data["data"]["id"]
        self.assertEqual(WebPushSubscription.objects.count(), 1)

        list_response = self.client.get("/api/v1/plans/notification-subscriptions/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data["data"]), 1)

        delete_response = self.client.delete(f"/api/v1/plans/notification-subscriptions/{subscription_id}/")
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)
        self.assertFalse(delete_response.data["data"]["is_active"])

        list_after_delete_response = self.client.get("/api/v1/plans/notification-subscriptions/")
        self.assertEqual(list_after_delete_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_after_delete_response.data["data"], [])


class RecoverySlotRoutineDifficultyApiTests(APITestCase):
    def setUp(self):
        self.device_code = uuid.uuid4()
        self.user = User.objects.create(id=self.device_code, timezone="Asia/Seoul")
        self.client.credentials(HTTP_X_DEVICE_CODE=str(self.device_code))
        self.plan = RecoveryPlan.objects.create(
            user=self.user,
            plan_date=today_for_user(self.user),
        )
        self.activity, _ = ActivityType.objects.update_or_create(
            code="SHIFT_EYE_RELAX",
            defaults={
                "stage_type": StageType.BRAIN_SHIFT,
                "name": "눈 피로 풀기",
                "purpose": "화면 사용으로 긴장된 눈과 시선을 쉬게 합니다.",
                "required_landmarks": ["LEFT_EYE", "RIGHT_EYE"],
                "min_difficulty": 1,
                "max_difficulty": 4,
                "default_duration_sec": 90,
                "is_active": True,
            },
        )

    def _create_slot_with_routine(self, sequence_no, status=SlotStatus.RECOMMENDED):
        slot = RecoverySlot.objects.create(
            recovery_plan=self.plan,
            sequence_no=sequence_no,
            recommended_at=timezone.now() + timedelta(minutes=sequence_no * 20),
            status=status,
        )
        routine = RoutineInstance.objects.create(
            recovery_slot=slot,
            activity=self.activity,
            sequence_no=1,
            difficulty_level=2,
            planned_duration_sec=90,
            status=RoutineInstanceStatus.AVAILABLE,
            locked_until_previous_done=False,
        )
        return slot, routine

    def test_routine_defaults_to_medium_without_previous_feedback(self):
        slot, _ = self._create_slot_with_routine(sequence_no=1)

        response = self.client.get(f"/api/v1/plans/recovery-slots/{slot.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        routine_data = response.data["data"]["routine_instances"][0]
        self.assertEqual(routine_data["frontend_session_base_id"], "eye-blink")
        self.assertEqual(routine_data["recommended_difficulty"], "medium")
        self.assertEqual(routine_data["recommended_difficulty_level"], 2)

    def test_routine_uses_previous_completed_session_feedback(self):
        previous_slot, previous_routine = self._create_slot_with_routine(
            sequence_no=1,
            status=SlotStatus.COMPLETED,
        )
        started_at = timezone.now() - timedelta(days=1, minutes=3)
        Session.objects.create(
            user=self.user,
            recovery_slot=previous_slot,
            routine_instance=previous_routine,
            activity=self.activity,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=2),
            duration_sec=120,
            accuracy=95,
            metrics={"difficulty": "medium"},
            status=SessionStatus.COMPLETED,
        )
        SessionFeedback.objects.create(
            recovery_slot=previous_slot,
            user=self.user,
            recovery_feeling=RecoveryFeeling.SAME,
            difficulty_feedback=DifficultyFeedback.TOO_EASY,
        )
        current_slot, _ = self._create_slot_with_routine(sequence_no=2)

        response = self.client.get(f"/api/v1/plans/recovery-slots/{current_slot.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        routine_data = response.data["data"]["routine_instances"][0]
        self.assertEqual(routine_data["recommended_difficulty"], "high")
        self.assertEqual(routine_data["recommended_difficulty_level"], 3)

    def test_routine_uses_previous_feedback_for_same_frontend_session_kind(self):
        previous_eye_activity, _ = ActivityType.objects.update_or_create(
            code="SHIFT_EYE_BLINK",
            defaults={
                "stage_type": StageType.BRAIN_SHIFT,
                "name": "눈 깜빡임",
                "purpose": "눈 피로를 낮춥니다.",
                "required_landmarks": ["LEFT_EYE", "RIGHT_EYE"],
                "min_difficulty": 1,
                "max_difficulty": 4,
                "default_duration_sec": 90,
                "is_active": True,
            },
        )
        previous_focus_activity, _ = ActivityType.objects.update_or_create(
            code="SHIFT_FOCUS_SWITCH",
            defaults={
                "stage_type": StageType.BRAIN_SHIFT,
                "name": "집중 전환",
                "purpose": "주의를 다시 모읍니다.",
                "required_landmarks": ["LEFT_HAND", "RIGHT_HAND"],
                "min_difficulty": 1,
                "max_difficulty": 4,
                "default_duration_sec": 90,
                "is_active": True,
            },
        )
        current_focus_activity, _ = ActivityType.objects.update_or_create(
            code="SHIFT_FOCUS_PINCH",
            defaults={
                "stage_type": StageType.BRAIN_SHIFT,
                "name": "집중 핀치",
                "purpose": "주의를 다시 모읍니다.",
                "required_landmarks": ["LEFT_HAND", "RIGHT_HAND"],
                "min_difficulty": 1,
                "max_difficulty": 4,
                "default_duration_sec": 90,
                "is_active": True,
            },
        )

        previous_slot = RecoverySlot.objects.create(
            recovery_plan=self.plan,
            sequence_no=1,
            recommended_at=timezone.now() - timedelta(days=1),
            status=SlotStatus.COMPLETED,
        )
        previous_eye_routine = RoutineInstance.objects.create(
            recovery_slot=previous_slot,
            activity=previous_eye_activity,
            sequence_no=1,
            difficulty_level=2,
            planned_duration_sec=90,
            status=RoutineInstanceStatus.COMPLETED,
            locked_until_previous_done=False,
            completed_at=timezone.now() - timedelta(days=1, minutes=10),
        )
        previous_focus_routine = RoutineInstance.objects.create(
            recovery_slot=previous_slot,
            activity=previous_focus_activity,
            sequence_no=2,
            difficulty_level=2,
            planned_duration_sec=90,
            status=RoutineInstanceStatus.COMPLETED,
            locked_until_previous_done=False,
            completed_at=timezone.now() - timedelta(days=1, minutes=8),
        )
        for index, routine in enumerate([previous_eye_routine, previous_focus_routine]):
            started_at = timezone.now() - timedelta(days=1, minutes=12 - index)
            Session.objects.create(
                user=self.user,
                recovery_slot=previous_slot,
                routine_instance=routine,
                activity=routine.activity,
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=2),
                duration_sec=120,
                accuracy=95,
                metrics={"difficulty": "medium"},
                status=SessionStatus.COMPLETED,
            )
        SessionFeedback.objects.create(
            recovery_slot=previous_slot,
            user=self.user,
            recovery_feeling=RecoveryFeeling.SAME,
            difficulty_feedback=DifficultyFeedback.A_BIT_HARD,
        )

        current_slot = RecoverySlot.objects.create(
            recovery_plan=self.plan,
            sequence_no=2,
            recommended_at=timezone.now() + timedelta(minutes=20),
            status=SlotStatus.RECOMMENDED,
        )
        RoutineInstance.objects.create(
            recovery_slot=current_slot,
            activity=self.activity,
            sequence_no=1,
            difficulty_level=2,
            planned_duration_sec=90,
            status=RoutineInstanceStatus.AVAILABLE,
            locked_until_previous_done=False,
        )
        RoutineInstance.objects.create(
            recovery_slot=current_slot,
            activity=current_focus_activity,
            sequence_no=2,
            difficulty_level=2,
            planned_duration_sec=90,
            status=RoutineInstanceStatus.LOCKED,
            locked_until_previous_done=True,
        )

        response = self.client.get(f"/api/v1/plans/recovery-slots/{current_slot.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        routines_by_code = {
            routine["activity"]["code"]: routine
            for routine in response.data["data"]["routine_instances"]
        }
        self.assertEqual(
            routines_by_code["SHIFT_EYE_RELAX"]["recommended_difficulty"],
            "low",
        )
        self.assertEqual(
            routines_by_code["SHIFT_EYE_RELAX"]["recommended_difficulty_level"],
            1,
        )
        self.assertEqual(
            routines_by_code["SHIFT_FOCUS_PINCH"]["recommended_difficulty"],
            "low",
        )
        self.assertEqual(
            routines_by_code["SHIFT_FOCUS_PINCH"]["recommended_difficulty_level"],
            1,
        )
