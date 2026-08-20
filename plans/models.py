from django.db import models
from django.db.models import Q

from common.models import BaseModel


class SlotStatus(models.TextChoices):
    RECOMMENDED = "RECOMMENDED", "추천됨"
    SCHEDULED = "SCHEDULED", "예약됨"
    CHANGED = "CHANGED", "변경됨"
    CANCELED = "CANCELED", "취소됨"
    STARTED = "STARTED", "시작됨"
    COMPLETED = "COMPLETED", "완료"
    # MISSED(놓침)는 삭제함 — 추천 시간이 지나도 사용자가 언제든 세션을 시작할 수 있는
    # 정책이라 "놓쳐서 시작 불가"를 나타내는 상태 자체가 필요 없음.


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", "대기중"
    SENT = "SENT", "발송됨"
    CLICKED = "CLICKED", "클릭됨"
    FAILED = "FAILED", "실패"
    CANCELED = "CANCELED", "취소됨"


class NotificationKind(models.TextChoices):
    RECOVERY_SLOT = "RECOVERY_SLOT", "회복 슬롯"
    REENGAGEMENT = "REENGAGEMENT", "재진입"


class SlotNotificationBasis(models.TextChoices):
    SNAPSHOT = "SNAPSHOT", "스냅샷 기반"
    FREQUENCY = "FREQUENCY", "빈도 기반"


class InsightType(models.TextChoices):
    TODAY_ANALYSIS = "TODAY_ANALYSIS", "오늘의 분석"
    RECOMMENDATION_REASON = "RECOMMENDATION_REASON", "추천 이유"
    ROUTINE_REASON = "ROUTINE_REASON", "루틴 이유"
    DATA_INSIGHT = "DATA_INSIGHT", "데이터 인사이트"  # IA 09-3 "Data Insight" 섹션과 매칭해서 확정


class PlanStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "활성"
    REPLACED = "REPLACED", "재설정됨"
    CANCELED = "CANCELED", "취소됨"
    COMPLETED = "COMPLETED", "완료"


# LLM 호출 1회의 입력/출력 원본 로그
class AIPlanRun(BaseModel):
    """
    ERD: ai_plan_runs. LLM 호출 원본 로그(입력/출력 스냅샷).

    reference_sessions(Session M2M), pc_usage_patterns(PcUsagePattern M2M)는 둘 다 제거함.
    이유 1(의존성 방향): reference_sessions는 sessions_app.Session을 참조하는데,
    sessions_app이 이미 plans(recovery_slot_id)를 참조하고 있어서 plans ↔ sessions_app이
    서로를 아는 구조가 됨(문자열 참조라 마이그레이션 자체는 도는데 "의존성 한 방향" 원칙은
    깨짐).
    이유 2(데이터 유실): pc_usage_patterns는 PUT /digital-state/patterns/bulk/가
    delete-then-create 방식이라, 사용자가 패턴을 수정하는 순간 on_delete=CASCADE로 과거
    AIPlanRun과의 M2M 연결이 조용히 끊어짐. AIPlanRun의 목적 자체가 "그 당시 뭘 보고
    추천했는지" 기록하는 것이라, 변경 가능한 DB row를 참조하는 대신 아래
    input_snapshot_json에 당시 값을 그대로 스냅샷으로 저장한다.
    예: {"context_snapshot": {...}, "next_activity_plan": {...},
         "previous_sessions": [...], "previous_feedback": [...],
         "pc_usage_patterns": [{"day_of_week": "MON", ...}]}
    """

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="ai_plan_runs")
    context_snapshot = models.ForeignKey(
        "context.UserContextSnapshot", on_delete=models.CASCADE, related_name="ai_plan_runs"
    )
    next_activity_plan = models.ForeignKey(
        "context.NextActivityPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_plan_runs",
    )
    model_name = models.CharField(max_length=100)
    input_snapshot_json = models.JSONField()
    output_snapshot_json = models.JSONField()

    class Meta:
        ordering = ["-created_at"]


# 하루치 휴식 일정의 상위 컨테이너(사용자·날짜당 1건)
class RecoveryPlan(BaseModel):
    """
    ERD: recovery_plans.

    사용자의 하루 회복 일정 컨테이너다. PC 패턴 기반인지 여부는 enum/boolean으로 중복 저장하지
    않고, generation_snapshot_json과 AIPlanRun.input_snapshot_json에 생성 당시 입력으로 남긴다.

    overall_reason / data_source_summary는 ai_insights와 겹쳐서 제거함(ai_reason을
    ai_insights로 합쳤을 때와 같은 논리). 플랜 전체 단위 설명이 필요하면 AIInsight를
    recovery_plan만 걸고(recovery_slot/routine_instance는 null) 만들면 된다 —
    overall_reason은 insight_type=RECOMMENDATION_REASON 정도로, data_source_summary는
    data_sources_json으로 표현.
    """

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="recovery_plans")
    ai_plan_run = models.ForeignKey(AIPlanRun, on_delete=models.SET_NULL, null=True, related_name="recovery_plans")
    plan_date = models.DateField(db_index=True)
    generation_snapshot_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=PlanStatus.choices, default=PlanStatus.ACTIVE)

    class Meta:
        ordering = ["-plan_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "plan_date"],
                condition=Q(status="ACTIVE"),
                name="unique_active_plan_per_user_date",
            )
        ]

    def __str__(self):
        return f"RecoveryPlan({self.user_id}, {self.plan_date})"


