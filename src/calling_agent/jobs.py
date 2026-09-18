"""Work that happens without anybody asking (PRD §12, §13).

Four things run on a clock:

  * expired holds give their capacity back, every minute. Without this the room
    fills with ghosts and the agent turns real people away from an empty venue;
  * queued messages are sent;
  * due outbound tasks run -- reminders, overflow deadlines, callbacks;
  * the staff digest goes out at the hour each venue chose.

The PRD names Celery, and the task table is shaped for it: `outbound_tasks` is
a queue with a schedule, attempts and a status, so a Celery beat entry calling
`run_due_tasks` is a drop-in. What runs here is an asyncio loop inside the web
process, which is correct for one server and the thing to move first when there
are several. Everything it calls is safe to run concurrently -- every claim
uses FOR UPDATE SKIP LOCKED -- so two of them racing is slow, not wrong.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from . import bookings, businesses, formatting, holds, notifications
from .businesses import Business
from .config import settings
from .db import fetch_all, transaction

log = logging.getLogger(__name__)

#: reason -> handler. Adding a scheduled behaviour is an entry here.
HANDLERS: dict[str, Any] = {}


def handles(reason: str):
    def register(fn):
        HANDLERS[reason] = fn
        return fn

    return register


# --- the tick ----------------------------------------------------------------


def tick() -> dict[str, int]:
    """One pass of everything. Safe to call as often as you like."""
    swept = _guard("sweep expired holds", holds.sweep_expired)
    ran = _guard("run due tasks", run_due_tasks)
    sent = _guard("send queued messages", notifications.send_queued)
    return {"holds_swept": swept, "tasks_run": ran, "messages_sent": sent}


def _guard(label: str, fn) -> int:
    """One failing job must never stop the others.

    The sweeper and the sender are independent; a carrier outage that broke
    sending must not also stop capacity being returned, or a bad afternoon
    becomes a fully booked empty restaurant.
    """
    try:
        return int(fn() or 0)
    except Exception:  # noqa: BLE001
        log.exception("job failed: %s", label)
        return 0


async def run_forever(interval_seconds: int | None = None) -> None:
    """The loop the web process starts on boot."""
    interval = interval_seconds or settings.sweep_interval_seconds
    log.info("background jobs running every %ss", interval)
    while True:
        await asyncio.to_thread(tick)
        await asyncio.sleep(interval)


# --- the task queue ----------------------------------------------------------


def run_due_tasks(limit: int = 100) -> int:
    """Claim and run whatever is due. Claimed with SKIP LOCKED, so workers share.

    THREE KINDS OF DUE, NOT ONE
    A task is claimed, its claim commits, and only then does it run -- so a
    process that dies in between leaves the row 'running' with nobody running
    it. Claiming only 'queued' rows stranded those forever, and the one that
    hurts is overflow_timeout: its whole job is to stop a waitlisted guest
    waiting in silence, which is exactly what it then did.

    So a claim also takes back a 'running' row older than the lease, and
    retries a 'failed' one until attempts run out. Retrying is only safe
    because the handlers dedupe their messages on the task id -- without that
    this would text somebody twice, which is why it was never added.
    """
    done = 0
    with transaction() as conn:
        rows = fetch_all(
            conn,
            "SELECT id, business_id, booking_id, reason, payload, attempts FROM outbound_tasks"
            " WHERE ("
            "   (status = 'queued' AND scheduled_for <= now())"
            "   OR (status = 'running'"
            "       AND updated_at < now() - make_interval(secs => :lease))"
            "   OR (status = 'failed' AND attempts < :max_attempts"
            "       AND updated_at < now() - make_interval(secs => :lease))"
            " )"
            " ORDER BY scheduled_for LIMIT :limit FOR UPDATE SKIP LOCKED",
            limit=limit,
            lease=settings.task_lease_seconds,
            max_attempts=settings.task_max_attempts,
        )
        # The rows were read before the UPDATE below raises `attempts`, so the
        # dicts would carry the previous count and a handler deciding whether
        # it has tries left would be one behind.
        claimed = [{**dict(r._mapping), "attempts": int(r.attempts or 0) + 1} for r in rows]
        if claimed:
            conn.execute(
                text(
                    "UPDATE outbound_tasks SET status = 'running', attempts = attempts + 1,"
                    " updated_at = now() WHERE id = ANY(:ids)"
                ),
                {"ids": [r["id"] for r in claimed]},
            )

    for task in claimed:
        _run_one(task)
        done += 1
    return done


def _run_one(task: dict[str, Any]) -> None:
    handler = HANDLERS.get(task["reason"])
    if handler is None:
        # Not retryable: another attempt calls the same missing handler.
        _finish(task["id"], "abandoned", f"no handler for {task['reason']}")
        return

    try:
        business = businesses.by_id(task["business_id"])
        handler(business, task)
    except Exception as exc:  # noqa: BLE001 - one bad task must not stop the queue
        attempts = int(task.get("attempts") or 0)
        spent = attempts >= settings.task_max_attempts
        log.exception(
            "task %s (%s) failed on attempt %d%s",
            task["id"], task["reason"], attempts, "" if spent else "; will retry",
        )
        _finish(task["id"], "abandoned" if spent else "failed", str(exc)[:500])
        return
    _finish(task["id"], "done", None)


def message_key(task: dict[str, Any], purpose: str = "") -> str:
    """The dedupe key for a message this task sends.

    Keyed on the TASK rather than the booking, so a retry cannot send a second
    text while a deliberate resend of the same booking still can.
    """
    return f"task:{task['id']}{':' + purpose if purpose else ''}"


def _finish(task_id, status: str, error: str | None) -> None:
    with transaction() as conn:
        conn.execute(
            text(
                "UPDATE outbound_tasks SET status = :s, last_error = :e, updated_at = now()"
                " WHERE id = :i"
            ),
            {"s": status, "e": error, "i": str(task_id)},
        )


# --- handlers ----------------------------------------------------------------


@handles("reminder")
def send_reminder(business: Business, task: dict[str, Any]) -> None:
    """The day-before text. Skipped if the booking is no longer happening."""
    booking = bookings.by_id(business, task["booking_id"])
    if booking.status not in (bookings.CONFIRMED, bookings.ARRIVED):
        return
    with transaction() as conn:
        notifications.queue_for_booking(
            conn, business, booking, "reminder", dedupe_key=message_key(task)
        )


@handles("overflow_timeout")
def close_overflow(business: Business, task: dict[str, Any]) -> None:
    """The deadline passed with no answer: auto-decline, and offer an alternative.

    PRD §11: never leave the customer silent. Silence from the owner is an
    answer to us and must never be an answer to the guest.
    """
    bookings.expire_overflow(business, task["booking_id"])


@handles("arrival_nudge")
def arrival_nudge(business: Business, task: dict[str, Any]) -> None:
    """"7:00 — Sharma, 5, regular", to the staff, half an hour before."""
    booking = bookings.by_id(business, task["booking_id"])
    if booking.status != bookings.CONFIRMED:
        return
    line = (
        f"{formatting.time_str(business, booking.start_time)} — {booking.name}, "
        f"{booking.party_size}"
    )
    if booking.visits >= 4:
        line += ", regular"
    if booking.notes:
        line += f" — {booking.notes}"
    log.info("[staff:%s] %s", business.slug, line)


@handles("digest")
def daily_digest(business: Business, task: dict[str, Any]) -> None:
    """Tonight's list, once, at the hour the venue chose."""
    today = datetime.now(business.tz).date()
    tonight = [
        b for b in bookings.on_local_date(business, today)
        if b.status in (bookings.CONFIRMED, bookings.ARRIVED)
    ]
    covers = sum(b.party_size for b in tonight)
    log.info(
        "[digest:%s] %d bookings, %d %s tonight",
        business.slug, len(tonight), covers, business.unit_plural,
    )


