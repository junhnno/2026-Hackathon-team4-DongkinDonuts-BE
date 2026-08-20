import json
import logging
import random
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from common.models import StateOption
from context.models import NextActivityPlan, UserContextSnapshot, UserContextSnapshotState
from context.services import get_current_valid_next_activity_plan
from context.utils import today_for_user
from digital_state.models import PcUsagePattern
from digital_state.services import DAY_LABELS, DAY_ORDER, analyze_pc_usage_patterns
from routines.models import ActivityType, RoutineInstance, RoutineInstanceStatus, StageType
from sessions_app.models import Session, SessionFeedback

from .models import AIInsight, AIPlanRun, InsightType, PlanStatus, RecoveryPlan, SlotNotificationBasis
from .openai_client import OpenAIClientError, OpenAIConfigurationError, create_structured_response
from .services import (
    build_plan_generation_snapshot,
    build_policy_recommended_slots,
    cancel_next_snapshot_slot_for_reentry,
    create_or_replace_today_plan,
    create_recovery_slot,
    get_today_pc_usage_patterns,
    is_within_pc_usage_pattern,
    recommend_next_reset_time,
    recovery_time_policy_for_context,
    validate_context_inputs,
)

logger = logging.getLogger(__name__)


STAGE_SEQUENCE = {
    StageType.BRAIN_WAKE: 1,
    StageType.BRAIN_SHIFT: 2,
    StageType.BRAIN_RESET: 3,
}

COMMON_STAGE_TYPES = [StageType.BRAIN_WAKE, StageType.BRAIN_RESET]
POLICY_GENERATOR_NAME = "server_policy"
# RECOVERY_PLAN_SCHEMA의 slots.maxItems와 맞춘 안전 상한선 — "몇 개를 만들지"는
# AI가 자유롭게 정하되, 폭주 응답으로부터 서버/사용자를 보호하는 최후의 방어선.
MAX_AI_SLOTS = 12
WAKE_ACTIVITY_CODE = "WAKE_HAND_ROUTINE"
RESET_ACTIVITY_CODE = "RESET_BREATH"
RANDOM_SHIFT_SENTINEL = "__RANDOM_PREPARED_SHIFT__"
STATE_SHIFT_ACTIVITY_CODES = {
    "EYE_TIRED": ["SHIFT_EYE_RELAX", "SHIFT_EYE_TRACKING"],
    "eye_tired": ["SHIFT_EYE_RELAX", "SHIFT_EYE_TRACKING"],
    "BODY_STIFF": ["SHIFT_BODY_STRETCH", "SHIFT_SHOULDER_PMR"],
    "neck_shoulder_stiff": ["SHIFT_BODY_STRETCH", "SHIFT_SHOULDER_PMR"],
    "LOW_FOCUS": ["SHIFT_FOCUS_SWITCH"],
    "cant_focus": ["SHIFT_FOCUS_SWITCH"],
    "SLEEPY": ["SHIFT_DROWSY_WAKE"],
    "drowsy_foggy": ["SHIFT_DROWSY_WAKE"],
    "OKAY": RANDOM_SHIFT_SENTINEL,
    "still_okay": RANDOM_SHIFT_SENTINEL,
}
RANDOM_PREPARED_SHIFT_ACTIVITY_CODES = [
    "SHIFT_EYE_RELAX",
    "SHIFT_EYE_TRACKING",
    "SHIFT_BODY_STRETCH",
    "SHIFT_SHOULDER_PMR",
    "SHIFT_FOCUS_SWITCH",
    "SHIFT_DROWSY_WAKE",
]

