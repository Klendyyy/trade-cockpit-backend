import os, time, asyncio
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from pathlib import Path

import httpx, aiosqlite
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "demo")
DB_PATH = os.getenv("DB_PATH", "trades.db")
PORT = int(os.getenv("PORT", 8000))

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
    "tokyo": {"name":"Токио", "open":0, "close":9, "pairs":[
        "USD/JPY","AUD/JPY","EUR/JPY","GBP/JPY","CAD/JPY","CHF/JPY",
        "AUD/USD","AUD/CAD","AUD/CHF","EUR/AUD","GBP/AUD"
    ], "desc":"Азиатская сессия, спокойная волатильность"},
    "london": {"name":"Лондон", "open":7, "close":16, "pairs":[
        "EUR/USD","GBP/USD","EUR/CHF","EUR/GBP","GBP/CHF","EUR/JPY",
        "GBP/JPY","EUR/CAD","EUR/AUD","GBP/CAD","GBP/AUD","USD/CHF",
        "AUD/USD","AUD/CAD","CAD/CHF"
    ], "desc":"Самая активная, высокая волатильность"},
    "new_york": {"name":"Нью-Йорк", "open":12, "close":21, "pairs":[
        "EUR/USD","GBP/USD","USD/CAD","USD/JPY","USD/CHF","CAD/JPY",
        "EUR/CAD","GBP/CAD","AUD/USD","AUD/CAD","CHF/JPY","CAD/CHF"
    ], "desc":"Высокая ликвидность, много новостей"},
}

vol_cache = {}; vol_ts = 0

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, asset TEXT, direction TEXT, result TEXT, bet_size REAL DEFAULT 0, pnl REAL DEFAULT 0, session TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE TABLE IF NOT EXISTS user_settings (user_id TEXT PRIMARY KEY, deposit REAL DEFAULT 500, risk_percent REAL DEFAULT 2, max_daily_loss_percent REAL DEFAULT 10, strategy TEXT DEFAULT 'fixed')")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tu ON trades(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_td ON trades(created_at)")
        await db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db(); yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def get_sessions():
    h = datetime.now(timezone.utc).hour; out = []
    for k, s in SESSIONS.items():
        active = s["open"] <= h < s["close"] if s["open"] < s["close"] else (h >= s["open"] or h < s["close"])
        r = {"key":k, "name":s["name"], "status":"active" if active else "closed", "best_pairs":s["pairs"], "description":s["desc"]}
        r["hours_left" if active else "opens_in_hours"] = ((s["close"] if active else s["open"]) - h) % 24
        out.append(r)
    out.sort(key=lambda x: 0 if x["status"]=="active" else 1); return out

async def get_vol():
    global vol_cache, vol_ts
    if time.time()-vol_ts < 300 and vol_cache: return vol_cache
    bp = {
        "EURUSD":1.08,"GBPUSD":1.27,"USDJPY":155,"USDCAD":1.37,"EURGBP":0.85,
        "AUDUSD":0.65,"USDCHF":0.88,"EURJPY":167,"GBPJPY":197,"EURCHF":0.95,
        "AUDCAD":0.90,"GBPAUD":1.95,"GBPCAD":1.74,"EURCAD":1.48,"EURAUD":1.66,
        "GBPCHF":1.12,"AUDJPY":100,"CADJPY":113,"AUDCHF":0.57,"CADCHF":0.64,"CHFJPY":176
    }
    res = {}
    # Запрашиваем двумя батчами по 8 пар (чтобы уложиться в лимит)
    batches = [PAIRS[:8], PAIRS[8:16]]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for batch in batches:
                syms = ",".join(p.replace("/","") for p in batch)
                r = await c.get("https://api.twelvedata.com/atr", params={"symbol":syms,"interval":"5min","time_period":14,"apikey":TWELVE_DATA_KEY,"outputsize":1})
                d = r.json()
                for p in batch:
                    s = p.replace("/","")
                    if s in d and "values" in d[s]:
                        atr = float(d[s]["values"][0]["atr"]); res[p] = {"volatility_score": min(100, round(atr/bp.get(s,1)*50000)), "atr": round(atr,5)}
    except Exception as e: print(f"API err: {e}")
    ak = [s["key"] for s in get_sessions() if s["status"]=="active"]
    for p in PAIRS:
        if p not in res:
            sc = 50
            for k in ak:
                if p in SESSIONS[k]["pairs"]: sc = {"london":75,"new_york":70,"tokyo":55}.get(k,50); break
            res[p] = {"volatility_score":sc, "atr":0}
    vol_cache = res; vol_ts = time.time(); return res

