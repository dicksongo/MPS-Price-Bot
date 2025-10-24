import os, re, asyncpg, httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel

# ===== settings =====
BOT_TOKEN   = os.environ["BOT_TOKEN"]          # e.g. 123456:ABC...
DATABASE_URL= os.environ["DATABASE_URL"]       # Supabase Postgres URI
ALLOWED_IDS = set(int(x) for x in os.getenv("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ===== app + db pool =====
app = FastAPI()
_pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global _pool
    _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=4)

async def db_fetch(sql: str, *params):
    async with _pool.acquire() as conn:
        return await conn.fetch(sql, *params)

# ===== core lookup (reads your DataObat) =====
SQL_LOOKUP = """
select "name" as name, "pack size" as pack, price
from "DataObat"
where lower("name") like '%'||$1||'%'
  and ($2 = '' or "pack size" ilike '%'||$2||'%')
order by "name", "pack size"
limit 5;
"""

async def find_prices(qname: str, qpack: str = ""):
    rows = await db_fetch(SQL_LOOKUP, qname.lower().strip(), (qpack or "").strip())
    return [dict(r) for r in rows]

# ===== Telegram webhook =====
@app.post("/telegram/webhook")
async def telegram_webhook(update: dict):
    msg = update.get("message") or {}
    chat    = msg.get("chat") or {}
    user    = msg.get("from") or {}
    chat_id = chat.get("id")
    uid     = user.get("id")
    text    = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return {"ok": True}

    # simple RBAC (optional): allowlist specific Telegram user IDs
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        await send(chat_id, "Access restricted. Ask admin to allow your Telegram ID.")
        return {"ok": True}

    # Command: "price <name> [pack <pack text>]"
    m = re.match(r"^/?price\s+(.+?)(?:\s+pack\s+(.+))?$", text, flags=re.I)
    if not m:
        await send(chat_id, "Usage:\nprice <name> [pack <text>]\nExample: price Vita Stress pack 100g")
        return {"ok": True}

    name, pack = m.group(1), (m.group(2) or "")
    rows = await find_prices(name, pack)
    if not rows:
        await send(chat_id, "No match. Try a simpler name or include pack (e.g., 100g / 250g / Box).")
        return {"ok": True}

    lines = [f"• {r['name']} — {r['pack']}: IDR {int(r['price']):,}".replace(",", ".") for r in rows]
    await send(chat_id, "\n".join(lines) + "\n— product info only; not medical advice.")
    return {"ok": True}

async def send(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})

# Health check
@app.get("/")
def health():
    return {"ok": True}