RECOVERY_PLAN_SCHEMA = {
    "name": "brainfit_recovery_plan",
    "description": "Brainfit 하루 회복 세션 추천 계획",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "slots", "insights"],
        "properties": {
            "summary": {"type": "string"},
            "slots": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "recommended_at",
                        "interval_minutes",
                        "reason",
                        "shift_recommendations",
                    ],
                    "properties": {
                        "recommended_at": {
                            "type": "string",
                            "description": "YYYY-MM-DDTHH:MM:SS 형식의 Asia/Seoul 기준 추천 시각",
                        },
                        "interval_minutes": {
                            "type": ["integer", "null"],
                            "minimum": 5,
                            "maximum": 240,
                        },
                        "reason": {"type": "string"},
                        "shift_recommendations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 2,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "activity_code",
                                    "difficulty_level",
                                    "planned_duration_sec",
                                    "reason",
                                ],
                                "properties": {
                                    "activity_code": {
                                        "type": "string",
                                        "description": "shift_activity_catalog에 있는 Brain Shift activity code",
                                    },
                                    "difficulty_level": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 5,
                                    },
                                    "planned_duration_sec": {
                                        "type": "integer",
                                        "minimum": 10,
                                        "maximum": 600,
                                    },
                                    "reason": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "insights": {
                "type": "array",
                "minItems": 0,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["insight_type", "body", "data_sources"],
                    "properties": {
                        "insight_type": {
                            "type": "string",
                            "enum": [
                                InsightType.TODAY_ANALYSIS,
                                InsightType.RECOMMENDATION_REASON,
                                InsightType.DATA_INSIGHT,
                            ],
                        },
                        "body": {"type": "string"},
                        "data_sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 8,
                        },
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT = """
너는 Brainfit의 회복 세션 플래너다.
사용자의 현재 상태, 이후 활동 계획, 이전 수행 기록과 피드백, 이전 상태 빈도, PC 사용 패턴을
함께 보고 오늘의 회복 세션 추천 시간과 Brain Shift 맞춤 활동을 만든다.

규칙:
- 하나의 회복 세션은 서버에서 항상 Brain Wake → Brain Shift 1~2개 → Brain Reset 순서로 생성된다.
- Brain Wake와 Brain Reset은 모든 상태에서 공통으로 제공되며 서버가 선택한다.
- 개인화는 Brain Shift에만 적용한다. shift_recommendation은 반드시 shift_activity_catalog에 있는
  activity_code 중 하나를 골라야 한다.
- [필수 제약 조건] 모든 recommended_at 추천 시각은 사용자가 선택한 pc_usage_patterns (PC 사용 시간대 블록) 범위 내부여야 한다. PC를 사용하지 않는 시간대에는 절대 알림을 생성하지 않는다.
- [AI 자율 판단] PC 사용 밀집 구간과 과거 세션/상태 빈도를 고려하여 알림의 수량(slots 개수)과 가장 피로도가 누적될 것으로 예상되는 최적의 발송 시각을 자유롭게 판단하여 추천한다.
- recommended_at은 current_time 이후, plan_date 당일, YYYY-MM-DDTHH:MM:SS 형식으로 작성한다.
- difficulty_level은 사용자의 상태와 피드백을 반영하되 활동의 난이도 범위 안에서 정한다.
""".strip()


def _isoformat(value):
    return value.isoformat() if value else None


def _latest_today_snapshot(user):
    return (
        UserContextSnapshot.objects.filter(user=user, service_date=today_for_user(user))
        .order_by("-created_at")
        .first()
    )


def _latest_today_activity_plan(user, context_snapshot=None):
    queryset = NextActivityPlan.objects.filter(user=user, service_date=today_for_user(user))
    if context_snapshot is not None:
        contextual_plan = queryset.filter(context_snapshot=context_snapshot).order_by("-created_at").first()
        if contextual_plan is not None:
            return contextual_plan
    return queryset.order_by("-created_at").first()


def resolve_generation_inputs(user, context_snapshot=None, next_activity_plan=None):
    if context_snapshot is None:
        context_snapshot = _latest_today_snapshot(user)
    if context_snapshot is None:
        raise ValidationError("오늘의 상태 스냅샷이 필요합니다.")

    if next_activity_plan is None:
        next_activity_plan = _latest_today_activity_plan(user, context_snapshot=context_snapshot)
    if next_activity_plan is None:
        raise ValidationError("오늘의 이후 활동 계획이 필요합니다.")
    if next_activity_plan.user_id != user.id:
        raise ValidationError("본인의 이후 활동 계획만 사용할 수 있습니다.")
    if context_snapshot.user_id != user.id:
        raise ValidationError("본인의 상태 스냅샷만 사용할 수 있습니다.")

    return context_snapshot, next_activity_plan


def _serialize_context_snapshot(context_snapshot):
    return {
        "id": str(context_snapshot.id),
        "service_date": str(context_snapshot.service_date),
        "note": context_snapshot.note,
        "state_options": [
            {
                "code": link.state_id,
                "label": link.state.label,
                "priority": link.priority,
                "default_difficulty": link.state.default_difficulty,
                "routine_direction": link.state.routine_direction,
            }
            for link in context_snapshot.state_links.select_related("state").order_by("priority")
        ],
        "created_at": _isoformat(context_snapshot.created_at),
    }


def _serialize_next_activity_plan(next_activity_plan):
    attached_snapshot = next_activity_plan.context_snapshot
    return {
        "id": str(next_activity_plan.id),
        "service_date": str(next_activity_plan.service_date),
        "expected_activity_minutes": next_activity_plan.expected_activity_minutes,
        "activity_tags": [
            {
                "code": link.activity_tag_id,
                "name": link.activity_tag.name,
                "category": link.activity_tag.category,
            }
            for link in next_activity_plan.activity_tag_links.select_related("activity_tag")
        ],
        "attached_context_snapshot": (
            _serialize_context_snapshot(attached_snapshot) if attached_snapshot else None
        ),
        "created_at": _isoformat(next_activity_plan.created_at),
    }


def _serialize_pc_usage_patterns(user):
    patterns = sorted(
        PcUsagePattern.objects.filter(user=user, is_used=True),
        key=lambda item: (DAY_ORDER.index(item.day_of_week), item.hour),
    )
    return [
        {
            "day_of_week": pattern.day_of_week,
            "day_label": DAY_LABELS[pattern.day_of_week],
            "hour": pattern.hour,
            "start_time": f"{pattern.hour:02d}:00",
            "end_time": f"{pattern.hour + 1:02d}:00",
        }
        for pattern in patterns
    ]


def _serialize_state_frequencies(user):
    counts = list(
        UserContextSnapshotState.objects.filter(context_snapshot__user=user)
        .values("state_id")
        .annotate(count=Count("state_id"))
        .order_by("-count", "state_id")[:10]
    )
    states = StateOption.objects.in_bulk([item["state_id"] for item in counts])
    return [
        {
            "code": item["state_id"],
            "label": states[item["state_id"]].label if item["state_id"] in states else item["state_id"],
            "count": item["count"],
        }
        for item in counts
    ]


def _serialize_previous_sessions(user):
    sessions = (
        Session.objects.filter(user=user)
        .select_related("activity", "recovery_slot")
        .order_by("-started_at")[:10]
    )
    return [
        {
            "id": str(session.id),
            "recovery_slot": str(session.recovery_slot_id),
            "activity_code": session.activity_id,
            "activity_name": session.activity.name,
            "stage_type": session.activity.stage_type,
            "started_at": _isoformat(session.started_at),
            "ended_at": _isoformat(session.ended_at),
            "duration_sec": session.duration_sec,
            "accuracy": session.accuracy,
            "status": session.status,
        }
        for session in sessions
    ]


def _serialize_previous_feedback(user):
    feedbacks = (
        SessionFeedback.objects.filter(user=user)
        .select_related("recovery_slot")
        .order_by("-created_at")[:10]
    )
    return [
        {
            "id": str(feedback.id),
            "recovery_slot": str(feedback.recovery_slot_id),
            "recovery_feeling": feedback.recovery_feeling,
            "difficulty_feedback": feedback.difficulty_feedback,
            "skipped": feedback.skipped,
            "created_at": _isoformat(feedback.created_at),
        }
        for feedback in feedbacks
    ]


def _serialize_activity(activity):
    return {
        "code": activity.code,
        "stage_type": activity.stage_type,
        "target_state": activity.target_state_id,
        "name": activity.name,
        "purpose": activity.purpose,
        "min_difficulty": activity.min_difficulty,
        "max_difficulty": activity.max_difficulty,
        "default_duration_sec": activity.default_duration_sec,
    }


def _serialize_activity_catalog():
    return [
        _serialize_activity(activity)
        for activity in ActivityType.objects.filter(is_active=True).order_by("stage_type", "code")
    ]


def _context_state_codes(context_snapshot):
    return list(context_snapshot.state_links.order_by("priority").values_list("state_id", flat=True))


def _ordered_activities_by_codes(queryset, codes):
    activities = list(queryset.filter(code__in=codes).order_by("code"))
    by_code = {activity.code: activity for activity in activities}
    return [
        by_code[code]
        for code in codes
        if code in by_code
    ]


def _shift_candidates_for_state_code(state_code):
    queryset = ActivityType.objects.filter(is_active=True, stage_type=StageType.BRAIN_SHIFT)
    mapped_codes = STATE_SHIFT_ACTIVITY_CODES.get(state_code)

    if mapped_codes == RANDOM_SHIFT_SENTINEL:
        return _ordered_activities_by_codes(queryset, RANDOM_PREPARED_SHIFT_ACTIVITY_CODES)

    if mapped_codes:
        return _ordered_activities_by_codes(queryset, mapped_codes)

    return list(queryset.filter(target_state_id=state_code).order_by("code"))


def _combined_state_codes(context_snapshot, next_activity_plan=None):
    state_codes = []

    for code in _context_state_codes(context_snapshot):
        if code not in state_codes:
            state_codes.append(code)

    attached_snapshot = getattr(next_activity_plan, "context_snapshot", None)
    if attached_snapshot is not None and attached_snapshot.id != context_snapshot.id:
        for code in _context_state_codes(attached_snapshot):
            if code not in state_codes:
                state_codes.append(code)

    return state_codes


def _shift_activities_for_context(context_snapshot, next_activity_plan=None):
    state_codes = _combined_state_codes(context_snapshot, next_activity_plan)
    queryset = ActivityType.objects.filter(is_active=True, stage_type=StageType.BRAIN_SHIFT)
    activities = []
    used_codes = set()

    for state_code in state_codes:
        for activity in _shift_candidates_for_state_code(state_code):
            if activity.code in used_codes:
                continue
            activities.append(activity)
            used_codes.add(activity.code)

    if activities:
        return activities

    if state_codes:
        fallback_activities = list(queryset.filter(target_state_id__in=state_codes))
        if fallback_activities:
            priority = {code: index for index, code in enumerate(state_codes)}
            return sorted(fallback_activities, key=lambda activity: (priority.get(activity.target_state_id, 999), activity.code))

    return list(queryset.filter(code__in=RANDOM_PREPARED_SHIFT_ACTIVITY_CODES).order_by("code"))


def _primary_shift_recommendations_for_state_codes(state_codes, context_snapshot, next_activity_plan):
    recommendations = []
    used_codes = set()
    state_defaults = _state_default_difficulty_map(
        context_snapshot,
        next_activity_plan,
    )

    for state_code in state_codes:
        candidates = [
            activity
            for activity in _shift_candidates_for_state_code(state_code)
            if activity.code not in used_codes
        ]
        if not candidates:
            continue
        mapped_codes = STATE_SHIFT_ACTIVITY_CODES.get(state_code)
        activity = (
            random.choice(candidates)
            if mapped_codes == RANDOM_SHIFT_SENTINEL
            else candidates[0]
        )
        used_codes.add(activity.code)
        recommendations.append(
            {
                "activity_code": activity.code,
                "difficulty_level": _difficulty_for_activity(
                    activity,
                    default_difficulty=state_defaults.get(
                        activity.target_state_id,
                        activity.min_difficulty,
                    ),
                ),
                "planned_duration_sec": activity.default_duration_sec,
                "reason": (
                    activity.purpose
                    or "현재 상태에 맞는 Brain Shift 활동입니다."
                ),
            }
        )

        if len(recommendations) >= 2:
            break

    return recommendations or [{}]


def _reentry_state_codes(context_snapshot, next_activity_plan):
    state_codes = []
    attached_snapshot = getattr(next_activity_plan, "context_snapshot", None)

    for snapshot in [context_snapshot, attached_snapshot]:
        if snapshot is None:
            continue
        for code in _context_state_codes(snapshot):
            if code not in state_codes:
                state_codes.append(code)
            if len(state_codes) >= 2:
                return state_codes

    return state_codes


def _reentry_shift_recommendations(context_snapshot, next_activity_plan):
    return _primary_shift_recommendations_for_state_codes(
        _reentry_state_codes(context_snapshot, next_activity_plan),
        context_snapshot,
        next_activity_plan,
    )


def _serialize_shift_activity_catalog(context_snapshot, next_activity_plan):
    return [
        _serialize_activity(activity)
        for activity in _shift_activities_for_context(context_snapshot, next_activity_plan)
    ]


def build_ai_input_snapshot(user, context_snapshot, next_activity_plan):
    has_today_pattern = get_today_pc_usage_patterns(user).exists()
    mode = "SNAPSHOT_AND_FREQUENCY" if has_today_pattern else "SNAPSHOT_ACTIVITY_WINDOW"
    return {
        "plan_date": str(today_for_user(user)),
        "current_time": _isoformat(timezone.now()),
        "timezone": getattr(user, "timezone", "Asia/Seoul"),
        "generation_mode": mode,
        "context_snapshot": _serialize_context_snapshot(context_snapshot),
        "next_activity_plan": _serialize_next_activity_plan(next_activity_plan),
        "previous_sessions": _serialize_previous_sessions(user),
        "previous_feedback": _serialize_previous_feedback(user),
        "previous_state_frequencies": _serialize_state_frequencies(user),
        "pc_usage_patterns": _serialize_pc_usage_patterns(user),
        "pc_usage_analysis": analyze_pc_usage_patterns(user),
        "time_policy": recovery_time_policy_for_context(context_snapshot),
        "activity_catalog": _serialize_activity_catalog(),
        "shift_activity_catalog": _serialize_shift_activity_catalog(context_snapshot, next_activity_plan),
        "routine_generation_policy": {
            "fixed_stage_order": [StageType.BRAIN_WAKE, StageType.BRAIN_SHIFT, StageType.BRAIN_RESET],
            "personalized_stage": StageType.BRAIN_SHIFT,
            "server_selected_common_stages": COMMON_STAGE_TYPES,
        },
    }


def build_input_messages(input_snapshot):
    return [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": json.dumps(input_snapshot, ensure_ascii=False),
                }
            ],
        },
    ]


