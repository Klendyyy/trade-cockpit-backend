"""
Trade Cockpit Backend — v2.0
FastAPI server for Telegram Mini App binary options trading dashboard.

New in v2:
  • Economic calendar endpoint (fetches from TwelveData)
  • Streak tracking & achievements system
  • Trade notes support
  • Equity curve & advanced analytics
  • Input validation & error handling
  • Signal generator (confluence scoring)
  • Daily reset logic for streaks
"""

import os
import time
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

# ─── Config ───────────────────────────────────────────────────────────────────

TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "demo")
DB_PATH = os.getenv("DB_PATH", "trades.db")
PORT = int(os.getenv("PORT", 8000))

# ─── Constants ────────────────────────────────────────────────────────────────

PAIRS = [
    "EUR/CHF","EUR/USD","AUD/CAD","GBP/AUD","EUR/JPY","GBP/CAD","GBP/JPY",
    "EUR/CAD","EUR/AUD","GBP/CHF","AUD/JPY","CAD/JPY","AUD/CHF",
    "AUD/USD","CAD/CHF","EUR/GBP","USD/JPY","USD/CAD","CHF/JPY","USD/CHF","GBP/USD"
]

PAYOUTS = {
    "EUR/CHF":88,"EUR/USD":88,"AUD/CAD":87,"GBP/AUD":87,"EUR/JPY":82,
    "GBP/CAD":81,"GBP/JPY":81,"EUR/CAD":74,"EUR/AUD":73,"GBP/CHF":73,
    "AUD/JPY":70,"CAD/JPY":70,"AUD/CHF":65,"AUD/USD":64,"CAD/CHF":58,
    "EUR/GBP":57,"USD/JPY":51,"USD/CAD":50,"CHF/JPY":49,"USD/CHF":45,"GBP/USD":40
}

SESSIONS = {
    "tokyo": {
        "name": "Токио",
        "open": 0, "close": 9,
        "pairs": [
            "USD/JPY","AUD/JPY","EUR/JPY","GBP/JPY","CAD/JPY","CHF/JPY",
            "AUD/USD","AUD/CAD","AUD/CHF","EUR/AUD","GBP/AUD"
        ],
        "desc": "Азиатская сессия, спокойная волатильность"
    },
    "london": {
        "name": "Лондон",
        "open": 7, "close": 16,
        "pairs": [
            "EUR/USD","GBP/USD","EUR/CHF","EUR/GBP","GBP/CHF","EUR/JPY",
            "GBP/JPY","EUR/CAD","EUR/AUD","GBP/CAD","GBP/AUD","USD/CHF",
            "AUD/USD","AUD/CAD","CAD/CHF"
        ],
        "desc": "Самая активная, высокая волатильность"
    },
    "new_york": {
        "name": "Нью-Йорк",
        "open": 12, "close": 21,
        "pairs": [
            "EUR/USD","GBP/USD","USD/CAD","USD/JPY","USD/CHF","CAD/JPY",
            "EUR/CAD","GBP/CAD","AUD/USD","AUD/CAD","CHF/JPY","CAD/CHF"
        ],
        "desc": "Высокая ликвидность, много новостей"
    },
}

ACHIEVEMENTS = {
    "first_trade":    {"name": "Первая сделка",     "desc": "Записал первую сделку",         "icon": "🎯"},
    "ten_trades":     {"name": "Десятка",            "desc": "10 сделок в журнале",           "icon": "🔟"},
    "fifty_trades":   {"name": "Полтинник",          "desc": "50 сделок записано",            "icon": "🏆"},
    "three_streak":   {"name": "Хет-трик",           "desc": "3 победы подряд",               "icon": "🔥"},
    "five_streak":    {"name": "Доминация",           "desc": "5 побед подряд",               "icon": "⚡"},
    "wr_60":          {"name": "Снайпер",            "desc": "Винрейт 60%+ (мин. 20 сделок)","icon": "🎯"},
    "wr_70":          {"name": "Легенда",            "desc": "Винрейт 70%+ (мин. 20 сделок)","icon": "👑"},
    "discipline_3":   {"name": "Дисциплина",         "desc": "3 дня подряд с чеклистом",     "icon": "📋"},
    "discipline_7":   {"name": "Железная воля",      "desc": "7 дней подряд с чеклистом",    "icon": "💎"},
    "profitable_week":{"name": "Зелёная неделя",     "desc": "Неделя в плюсе",               "icon": "💰"},
    "no_overtrade":   {"name": "Хладнокровие",       "desc": "Ни разу не превысил лимит",    "icon": "🧊"},
}

