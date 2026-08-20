import json
from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from .models import Notification, NotificationKind, NotificationStatus, WebPushSubscription


STALE_SUBSCRIPTION_STATUS_CODES = {404, 410}


@dataclass
class WebPushDeliveryResult:
    success_count: int = 0
    failure_count: int = 0
    deactivated_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def delivered(self):
        return self.success_count > 0


@dataclass
class DueNotificationResult:
    processed_count: int = 0
    sent_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    deactivated_subscription_count: int = 0


def load_pywebpush():
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:
        raise ImproperlyConfigured("pywebpush 패키지가 설치되어 있지 않습니다.") from exc
    return webpush, WebPushException


def notification_user(notification):
    if notification.user_id:
        return notification.user
    if notification.recovery_slot_id:
        return notification.recovery_slot.recovery_plan.user
    return None


def subscription_info(subscription):
    return {
        "endpoint": subscription.endpoint,
        "keys": {
            "p256dh": subscription.p256dh,
            "auth": subscription.auth,
        },
    }


def build_web_push_payload(notification):
    title = "회복 세션 알림"
    # 알림을 눌렀을 때 특정 라우트(/handroutine 등)로 보내는 대신, 그냥 사이트
    # 첫 화면으로 이동시킨다 — 오늘의 회복 계획/루틴이 이미 랜딩 페이지에서
    # 다 보이기 때문에 별도 진입점이 필요 없다는 판단.
    url = "/"
    recovery_slot_id = None
    if notification.kind == NotificationKind.REENGAGEMENT:
        title = "회복 루틴 다시 시작하기"
    if notification.recovery_slot_id:
        recovery_slot_id = str(notification.recovery_slot_id)
    if notification.data_json.get("url"):
        url = notification.data_json["url"]

    return {
        "title": title,
        "body": notification.message,
        "data": {
            "notification_id": str(notification.id),
            "kind": notification.kind,
            "recovery_slot_id": recovery_slot_id,
            "url": url,
        },
    }


def send_web_push(subscription, payload):
    private_key = settings.WEB_PUSH_VAPID_PRIVATE_KEY
    if not private_key:
        raise ImproperlyConfigured("WEB_PUSH_VAPID_PRIVATE_KEY 설정이 필요합니다.")

    webpush, _ = load_pywebpush()
    return webpush(
        subscription_info=subscription_info(subscription),
        data=json.dumps(payload, ensure_ascii=False),
        vapid_private_key=private_key,
        vapid_claims={"sub": settings.WEB_PUSH_VAPID_SUBJECT},
        ttl=settings.WEB_PUSH_TTL_SECONDS,
        timeout=settings.WEB_PUSH_TIMEOUT_SECONDS,
    )


def response_status_code(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def send_notification_to_subscriptions(notification, subscriptions):
    payload = build_web_push_payload(notification)
    result = WebPushDeliveryResult()

    for subscription in subscriptions:
        try:
            send_web_push(subscription, payload)
        except ImproperlyConfigured:
            raise
        except Exception as exc:
            result.failure_count += 1
            status_code = response_status_code(exc)
            if status_code in STALE_SUBSCRIPTION_STATUS_CODES:
                subscription.is_active = False
                subscription.save(update_fields=["is_active", "updated_at"])
                result.deactivated_count += 1
            result.errors.append(str(exc))
        else:
            result.success_count += 1

    return result


def due_notifications(now=None, limit=100):
    now = now or timezone.now()
    return (
        Notification.objects.select_related("user", "recovery_slot", "recovery_slot__recovery_plan__user")
        .filter(status=NotificationStatus.PENDING, scheduled_at__lte=now)
        .order_by("scheduled_at", "created_at")[:limit]
    )


def mark_notification_sent(notification, sent_at):
    notification.status = NotificationStatus.SENT
    notification.sent_at = sent_at
    notification.delivery_error = ""
    notification.save(update_fields=["status", "sent_at", "delivery_error", "updated_at"])


def mark_notification_failed(notification, error_message):
    notification.status = NotificationStatus.FAILED
    notification.delivery_error = error_message[:2000]
    notification.save(update_fields=["status", "delivery_error", "updated_at"])


def send_due_notifications(*, now=None, limit=100, dry_run=False):
    now = now or timezone.now()
    result = DueNotificationResult()

    for notification in list(due_notifications(now=now, limit=limit)):
        user = notification_user(notification)
        if user is None:
            result.skipped_count += 1
            if not dry_run:
                mark_notification_failed(notification, "알림 소유 사용자를 찾을 수 없습니다.")
            continue

        subscriptions = list(WebPushSubscription.objects.filter(user=user, is_active=True))
        if not subscriptions:
            result.failed_count += 1
            if not dry_run:
                mark_notification_failed(notification, "활성 Web Push 구독이 없습니다.")
            continue

        result.processed_count += 1
        if dry_run:
            continue

        with transaction.atomic():
            notification = Notification.objects.select_for_update().get(id=notification.id)
            if notification.status != NotificationStatus.PENDING or notification.scheduled_at > now:
                result.skipped_count += 1
                continue

            delivery = send_notification_to_subscriptions(notification, subscriptions)
            result.deactivated_subscription_count += delivery.deactivated_count
            if delivery.delivered:
                mark_notification_sent(notification, now)
                result.sent_count += 1
            else:
                error_message = "; ".join(delivery.errors) or "모든 Web Push 발송이 실패했습니다."
                mark_notification_failed(notification, error_message)
                result.failed_count += 1

    return result