def _safe_int(value, default=None, minimum=None, maximum=None):
    if value is None:
        return default
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        integer = max(minimum, integer)
    if maximum is not None:
        integer = min(maximum, integer)
    return integer


def _time_policy_reason(policy, raw_reason=None):
    state_intervals = policy.get("state_intervals") or []
    if state_intervals:
        selected_codes = set(policy.get("selected_state_codes") or [])
        selected_labels = [
            item["state_label"] for item in state_intervals if item["state_code"] in selected_codes
        ]
        if selected_labels:
            base_reason = (
                f"{', '.join(selected_labels)} 상태 기준 {policy['interval_minutes']}분 "
                "회복 타이머 정책을 적용했습니다."
            )
        else:
            base_reason = f"{policy['interval_minutes']}분 회복 타이머 정책을 적용했습니다."
    else:
        base_reason = f"기본 {policy['interval_minutes']}분 회복 타이머 정책을 적용했습니다."

    if raw_reason:
        return f"{base_reason} {raw_reason}"
    return base_reason


def _coerce_shift_recommendations(raw_slot):
    recommendations = raw_slot.get("shift_recommendations")
    if isinstance(recommendations, list):
        return [item for item in recommendations if isinstance(item, dict)][:2]

    legacy_recommendation = raw_slot.get("shift_recommendation")
    if isinstance(legacy_recommendation, dict):
        return [legacy_recommendation]

    return [{}]


