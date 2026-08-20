import itertools
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from context.models import NextActivityPlan, UserContextSnapshot
from context.utils import today_for_user
from digital_state.models import DayOfWeek, PcUsagePattern
from sessions_app.models import Session, SessionFeedback, SessionStatus

from .models import (
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

WEEKDAY_TO_DAY_OF_WEEK = {
    0: DayOfWeek.MON,
    1: DayOfWeek.TUE,
    2: DayOfWeek.WED,
    3: DayOfWeek.THU,
    4: DayOfWeek.FRI,
    5: DayOfWeek.SAT,
    6: DayOfWeek.SUN,
}

OPEN_SLOT_STATUSES = [
    SlotStatus.RECOMMENDED,
    SlotStatus.SCHEDULED,
    SlotStatus.CHANGED,
]

RECOVERY_INTERVAL_MINUTES_BY_STATE = {
    "EYE_TIRED": 20,       # 눈 피로 (20분 법칙)
    "BODY_STIFF": 30,      # 몸/목/어깨 뻐근 (30분 이완)
    "SLEEPY": 30,          # 졸림/피곤 (30분 각성)
    "LOW_FOCUS": 45,       # 집중 저하 (45분 인지 회복)
    "OKAY": 90,            # 아직 괜찮아요 (90분)
}
DEFAULT_RECOVERY_INTERVAL_MINUTES = 45
MAX_POLICY_RECOMMENDED_TIMES = 12
PREVIOUS_SESSION_FREQUENCY_LOOKBACK_DAYS = 30
PREVIOUS_SESSION_FREQUENCY_MIN_COUNT = 3
FREQUENCY_CLUSTER_TOLERANCE_MINUTES = 30
MAX_PREVIOUS_SESSION_FREQUENCY_TIMES = 2
NOTIFICATION_RESPONSE_GRACE_MINUTES = 10


def today_day_of_week_for_user(user):
    today = today_for_user(user)
    return WEEKDAY_TO_DAY_OF_WEEK[today.weekday()]


def get_today_pc_usage_patterns(user):
    """오늘 요일에 대해 사용자가 명시 입력한 PC 패턴 rows를 시간순으로 반환한다."""

    return PcUsagePattern.objects.filter(
        user=user,
        day_of_week=today_day_of_week_for_user(user),
        is_used=True,
    ).order_by("hour")


def has_today_pc_usage_pattern(user):
    return get_today_pc_usage_patterns(user).exists()


def _context_from_inputs(context_snapshot=None, next_activity_plan=None):
    if context_snapshot is not None:
        return context_snapshot
    return getattr(next_activity_plan, "context_snapshot", None)


def _state_interval_items(context_snapshot):
    if context_snapshot is None:
        return []

    links = context_snapshot.state_links.select_related("state").order_by("priority")
    items = []
    for link in links:
        interval_minutes = RECOVERY_INTERVAL_MINUTES_BY_STATE.get(link.state_id)
        if interval_minutes is None:
            continue
        items.append(
            {
                "state_code": link.state_id,
                "state_label": link.state.label,
                "priority": link.priority,
                "interval_minutes": interval_minutes,
            }
        )
    return items


def recovery_time_policy_for_context(context_snapshot=None):
    """
    첨부 정책집의 상태별 타이머를 서버 기준값으로 계산한다.

    복수 상태가 들어오면 더 짧은 간격을 우선해 피로 누적을 막는다.
    """

    state_intervals = _state_interval_items(context_snapshot)
    if state_intervals:
        interval_minutes = min(item["interval_minutes"] for item in state_intervals)
        selected = [
            item for item in state_intervals if item["interval_minutes"] == interval_minutes
        ]
        return {
            "interval_minutes": interval_minutes,
            "basis": "selected_state_shortest_interval",
            "state_intervals": state_intervals,
            "selected_state_codes": [item["state_code"] for item in selected],
            "reason": "복수 상태 중 가장 짧은 권장 타이머 간격을 적용했습니다.",
        }

    return {
        "interval_minutes": DEFAULT_RECOVERY_INTERVAL_MINUTES,
        "basis": "default_focus_interval",
        "state_intervals": state_intervals,
        "selected_state_codes": [],
        "reason": "정책 매핑 상태가 없어 기본 집중 회복 간격을 적용했습니다.",
    }


def recovery_interval_minutes_for_context(context_snapshot=None):
    return recovery_time_policy_for_context(context_snapshot)["interval_minutes"]


def plan_has_today_pc_usage_pattern(plan):
    return bool(plan.generation_snapshot_json.get("has_today_pc_usage_pattern", False))


def notification_basis_for_plan(plan):
    return SlotNotificationBasis.SNAPSHOT


def get_next_open_slot(plan):
    return (
        plan.slots.filter(status__in=OPEN_SLOT_STATUSES)
        .annotate(effective_at=Coalesce("user_changed_at", "scheduled_at", "recommended_at"))
        .order_by("effective_at", "sequence_no")
        .first()
    )


def get_runnable_slot(plan):
    started_slot = (
        plan.slots.filter(status=SlotStatus.STARTED)
        .annotate(effective_at=Coalesce("user_changed_at", "scheduled_at", "recommended_at"))
        .order_by("effective_at", "sequence_no")
        .first()
    )
    return started_slot or get_next_open_slot(plan)


def get_next_slot_for_user(user):
    expire_unanswered_recovery_slots(user=user)
    plan = (
        RecoveryPlan.objects.filter(
            user=user,
            plan_date=today_for_user(user),
            status=PlanStatus.ACTIVE,
        )
        .prefetch_related("slots")
        .first()
    )
    if plan is None:
        return None
    return get_next_open_slot(plan)


def get_runnable_slot_for_user(user):
    expire_unanswered_recovery_slots(user=user)
    plan = (
        RecoveryPlan.objects.filter(
            user=user,
            plan_date=today_for_user(user),
            status=PlanStatus.ACTIVE,
        )
        .prefetch_related("slots")
        .first()
    )
    if plan is None:
        return None
    return get_runnable_slot(plan)


def build_notification_message(slot):
    return f"{slot.effective_time:%H:%M} 회복 세션을 시작할 시간입니다."


def build_reengagement_message(scheduled_at):
    return f"{scheduled_at:%H:%M}에 짧은 회복 루틴을 다시 시작해볼까요?"


def notification_user_filter(user):
    return Q(user=user) | Q(user__isnull=True, recovery_slot__recovery_plan__user=user)


def pending_notifications_for_user(user):
    return Notification.objects.filter(status=NotificationStatus.PENDING).filter(notification_user_filter(user))


def cancel_pending_reengagement_notifications(*, user):
    return Notification.objects.filter(
        user=user,
        kind=NotificationKind.REENGAGEMENT,
        status=NotificationStatus.PENDING,
    ).update(status=NotificationStatus.CANCELED, updated_at=timezone.now())


def first_slot_for_reengagement(reference_slot):
    plan = reference_slot.recovery_plan
    return (
        RecoverySlot.objects.filter(recovery_plan__user=plan.user, recovery_plan__plan_date=plan.plan_date)
        .annotate(effective_at=Coalesce("user_changed_at", "scheduled_at", "recommended_at"))
        .order_by("effective_at", "sequence_no")
        .first()
    )


def schedule_reengagement_notification_if_needed(*, user, reference_slot):
    if pending_notifications_for_user(user).exists():
        return None

    source_slot = first_slot_for_reengagement(reference_slot)
    if source_slot is None:
        return None

    scheduled_at = source_slot.effective_time + timedelta(days=7)
    now = timezone.now()
    while scheduled_at <= now:
        scheduled_at += timedelta(days=7)

    return Notification.objects.create(
        user=user,
        recovery_slot=None,
        kind=NotificationKind.REENGAGEMENT,
        message=build_reengagement_message(scheduled_at),
        scheduled_at=scheduled_at,
        data_json={
            "source_recovery_slot": str(source_slot.id),
            "source_plan_date": str(source_slot.recovery_plan.plan_date),
            "url": "/",
        },
    )


def sync_slot_notification(slot):
    """
    슬롯 알림 설정을 Notification 발신 대기 목록과 맞춘다.

    Notification은 실제 발송 이력/대기 항목이고, RecoverySlot.notification_enabled는 사용자가
    해당 슬롯에 알림을 켰는지의 설정값이다.
    """

    pending_notifications = slot.notifications.filter(
        kind=NotificationKind.RECOVERY_SLOT,
        status=NotificationStatus.PENDING,
    )
    if slot.status not in OPEN_SLOT_STATUSES or not slot.notification_enabled:
        pending_notifications.update(status=NotificationStatus.CANCELED)
        return None

    cancel_pending_reengagement_notifications(user=slot.recovery_plan.user)
    notification = pending_notifications.order_by("-created_at").first()
    if notification is None:
        return Notification.objects.create(
            user=slot.recovery_plan.user,
            recovery_slot=slot,
            kind=NotificationKind.RECOVERY_SLOT,
            message=build_notification_message(slot),
            scheduled_at=slot.effective_time,
        )

    notification.user = slot.recovery_plan.user
    notification.message = build_notification_message(slot)
    notification.scheduled_at = slot.effective_time
    notification.save(update_fields=["user", "message", "scheduled_at", "updated_at"])
    return notification


def _slot_effective_time_annotation():
    return Coalesce("user_changed_at", "scheduled_at", "recommended_at")


def _open_slots_for_plan(plan, *, notification_basis=None):
    queryset = RecoverySlot.objects.select_for_update().filter(
        recovery_plan=plan,
        status__in=OPEN_SLOT_STATUSES,
    )
    if notification_basis is not None:
        queryset = queryset.filter(notification_basis=notification_basis)
    return queryset.annotate(effective_at=_slot_effective_time_annotation())


def _cancel_open_slots(slots):
    canceled_slots = []
    for slot in slots:
        if slot.status not in OPEN_SLOT_STATUSES:
            continue
        slot.status = SlotStatus.CANCELED
        slot.save(update_fields=["status", "updated_at"])
        sync_slot_notification(slot)
        canceled_slots.append(slot)
    return canceled_slots


@transaction.atomic
def expire_unanswered_recovery_slots(*, user=None, now=None):
    now = now or timezone.now()
    deadline = now - timedelta(minutes=NOTIFICATION_RESPONSE_GRACE_MINUTES)
    queryset = RecoverySlot.objects.select_for_update().filter(
        status__in=OPEN_SLOT_STATUSES,
        notifications__kind=NotificationKind.RECOVERY_SLOT,
        notifications__status=NotificationStatus.SENT,
        notifications__sent_at__lte=deadline,
    )
    if user is not None:
        queryset = queryset.filter(recovery_plan__user=user)

    slots = list(
        queryset.exclude(sessions__isnull=False)
        .distinct()
        .annotate(effective_at=_slot_effective_time_annotation())
        .order_by("effective_at", "sequence_no")
    )
    return _cancel_open_slots(slots)


def _minute_floor(value):
    return value.replace(second=0, microsecond=0)


def recommend_next_reset_time(next_activity_plan=None, base_time=None, context_snapshot=None):
    """
    다음 회복 슬롯 시각을 계산하는 정책 기반 기본값.

    첨부 정책집 기준으로 현재 상태별 타이머 간격을 사용한다. 사용자가 시간을 직접
    지정하는 수동/예약 흐름은 view가 recommended_at/recommended_times를 명시 전달한다.
    """

    base_time = _minute_floor(base_time or timezone.now())
    policy_context = _context_from_inputs(context_snapshot, next_activity_plan)
    minutes = recovery_interval_minutes_for_context(policy_context)
    return base_time + timedelta(minutes=minutes)


def _datetime_at_hour(date_value, hour):
    if hour >= 24:
        return datetime.combine(date_value + timedelta(days=1), time.min)
    return datetime.combine(date_value, time(hour=hour))


def _today_pc_usage_windows(user):
    patterns = list(get_today_pc_usage_patterns(user))
    if not patterns:
        return []

    plan_date = today_for_user(user)
    hours = sorted({pattern.hour for pattern in patterns})
    windows = []
    start_hour = previous_hour = hours[0]
    for hour in hours[1:]:
        if hour == previous_hour + 1:
            previous_hour = hour
            continue
        windows.append(
            (_datetime_at_hour(plan_date, start_hour), _datetime_at_hour(plan_date, previous_hour + 1))
        )
        start_hour = previous_hour = hour
    windows.append(
        (_datetime_at_hour(plan_date, start_hour), _datetime_at_hour(plan_date, previous_hour + 1))
    )
    return windows


def _pc_usage_window_interval_times(user, base_time, interval, windows=None):
    """
    (이력 부족 시 쓰는 기본값) 오늘 PC 사용 패턴의 연속된 시간 블록(윈도우)마다,
    그 블록 안에서 상태 기반 interval 간격으로 균일하게 반복되는 알림 후보
    시각을 만든다 — 긴 블록일수록 자연히 더 자주, interval보다 짧은 블록은
    중간 지점 하나로 최소 1개는 보장한다. 이미 지난 시각은 건너뛴다.

    블록별로 만든 후보를 그대로 이어붙이지 않고 라운드로빈으로 섞는다 — 안 그러면
    긴 블록 하나가 상한선을 혼자 다 써버려서 뒤에 있는 다른 블록엔 알림이 하나도
    안 배정되는 문제가 생긴다.
    """
    if windows is None:
        windows = _today_pc_usage_windows(user)

    per_window_times = []
    for start, end in windows:
        window_times = []
        candidate = _minute_floor(start + interval)
        while candidate <= end:
            window_times.append(candidate)
            candidate += interval

        if not window_times:
            window_times = [_minute_floor(start + (end - start) / 2)]

        window_times = [t for t in window_times if t > base_time]
        if window_times:
            per_window_times.append(window_times)

    interleaved = []
    for group in itertools.zip_longest(*per_window_times):
        interleaved.extend(t for t in group if t is not None)
    return interleaved


def _digital_state_break_times(user, base_time, interval, max_slots):
    """
    My Digital State 흐름의 알림 후보 시각을 만든다. 단순히 상태별 interval을
    기계적으로 반복하면 상태 선택 모달이랑 다를 게 없어서(개인화가 아님), 실제
    과거 세션 기록(최대 지난 30일)을 분석해서 사용자가 실제로 자주 활동했던
    시간대(빈도 클러스터, 간격이 균일할 필요 없음)를 우선 쓴다.

    이력이 부족해서 못 채우는 PC 사용 블록만, 그 블록 안에서 상태 interval로
    반복하는 기본값(_pc_usage_window_interval_times)으로 보충한다 — 완전히
    새 사용자라 이력이 하나도 없어도 최소한의 알림은 보장하기 위함.

    반환값은 (recommended_at, source) 튜플 리스트 — source는 "history"(과거
    기록 기반) 또는 "bootstrap"(이력 없어서 기본값으로 채운 것)이고, 호출부가
    이걸로 인사이트 문구를 다르게 붙인다.
    """
    frequency_times = _previous_session_frequency_times(
        user=user, base_time=base_time, max_slots=max_slots,
    )
    entries = [(recommended_at, "history") for recommended_at in frequency_times]

    remaining_budget = max_slots - len(entries)
    if remaining_budget <= 0:
        return entries

    windows = _today_pc_usage_windows(user)
    covered_indexes = set()
    for recommended_at in frequency_times:
        for index, (start, end) in enumerate(windows):
            if start <= recommended_at < end:
                covered_indexes.add(index)
                break

    uncovered_windows = [
        window for index, window in enumerate(windows) if index not in covered_indexes
    ]
    bootstrap_times = _pc_usage_window_interval_times(
        user, base_time, interval, windows=uncovered_windows
    )
    entries.extend((recommended_at, "bootstrap") for recommended_at in bootstrap_times[:remaining_budget])
    return entries


def _activity_window_end(next_activity_plan, base_time):
    expected_minutes = getattr(next_activity_plan, "expected_activity_minutes", None)
    if not expected_minutes:
        return None

    started_at = getattr(next_activity_plan, "created_at", None) or base_time
    return _minute_floor(started_at) + timedelta(minutes=expected_minutes)


def _activity_interval_times(*, base_time, end_time, interval, max_slots):
    if end_time is None:
        return [base_time + interval]

    if end_time <= base_time:
        return [base_time + interval]

    times = []
    candidate = base_time + interval
    while candidate <= end_time and len(times) < max_slots:
        times.append(candidate)
        candidate += interval

    if not times:
        times.append(_minute_floor(end_time))
    return times


def _local_datetime(value):
    if timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def _minute_of_day(value):
    return value.hour * 60 + value.minute


def _pc_usage_pattern_hour_keys(user):
    return set(
        PcUsagePattern.objects.filter(user=user, is_used=True).values_list(
            "day_of_week",
            "hour",
        )
    )


def _matches_pc_usage_pattern(value, pattern_keys):
    local_value = _local_datetime(value)
    return (WEEKDAY_TO_DAY_OF_WEEK[local_value.weekday()], local_value.hour) in pattern_keys


def is_within_pc_usage_pattern(user, value):
    """
    value(datetime)가 사용자의 PC 사용 패턴 블록(요일+시간대) 안에 들어가는지 확인한다.
    AI가 자율적으로 정한 recommended_at을 서버에서 검증할 때 씀 — 패턴을 하나도
    안 넣은 사용자는 애초에 검증할 블록이 없으므로 항상 False.
    """
    pattern_keys = _pc_usage_pattern_hour_keys(user)
    if not pattern_keys:
        return False
    return _matches_pc_usage_pattern(value, pattern_keys)


def _today_pattern_hours(user):
    today_day_of_week = today_day_of_week_for_user(user)
    return set(
        PcUsagePattern.objects.filter(
            user=user,
            day_of_week=today_day_of_week,
            is_used=True,
        ).values_list("hour", flat=True)
    )


def _top_frequency_minute_clusters(minutes, max_clusters):
    remaining = sorted(minutes)
    clusters = []

    while remaining and len(clusters) < max_clusters:
        best_cluster = []
        best_average = 0

        for anchor in remaining:
            cluster = [
                minute
                for minute in remaining
                if abs(minute - anchor) <= FREQUENCY_CLUSTER_TOLERANCE_MINUTES
            ]
            average = sum(cluster) / len(cluster)
            if len(cluster) > len(best_cluster) or (
                len(cluster) == len(best_cluster) and average < best_average
            ):
                best_cluster = cluster
                best_average = average

        if len(best_cluster) < PREVIOUS_SESSION_FREQUENCY_MIN_COUNT:
            break

        clusters.append(
            {
                "minute": min(23 * 60 + 59, max(0, round(best_average))),
                "count": len(best_cluster),
            }
        )
        for minute in best_cluster:
            remaining.remove(minute)

    clusters.sort(key=lambda item: (-item["count"], item["minute"]))
    return clusters[:max_clusters]


def _previous_session_frequency_times(*, user, base_time, max_slots):
    pattern_keys = _pc_usage_pattern_hour_keys(user)
    today_hours = _today_pattern_hours(user)
    if not pattern_keys or not today_hours:
        return []

    since = base_time - timedelta(days=PREVIOUS_SESSION_FREQUENCY_LOOKBACK_DAYS)
    sessions = (
        Session.objects.filter(
            user=user,
            status=SessionStatus.COMPLETED,
            started_at__gte=since,
        )
        .exclude(started_at__isnull=True)
        .order_by("-started_at")
    )

    session_minutes = [
        _minute_of_day(_local_datetime(session.started_at))
        for session in sessions
        if _matches_pc_usage_pattern(session.started_at, pattern_keys)
    ]
    frequent_clusters = _top_frequency_minute_clusters(session_minutes, max_slots)

    plan_date = today_for_user(user)
    times = []
    for cluster in frequent_clusters:
        minute_of_day = cluster["minute"]
        hour = minute_of_day // 60
        minute = minute_of_day % 60
        if hour not in today_hours:
            continue
        candidate = datetime.combine(plan_date, time(hour=hour, minute=minute))
        if candidate <= base_time:
            continue
        times.append(candidate)

    return sorted(times)


def _merge_recommended_slot(slots_by_time, *, recommended_at, notification_basis, reason, data_sources):
    key = _minute_floor(recommended_at)
    existing = slots_by_time.get(key)
    if existing is None:
        slots_by_time[key] = {
            "recommended_at": key,
            "notification_basis": notification_basis,
            "reason": reason,
            "data_sources": list(data_sources),
        }
        return

    if notification_basis == SlotNotificationBasis.FREQUENCY:
        existing["notification_basis"] = SlotNotificationBasis.FREQUENCY
    if reason and reason not in existing["reason"]:
        existing["reason"] = f"{existing['reason']} {reason}".strip()
    for source in data_sources:
        if source not in existing["data_sources"]:
            existing["data_sources"].append(source)




def build_policy_recommended_slots(
    *,
    user,
    context_snapshot=None,
    next_activity_plan=None,
    base_time=None,
    max_slots=MAX_POLICY_RECOMMENDED_TIMES,
    include_frequency_slots=True,
    prioritize_pc_usage_windows=False,
):
    """
    상태 기반 스냅샷 슬롯과(옵션으로) 빈도 기반 슬롯을 함께 계산한다.

    include_frequency_slots=False면 PC 사용 패턴/과거 세션 빈도(digital_state)는
    아예 참고하지 않고 상태 스냅샷+다음 활동 시간만으로 슬롯을 만든다 — 상태
    선택 모달("회복 루틴 시작하기" 등)처럼 My Digital State와 완전히 무관해야
    하는 흐름에서 쓴다.

    prioritize_pc_usage_windows=True(My Digital State에서 PC 사용 패턴을 입력하고
    만든 흐름에서만 킴)면, 오늘 PC 사용 패턴이 있는 한 하루 전체 인터벌 반복
    대신 실제 과거 세션 기록을 분석해서 사용자가 자주 활동했던 시간대(빈도
    클러스터, 간격이 균일할 필요 없음)를 우선 배치하고, 이력이 부족한 블록만
    상태 interval 반복으로 보충한다(_digital_state_break_times). 이 플래그가
    False인 다른 호출부(예: 이후 활동 다시 설정)는 기존 동작 그대로다.
    """

    base_time = _minute_floor(base_time or timezone.now())
    policy_context = _context_from_inputs(context_snapshot, next_activity_plan)
    interval_minutes = recovery_interval_minutes_for_context(policy_context)
    interval = timedelta(minutes=interval_minutes)
    slots_by_time = {}

    pc_break_entries = (
        _digital_state_break_times(user, base_time, interval, max_slots)
        if prioritize_pc_usage_windows and include_frequency_slots
        else []
    )

    if pc_break_entries:
        for recommended_at, source in pc_break_entries[:max_slots]:
            if source == "history":
                reason = "과거 실제 활동 기록을 보면 이 시간대에 자주 활동하셔서 배치했습니다."
                data_sources = ["pc_usage_patterns", "previous_sessions", "time_policy"]
            else:
                reason = "PC 사용 구간 안에서 짧은 휴식을 권합니다."
                data_sources = ["pc_usage_patterns", "time_policy"]
            _merge_recommended_slot(
                slots_by_time,
                recommended_at=recommended_at,
                notification_basis=SlotNotificationBasis.FREQUENCY,
                reason=reason,
                data_sources=data_sources,
            )
    else:
        activity_end = _activity_window_end(next_activity_plan, base_time)
        for recommended_at in _activity_interval_times(
            base_time=base_time,
            end_time=activity_end,
            interval=interval,
            max_slots=max_slots,
        ):
            _merge_recommended_slot(
                slots_by_time,
                recommended_at=recommended_at,
                notification_basis=SlotNotificationBasis.SNAPSHOT,
                reason="이후 활동 시간 동안 현재 상태에 맞춘 회복 간격으로 배치했습니다.",
                data_sources=["context_snapshot", "next_activity_plan", "time_policy"],
            )

    remaining_slots = max(0, min(MAX_PREVIOUS_SESSION_FREQUENCY_TIMES, max_slots - len(slots_by_time)))
    if include_frequency_slots and remaining_slots:
        for recommended_at in _previous_session_frequency_times(
            user=user,
            base_time=base_time,
            max_slots=remaining_slots,
        ):
            _merge_recommended_slot(
                slots_by_time,
                recommended_at=recommended_at,
                notification_basis=SlotNotificationBasis.FREQUENCY,
                reason="주간 PC 사용 패턴과 최근 회복 세션 기록이 함께 몰린 시간대에 배치했습니다.",
                data_sources=["pc_usage_patterns", "previous_sessions", "time_policy"],
            )

    slots = sorted(slots_by_time.values(), key=lambda item: item["recommended_at"])
    return slots[:max_slots]


def build_policy_recommended_times(
    *,
    user,
    context_snapshot=None,
    next_activity_plan=None,
    base_time=None,
    max_slots=MAX_POLICY_RECOMMENDED_TIMES,
):
    return [
        slot["recommended_at"]
        for slot in build_policy_recommended_slots(
            user=user,
            context_snapshot=context_snapshot,
            next_activity_plan=next_activity_plan,
            base_time=base_time,
            max_slots=max_slots,
        )
    ]


def build_plan_generation_snapshot(user, context_snapshot=None, next_activity_plan=None):
    """
    계획 생성 시점의 분기 근거를 불변 JSON으로 저장한다.

    RecoveryPlan/RecoverySlot에 PC 패턴 기반 여부 enum을 두지 않고도 이후 로직이 생성 당시의
    판단을 재사용할 수 있게 하는 최소 스냅샷이다.
    """

    today_patterns = list(get_today_pc_usage_patterns(user).values("day_of_week", "hour", "is_used"))
    pc_usage_pattern_count = PcUsagePattern.objects.filter(user=user, is_used=True).count()
    return {
        "service_date": str(today_for_user(user)),
        "has_today_pc_usage_pattern": bool(today_patterns),
        "has_pc_usage_pattern": pc_usage_pattern_count > 0,
        "pc_usage_pattern_count": pc_usage_pattern_count,
        "pc_usage_patterns": today_patterns,
        "time_policy": recovery_time_policy_for_context(context_snapshot),
        "context_snapshot": serialize_context_snapshot(context_snapshot),
        "next_activity_plan": serialize_next_activity_plan(next_activity_plan),
    }


def serialize_context_snapshot(context_snapshot):
    if context_snapshot is None:
        return None
    return {
        "id": str(context_snapshot.id),
        "state_options": list(
            context_snapshot.state_links.order_by("priority").values_list("state_id", flat=True)
        ),
        "note": context_snapshot.note,
        "created_at": context_snapshot.created_at.isoformat() if context_snapshot.created_at else None,
    }


def serialize_next_activity_plan(next_activity_plan):
    if next_activity_plan is None:
        return None
    return {
        "id": str(next_activity_plan.id),
        "context_snapshot": (
            str(next_activity_plan.context_snapshot_id)
            if next_activity_plan.context_snapshot_id
            else None
        ),
        "activity_tags": list(next_activity_plan.activity_tag_links.values_list("activity_tag_id", flat=True)),
        "expected_activity_minutes": next_activity_plan.expected_activity_minutes,
        "created_at": next_activity_plan.created_at.isoformat() if next_activity_plan.created_at else None,
    }


def validate_context_inputs(user, context_snapshot=None, next_activity_plan=None):
    if context_snapshot is not None and context_snapshot.user_id != user.id:
        raise ValidationError("본인의 상태 스냅샷만 사용할 수 있습니다.")
    if next_activity_plan is not None and next_activity_plan.user_id != user.id:
        raise ValidationError("본인의 이후 활동 계획만 사용할 수 있습니다.")


@transaction.atomic
def create_or_replace_today_plan(
    *,
    user,
    context_snapshot=None,
    next_activity_plan=None,
    recommended_times=None,
    recommended_slots=None,
    notification_enabled=True,
    ai_plan_run=None,
    use_ai_decision=None,
):
    """
    오늘 RecoveryPlan을 만들거나(없으면) 이어서 쓴다(있으면).

    상태 선택 모달 흐름(use_ai_decision=False/None)과 My Digital State 흐름
    (use_ai_decision=True)은 완전히 독립적이어야 한다 — 한쪽을 다시 생성해도
    다른 쪽이 이미 예약해둔 알림은 건드리면 안 된다. 그래서 예전처럼 "오늘 active
    plan을 통째로 닫고 새로 만드는" 대신, 오늘 하루엔 active plan을 하나만
    재사용하면서 "이번 생성과 같은 흐름으로 만들어졌던 열린 슬롯"만 취소하고
    다른 흐름의 슬롯은 그대로 둔다.

    recommended_times는 기존 호출부 호환용이고, 슬롯마다 알림 basis를 지정해야 하는 정책 생성
    흐름은 recommended_slots를 사용한다.
    """

    validate_context_inputs(user, context_snapshot, next_activity_plan)

    plan_date = today_for_user(user)
    active_plans = list(
        RecoveryPlan.objects.select_for_update().filter(
            user=user,
            plan_date=plan_date,
            status=PlanStatus.ACTIVE,
        )
    )

    for active_plan in active_plans:
        same_flow_open_slots = _open_slots_for_plan(active_plan).filter(use_ai_decision=use_ai_decision)
        _cancel_open_slots(same_flow_open_slots.order_by("effective_at", "sequence_no"))

    generation_snapshot = build_plan_generation_snapshot(user, context_snapshot, next_activity_plan)
    plan = active_plans[0] if active_plans else None
    if plan is None:
        plan = RecoveryPlan.objects.create(
            user=user,
            ai_plan_run=ai_plan_run,
            plan_date=plan_date,
            generation_snapshot_json=generation_snapshot,
        )
    else:
        # 유니크 제약상 동시에 active plan이 여러 개일 순 없지만, 방어적으로
        # 남는 게 있으면 닫아둔다.
        for extra_plan in active_plans[1:]:
            extra_plan.status = PlanStatus.REPLACED
            extra_plan.save(update_fields=["status", "updated_at"])
        plan.ai_plan_run = ai_plan_run
        plan.generation_snapshot_json = generation_snapshot
        plan.save(update_fields=["ai_plan_run", "generation_snapshot_json", "updated_at"])

    slot_inputs = list(recommended_slots or [])
    if not slot_inputs:
        slot_inputs = [
            {
                "recommended_at": recommended_at,
                "notification_basis": notification_basis_for_plan(plan),
            }
            for recommended_at in list(recommended_times or [])
        ]
    if not slot_inputs:
        slot_inputs = [
            {
                "recommended_at": None,
                "notification_basis": notification_basis_for_plan(plan),
            }
        ]

    for slot_input in slot_inputs:
        create_recovery_slot(
            plan=plan,
            context_snapshot=context_snapshot,
            next_activity_plan=next_activity_plan,
            recommended_at=slot_input.get("recommended_at"),
            notification_enabled=slot_input.get("notification_enabled", notification_enabled),
            notification_basis=slot_input.get("notification_basis") or notification_basis_for_plan(plan),
            ai_plan_run=ai_plan_run,
            use_ai_decision=use_ai_decision,
        )
    return plan


@transaction.atomic
def create_recovery_slot(
    *,
    plan,
    context_snapshot=None,
    next_activity_plan=None,
    recommended_at=None,
    notification_enabled=True,
    notification_basis=None,
    ai_plan_run=None,
    use_ai_decision=None,
):
    validate_context_inputs(plan.user, context_snapshot, next_activity_plan)

    locked_plan = RecoveryPlan.objects.select_for_update().get(id=plan.id)
    last_sequence = locked_plan.slots.aggregate(max_sequence=Max("sequence_no"))["max_sequence"] or 0
    generated_recommended_at = recommended_at is None
    if generated_recommended_at:
        recommended_at = recommend_next_reset_time(
            next_activity_plan,
            context_snapshot=context_snapshot,
        )
    notification_basis = notification_basis or notification_basis_for_plan(locked_plan)
    slot = RecoverySlot.objects.create(
        recovery_plan=locked_plan,
        ai_plan_run=ai_plan_run,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        sequence_no=last_sequence + 1,
        recommended_at=recommended_at,
        interval_minutes=(
            recovery_interval_minutes_for_context(_context_from_inputs(context_snapshot, next_activity_plan))
            if generated_recommended_at
            else None
        ),
        notification_enabled=notification_enabled,
        notification_basis=notification_basis,
        use_ai_decision=use_ai_decision,
    )
    sync_slot_notification(slot)
    return slot


@transaction.atomic
def reset_next_activity_and_slot(
    *,
    user,
    target_slot=None,
    context_snapshot=None,
    next_activity_plan=None,
    recommended_at=None,
    notification_enabled=True,
    ai_plan_run=None,
):
    """
    '내 계획 다시 설정' 흐름.

    이후 활동은 현재 활성 구간 기준으로 하나만 존재할 수 있으므로, 열린 스냅샷 기반 슬롯은
    모두 닫고 새 이후 활동 시간 안에서 다시 배치한다. 빈도 기반 슬롯은 독립 알림이라 유지한다.
    """

    validate_context_inputs(user, context_snapshot, next_activity_plan)
    plan = RecoveryPlan.objects.select_for_update().get(
        user=user,
        plan_date=today_for_user(user),
        status=PlanStatus.ACTIVE,
    )

    slot_to_replace = None
    if target_slot is not None:
        slot_to_replace = RecoverySlot.objects.select_for_update().get(id=target_slot.id, recovery_plan=plan)
        if slot_to_replace.status not in OPEN_SLOT_STATUSES:
            raise ValidationError("열려 있는 회복 슬롯만 다시 설정할 수 있습니다.")
        if slot_to_replace.notification_basis != SlotNotificationBasis.SNAPSHOT:
            raise ValidationError("스냅샷 기반 회복 슬롯만 이후 활동으로 다시 설정할 수 있습니다.")
    else:
        slot_to_replace = (
            _open_slots_for_plan(plan, notification_basis=SlotNotificationBasis.SNAPSHOT)
            .order_by("effective_at", "sequence_no")
            .first()
        )

    snapshot_slots = _open_slots_for_plan(
        plan,
        notification_basis=SlotNotificationBasis.SNAPSHOT,
    ).order_by("effective_at", "sequence_no")
    _cancel_open_slots(snapshot_slots)

    if recommended_at is not None:
        replacement_slots = [
            {
                "recommended_at": recommended_at,
                "notification_basis": SlotNotificationBasis.SNAPSHOT,
            }
        ]
    else:
        replacement_slots = [
            slot
            for slot in build_policy_recommended_slots(
                user=user,
                context_snapshot=context_snapshot,
                next_activity_plan=next_activity_plan,
                base_time=timezone.now(),
            )
            if slot["notification_basis"] == SlotNotificationBasis.SNAPSHOT
        ]

    if not replacement_slots:
        replacement_slots = [{"recommended_at": None, "notification_basis": SlotNotificationBasis.SNAPSHOT}]

    created_slots = []
    for slot_input in replacement_slots:
        created_slots.append(
            create_recovery_slot(
                plan=plan,
                context_snapshot=context_snapshot,
                next_activity_plan=next_activity_plan,
                recommended_at=slot_input.get("recommended_at"),
                notification_enabled=notification_enabled,
                notification_basis=SlotNotificationBasis.SNAPSHOT,
                ai_plan_run=ai_plan_run,
            )
        )

    return created_slots[0]


@transaction.atomic
def schedule_next_slot_after_completed_slot(
    *,
    completed_slot,
    context_snapshot=None,
    next_activity_plan=None,
    recommended_at=None,
    notification_enabled=True,
    ai_plan_run=None,
):
    """
    PC 패턴이 없는 순차 생성 흐름에서 세션 완료 후 다음 RecoverySlot을 1개 추가한다.

    생성 당시 PC 패턴이 있었던 plan은 이미 하루치 슬롯을 갖고 있으므로 새 슬롯을 만들지 않는다.
    """

    slot = RecoverySlot.objects.select_for_update().select_related("recovery_plan__user").get(
        id=completed_slot.id
    )
    if slot.status != SlotStatus.COMPLETED:
        slot.status = SlotStatus.COMPLETED
        slot.save(update_fields=["status", "updated_at"])

    plan = slot.recovery_plan
    if plan_has_today_pc_usage_pattern(plan):
        return None

    return create_recovery_slot(
        plan=plan,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        recommended_at=recommended_at,
        notification_enabled=notification_enabled,
        notification_basis=SlotNotificationBasis.SNAPSHOT,
        ai_plan_run=ai_plan_run,
    )


@transaction.atomic
def set_slot_notification(*, slot, enabled, repeat_rule=""):
    slot.notification_enabled = enabled
    slot.repeat_rule = repeat_rule if enabled else ""
    slot.save(update_fields=["notification_enabled", "repeat_rule", "updated_at"])
    sync_slot_notification(slot)
    if not enabled:
        schedule_reengagement_notification_if_needed(
            user=slot.recovery_plan.user,
            reference_slot=slot,
        )
    return slot


@transaction.atomic
def schedule_slot_time(*, slot, scheduled_at):
    slot.user_changed_at = scheduled_at
    slot.status = SlotStatus.CHANGED
    slot.save(update_fields=["user_changed_at", "status", "updated_at"])
    sync_slot_notification(slot)
    return slot


@transaction.atomic
def cancel_slot(*, slot):
    slot.status = SlotStatus.CANCELED
    slot.save(update_fields=["status", "updated_at"])
    sync_slot_notification(slot)
    return slot


def _open_snapshot_slots_for_user(user):
    return (
        RecoverySlot.objects.select_for_update()
        .select_related("recovery_plan")
        .filter(
            recovery_plan__user=user,
            recovery_plan__plan_date=today_for_user(user),
            recovery_plan__status=PlanStatus.ACTIVE,
            notification_basis=SlotNotificationBasis.SNAPSHOT,
            status__in=OPEN_SLOT_STATUSES,
        )
        .annotate(effective_at=_slot_effective_time_annotation())
    )


@transaction.atomic
def cancel_snapshot_slots_before(*, user, before, exclude_slot=None):
    queryset = _open_snapshot_slots_for_user(user).filter(effective_at__lt=before)
    if exclude_slot is not None:
        queryset = queryset.exclude(id=exclude_slot.id)

    slots = list(queryset.order_by("effective_at", "sequence_no"))
    for slot in slots:
        slot.status = SlotStatus.CANCELED
        slot.save(update_fields=["status", "updated_at"])
        sync_slot_notification(slot)
    return slots


@transaction.atomic
def cancel_next_snapshot_slot_for_reentry(*, user, now=None):
    now = now or timezone.now()
    slot = _open_snapshot_slots_for_user(user).filter(effective_at__gt=now).order_by("effective_at", "sequence_no").first()
    if slot is None:
        return None
    slot.status = SlotStatus.CANCELED
    slot.save(update_fields=["status", "updated_at"])
    sync_slot_notification(slot)
    return slot


@transaction.atomic
def mark_notification_clicked(*, notification):
    notification.status = NotificationStatus.CLICKED
    notification.clicked_at = timezone.now()
    notification.save(update_fields=["status", "clicked_at", "updated_at"])
    return notification


@transaction.atomic
def submit_slot_feedback(*, slot, recovery_feeling, difficulty_feedback):
    feedback, _ = SessionFeedback.objects.update_or_create(
        recovery_slot=slot,
        defaults={
            "user": slot.recovery_plan.user,
            "recovery_feeling": recovery_feeling,
            "difficulty_feedback": difficulty_feedback,
            "skipped": False,
        },
    )
    if slot.status != SlotStatus.COMPLETED:
        slot.status = SlotStatus.COMPLETED
        slot.save(update_fields=["status", "updated_at"])
    sync_slot_notification(slot)
    return feedback


@transaction.atomic
def upsert_web_push_subscription(*, user, endpoint, p256dh, auth, user_agent=""):
    subscription, _ = WebPushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": user,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": user_agent,
            "is_active": True,
            "last_seen_at": timezone.now(),
        },
    )
    return subscription