# RecoveryPlan에 속한 개별 휴식 타임슬롯
class RecoverySlot(BaseModel):
    """
    ERD: recovery_slots.
    ai_reason 컬럼은 제거함 — ai_insights(recovery_slot_id로 연결)로 일원화.
    순차 생성 흐름에서 슬롯마다 다른 상태 스냅샷/이후 활동 계획을 기준으로 만들어질 수 있으므로
    context_snapshot, next_activity_plan을 슬롯에 직접 연결한다.
    """

    recovery_plan = models.ForeignKey(RecoveryPlan, on_delete=models.CASCADE, related_name="slots")
    ai_plan_run = models.ForeignKey(
        AIPlanRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recovery_slots",
    )
    context_snapshot = models.ForeignKey(
        "context.UserContextSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recovery_slots",
    )
    next_activity_plan = models.ForeignKey(
        "context.NextActivityPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recovery_slots",
    )
    sequence_no = models.PositiveSmallIntegerField()
    recommended_at = models.DateTimeField()
    scheduled_at = models.DateTimeField(null=True, blank=True)
    user_changed_at = models.DateTimeField(null=True, blank=True)
    interval_minutes = models.PositiveIntegerField(null=True, blank=True)
    repeat_rule = models.CharField(max_length=120, blank=True)
    notification_enabled = models.BooleanField(default=True)
    notification_basis = models.CharField(
        max_length=20,
        choices=SlotNotificationBasis.choices,
        default=SlotNotificationBasis.SNAPSHOT,
    )
    status = models.CharField(max_length=20, choices=SlotStatus.choices, default=SlotStatus.RECOMMENDED)
    # 상태 선택 모달(False)과 My Digital State(True) 중 이 슬롯을 만든 흐름.
    # 두 흐름은 완전히 독립적이어야 해서(한쪽을 재생성해도 다른 쪽 알림은 그대로),
    # 하루치 RecoveryPlan을 재사용하게 되면서 "이 슬롯이 어느 흐름 소속인지"를
    # 슬롯 단위로 알아야만 재생성 시 같은 흐름의 슬롯만 골라 취소할 수 있다.
    # None은 두 흐름을 거치지 않은 레거시/수동 생성 슬롯.
    use_ai_decision = models.BooleanField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["recovery_plan", "sequence_no"]
        constraints = [
            models.UniqueConstraint(fields=["recovery_plan", "sequence_no"], name="unique_slot_sequence")
        ]

    @property
    def effective_time(self):
        return self.user_changed_at or self.scheduled_at or self.recommended_at

    def __str__(self):
        return f"RecoverySlot(plan={self.recovery_plan_id}, #{self.sequence_no})"


class Notification(BaseModel):
    """웹 푸시로 보낼 사용자 알림 큐와 발송 이력."""

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    recovery_slot = models.ForeignKey(
        RecoverySlot,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    kind = models.CharField(
        max_length=30,
        choices=NotificationKind.choices,
        default=NotificationKind.RECOVERY_SLOT,
    )
    message = models.CharField(max_length=255)
    scheduled_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    data_json = models.JSONField(default=dict, blank=True)
    delivery_error = models.TextField(blank=True)


class WebPushSubscription(BaseModel):
    """브라우저 Web Push 발송 대상 구독 정보."""

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="web_push_subscriptions")
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"WebPushSubscription({self.user_id}, active={self.is_active})"


# AI 추천/판단에 대한 설명 텍스트 모음
class AIInsight(BaseModel):
    """
    ERD: ai_insights.
    recovery_slots.ai_reason / routine_instances.ai_reason이 없어졌으니,
    AI 추천/판단에 대한 설명 텍스트는 전부 여기로 모인다.
    """

    recovery_plan = models.ForeignKey(RecoveryPlan, on_delete=models.CASCADE, related_name="insights")
    recovery_slot = models.ForeignKey(
        RecoverySlot, on_delete=models.CASCADE, null=True, blank=True, related_name="insights"
    )
    routine_instance = models.ForeignKey(
        "routines.RoutineInstance", on_delete=models.CASCADE, null=True, blank=True, related_name="insights"
    )
    insight_type = models.CharField(max_length=30, choices=InsightType.choices)
    body = models.TextField()
    data_sources_json = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]