def _policy_summary(input_snapshot):
    state_labels = [
        item["label"]
        for item in input_snapshot["context_snapshot"]["state_options"]
    ]
    activity_names = [
        item["name"]
        for item in input_snapshot["next_activity_plan"]["activity_tags"]
    ]

    state_text = ", ".join(state_labels) if state_labels else "현재 상태"
    activity_text = ", ".join(activity_names) if activity_names else "다음 활동"
    interval = input_snapshot["time_policy"]["interval_minutes"]

    return (
        f"{state_text}와 {activity_text} 정보를 바탕으로 "
        f"{interval}분 간격의 회복 계획을 생성했습니다."
    )


def _policy_insights(input_snapshot, *, include_frequency_slots=True):
    insights = []

    # include_frequency_slots=False(My Digital State와 무관한 흐름)일 땐 실제로
    # PC 사용 패턴/과거 세션을 슬롯 배치에 안 썼으니, 썼다고 오해하게 만드는
    # 인사이트 문구도 같이 빼야 한다.
    if include_frequency_slots and input_snapshot["pc_usage_patterns"]:
        insights.append(
            {
                "insight_type": InsightType.DATA_INSIGHT,
                "body": "오늘 PC 사용 패턴이 있는 시간대 안에 회복 알림을 배치했습니다.",
                "data_sources": [
                    "pc_usage_patterns",
                    "pc_usage_analysis",
                    "time_policy",
                ],
            }
        )

    if include_frequency_slots and input_snapshot["previous_sessions"]:
        insights.append(
            {
                "insight_type": InsightType.DATA_INSIGHT,
                "body": "최근 회복 세션을 자주 수행한 시간대도 빈도 기반 알림 후보로 반영했습니다.",
                "data_sources": [
                    "previous_sessions",
                    "time_policy",
                ],
            }
        )

    return insights


def _policy_shift_recommendations(context_snapshot, next_activity_plan):
    recommendations = []
    state_defaults = _state_default_difficulty_map(
        context_snapshot,
        next_activity_plan,
    )
    activities = _shift_activities_for_context(
        context_snapshot,
        next_activity_plan,
    )
    if any(
        STATE_SHIFT_ACTIVITY_CODES.get(code) == RANDOM_SHIFT_SENTINEL
        for code in _combined_state_codes(context_snapshot, next_activity_plan)
    ) and activities:
        activities = [random.choice(activities)]

    for activity in activities[:2]:
        recommendations.append(
            {
                "activity_code": activity.code,
                "difficulty_level": _difficulty_for_activity(
                    activity,
                    default_difficulty=state_defaults.get(
                        activity.target_state_id,
                        activity.min_difficulty,
                    ),
                ),
                "planned_duration_sec": activity.default_duration_sec,
                "reason": (
                    activity.purpose
                    or "현재 상태에 맞는 Brain Shift 활동입니다."
                ),
            }
        )

    return recommendations or [{}]