@transaction.atomic
def deactivate_web_push_subscription(*, subscription):
    subscription.is_active = False
    subscription.save(update_fields=["is_active", "updated_at"])
    return subscription


def cancel_nearest_upcoming_pc_usage_block_notifications(*, user, current_time=None):
    """
    [상태 선택 모달로 알림을 직접 설정했을 때의 정리 정책]
    My Digital State가 오늘 하루치를 미리 예약해두는 이유는 "서비스를 써야
    한다는 걸 인지 못 하는 순간에도 알려주기 위함"이다. 근데 사용자가 이미
    스스로 인지하고 서비스에 들어와서 상태 선택 모달로 알림을 새로 설정했다면,
    지금 시점 이후 가장 가까운(아직 시작 안 한) PC 사용 블록 하나는 굳이
    중복으로 울릴 필요가 없다고 보고 그 블록의 빈도 기반 알림만 취소한다.
    더 먼 다른 블록들은 손대지 않는다.
    """
    now = current_time or timezone.now()

    upcoming_windows = [
        window for window in _today_pc_usage_windows(user) if window[0] > now
    ]
    if not upcoming_windows:
        return []

    nearest_start, nearest_end = min(upcoming_windows, key=lambda window: window[0])

    open_slots = RecoverySlot.objects.filter(
        recovery_plan__user=user,
        recovery_plan__plan_date=today_for_user(user),
        status__in=OPEN_SLOT_STATUSES,
        notification_basis=SlotNotificationBasis.FREQUENCY,
    ).select_related("recovery_plan")

    canceled_slots = []
    for slot in open_slots:
        slot_time = slot.effective_time
        if slot_time and nearest_start <= slot_time < nearest_end:
            slot.status = SlotStatus.CANCELED
            slot.save(update_fields=["status", "updated_at"])
            sync_slot_notification(slot)
            canceled_slots.append(slot)

    return canceled_slots


@transaction.atomic
def update_slot_context_on_session_start(*, slot, context_snapshot):
    """
    [세션 진입 시 슬롯 피로 상태 최신화]
    사용자가 특정 시각 알림으로 들어와 최신 피로 상태(UserContextSnapshot)를 선택하면
    해당 슬롯의 context_snapshot을 갱신하고 맞춤 루틴(Brain Shift) 조합을 재설정한다.
    """
    if context_snapshot is None:
        return slot

    slot.context_snapshot = context_snapshot
    slot.save(update_fields=["context_snapshot", "updated_at"])

    # 세션 미작동 상태인 기존 루틴 인스턴스 정리 및 최신 상태 기반 조합 재생성
    from plans.ai_planner import _create_routine_instances
    slot.routine_instances.filter(status__in=["LOCKED", "AVAILABLE"]).delete()
    _create_routine_instances(slot, context_snapshot, slot.next_activity_plan, [])

    return slot