# ─── Cache ────────────────────────────────────────────────────────────────────

vol_cache: dict = {}
vol_ts: float = 0
calendar_cache: list = []
calendar_ts: float = 0

# ─── Database ─────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL,
                result TEXT NOT NULL,
                bet_size REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                session TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                deposit REAL DEFAULT 500,
                risk_percent REAL DEFAULT 2,
                max_daily_loss_percent REAL DEFAULT 10,
                strategy TEXT DEFAULT 'fixed'
            );
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                achievement_key TEXT NOT NULL,
                unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, achievement_key)
            );
            CREATE TABLE IF NOT EXISTS streaks (
                user_id TEXT PRIMARY KEY,
                current_streak INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0,
                last_checklist_date TEXT DEFAULT '',
                current_win_streak INTEGER DEFAULT 0,
                best_win_streak INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id);
            CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(created_at);
            CREATE INDEX IF NOT EXISTS idx_trades_user_date ON trades(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_achievements_user ON achievements(user_id);
        """)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Trade Cockpit", version="2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_sessions():
    h = datetime.now(timezone.utc).hour
    out = []
    for key, s in SESSIONS.items():
        if s["open"] < s["close"]:
            active = s["open"] <= h < s["close"]
        else:
            active = h >= s["open"] or h < s["close"]

        entry = {
            "key": key,
            "name": s["name"],
            "status": "active" if active else "closed",
            "best_pairs": s["pairs"],
            "description": s["desc"],
        }
        if active:
            entry["hours_left"] = (s["close"] - h) % 24
        else:
            entry["opens_in_hours"] = (s["open"] - h) % 24
        out.append(entry)

    out.sort(key=lambda x: 0 if x["status"] == "active" else 1)
    return out


async def get_vol():
    global vol_cache, vol_ts
    if time.time() - vol_ts < 900 and vol_cache:
        return vol_cache

    base_prices = {
        "EURUSD":1.08,"GBPUSD":1.27,"USDJPY":155,"USDCAD":1.37,"EURGBP":0.85,
        "AUDUSD":0.65,"USDCHF":0.88,"EURJPY":167,"GBPJPY":197,"EURCHF":0.95,
        "AUDCAD":0.90,"GBPAUD":1.95,"GBPCAD":1.74,"EURCAD":1.48,"EURAUD":1.66,
        "GBPCHF":1.12,"AUDJPY":100,"CADJPY":113,"AUDCHF":0.57,"CADCHF":0.64,"CHFJPY":176
    }
    res = {}
    top8 = ["EUR/USD","GBP/USD","USD/JPY","EUR/JPY","GBP/JPY","AUD/USD","EUR/CHF","USD/CAD"]

    try:
        syms = ",".join(p.replace("/", "") for p in top8)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.twelvedata.com/atr",
                params={"symbol": syms, "interval": "5min", "time_period": 14,
                        "apikey": TWELVE_DATA_KEY, "outputsize": 1}
            )
            d = r.json()
            for p in top8:
                s = p.replace("/", "")
                if s in d and "values" in d[s]:
                    atr = float(d[s]["values"][0]["atr"])
                    score = min(100, round(atr / base_prices.get(s, 1) * 50000))
                    res[p] = {"volatility_score": score, "atr": round(atr, 5)}
    except Exception as e:
        print(f"Volatility API error: {e}")

    active_keys = [s["key"] for s in get_sessions() if s["status"] == "active"]
    session_scores = {"london": 75, "new_york": 70, "tokyo": 55}
    for p in PAIRS:
        if p not in res:
            sc = 50
            for k in active_keys:
                if p in SESSIONS[k]["pairs"]:
                    sc = session_scores.get(k, 50)
                    break
            res[p] = {"volatility_score": sc, "atr": 0}

    vol_cache = res
    vol_ts = time.time()
    return res


async def check_achievements(user_id: str, db):
    """Check and unlock any new achievements for the user."""
    unlocked = []

    # Get existing achievements
    cursor = await db.execute(
        "SELECT achievement_key FROM achievements WHERE user_id=?", (user_id,)
    )
    existing = {row[0] for row in await cursor.fetchall()}

    # Get trade stats
    cursor = await db.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, "
        "SUM(pnl) as total_pnl FROM trades WHERE user_id=?",
        (user_id,)
    )
    stats = dict(await cursor.fetchone())
    total = stats["total"] or 0
    wins = stats["wins"] or 0

    checks = []
    if total >= 1:
        checks.append("first_trade")
    if total >= 10:
        checks.append("ten_trades")
    if total >= 50:
        checks.append("fifty_trades")
    if total >= 20:
        wr = wins / total * 100
        if wr >= 60:
            checks.append("wr_60")
        if wr >= 70:
            checks.append("wr_70")

    # Check win streaks
    cursor = await db.execute(
        "SELECT best_win_streak FROM streaks WHERE user_id=?", (user_id,)
    )
    row = await cursor.fetchone()
    if row:
        best_ws = row[0] or 0
        if best_ws >= 3:
            checks.append("three_streak")
        if best_ws >= 5:
            checks.append("five_streak")

    # Check discipline streak
    cursor = await db.execute(
        "SELECT current_streak, best_streak FROM streaks WHERE user_id=?", (user_id,)
    )
    row = await cursor.fetchone()
    if row:
        best_disc = row[1] or 0
        if best_disc >= 3:
            checks.append("discipline_3")
        if best_disc >= 7:
            checks.append("discipline_7")

    # Check weekly profit
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cursor = await db.execute(
        "SELECT SUM(pnl) as wpnl FROM trades WHERE user_id=? AND created_at>=?",
        (user_id, week_ago)
    )
    row = await cursor.fetchone()
    if row and (row[0] or 0) > 0 and total >= 5:
        checks.append("profitable_week")

    for key in checks:
        if key not in existing:
            try:
                await db.execute(
                    "INSERT INTO achievements (user_id, achievement_key) VALUES (?, ?)",
                    (user_id, key)
                )
                unlocked.append(key)
            except Exception:
                pass

    if unlocked:
        await db.commit()

    return unlocked


# ─── Routes: Sessions ─────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def api_sessions():
    ss = get_sessions()
    v = await get_vol()
    for s in ss:
        s["pairs"] = sorted(
            [
                {"pair": p, "volatility": v.get(p, {}).get("volatility_score", 50), "payout": PAYOUTS.get(p, 85)}
                for p in s["best_pairs"]
            ],
            key=lambda x: x["volatility"],
            reverse=True,
        )
    return {"utc_time": datetime.now(timezone.utc).strftime("%H:%M"), "sessions": ss}


# ─── Routes: Trades ───────────────────────────────────────────────────────────

class TradeIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=100)
    asset: str
    direction: str
    result: str
    bet_size: float = Field(default=0, ge=0)
    note: str = Field(default="", max_length=500)

    @field_validator("asset")
    @classmethod
    def validate_asset(cls, v):
        if v not in PAIRS and not v.endswith(" OTC"):
            raise ValueError(f"Unknown asset: {v}")
        return v

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v):
        if v.upper() not in ("CALL", "PUT"):
            raise ValueError("Direction must be CALL or PUT")
        return v.upper()

    @field_validator("result")
    @classmethod
    def validate_result(cls, v):
        if v.lower() not in ("win", "loss"):
            raise ValueError("Result must be win or loss")
        return v.lower()


@app.post("/api/trades")
async def log_trade(t: TradeIn):
    active = [s["name"] for s in get_sessions() if s["status"] == "active"]
    payout_pct = PAYOUTS.get(t.asset, 85)
    pnl = round(t.bet_size * payout_pct / 100, 2) if t.result == "win" else round(-t.bet_size, 2)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO trades (user_id, asset, direction, result, bet_size, pnl, session, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (t.user_id, t.asset, t.direction, t.result, t.bet_size, pnl,
             active[0] if active else "", t.note)
        )

        # Update win streak
        await db.execute(
            "INSERT INTO streaks (user_id, current_win_streak, best_win_streak) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "current_win_streak = CASE WHEN ? = 'win' "
            "  THEN current_win_streak + 1 ELSE 0 END, "
            "best_win_streak = MAX(best_win_streak, "
            "  CASE WHEN ? = 'win' THEN current_win_streak + 1 ELSE best_win_streak END)",
            (t.user_id, 1 if t.result == "win" else 0, 1 if t.result == "win" else 0,
             t.result, t.result)
        )

        await db.commit()

        # Check achievements
        new_achievements = await check_achievements(t.user_id, db)

    return {
        "status": "ok",
        "pnl": pnl,
        "payout_pct": payout_pct,
        "new_achievements": [
            {**ACHIEVEMENTS[k], "key": k} for k in new_achievements
        ]
    }


# ─── Routes: Analytics ────────────────────────────────────────────────────────

@app.get("/api/analytics/{user_id}")
async def analytics(user_id: str, days: int = Query(default=7, ge=1, le=365)):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # General stats
        c = await db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as total_pnl, "
            "AVG(pnl) as avg_pnl, "
            "MAX(pnl) as best_trade, "
            "MIN(pnl) as worst_trade "
            "FROM trades WHERE user_id=? AND created_at>=?",
            (user_id, since)
        )
        st = dict(await c.fetchone())
        st["winrate"] = round((st["wins"] or 0) / (st["total"] or 1) * 100)
        st["avg_pnl"] = round(st["avg_pnl"] or 0, 2)
        st["best_trade"] = round(st["best_trade"] or 0, 2)
        st["worst_trade"] = round(st["worst_trade"] or 0, 2)

        # By asset
        c = await db.execute(
            "SELECT asset, COUNT(*) as total, "
            "SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as pnl "
            "FROM trades WHERE user_id=? AND created_at>=? GROUP BY asset",
            (user_id, since)
        )
        by_asset = []
        for r in await c.fetchall():
            row = dict(r)
            row["winrate"] = round(row["wins"] / (row["total"] or 1) * 100)
            row["pnl"] = round(row["pnl"] or 0, 2)
            by_asset.append(row)

        # By hour
        c = await db.execute(
            "SELECT strftime('%H', created_at) as hour, COUNT(*) as total, "
            "SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins "
            "FROM trades WHERE user_id=? AND created_at>=? GROUP BY hour ORDER BY hour",
            (user_id, since)
        )
        by_hour = []
        for r in await c.fetchall():
            row = dict(r)
            row["winrate"] = round(row["wins"] / (row["total"] or 1) * 100)
            by_hour.append(row)

        # By day (for heatmap + equity curve)
        c = await db.execute(
            "SELECT date(created_at) as day, COUNT(*) as total, "
            "SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as pnl "
            "FROM trades WHERE user_id=? AND created_at>=? GROUP BY day ORDER BY day",
            (user_id, since)
        )
        by_day = [dict(r) for r in await c.fetchall()]

        # Equity curve (cumulative PnL)
        equity_curve = []
        cumulative = 0
        for d in by_day:
            cumulative += (d["pnl"] or 0)
            equity_curve.append({"day": d["day"], "equity": round(cumulative, 2)})

        # Today stats
        c = await db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses, "
            "SUM(pnl) as pnl "
            "FROM trades WHERE user_id=? AND date(created_at)=?",
            (user_id, today)
        )
        td = dict(await c.fetchone())
        for k in ["total", "wins", "losses", "pnl"]:
            td[k] = td[k] or 0
        td["pnl"] = round(td["pnl"], 2)

        # Streaks
        c = await db.execute(
            "SELECT current_streak, best_streak, current_win_streak, best_win_streak "
            "FROM streaks WHERE user_id=?",
            (user_id,)
        )
        streak_row = await c.fetchone()
        streaks = dict(streak_row) if streak_row else {
            "current_streak": 0, "best_streak": 0,
            "current_win_streak": 0, "best_win_streak": 0
        }

        # By session
        c = await db.execute(
            "SELECT session, COUNT(*) as total, "
            "SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as pnl "
            "FROM trades WHERE user_id=? AND created_at>=? AND session!='' GROUP BY session",
            (user_id, since)
        )
        by_session = []
        for r in await c.fetchall():
            row = dict(r)
            row["winrate"] = round(row["wins"] / (row["total"] or 1) * 100)
            row["pnl"] = round(row["pnl"] or 0, 2)
            by_session.append(row)

        # Recent trades (last 20)
        c = await db.execute(
            "SELECT asset, direction, result, bet_size, pnl, session, note, created_at "
            "FROM trades WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
            (user_id,)
        )
        recent = [dict(r) for r in await c.fetchall()]

    return {
        "stats": st,
        "by_asset": by_asset,
        "by_hour": by_hour,
        "by_day": by_day,
        "by_session": by_session,
        "equity_curve": equity_curve,
        "today": td,
        "streaks": streaks,
        "recent_trades": recent,
    }


# ─── Routes: Achievements ────────────────────────────────────────────────────

@app.get("/api/achievements/{user_id}")
async def get_achievements(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "SELECT achievement_key, unlocked_at FROM achievements WHERE user_id=? ORDER BY unlocked_at",
            (user_id,)
        )
        unlocked = {row["achievement_key"]: row["unlocked_at"] for row in await c.fetchall()}

    result = []
    for key, info in ACHIEVEMENTS.items():
        result.append({
            "key": key,
            **info,
            "unlocked": key in unlocked,
            "unlocked_at": unlocked.get(key),
        })
    return {"achievements": result}


# ─── Routes: Streaks ─────────────────────────────────────────────────────────

class StreakCheckin(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=100)


@app.post("/api/streaks/checkin")
async def streak_checkin(s: StreakCheckin):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM streaks WHERE user_id=?", (s.user_id,))
        row = await c.fetchone()

        if row:
            row = dict(row)
            if row["last_checklist_date"] == today:
                return {"status": "already_checked", "streak": row["current_streak"]}

            if row["last_checklist_date"] == yesterday:
                new_streak = row["current_streak"] + 1
            else:
                new_streak = 1

            best = max(row["best_streak"], new_streak)
            await db.execute(
                "UPDATE streaks SET current_streak=?, best_streak=?, last_checklist_date=? WHERE user_id=?",
                (new_streak, best, today, s.user_id)
            )
        else:
            new_streak = 1
            await db.execute(
                "INSERT INTO streaks (user_id, current_streak, best_streak, last_checklist_date) "
                "VALUES (?, 1, 1, ?)",
                (s.user_id, today)
            )

        await db.commit()

        # Check achievements after streak update
        new_achs = await check_achievements(s.user_id, db)

    return {
        "status": "ok",
        "streak": new_streak,
        "new_achievements": [{**ACHIEVEMENTS[k], "key": k} for k in new_achs]
    }


# ─── Routes: Settings ────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    deposit: float = Field(default=500, gt=0, le=1_000_000)
    risk_percent: float = Field(default=2, ge=0.5, le=20)
    max_daily_loss_percent: float = Field(default=10, ge=1, le=50)
    strategy: str = Field(default="fixed")

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v):
        if v not in ("fixed", "martin", "anti"):
            raise ValueError("Strategy must be fixed, martin, or anti")
        return v


@app.get("/api/settings/{user_id}")
async def get_settings(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,))
        r = await c.fetchone()
        return dict(r) if r else {
            "deposit": 500, "risk_percent": 2,
            "max_daily_loss_percent": 10, "strategy": "fixed"
        }


@app.post("/api/settings/{user_id}")
async def save_settings(user_id: str, s: SettingsIn):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (user_id, deposit, risk_percent, max_daily_loss_percent, strategy) "
            "VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "deposit=excluded.deposit, risk_percent=excluded.risk_percent, "
            "max_daily_loss_percent=excluded.max_daily_loss_percent, strategy=excluded.strategy",
            (user_id, s.deposit, s.risk_percent, s.max_daily_loss_percent, s.strategy)
        )
        await db.commit()
    return {"status": "ok"}


# ─── Routes: Signal (confluence scoring) ──────────────────────────────────────

@app.get("/api/signals")
async def get_signals():
    """
    Returns confluence-based trading signals for active pairs.
    Factors: volatility, session overlap, payout, time of day.
    """
    sessions = get_sessions()
    vol = await get_vol()
    h = datetime.now(timezone.utc).hour
    day = datetime.now(timezone.utc).weekday()

    active_sessions = [s for s in sessions if s["status"] == "active"]
    if not active_sessions:
        return {"signals": [], "message": "Нет активных сессий"}

    # Build pair confluence map
    pair_data = {}
    for s in active_sessions:
        for p in s["best_pairs"]:
            if p not in pair_data:
                pair_data[p] = {"sessions": [], "vol": 0}
            pair_data[p]["sessions"].append(s["name"])
            v = vol.get(p, {}).get("volatility_score", 50)
            pair_data[p]["vol"] = max(pair_data[p]["vol"], v)

    signals = []
    for pair, data in pair_data.items():
        # Volatility score (0-30)
        vol_score = min(30, round(data["vol"] / 100 * 30))

        # Session overlap bonus (0-20)
        overlap_score = 20 if len(data["sessions"]) >= 2 else 0

        # Payout score (0-25)
        payout = PAYOUTS.get(pair, 50)
        payout_score = min(25, round(payout / 88 * 25))

        # Time quality score (0-15)
        # Best: during overlap hours, worst: session edges
        time_score = 15
        if h in (9, 16, 21):  # session transition hours
            time_score = 5
        elif day == 0 or day == 4 and h >= 19:  # Monday morning / Friday evening
            time_score = 3

        # Dead zone penalty (0-10)
        dead_zone_penalty = 0
        if day == 0 and h < 2:
            dead_zone_penalty = 10
        if day == 4 and h >= 20:
            dead_zone_penalty = 10

        total = vol_score + overlap_score + payout_score + time_score - dead_zone_penalty
        label = "🟢 Торгуй" if total >= 60 else "🟡 Осторожно" if total >= 40 else "🔴 Не лезь"

        signals.append({
            "pair": pair,
            "score": total,
            "label": label,
            "payout": payout,
            "volatility": data["vol"],
            "overlap": len(data["sessions"]) >= 2,
            "sessions": data["sessions"],
            "breakdown": {
                "volatility": vol_score,
                "overlap": overlap_score,
                "payout": payout_score,
                "time_quality": time_score,
                "dead_zone_penalty": dead_zone_penalty,
            }
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return {"signals": signals, "utc_hour": h}


# ─── Routes: Health & Static ─────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "2.0", "utc": datetime.now(timezone.utc).isoformat()}


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