def build_policy_output(user, context_snapshot, next_activity_plan, input_snapshot, *, include_frequency_slots=True):
    policy = recovery_time_policy_for_context(context_snapshot)
    recommended_slots = build_policy_recommended_slots(
        user=user,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        base_time=timezone.now().replace(microsecond=0),
        include_frequency_slots=include_frequency_slots,
        prioritize_pc_usage_windows=include_frequency_slots,
    )

    return {
        "summary": _policy_summary(input_snapshot),
        "slots": [
            {
                "recommended_at": _isoformat(policy_slot["recommended_at"]),
                "interval_minutes": policy["interval_minutes"],
                "reason": policy_slot["reason"],
                "shift_recommendations": _policy_shift_recommendations(
                    context_snapshot,
                    next_activity_plan,
                ),
            }
            for policy_slot in recommended_slots
        ],
        "insights": _policy_insights(input_snapshot, include_frequency_slots=include_frequency_slots),
    }


def _parse_ai_recommended_at(raw_value):
    if not isinstance(raw_value, str):
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def _normalize_llm_authored_slots(raw_slots, *, user, now, today, policy):
    """
    LLM이 스스로 정한 슬롯 "개수"와 "시각"을 그대로 신뢰해서 쓴다(정책 엔진의
    build_policy_recommended_slots를 거치지 않음) — 대신 서버가 최소한의 안전장치로
    (1) 현재 시각 이후인지, (2) plan_date 당일인지, (3) 사용자의 PC 사용 패턴
    블록 안에 들어가는지를 검증해서, 통과 못 하는 슬롯은 조용히 버린다.
    프롬프트의 [필수 제약 조건]을 코드로도 강제하는 것.
    """
    seen_times = set()
    candidates = []
    for raw_slot in raw_slots[:MAX_AI_SLOTS]:
        if not isinstance(raw_slot, dict):
            continue

        recommended_at = _parse_ai_recommended_at(raw_slot.get("recommended_at"))
        if recommended_at is None or recommended_at <= now:
            continue
        if recommended_at.date() != today:
            continue
        if not is_within_pc_usage_pattern(user, recommended_at):
            continue
        if recommended_at in seen_times:
            continue
        seen_times.add(recommended_at)

        candidates.append(
            {
                "recommended_at": recommended_at,
                "interval_minutes": _safe_int(
                    raw_slot.get("interval_minutes"),
                    default=policy["interval_minutes"],
                    minimum=5,
                    maximum=240,
                ),
                "notification_basis": SlotNotificationBasis.FREQUENCY,
                "reason": _time_policy_reason(policy, raw_slot.get("reason")),
                "data_sources": ["pc_usage_patterns", "previous_sessions", "ai_decision"],
                "shift_recommendations": _coerce_shift_recommendations(raw_slot),
            }
        )

    candidates.sort(key=lambda item: item["recommended_at"])
    return candidates


def normalize_ai_slots(
    ai_output,
    user,
    context_snapshot,
    next_activity_plan,
    *,
    is_ai_generated=False,
    use_ai_decision=False,
):
    now = timezone.now().replace(microsecond=0)
    policy = recovery_time_policy_for_context(context_snapshot)
    raw_slots = ai_output.get("slots", [])

    if is_ai_generated:
        llm_slots = _normalize_llm_authored_slots(
            raw_slots,
            user=user,
            now=now,
            today=today_for_user(user),
            policy=policy,
        )
        if llm_slots:
            return llm_slots
        # LLM이 준 슬롯 중 검증을 통과한 게 하나도 없으면(전부 PC 패턴 밖이거나
        # 과거 시각이거나 등) 정책 엔진으로 안전하게 폴백한다.
        logger.warning("AI가 반환한 슬롯이 전부 검증에 실패해 정책 엔진으로 폴백합니다.")

    # use_ai_decision=False(My Digital State 흐름이 아님, 예: 상태 선택 모달)면
    # PC 사용 패턴/과거 세션 빈도는 아예 참고하지 않는다 — 이 흐름은
    # digital_state와 완전히 무관해야 한다.
    policy_slots = build_policy_recommended_slots(
        user=user,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        base_time=now,
        include_frequency_slots=use_ai_decision,
        prioritize_pc_usage_windows=use_ai_decision,
    )
    normalized = []

    for index, policy_slot in enumerate(policy_slots):
        raw_slot = raw_slots[index] if index < len(raw_slots) else {}
        normalized.append(
            {
                "recommended_at": policy_slot["recommended_at"],
                "interval_minutes": policy["interval_minutes"],
                "notification_basis": policy_slot["notification_basis"],
                "reason": _time_policy_reason(policy, raw_slot.get("reason") or policy_slot["reason"]),
                "data_sources": policy_slot["data_sources"],
                "shift_recommendations": _coerce_shift_recommendations(raw_slot),
            }
        )

    if not normalized:
        normalized.append(
            {
                "recommended_at": recommend_next_reset_time(
                    next_activity_plan,
                    base_time=now,
                    context_snapshot=context_snapshot,
                ).replace(microsecond=0),
                "interval_minutes": policy["interval_minutes"],
                "notification_basis": "SNAPSHOT",
                "reason": _time_policy_reason(policy, "정책 결과에 유효한 추천 시간이 없어 보정했습니다."),
                "data_sources": ["context_snapshot", "next_activity_plan", "time_policy"],
                "shift_recommendations": [{}],
            }
        )

    normalized.sort(key=lambda item: item["recommended_at"])
    return normalized


