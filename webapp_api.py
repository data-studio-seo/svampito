"""
Svampito Mini App — REST API
Serves the Telegram Web App frontend with reminder data.

Endpoints:
  GET  /api/reminders          — list active reminders (with filters)
  POST /api/reminders          — create a new reminder
  PUT  /api/reminders/{id}     — update a reminder
  DELETE /api/reminders/{id}   — cancel a reminder
  POST /api/reminders/{id}/done — mark as done
  GET  /api/stats              — completion stats & streaks
  GET  /api/calendar/{year}/{month} — reminders for a month

Auth: Telegram initData validation (HMAC-SHA256)
"""
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import unquote, parse_qs

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, and_, func, extract
from sqlalchemy.ext.asyncio import AsyncSession
import pytz

from database import (
    async_session, Reminder, ReminderLog, User,
    ReminderStatus, ReminderCategory, RecurrenceType
)
from services.messages import get_emoji

logger = logging.getLogger(__name__)

app = FastAPI(title="Svampito Mini App API")

# CORS for Telegram Web App
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# Auth: validate Telegram initData
# ─────────────────────────────────────────────

def _validate_init_data(init_data: str) -> dict:
    """
    Validate Telegram Web App initData using HMAC-SHA256.
    Returns user data if valid, raises HTTPException if not.
    """
    bot_token = os.environ.get("BOT_TOKEN", "")
    if not bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")

    # Parse the query string
    parsed = parse_qs(init_data)

    # Extract hash
    received_hash = parsed.get("hash", [None])[0]
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")

    # Build data-check-string (sorted, without hash)
    data_pairs = []
    for key, values in sorted(parsed.items()):
        if key != "hash":
            data_pairs.append(f"{key}={values[0]}")
    data_check_string = "\n".join(data_pairs)

    # HMAC-SHA256 validation
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if computed_hash != received_hash:
        raise HTTPException(status_code=401, detail="Invalid initData")

    # Extract user info
    user_json = parsed.get("user", [None])[0]
    if not user_json:
        raise HTTPException(status_code=401, detail="No user data")

    return json.loads(unquote(user_json))


async def get_current_user(x_init_data: str = Header(alias="X-Init-Data", default="")) -> dict:
    """Dependency: extract and validate Telegram user from initData header."""
    if not x_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Init-Data header")

    # In development, allow bypass
    if x_init_data == "dev" and os.environ.get("DEBUG"):
        return {"id": 1, "first_name": "Dev"}

    return _validate_init_data(x_init_data)


# ─────────────────────────────────────────────
# Request/Response models
# ─────────────────────────────────────────────

class ReminderOut(BaseModel):
    id: int
    title: str
    category: str
    emoji: str
    next_fire: str  # ISO format in user timezone
    next_fire_time: str  # HH:MM
    recurrence: str
    recurrence_days: Optional[str] = None
    fire_times: Optional[str] = None
    status: str
    nudge_count: int = 0
    snooze_count: int = 0
    created_at: str

class ReminderCreate(BaseModel):
    title: str
    category: str = "generic"
    date: str  # YYYY-MM-DD
    time: str = "09:00"  # HH:MM
    recurrence: str = "once"
    recurrence_days: Optional[str] = None
    fire_times: Optional[list[str]] = None
    end_date: Optional[str] = None

class ReminderUpdate(BaseModel):
    title: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    category: Optional[str] = None

class StatsOut(BaseModel):
    total_active: int
    completed_today: int
    completed_week: int
    streak_days: int
    completion_rate_week: float  # 0-100
    by_category: dict  # {category: count}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

async def _get_user_tz(user_id: int) -> pytz.BaseTzInfo:
    async with async_session() as session:
        user = await session.get(User, user_id)
        tz_name = user.timezone if user else "Europe/Rome"
    return pytz.timezone(tz_name)