@app.get("/api/sessions")
async def api_sessions():
    ss = get_sessions(); v = await get_vol()
    for s in ss:
        s["pairs"] = sorted([{"pair":p,"volatility":v.get(p,{}).get("volatility_score",50),"payout":PAYOUTS.get(p,85)} for p in s["best_pairs"]], key=lambda x:x["volatility"], reverse=True)
    return {"utc_time":datetime.now(timezone.utc).strftime("%H:%M"), "sessions":ss}

class TradeIn(BaseModel):
    user_id:str; asset:str; direction:str; result:str; bet_size:float=0

@app.post("/api/trades")
async def log_trade(t: TradeIn):
    active = [s["name"] for s in get_sessions() if s["status"]=="active"]
    pnl = round(t.bet_size * PAYOUTS.get(t.asset,85)/100, 2) if t.result=="win" else round(-t.bet_size, 2)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO trades (user_id,asset,direction,result,bet_size,pnl,session) VALUES (?,?,?,?,?,?,?)",
            (t.user_id, t.asset, t.direction, t.result, t.bet_size, pnl, active[0] if active else ""))
        await db.commit()
    return {"status":"ok","pnl":pnl}

@app.get("/api/analytics/{user_id}")
async def analytics(user_id:str, days:int=7):
    since = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT COUNT(*) as total, SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, SUM(pnl) as total_pnl FROM trades WHERE user_id=? AND created_at>=?", (user_id,since))
        st = dict(await c.fetchone()); st["winrate"] = round((st["wins"] or 0)/(st["total"] or 1)*100)
        c = await db.execute("SELECT asset,COUNT(*) as total,SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,SUM(pnl) as pnl FROM trades WHERE user_id=? AND created_at>=? GROUP BY asset",(user_id,since))
        ba = [{**dict(r),"winrate":round(dict(r)["wins"]/(dict(r)["total"] or 1)*100)} for r in await c.fetchall()]
        c = await db.execute("SELECT strftime('%H',created_at) as hour,COUNT(*) as total,SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins FROM trades WHERE user_id=? AND created_at>=? GROUP BY hour ORDER BY hour",(user_id,since))
        bh = [{**dict(r),"winrate":round(dict(r)["wins"]/(dict(r)["total"] or 1)*100)} for r in await c.fetchall()]
        c = await db.execute("SELECT date(created_at) as day,COUNT(*) as total,SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,SUM(pnl) as pnl FROM trades WHERE user_id=? AND created_at>=? GROUP BY day ORDER BY day",(user_id,since))
        bd = [dict(r) for r in await c.fetchall()]
        c = await db.execute("SELECT COUNT(*) as total,SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses,SUM(pnl) as pnl FROM trades WHERE user_id=? AND date(created_at)=?",(user_id,today))
        td = dict(await c.fetchone()); [td.__setitem__(k, td[k] or 0) for k in ["total","wins","losses","pnl"]]
    return {"stats":st,"by_asset":ba,"by_hour":bh,"by_day":bd,"today":td}

class SettingsIn(BaseModel):
    deposit:float=500; risk_percent:float=2; max_daily_loss_percent:float=10; strategy:str="fixed"

@app.get("/api/settings/{user_id}")
async def get_settings(user_id:str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM user_settings WHERE user_id=?",(user_id,))
        r = await c.fetchone()
        return dict(r) if r else {"deposit":500,"risk_percent":2,"max_daily_loss_percent":10,"strategy":"fixed"}

@app.post("/api/settings/{user_id}")
async def save_settings(user_id:str, s:SettingsIn):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO user_settings (user_id,deposit,risk_percent,max_daily_loss_percent,strategy) VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET deposit=excluded.deposit,risk_percent=excluded.risk_percent,max_daily_loss_percent=excluded.max_daily_loss_percent,strategy=excluded.strategy",
            (user_id,s.deposit,s.risk_percent,s.max_daily_loss_percent,s.strategy))
        await db.commit()
    return {"status":"ok"}

@app.get("/api/health")
async def health():
    return {"status":"ok"}

# ═══ MINI APP ═══
@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")

if __name__ == "__main__":
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=PORT)