def _create_plan_insights(plan, ai_output):
    summary = ai_output.get("summary")
    if summary:
        AIInsight.objects.create(
            recovery_plan=plan,
            insight_type=InsightType.RECOMMENDATION_REASON,
            body=summary,
            data_sources_json=[
                "context_snapshot",
                "next_activity_plan",
                "time_policy",
            ],
        )

    for insight in ai_output.get("insights", []):
        insight_type = insight.get("insight_type")
        if insight_type not in InsightType.values:
            insight_type = InsightType.DATA_INSIGHT
        body = insight.get("body")
        if not body:
            continue
        AIInsight.objects.create(
            recovery_plan=plan,
            insight_type=insight_type,
            body=body,
            data_sources_json=insight.get("data_sources", []),
        )


def _create_slot_insight(slot, reason, data_sources=None):
    if not reason:
        return
    AIInsight.objects.create(
        recovery_plan=slot.recovery_plan,
        recovery_slot=slot,
        insight_type=InsightType.RECOMMENDATION_REASON,
        body=reason,
        data_sources_json=data_sources or ["time_policy"],
    )


def _active_stage_activities(stage_type):
    activities = list(ActivityType.objects.filter(is_active=True, stage_type=stage_type).order_by("code"))
    if not activities:
        raise ValidationError(f"{stage_type} 활동 카탈로그가 필요합니다.")
    return activities


def _activity_by_code(activity_code, stage_type):
    activity = ActivityType.objects.filter(
        code=activity_code,
        stage_type=stage_type,
        is_active=True,
    ).first()
    if activity is None:
        raise ValidationError(f"{activity_code} 활동 카탈로그가 필요합니다.")
    return activity


def _validate_recovery_activity_catalog(context_snapshot, next_activity_plan):
    _activity_by_code(WAKE_ACTIVITY_CODE, StageType.BRAIN_WAKE)
    _activity_by_code(RESET_ACTIVITY_CODE, StageType.BRAIN_RESET)
    if not _shift_activities_for_context(context_snapshot, next_activity_plan):
        raise ValidationError("현재 상태에 맞는 Brain Shift 활동 카탈로그가 필요합니다.")


def _state_default_difficulty_map(context_snapshot, next_activity_plan=None):
    mapping = {}
    snapshots = [context_snapshot]
    attached_snapshot = getattr(next_activity_plan, "context_snapshot", None)
    if attached_snapshot is not None and attached_snapshot.id != context_snapshot.id:
        snapshots.append(attached_snapshot)

    for snapshot in snapshots:
        for link in snapshot.state_links.select_related("state").order_by("priority"):
            mapping.setdefault(link.state_id, link.state.default_difficulty)
    return mapping


def _difficulty_for_activity(activity, requested_difficulty=None, default_difficulty=None):
    return _safe_int(
        requested_difficulty,
        default=default_difficulty or activity.min_difficulty,
        minimum=activity.min_difficulty,
        maximum=activity.max_difficulty,
    )


def _duration_for_activity(activity, requested_duration=None):
    return _safe_int(
        requested_duration,
        default=activity.default_duration_sec,
        minimum=10,
        maximum=600,
    )


def _select_shift_activity(context_snapshot, next_activity_plan, recommendation, used_codes):
    activities = _shift_activities_for_context(context_snapshot, next_activity_plan)
    if not activities:
        raise ValidationError("현재 상태에 맞는 Brain Shift 활동 카탈로그가 필요합니다.")

    activity_by_code = {activity.code: activity for activity in activities}
    requested_activity = activity_by_code.get(recommendation.get("activity_code"))
    if requested_activity is not None and requested_activity.code not in used_codes:
        activity = requested_activity
    else:
        candidates = [item for item in activities if item.code not in used_codes]
        if not candidates and used_codes:
            return None
        activity = candidates[0] if candidates else activities[0]
    used_codes.add(activity.code)

    state_defaults = _state_default_difficulty_map(context_snapshot, next_activity_plan)
    default_difficulty = state_defaults.get(activity.target_state_id, activity.min_difficulty)
    if requested_activity is not None and requested_activity.code == activity.code:
        reason = recommendation.get("reason") or "현재 상태에 맞는 Brain Shift 활동입니다."
        data_sources = ["context_snapshot", "activity_catalog"]
    else:
        reason = "현재 상태에 맞는 Brain Shift 활동으로 보정했습니다."
        data_sources = ["context_snapshot", "activity_catalog"]

    return {
        "activity": activity,
        "sequence_no": STAGE_SEQUENCE[StageType.BRAIN_SHIFT],
        "difficulty_level": _difficulty_for_activity(
            activity,
            requested_difficulty=recommendation.get("difficulty_level"),
            default_difficulty=default_difficulty,
        ),
        "planned_duration_sec": _duration_for_activity(activity, recommendation.get("planned_duration_sec")),
        "reason": reason,
        "data_sources": data_sources,
    }


def _common_routine_spec(*, activity_code, stage_type, sequence_no):
    activity = _activity_by_code(activity_code, stage_type)
    return {
        "activity": activity,
        "sequence_no": sequence_no,
        "difficulty_level": _difficulty_for_activity(activity),
        "planned_duration_sec": activity.default_duration_sec,
        "reason": "",
        "data_sources": [],
    }


