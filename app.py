import os, re, asyncpg, httpx
from fastapi import FastAPI
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

# ===== core lookup (FUZZY first, then fallback) =====
SQL_LOOKUP_FUZZY = """
select
  "name" as name,
  "pack size" as pack,
  price,
  similarity("name", $1) as score
from "DataObat"
where
  similarity("name", $1) >= $3
  and ($2 = '' or "pack size" ilike '%'||$2||'%')
order by
  similarity("name", $1) desc,
  "name"
limit 5;
"""

SQL_LOOKUP_SUBSTRING = """
select "name" as name, "pack size" as pack, price
from "DataObat"
where ( "name" ilike '%'||$1||'%' or "SKU" ilike '%'||$1||'%' )
  and ($2 = '' or "pack size" ilike '%'||$2||'%')
order by "name", "pack size"
limit 5;
"""

async def find_prices(qname: str, qpack: str = "", threshold: float = 0.30):
    qname = qname.strip()
    qpack = (qpack or "").strip()
    # Try fuzzy (requires pg_trgm extension)
    try:
        rows = await db_fetch(SQL_LOOKUP_FUZZY, qname, qpack, threshold)
        rows = [dict(r) for r in rows]
    except Exception:
        rows = []
    # Fallback to substring if nothing (or extension missing)
    if not rows:
        rows = [dict(r) for r in await db_fetch(SQL_LOOKUP_SUBSTRING, qname, qpack)]
    return rows

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

    # Command: "harga <name> [pack <pack text>]"
    m = re.match(r"^/?harga\s+(.+?)(?:\s+pack\s+(.+))?$", text, flags=re.I)
    if not m:
        await send(chat_id, "Format:\nharga <nama> [pack <teks>]\nContoh: harga Vita Stress pack 100g")
        return {"ok": True}

    name, pack = m.group(1), (m.group(2) or "")
    rows = await find_prices(name, pack)
    if not rows:
        await send(chat_id, "Tidak ditemukan. Coba nama lebih sederhana atau sertakan pack (mis. 100g / 250g / Box).")
        return {"ok": True}

    lines = []
    for r in rows:
        formatted_price = "Rp" + format(int(r['price']), ",").replace(",", ".")
        lines.append(f"• {r['name']} — {r['pack']}: {formatted_price}")

    await send(chat_id, "\n".join(lines))
    return {"ok": True}

async def send(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})

# Health check
@app.get("/")
def health():
    return {"ok": True}