def _reminder_to_out(r: Reminder, tz) -> ReminderOut:
    fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
    return ReminderOut(
        id=r.id,
        title=r.title,
        category=r.category or "generic",
        emoji=get_emoji(r.category or "generic"),
        next_fire=fire_local.isoformat(),
        next_fire_time=fire_local.strftime("%H:%M"),
        recurrence=r.recurrence or "once",
        recurrence_days=r.recurrence_days,
        fire_times=r.fire_times,
        status=r.status or "active",
        nudge_count=r.nudge_count or 0,
        snooze_count=r.snooze_count or 0,
        created_at=r.created_at.isoformat() if r.created_at else "",
    )


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@app.get("/api/reminders")
async def list_reminders(
    period: str = Query("all", regex="^(today|tomorrow|week|all)$"),
    category: str = Query("all"),
    user: dict = Depends(get_current_user),
):
    """List active reminders with optional filters."""
    user_id = user["id"]
    tz = await _get_user_tz(user_id)
    now = datetime.now(tz)

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
    elif period == "tomorrow":
        start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)
    elif period == "week":
        start = now.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=7)
    else:
        start = now.replace(hour=0, minute=0, second=0) - timedelta(days=1)
        end = start + timedelta(days=365)

    start_utc = start.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = end.astimezone(pytz.UTC).replace(tzinfo=None)

    async with async_session() as session:
        conditions = [
            Reminder.user_id == user_id,
            Reminder.status == ReminderStatus.ACTIVE,
            Reminder.next_fire >= start_utc,
            Reminder.next_fire < end_utc,
        ]
        if category != "all":
            conditions.append(Reminder.category == category)

        stmt = select(Reminder).where(and_(*conditions)).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    # Deduplicate multi-time slots (show only time_slot_index == 0 or None)
    seen_titles = {}
    out = []
    for r in reminders:
        if r.time_slot_index and r.time_slot_index > 0:
            continue
        out.append(_reminder_to_out(r, tz))

    return {"reminders": out, "count": len(out)}


@app.post("/api/reminders")
async def create_reminder(
    data: ReminderCreate,
    user: dict = Depends(get_current_user),
):
    """Create a new reminder."""
    user_id = user["id"]
    tz = await _get_user_tz(user_id)

    try:
        fire_date = datetime.strptime(data.date, "%Y-%m-%d")
        parts = data.time.split(":")
        fire_dt = tz.localize(fire_date.replace(hour=int(parts[0]), minute=int(parts[1]), second=0))
        fire_utc = fire_dt.astimezone(pytz.UTC).replace(tzinfo=None)
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid date/time format")

    end_date = None
    if data.end_date:
        try:
            ed = datetime.strptime(data.end_date, "%Y-%m-%d")
            end_date = tz.localize(ed).astimezone(pytz.UTC).replace(tzinfo=None)
        except ValueError:
            pass

    async with async_session() as session:
        fire_times_str = ",".join(data.fire_times) if data.fire_times else None

        if data.fire_times and len(data.fire_times) > 1:
            # Multi-time: create one reminder per time slot
            for idx, t in enumerate(data.fire_times):
                h, m = map(int, t.split(":"))
                slot_dt = tz.localize(fire_date.replace(hour=h, minute=m, second=0))
                slot_utc = slot_dt.astimezone(pytz.UTC).replace(tzinfo=None)

                r = Reminder(
                    user_id=user_id, title=data.title, category=data.category,
                    next_fire=slot_utc, recurrence=data.recurrence,
                    recurrence_days=data.recurrence_days,
                    fire_times=fire_times_str, end_date=end_date,
                    time_slot_index=idx, time_slot_total=len(data.fire_times),
                )
                session.add(r)
        else:
            r = Reminder(
                user_id=user_id, title=data.title, category=data.category,
                next_fire=fire_utc, recurrence=data.recurrence,
                recurrence_days=data.recurrence_days,
                fire_times=fire_times_str, end_date=end_date,
            )
            session.add(r)

        await session.commit()

    return {"ok": True, "message": f"{data.title} creato!"}