def _unique_list(values):
    seen = set()
    unique = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _create_routine_instances(slot, context_snapshot, next_activity_plan, shift_recommendations):
    used_shift_codes = set()
    shift_specs = []

    # 1) 명시적인 shift_recommendations가 2개 이상 들어온 경우 우선 채택
    if shift_recommendations and len(shift_recommendations) >= 2:
        for recommendation in shift_recommendations[:2]:
            spec = _select_shift_activity(
                context_snapshot,
                next_activity_plan,
                recommendation,
                used_shift_codes,
            )
            if spec is not None:
                shift_specs.append(spec)

    # 2) 명시적 추천이 2개가 안 되는 경우: 최초 스냅샷 상태 + 현재 세션 진입 스냅샷 상태 조합 (다르면 2개, 같으면 1개)
    if len(shift_specs) < 2:
        initial_state_codes = []
        plan_snapshot = slot.recovery_plan.generation_snapshot_json or {}
        init_context = plan_snapshot.get("context_snapshot") or {}
        if isinstance(init_context, dict) and init_context.get("state_options"):
            initial_state_codes = init_context["state_options"]

        current_state_codes = []
        if context_snapshot is not None:
            current_state_codes = [link.state_id for link in context_snapshot.state_links.all()]

        all_states = _unique_list(current_state_codes + initial_state_codes)

        for state_code in all_states[:2]:
            if len(shift_specs) >= 2:
                break
            activities = ActivityType.objects.filter(
                is_active=True,
                stage_type=StageType.BRAIN_SHIFT,
                target_state_id=state_code,
            ).order_by("min_difficulty", "code")

            candidates = [act for act in activities if act.code not in used_shift_codes]
            if not candidates:
                candidates = [act for act in ActivityType.objects.filter(is_active=True, stage_type=StageType.BRAIN_SHIFT) if act.code not in used_shift_codes]

            if candidates:
                act = candidates[0]
                used_shift_codes.add(act.code)
                shift_specs.append(
                    {
                        "activity": act,
                        "sequence_no": len(shift_specs) + 2,
                        "difficulty_level": act.min_difficulty,
                        "planned_duration_sec": act.default_duration_sec,
                        "reason": f"{act.target_state.label if hasattr(act, 'target_state') and act.target_state else '상태'} 맞춤 루틴입니다.",
                        "data_sources": ["context_snapshot", "activity_catalog"],
                    }
                )

    if not shift_specs:
        fallback_spec = _select_shift_activity(
            context_snapshot,
            next_activity_plan,
            {},
            used_shift_codes,
        )
        if fallback_spec is not None:
            shift_specs.append(fallback_spec)

    for index, spec in enumerate(shift_specs):
        spec["sequence_no"] = index + 2



    routine_specs = [
        _common_routine_spec(
            activity_code=WAKE_ACTIVITY_CODE,
            stage_type=StageType.BRAIN_WAKE,
            sequence_no=1,
        ),
        *shift_specs,
        _common_routine_spec(
            activity_code=RESET_ACTIVITY_CODE,
            stage_type=StageType.BRAIN_RESET,
            sequence_no=len(shift_specs) + 2,
        ),
    ]

    for index, spec in enumerate(sorted(routine_specs, key=lambda item: item["sequence_no"])):
        routine = RoutineInstance.objects.create(
            recovery_slot=slot,
            activity=spec["activity"],
            sequence_no=spec["sequence_no"],
            difficulty_level=spec["difficulty_level"],
            planned_duration_sec=spec["planned_duration_sec"],
            status=RoutineInstanceStatus.AVAILABLE if index == 0 else RoutineInstanceStatus.LOCKED,
            locked_until_previous_done=index != 0,
        )
        if spec["reason"] and spec["activity"].stage_type == StageType.BRAIN_SHIFT:
            AIInsight.objects.create(
                recovery_plan=slot.recovery_plan,
                recovery_slot=slot,
                routine_instance=routine,
                insight_type=InsightType.ROUTINE_REASON,
                body=spec["reason"],
                data_sources_json=spec["data_sources"],
            )


@transaction.atomic
def _persist_ai_plan(
    *,
    user,
    context_snapshot,
    next_activity_plan,
    input_snapshot,
    ai_output,
    raw_response,
    normalized_slots,
    notification_enabled,
    generator_name=POLICY_GENERATOR_NAME,
    use_ai_decision=False,
):
    ai_run = AIPlanRun.objects.create(
        user=user,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        model_name=generator_name,
        input_snapshot_json=input_snapshot,
        output_snapshot_json={"parsed": ai_output, "raw_response": raw_response},
    )
    plan = create_or_replace_today_plan(
        user=user,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        recommended_slots=[
            {
                "recommended_at": slot["recommended_at"],
                "notification_basis": slot["notification_basis"],
            }
            for slot in normalized_slots
        ],
        notification_enabled=notification_enabled,
        ai_plan_run=ai_run,
        use_ai_decision=use_ai_decision,
    )
    _create_plan_insights(plan, ai_output)

    # plan.slots는 이제(플랜을 재사용하면서) 다른 흐름의 기존 열린 슬롯까지
    # 섞여 있을 수 있다 — 방금 이 호출에서 만든 슬롯만 골라야 한다. 매 호출마다
    # 새로 만드는 ai_run으로 필터링하면 정확히 그것만 남는다.
    slots = list(plan.slots.filter(ai_plan_run=ai_run).order_by("sequence_no"))
    for slot, ai_slot in zip(slots, normalized_slots):
        slot.interval_minutes = ai_slot["interval_minutes"]
        slot.save(update_fields=["interval_minutes", "updated_at"])
        _create_slot_insight(slot, ai_slot["reason"], ai_slot.get("data_sources"))
        _create_routine_instances(
            slot,
            context_snapshot,
            next_activity_plan,
            ai_slot["shift_recommendations"],
        )

    return plan


def _is_next_activity_plan_active(next_activity_plan, now):
    if next_activity_plan.expected_activity_minutes is None:
        return False
    valid_until = next_activity_plan.created_at + timedelta(
        minutes=next_activity_plan.expected_activity_minutes,
    )
    return valid_until > now


