"""The overseer: watching the worker work, as it happens.

Three routes:

- GET /overseer         the page
- GET /overseer/stream  Server-Sent Events: live worker steps and broker queue depths
- GET /overseer/recent  the last few audit_log rows, i.e. what has been committed

The stream and the record are deliberately shown side by side on the page. The
stream says what the worker is doing now; the record only moves when a
transaction commits. Watching the record catch up at the end of a ticket is
watching the transaction boundary, which is worth being able to point at.

Server-Sent Events rather than WebSockets because the traffic only goes one
way. SSE is plain HTTP, reconnects on its own in every browser, and needs no
library at either end.
"""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.schemas import AuditEventOut
from shared.db import get_db
from shared.models import AuditLog, Ticket
from shared.progress import WORKER_PROGRESS_EXCHANGE, declare_progress_exchange
from shared.rabbitmq import (
    TICKET_DEAD_LETTER_QUEUE,
    TICKET_PROCESSING_QUEUE,
    TICKET_RETRY_QUEUE,
    declare_topology,
    get_connection,
)

logger = logging.getLogger("api.overseer")

router = APIRouter(prefix="/overseer", tags=["overseer"])

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

BROKER_QUEUES = (TICKET_PROCESSING_QUEUE, TICKET_RETRY_QUEUE, TICKET_DEAD_LETTER_QUEUE)

# How often queue depths are re-read. A passive declare is cheap, but it is
# still a round trip per queue, and depths do not change faster than this in
# any way a person could read.
DEPTH_EVERY_SECONDS = 2.0

# Proxies and browsers drop an HTTP response that has said nothing for a
# while. A comment line every so often keeps a quiet stream alive.
KEEPALIVE_EVERY_SECONDS = 15.0

# If the page cannot keep up, drop the oldest events rather than let memory
# grow without limit behind a slow tab.
INBOX_LIMIT = 1000


def sse(event: str, data) -> str:
    """One Server-Sent Event, in the wire format EventSource expects."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def queue_depths(channel) -> list[dict]:
    """Messages waiting and consumers attached, per broker queue.

    consumer_count on ticket.processing is the number of workers connected
    right now, straight from the broker. The page uses it as the honest answer
    to "is anyone actually doing the work", rather than inferring it from
    whether progress events happen to be arriving.
    """
    depths = []
    for name in BROKER_QUEUES:
        ok = channel.queue_declare(queue=name, passive=True)
        depths.append(
            {"name": name, "messages": ok.method.message_count, "consumers": ok.method.consumer_count}
        )
    return depths


def pump(stop: threading.Event, push) -> None:
    """Relay broker traffic to one browser. Runs on its own thread.

    Owns its pika connection for its whole life, on one thread. pika's
    BlockingConnection is not safe to use across threads, and letting the web
    server call into it from whichever worker thread happened to be free is
    exactly how that goes wrong.

    The queue it reads from is exclusive and server-named, so it is private to
    this one browser tab and deleted by the broker the moment the connection
    closes. A tab that is closed without warning leaves nothing behind.
    """
    try:
        connection = get_connection()
    except Exception as exc:
        push("unavailable", {"detail": f"RabbitMQ is unreachable: {exc}"})
        return

    try:
        channel = connection.channel()
        declare_topology(channel)
        declare_progress_exchange(channel)
        inbox = channel.queue_declare(queue="", exclusive=True, auto_delete=True).method.queue
        channel.queue_bind(queue=inbox, exchange=WORKER_PROGRESS_EXCHANGE)

        push("queues", queue_depths(channel))
        last_depths = time.monotonic()

        # inactivity_timeout makes consume() yield (None, None, None) when
        # nothing arrives, which is what lets this loop notice it has been told
        # to stop and refresh the depths during a quiet spell.
        for _method, _props, body in channel.consume(inbox, auto_ack=True, inactivity_timeout=0.5):
            if stop.is_set():
                break
            if body is not None:
                try:
                    push("progress", json.loads(body))
                except json.JSONDecodeError:
                    logger.debug("ignoring a progress message that was not JSON")
            if time.monotonic() - last_depths >= DEPTH_EVERY_SECONDS:
                push("queues", queue_depths(channel))
                last_depths = time.monotonic()
    except Exception as exc:
        push("unavailable", {"detail": f"lost the connection to RabbitMQ: {exc}"})
    finally:
        try:
            connection.close()
        except Exception:
            pass


@router.get("", include_in_schema=False)
def overseer_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "overseer.html")


@router.get("/stream")
async def overseer_stream(request: Request) -> StreamingResponse:
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def offer(event: str, data) -> None:
        if inbox.qsize() >= INBOX_LIMIT:
            inbox.get_nowait()
        inbox.put_nowait((event, data))

    def push(event: str, data) -> None:
        # Called on the pump thread. asyncio.Queue is not thread-safe, so the
        # hand-over goes through the event loop rather than touching it here.
        try:
            loop.call_soon_threadsafe(offer, event, data)
        except RuntimeError:
            # The loop has gone - the server is shutting down. Nothing to tell.
            stop.set()

    threading.Thread(target=pump, args=(stop, push), daemon=True, name="overseer-pump").start()

    async def events():
        # Tell the browser how long to wait before reconnecting after a drop.
        yield "retry: 3000\n\n"
        last_sent = time.monotonic()
        try:
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(inbox.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_sent >= KEEPALIVE_EVERY_SECONDS:
                        yield ": keepalive\n\n"
                        last_sent = time.monotonic()
                    continue

                yield sse(event, data)
                last_sent = time.monotonic()
                if event == "unavailable":
                    # End the response; EventSource will reconnect after the
                    # retry interval above and try the broker again.
                    return
        finally:
            # However the response ends - tab closed, network dropped, server
            # stopping - the pump thread is told to let go of its connection.
            stop.set()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # no-cache so nothing between here and the browser holds events back,
        # and the nginx hint for the same reason if this ever sits behind one.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/recent", response_model=list[AuditEventOut])
def overseer_recent(
    limit: int = Query(40, ge=1, le=200), db: Session = Depends(get_db)
) -> list[AuditEventOut]:
    rows = (
        db.query(AuditLog, Ticket.subject)
        .outerjoin(Ticket, Ticket.id == AuditLog.ticket_id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    return [
        AuditEventOut(
            id=log.id,
            ticket_id=log.ticket_id,
            subject=subject,
            event_type=log.event_type,
            detail=log.detail,
            created_at=log.created_at,
        )
        for log, subject in rows
    ]