# --- scheduling the recurring ones -------------------------------------------


def schedule_daily_work(now: datetime | None = None) -> int:
    """Queue today's digest and arrival nudges for every active business.

    Idempotent through dedupe keys, so running it twice is a no-op rather than
    two texts. That matters: "run it again to be sure" is what an operator does
    at seven on a Friday.
    """
    now = now or datetime.now(UTC)
    queued = 0

    with transaction() as conn:
        rows = fetch_all(conn, "SELECT id FROM businesses WHERE status = 'active'")
        ids = [r.id for r in rows]

    for business_id in ids:
        business = businesses.by_id(business_id)
        queued += _schedule_digest(business, now)
        queued += _schedule_nudges(business, now)
    return queued


def _schedule_digest(business: Business, now: datetime) -> int:
    messaging = business.config["messaging"]
    hour, minute = (int(p) for p in messaging["digest_local_time"].split(":")[:2])
    local_today = now.astimezone(business.tz).date()
    at = datetime.combine(
        local_today, datetime.min.time().replace(hour=hour, minute=minute), tzinfo=business.tz
    ).astimezone(UTC)
    if at < now:
        return 0

    with transaction() as conn:
        notifications.queue_outbound_task(
            conn, business, reason="digest", scheduled_for=at,
            dedupe_key=f"digest:{local_today.isoformat()}",
        )
    return 1


def _schedule_nudges(business: Business, now: datetime) -> int:
    offset = int(business.config["messaging"]["arrival_nudge_minutes"])
    if offset <= 0:
        return 0

    today = now.astimezone(business.tz).date()
    upcoming = [
        b for b in bookings.on_local_date(business, today)
        if b.status == bookings.CONFIRMED and b.start_time > now
    ]
    if not upcoming:
        return 0

    with transaction() as conn:
        for booking in upcoming:
            notifications.queue_outbound_task(
                conn, business, reason="arrival_nudge", booking_id=booking.id,
                scheduled_for=booking.start_time - timedelta(minutes=offset),
                dedupe_key=f"nudge:{booking.id}",
            )
    return len(upcoming)


def main() -> None:
    """Run the jobs as their own process.

    What `RUN_JOBS_IN_PROCESS=false` expects on the other side. One of these
    is enough; several are safe, because every claim uses FOR UPDATE SKIP
    LOCKED and shares the work rather than repeating it.
    """
    import logging as _logging

    _logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    schedule_daily_work()
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
