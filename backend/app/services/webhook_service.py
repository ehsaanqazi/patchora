"""Webhook ingestion.

The HTTP handler verifies the signature, records the delivery (unique per provider +
delivery id) and enqueues a processing job, then returns immediately. Processing is
retry-safe: a delivery is only processed once, and run creation itself is deduplicated.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.enums import DeliveryStatus, JobKind
from app.core.exceptions import InvalidRequestError
from app.db.base import utcnow
from app.db.models import PullRequest, WebhookDelivery
from app.db.session import session_scope, with_write_retry
from app.providers.factory import ProviderFactory
from app.schemas.issues import NormalizedIssue
from app.services import audit_service, run_service
from app.services.sentry_service import parse_issue
from app.services.settings_service import get_runtime_settings
from app.workers import job_runner

SENTRY_RESOURCES = {"issue", "event_alert", "error", "installation", "metric_alert", "comment", "seer"}


class SentryWebhookEnvelope(BaseModel):
    action: str | None = None
    data: dict[str, Any]


def record_delivery(session: Session, *, provider: str, delivery_id: str, event_type: str, body_hash: str,
                    payload: dict[str, Any]) -> tuple[WebhookDelivery, bool]:
    """Returns (delivery, is_new). Duplicate deliveries are detected by unique constraint."""
    existing = session.scalar(select(WebhookDelivery).where(WebhookDelivery.provider == provider,
                                                            WebhookDelivery.delivery_id == delivery_id))
    if existing is not None:
        return existing, False
    delivery = WebhookDelivery(provider=provider, delivery_id=delivery_id[:128], event_type=event_type[:60],
                               payload_hash=body_hash, payload_json=payload,
                               processing_status=DeliveryStatus.RECEIVED.value)
    session.add(delivery)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(WebhookDelivery).where(WebhookDelivery.provider == provider,
                                                                WebhookDelivery.delivery_id == delivery_id))
        assert existing is not None
        return existing, False
    job_runner.enqueue(session, JobKind.PROCESS_WEBHOOK, {"delivery_id": delivery.id},
                       idempotency_key=f"webhook:{provider}:{delivery_id}", max_attempts=5)
    return delivery, True


def validate_sentry_payload(resource: str, payload: Any) -> SentryWebhookEnvelope:
    if resource not in SENTRY_RESOURCES:
        raise InvalidRequestError(f"Unsupported Sentry-Hook-Resource '{resource}'.")
    try:
        return SentryWebhookEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise InvalidRequestError("Malformed Sentry webhook payload.") from exc


def _issue_from_payload(resource: str, data: dict[str, Any],
                        organization: str) -> tuple[str | None, NormalizedIssue | None]:
    if resource == "issue" and isinstance(data.get("issue"), dict):
        issue = data["issue"]
        return str(issue.get("id")), parse_issue(issue, organization)
    if resource in {"event_alert", "error"}:
        event = data.get("event") or data.get("error") or {}
        issue_id = event.get("issue_id") or event.get("groupID")
        return (str(issue_id) if issue_id else None), None
    return None, None


async def process_delivery(factory: ProviderFactory, settings: Settings, delivery_pk: int) -> None:
    def load(session: Session) -> tuple[str, str, dict[str, Any], str] | None:
        delivery = session.get(WebhookDelivery, delivery_pk)
        if delivery is None or delivery.processing_status != DeliveryStatus.RECEIVED.value:
            return None  # already processed (retry-safe)
        return delivery.provider, delivery.event_type, dict(delivery.payload_json), delivery.delivery_id

    loaded = with_write_retry(load)
    if loaded is None:
        return
    provider, event_type, payload, delivery_id = loaded
    try:
        if provider == "sentry":
            status, note = await _process_sentry(factory, settings, event_type, payload, delivery_id)
        elif provider == "github":
            status, note = with_write_retry(lambda s: _process_github(s, event_type, payload))
        else:
            status, note = DeliveryStatus.IGNORED, "unknown provider"
    except Exception as exc:
        message = str(getattr(exc, "message", exc))[:1000]

        def mark_failed(session: Session) -> None:
            delivery = session.get(WebhookDelivery, delivery_pk)
            if delivery is not None:
                delivery.error_message = message
        with_write_retry(mark_failed)
        raise

    def finish(session: Session) -> None:
        delivery = session.get(WebhookDelivery, delivery_pk)
        if delivery is not None:
            delivery.processing_status = status.value
            delivery.processed_at = utcnow()
            delivery.error_message = note if status == DeliveryStatus.IGNORED else None

    with_write_retry(finish)


async def _process_sentry(factory: ProviderFactory, settings: Settings, resource: str, payload: dict[str, Any],
                          delivery_id: str) -> tuple[DeliveryStatus, str]:
    envelope = SentryWebhookEnvelope.model_validate(payload)
    with_session = with_write_retry

    def org_and_settings(session: Session) -> tuple[str | None, bool, str]:
        from app.services.credential_service import resolve_credential

        runtime = get_runtime_settings(session, settings)
        org = resolve_credential(session, "sentry", "organization", settings).value
        return org, runtime.webhook_auto_analyze, runtime.autonomy_mode.value

    organization, auto_analyze, mode = with_session(org_and_settings)
    if not organization:
        return DeliveryStatus.IGNORED, "SENTRY_ORG is not configured"
    if resource == "issue" and envelope.action not in {"created", "unresolved", "regression"}:
        return DeliveryStatus.IGNORED, f"issue action '{envelope.action}' does not trigger analysis"
    issue_id, issue = _issue_from_payload(resource, envelope.data, organization)
    if not issue_id:
        return DeliveryStatus.IGNORED, f"resource '{resource}' carries no issue"
    if issue is None:
        with session_scope() as session:
            source = factory.sentry(session)
        try:
            issue = await source.get_issue(issue_id)
        finally:
            await source.aclose()

    from app.config import AutonomyMode

    def ingest(session: Session) -> tuple[DeliveryStatus, str]:
        assert issue is not None
        row = run_service.upsert_issue(session, issue)
        audit_service.record(session, actor_type="webhook", actor_id=f"sentry:{delivery_id}",
                             action="sentry_issue.ingested", resource_type="sentry_issue", resource_id=row.id)
        if not auto_analyze:
            return DeliveryStatus.PROCESSED, "ingested (auto-analysis disabled)"
        if row.repository_id is None:
            return DeliveryStatus.PROCESSED, "ingested (no repository mapping)"
        run, created = run_service.create_run(
            session, row, trigger="webhook", actor_type="webhook", actor_id=f"sentry:{delivery_id}",
            autonomy_mode=AutonomyMode(mode), idempotency_key=f"sentry-webhook:{delivery_id}")
        return DeliveryStatus.PROCESSED, f"run #{run.id} {'created' if created else 'already active'}"

    return with_session(ingest)


def _process_github(session: Session, event_type: str, payload: dict[str, Any]) -> tuple[DeliveryStatus, str]:
    if event_type != "pull_request":
        return DeliveryStatus.IGNORED, f"event '{event_type}' not handled"
    pr_data = payload.get("pull_request") or {}
    number = pr_data.get("number")
    repo_full = (payload.get("repository") or {}).get("full_name")
    if not number or not repo_full:
        return DeliveryStatus.IGNORED, "incomplete pull_request payload"
    from app.db.models import Repository

    pr = session.scalar(select(PullRequest).join(Repository, PullRequest.repository_id == Repository.id)
                        .where(Repository.full_name == repo_full, PullRequest.external_pr_number == int(number)))
    if pr is None:
        return DeliveryStatus.IGNORED, "pull request not created by Patchora"
    new_status = "merged" if pr_data.get("merged") else pr_data.get("state", pr.status)
    if new_status != pr.status:
        pr.status = str(new_status)[:20]
        pr.draft = bool(pr_data.get("draft", pr.draft))
        audit_service.record(session, actor_type="webhook", actor_id="github", action="pull_request.status_changed",
                             resource_type="pull_request", resource_id=pr.id, details={"status": pr.status})
    return DeliveryStatus.PROCESSED, f"pull request status {pr.status}"


def parse_json_body(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidRequestError("Request body is not valid JSON.") from exc