@app.put("/api/reminders/{reminder_id}")
async def update_reminder(
    reminder_id: int,
    data: ReminderUpdate,
    user: dict = Depends(get_current_user),
):
    """Update an existing reminder."""
    user_id = user["id"]
    tz = await _get_user_tz(user_id)

    async with async_session() as session:
        r = await session.get(Reminder, reminder_id)
        if not r or r.user_id != user_id:
            raise HTTPException(status_code=404, detail="Not found")

        if data.title:
            r.title = data.title
        if data.category:
            r.category = data.category

        if data.date or data.time:
            old_fire = pytz.UTC.localize(r.next_fire).astimezone(tz)
            new_dt = old_fire
            if data.date:
                d = datetime.strptime(data.date, "%Y-%m-%d")
                new_dt = new_dt.replace(year=d.year, month=d.month, day=d.day)
            if data.time:
                parts = data.time.split(":")
                new_dt = new_dt.replace(hour=int(parts[0]), minute=int(parts[1]))
            r.next_fire = new_dt.astimezone(pytz.UTC).replace(tzinfo=None)

        await session.commit()
        return {"ok": True, "reminder": _reminder_to_out(r, tz)}


@app.delete("/api/reminders/{reminder_id}")
async def delete_reminder(
    reminder_id: int,
    user: dict = Depends(get_current_user),
):
    """Cancel a reminder."""
    user_id = user["id"]

    async with async_session() as session:
        r = await session.get(Reminder, reminder_id)
        if not r or r.user_id != user_id:
            raise HTTPException(status_code=404, detail="Not found")

        r.status = ReminderStatus.CANCELLED
        log = ReminderLog(user_id=user_id, reminder_id=r.id, action="cancelled")
        session.add(log)
        await session.commit()

    return {"ok": True}


@app.post("/api/reminders/{reminder_id}/done")
async def mark_done(
    reminder_id: int,
    user: dict = Depends(get_current_user),
):
    """Mark a reminder as done."""
    from services.scheduler import reschedule_reminder

    user_id = user["id"]

    async with async_session() as session:
        r = await session.get(Reminder, reminder_id)
        if not r or r.user_id != user_id:
            raise HTTPException(status_code=404, detail="Not found")

        log = ReminderLog(user_id=user_id, reminder_id=r.id, action="done")
        session.add(log)
        await reschedule_reminder(r, session)
        await session.commit()

    return {"ok": True}


@app.get("/api/calendar/{year}/{month}")
async def calendar_month(
    year: int,
    month: int,
    user: dict = Depends(get_current_user),
):
    """Get reminders grouped by day for a calendar month."""
    user_id = user["id"]
    tz = await _get_user_tz(user_id)

    start = tz.localize(datetime(year, month, 1))
    if month == 12:
        end = tz.localize(datetime(year + 1, 1, 1))
    else:
        end = tz.localize(datetime(year, month + 1, 1))

    start_utc = start.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = end.astimezone(pytz.UTC).replace(tzinfo=None)

    async with async_session() as session:
        stmt = select(Reminder).where(
            and_(
                Reminder.user_id == user_id,
                Reminder.status == ReminderStatus.ACTIVE,
                Reminder.next_fire >= start_utc,
                Reminder.next_fire < end_utc,
            )
        ).order_by(Reminder.next_fire)
        result = await session.execute(stmt)
        reminders = result.scalars().all()

    # Group by day
    days = {}
    for r in reminders:
        if r.time_slot_index and r.time_slot_index > 0:
            continue
        fire_local = pytz.UTC.localize(r.next_fire).astimezone(tz)
        day_key = fire_local.day
        if day_key not in days:
            days[day_key] = []
        days[day_key].append(_reminder_to_out(r, tz).dict())

    return {"year": year, "month": month, "days": days}