@transaction.atomic
def create_reentry_recovery_slot(
    *,
    user,
    context_snapshot,
    next_activity_plan=None,
):
    """
    활성 활동 구간 중 서비스에 재진입해 바로 휴식 루틴을 시작하는 흐름.

    기존 활동에 묶인 상태와 방금 입력한 현재 상태를 Brain Shift에 반영하고,
    새 알림은 만들지 않는다. 이미 잡혀 있던 상태 기반 알림은 가장 가까운
    미래 슬롯 1개만 취소한다.
    """

    if next_activity_plan is None:
        next_activity_plan = get_current_valid_next_activity_plan(user)

    if next_activity_plan is None:
        raise ValidationError("현재 활성화된 이후 활동 계획이 없습니다.")

    validate_context_inputs(user, context_snapshot, next_activity_plan)

    now = timezone.now().replace(microsecond=0)
    if not _is_next_activity_plan_active(next_activity_plan, now):
        raise ValidationError("현재 활성화된 이후 활동 계획이 없습니다.")

    _validate_recovery_activity_catalog(context_snapshot, next_activity_plan)

    plan = (
        RecoveryPlan.objects.select_for_update()
        .filter(
            user=user,
            plan_date=today_for_user(user),
            status=PlanStatus.ACTIVE,
        )
        .first()
    )
    if plan is None:
        plan = RecoveryPlan.objects.create(
            user=user,
            plan_date=today_for_user(user),
            generation_snapshot_json=build_plan_generation_snapshot(
                user,
                context_snapshot,
                next_activity_plan,
            ),
        )

    cancel_next_snapshot_slot_for_reentry(user=user, now=now)
    slot = create_recovery_slot(
        plan=plan,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        recommended_at=now,
        notification_enabled=False,
        notification_basis=SlotNotificationBasis.SNAPSHOT,
    )
    _create_slot_insight(
        slot,
        "활성 활동 중 재진입해 현재 상태를 반영한 즉시 회복 세션입니다.",
        ["context_snapshot", "next_activity_plan"],
    )
    _create_routine_instances(
        slot,
        context_snapshot,
        next_activity_plan,
        _reentry_shift_recommendations(context_snapshot, next_activity_plan),
    )

    return slot


def generate_ai_recovery_plan(
    *,
    user,
    context_snapshot=None,
    next_activity_plan=None,
    notification_enabled=True,
    use_ai_decision=False,
):
    context_snapshot, next_activity_plan = resolve_generation_inputs(
        user=user,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
    )
    _validate_recovery_activity_catalog(context_snapshot, next_activity_plan)
    input_snapshot = build_ai_input_snapshot(user, context_snapshot, next_activity_plan)

    ai_output, raw_response, generator_name, is_ai_generated = _generate_plan_output(
        user=user,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        input_snapshot=input_snapshot,
        use_ai_decision=use_ai_decision,
    )
    normalized_slots = normalize_ai_slots(
        ai_output,
        user,
        context_snapshot,
        next_activity_plan,
        is_ai_generated=is_ai_generated,
        use_ai_decision=use_ai_decision,
    )
    plan = _persist_ai_plan(
        user=user,
        context_snapshot=context_snapshot,
        next_activity_plan=next_activity_plan,
        input_snapshot=input_snapshot,
        ai_output=ai_output,
        raw_response=raw_response,
        normalized_slots=normalized_slots,
        notification_enabled=notification_enabled,
        generator_name=generator_name,
        use_ai_decision=use_ai_decision,
    )

    return plan


def _generate_plan_output(*, user, context_snapshot, next_activity_plan, input_snapshot, use_ai_decision):
    """
    use_ai_decision=True일 때만 실제 LLM 호출을 먼저 시도한다 — 성공하면 개수/
    시각까지 AI가 자율적으로 정한 결과를 쓴다. API 키 미설정/호출 실패/예기치
    못한 오류가 나면 서버 정책 엔진으로 안전하게 폴백해서, OpenAI 장애가 통째로
    회복 계획 생성 실패로 이어지지 않게 한다(하루 회복 루틴은 사용자에게 핵심
    기능이라 가용성이 자율성보다 우선).

    use_ai_decision=False(기본값)면 LLM은 아예 시도하지 않고 곧장 정책 엔진으로
    간다 — My Digital State에서 PC 사용 패턴을 입력하고 만든 흐름이 아니면
    (예: 상태 선택 모달로 진행하는 "회복 루틴 시작하기") 원래 로직 그대로다.
    """
    if use_ai_decision:
        try:
            input_messages = build_input_messages(input_snapshot)
            ai_output, raw_openai_response = create_structured_response(
                input_messages=input_messages,
                schema=RECOVERY_PLAN_SCHEMA,
            )
            generator_name = f"openai:{settings.OPENAI_MODEL}"
            raw_response = {
                "generator": generator_name,
                "external_api_called": True,
                "response": raw_openai_response,
            }
            return ai_output, raw_response, generator_name, True
        except OpenAIConfigurationError:
            logger.info("OPENAI_API_KEY 미설정으로 정책 엔진으로 생성합니다.")
        except OpenAIClientError:
            logger.warning("OpenAI API 호출 실패로 정책 엔진으로 폴백합니다.", exc_info=True)
        except Exception:
            logger.exception("AI 회복 계획 생성 중 예기치 못한 오류로 정책 엔진으로 폴백합니다.")

    ai_output = build_policy_output(
        user,
        context_snapshot,
        next_activity_plan,
        input_snapshot,
        include_frequency_slots=use_ai_decision,
    )
    raw_response = {
        "generator": POLICY_GENERATOR_NAME,
        "external_api_called": False,
    }
    return ai_output, raw_response, POLICY_GENERATOR_NAME, False
