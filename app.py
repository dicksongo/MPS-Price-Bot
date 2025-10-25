import os, re, asyncpg, httpx
from fastapi import FastAPI

# ===== settings =====
BOT_TOKEN    = os.environ["BOT_TOKEN"]          # e.g. 123456:ABC...
DATABASE_URL = os.environ["DATABASE_URL"]       # Supabase Postgres URI (include ?sslmode=require)
ALLOWED_IDS  = set(int(x) for x in os.getenv("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())

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

# ===== helpers =====
PAGE_SIZE = 5

def rupiah(x: int | float) -> str:
    return "Rp" + format(int(x), ",").replace(",", ".")

def mdv2_escape(s: str) -> str:
    # Escape Telegram MarkdownV2 reserved characters
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', s or "")

async def send(chat_id: int, text: str, parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/sendMessage", json=payload)

async def send_with_keyboard(chat_id: int, text: str, buttons: list[list[dict]]):
    payload = {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": buttons}}
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/sendMessage", json=payload)

async def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup: payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/editMessageText", json=payload)

async def send_photo(chat_id: int, photo_url: str, caption: str, parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption}
    if parse_mode: payload["parse_mode"] = parse_mode
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/sendPhoto", json=payload)

async def answer_callback(callback_query_id: str):
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": callback_query_id})

async def send_long_text(chat_id: int, text: str, parse_mode: str | None = None):
    MAX = 4000  # safety below Telegram ~4096 cap
    while text:
        cut = text.rfind("\n", 0, MAX)
        if cut == -1 or cut < MAX * 0.6:
            cut = min(len(text), MAX)
        chunk, text = text[:cut], text[cut:]
        await send(chat_id, chunk, parse_mode=parse_mode)

# ===== SQL: catalog + detail (matches your Supabase column names) =====
SQL_LIST_PRODUCTS = """
select id,
       "nama"            as name,
       "kemasan"         as pack,
       price,
       coalesce("Kategori",'')    as category,
       coalesce("Sub-kategor",'') as subcategory
from "DataObat"
where ($1 = '' or lower("nama") ilike '%'||lower($1)||'%')
  and ($2 = '' or lower("Kategori") = lower($2))
order by lower("nama"), "kemasan"
limit $3 offset $4;
"""

SQL_COUNT_PRODUCTS = """
select count(*)::int as cnt
from "DataObat"
where ($1 = '' or lower("nama") ilike '%'||lower($1)||'%')
  and ($2 = '' or lower("Kategori") = lower($2));
"""

SQL_PRODUCT_DETAIL = """
select id,
       "SKU"           as sku,
       "nama"          as name,
       "kemasan"       as pack,
       price,
       "Kategori"      as category,
       "Sub-kategor"   as subcategory,
       "Fungsi"        as function,
       "Deskripsi"     as description,
       "Indikasi"      as indications,
       "Aturan paka"   as dosage,
       "URL"           as url,
       "Image URL"     as image_url
from "DataObat"
where id = $1;
"""

# ===== SQL: price search (/harga) adapted to nama/kemasan =====
SQL_LOOKUP_FUZZY = """
select
  "nama"     as name,
  "kemasan"  as pack,
  price,
  similarity("nama", $1) as score
from "DataObat"
where similarity("nama", $1) >= $3
  and ($2 = '' or "kemasan" ilike '%'||$2||'%')
order by similarity("nama", $1) desc, "nama"
limit 5;
"""

SQL_LOOKUP_SUBSTRING = """
select "nama" as name, "kemasan" as pack, price
from "DataObat"
where ( "nama" ilike '%'||$1||'%' or "SKU" ilike '%'||$1||'%' )
  and ($2 = '' or "kemasan" ilike '%'||$2||'%')
order by "nama", "kemasan"
limit 5;
"""

async def find_prices(qname: str, qpack: str = "", threshold: float = 0.30):
    qname = (qname or "").strip()
    qpack = (qpack or "").strip()
    try:
        rows = await db_fetch(SQL_LOOKUP_FUZZY, qname, qpack, threshold)
        rows = [dict(r) for r in rows]
    except Exception:
        rows = []
    if not rows:
        rows = [dict(r) for r in await db_fetch(SQL_LOOKUP_SUBSTRING, qname, qpack)]
    return rows

# ===== Catalog + detail functions =====
async def list_products(q: str = "", category: str = "", page: int = 1, page_size: int = PAGE_SIZE):
    total = (await db_fetch(SQL_COUNT_PRODUCTS, q, category))[0]["cnt"]
    offset = (page - 1) * page_size
    rows = [dict(r) for r in await db_fetch(SQL_LIST_PRODUCTS, q, category, page_size, offset)]
    return total, rows

async def send_catalog(chat_id: int, q: str = "", category: str = "", page: int = 1):
    total, rows = await list_products(q, category, page)
    if not rows:
        await send(chat_id, "Tidak ada produk untuk filter tersebut.")
        return

    start = (page - 1) * PAGE_SIZE + 1
    lines = [f"{i}. {r['name']} — {r['pack']} — {rupiah(r['price'])}"
             for i, r in enumerate(rows, start=start)]

    header = []
    if q: header.append(f"filter nama: {q}")
    if category: header.append(f"kategori: {category}")
    body = (f"Daftar Produk ({' | '.join(header)})\n" if header else "Daftar Produk\n") + "\n".join(lines)

    # Detail buttons + pagination
    buttons: list[list[dict]] = []
    buttons.append([{"text": f"Detail {idx}", "callback_data": f"product:{r['id']}"}
                    for idx, r in enumerate(rows, start=start)])
    last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = []
    if page > 1: nav.append({"text":"« Prev","callback_data":f"page:{page-1}:{q}:{category}"})
    nav.append({"text": f"{page}/{last_page}", "callback_data":"noop"})
    if page < last_page: nav.append({"text":"Next »","callback_data":f"page:{page+1}:{q}:{category}"})
    buttons.append(nav)

    await send_with_keyboard(chat_id, body, buttons)

async def edit_message_with_catalog(cb, page: int, q: str, category: str):
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    total, rows = await list_products(q, category, page)
    if not rows:
        await edit_message_text(chat_id, message_id, "Tidak ada produk untuk filter tersebut.")
        return

    start = (page - 1) * PAGE_SIZE + 1
    lines = [f"{i}. {r['name']} — {r['pack']} — {rupiah(r['price'])}"
             for i, r in enumerate(rows, start=start)]
    header = []
    if q: header.append(f"filter nama: {q}")
    if category: header.append(f"kategori: {category}")
    body = (f"Daftar Produk ({' | '.join(header)})\n" if header else "Daftar Produk\n") + "\n".join(lines)

    buttons: list[list[dict]] = []
    buttons.append([{"text": f"Detail {idx}", "callback_data": f"product:{r['id']}"}
                    for idx, r in enumerate(rows, start=start)])
    last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = []
    if page > 1: nav.append({"text":"« Prev","callback_data":f"page:{page-1}:{q}:{category}"})
    nav.append({"text": f"{page}/{last_page}", "callback_data":"noop"})
    if page < last_page: nav.append({"text":"Next »","callback_data":f"page:{page+1}:{q}:{category}"})
    buttons.append(nav)

    await edit_message_text(chat_id, message_id, body, {"inline_keyboard": buttons})

async def send_product_detail(chat_id: int, pid: int):
    rows = await db_fetch(SQL_PRODUCT_DETAIL, pid)
    if not rows:
        await send(chat_id, "Produk tidak ditemukan.")
        return
    r = dict(rows[0])

    # caption (short)
    title = f"*{mdv2_escape(r['name'])}*"
    bits = []
    if r.get("pack"):        bits.append(mdv2_escape(r["pack"]))
    if r.get("category"):    bits.append(mdv2_escape(r["category"]))
    if r.get("subcategory"): bits.append(mdv2_escape(r["subcategory"]))
    subtitle = "  •  ".join(bits)
    caption_lines = [title]
    if subtitle: caption_lines.append(subtitle)
    caption_lines.append(f"Harga: *{mdv2_escape(rupiah(r['price']))}*")
    if r.get("sku"): caption_lines.append(f"SKU: {mdv2_escape(r['sku'])}")
    if r.get("url"): caption_lines.append(f"[Info produk]({mdv2_escape(r['url'])})")
    caption = "\n".join(caption_lines)

    # long text
    details = []
    for label, key in [("Fungsi","function"), ("Deskripsi","description"),
                       ("Indikasi","indications"), ("Aturan pakai","dosage")]:
        if r.get(key):
            details.append(f"*{label}:*\n{mdv2_escape(str(r[key]))}")
    long_text = "\n\n".join(details)

    if r.get("image_url"):
        await send_photo(chat_id, r["image_url"], caption, parse_mode="MarkdownV2")
        if long_text:
            await send_long_text(chat_id, long_text, parse_mode="MarkdownV2")
    else:
        await send_long_text(chat_id, caption + ("\n\n"+long_text if long_text else ""), parse_mode="MarkdownV2")

# ===== Telegram webhook =====
@app.post("/telegram/webhook")
async def telegram_webhook(update: dict):
    # Callback queries (inline keyboard)
    cb = update.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data") or ""
        if data.startswith("page:"):
            _, p, q, category = data.split(":", 3)
            await edit_message_with_catalog(cb, int(p), q, category)
        elif data.startswith("product:"):
            _, pid = data.split(":", 1)
            await send_product_detail(chat_id, int(pid))
        await answer_callback(cb["id"])
        return {"ok": True}

    # Normal messages
    msg  = update.get("message") or {}
    chat = msg.get("chat") or {}
    user = msg.get("from") or {}
    chat_id = chat.get("id")
    uid     = user.get("id")
    text    = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return {"ok": True}

    # simple RBAC
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        await send(chat_id, "Access restricted. Ask admin to allow your Telegram ID.")
        return {"ok": True}

    # /produk [kata] [kategori X] [page N]
    m = re.match(r"^/?produk(?:\s+(.*))?$", text, flags=re.I)
    if m:
        args = (m.group(1) or "").strip()
        q = ""
        category = ""
        page = 1
        if args:
            mp = re.search(r"\bpage\s+(\d+)\b", args, flags=re.I)
            if mp: page = max(1, int(mp.group(1)))
            mc = re.search(r"\bkategori\s+(\S+)\b", args, flags=re.I)
            if mc: category = mc.group(1)
            q = re.sub(r"\b(page\s+\d+|kategori\s+\S+)\b", "", args, flags=re.I).strip()
        await send_catalog(chat_id, q, category, page)
        return {"ok": True}

    # /vaksin [kata] [page N]  (Kategori = vaccine)
    m = re.match(r"^/?vaksin(?:\s+(.*))?$", text, flags=re.I)
    if m:
        args = (m.group(1) or "").strip()
        q = ""
        page = 1
        if args:
            mp = re.search(r"\bpage\s+(\d+)\b", args, flags=re.I)
            if mp: page = max(1, int(mp.group(1)))
            q = re.sub(r"\bpage\s+\d+\b", "", args, flags=re.I).strip()
        await send_catalog(chat_id, q, "vaccine", page)
        return {"ok": True}

    # /harga <nama> [pack <teks>]
    m = re.match(r"^/?harga\s+(.+?)(?:\s+pack\s+(.+))?$", text, flags=re.I)
    if m:
        name, pack = m.group(1), (m.group(2) or "")
        rows = await find_prices(name, pack)
        if not rows:
            await send(chat_id, "Tidak ditemukan. Coba nama lebih sederhana atau sertakan pack (mis. 100g / 250g / Box).")
            return {"ok": True}
        lines = [f"• {r['name']} — {r['pack']}: {rupiah(r['price'])}" for r in rows]
        await send(chat_id, "\n".join(lines))
        return {"ok": True}

    # Help
    await send(chat_id,
        "Perintah:\n"
        "/harga <nama> [pack <teks>]\n"
        "/produk [kata] [kategori X] [page N]\n"
        "/vaksin [kata] [page N]\n"
        "Contoh: /produk vita kategori Peternakan page 2"
    )
    return {"ok": True}

# Health check
@app.get("/")
def health():
    return {"ok": True}