@app.get("/api/stats")
async def get_stats(user: dict = Depends(get_current_user)):
    """Get completion stats and streaks."""
    user_id = user["id"]
    tz = await _get_user_tz(user_id)
    now = datetime.now(tz)

    async with async_session() as session:
        # Total active
        stmt = select(func.count()).where(
            and_(Reminder.user_id == user_id, Reminder.status == ReminderStatus.ACTIVE)
        )
        total_active = (await session.execute(stmt)).scalar() or 0

        # Completed today
        today_start = now.replace(hour=0, minute=0, second=0).astimezone(pytz.UTC).replace(tzinfo=None)
        stmt = select(func.count()).where(
            and_(
                ReminderLog.user_id == user_id,
                ReminderLog.action == "done",
                ReminderLog.created_at >= today_start,
            )
        )
        completed_today = (await session.execute(stmt)).scalar() or 0

        # Completed this week
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0
        ).astimezone(pytz.UTC).replace(tzinfo=None)
        stmt = select(func.count()).where(
            and_(
                ReminderLog.user_id == user_id,
                ReminderLog.action == "done",
                ReminderLog.created_at >= week_start,
            )
        )
        completed_week = (await session.execute(stmt)).scalar() or 0

        # Total actions this week (for completion rate)
        stmt = select(func.count()).where(
            and_(
                ReminderLog.user_id == user_id,
                ReminderLog.action.in_(["done", "skipped", "snoozed"]),
                ReminderLog.created_at >= week_start,
            )
        )
        total_week = (await session.execute(stmt)).scalar() or 0
        completion_rate = round((completed_week / total_week * 100) if total_week > 0 else 0, 1)

        # Streak: consecutive days with at least one "done"
        streak = 0
        check_date = now.replace(hour=0, minute=0, second=0)
        for i in range(60):  # Max 60 day streak
            day_start = (check_date - timedelta(days=i)).astimezone(pytz.UTC).replace(tzinfo=None)
            day_end = day_start + timedelta(days=1)
            stmt = select(func.count()).where(
                and_(
                    ReminderLog.user_id == user_id,
                    ReminderLog.action == "done",
                    ReminderLog.created_at >= day_start,
                    ReminderLog.created_at < day_end,
                )
            )
            count = (await session.execute(stmt)).scalar() or 0
            if count > 0:
                streak += 1
            else:
                if i > 0:  # Allow today to have no completions yet
                    break

        # By category
        stmt = select(Reminder.category, func.count()).where(
            and_(Reminder.user_id == user_id, Reminder.status == ReminderStatus.ACTIVE)
        ).group_by(Reminder.category)
        result = await session.execute(stmt)
        by_category = {row[0] or "generic": row[1] for row in result.all()}

    return StatsOut(
        total_active=total_active,
        completed_today=completed_today,
        completed_week=completed_week,
        streak_days=streak,
        completion_rate_week=completion_rate,
        by_category=by_category,
    )


# ─────────────────────────────────────────────
# Serve static frontend
# ─────────────────────────────────────────────

def _find_dist_dir():
    """Find the webapp/dist directory, trying multiple paths."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "dist"),
        os.path.join(os.getcwd(), "webapp", "dist"),
        "/app/webapp/dist",
    ]
    for path in candidates:
        if os.path.exists(path):
            logger.info(f"Found webapp dist at: {path}")
            return path
    logger.warning(f"webapp/dist not found. Tried: {candidates}")
    return None

STATIC_DIR = _find_dist_dir()

if STATIC_DIR:
    assets_dir = os.path.join(STATIC_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

@app.get("/")
async def serve_index():
    if STATIC_DIR:
        index_path = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
    return {"error": "Frontend not built", "hint": "Run: cd webapp && npm install && npm run build"}

# Catch-all for SPA client-side routing
@app.get("/{path:path}")
async def spa_catchall(path: str):
    if STATIC_DIR:
        # Try serving the exact file first
        file_path = os.path.join(STATIC_DIR, path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # Otherwise serve index.html (SPA routing)
        index_path = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Not found")
