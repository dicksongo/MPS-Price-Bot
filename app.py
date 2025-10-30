import os, re, ssl, asyncio, asyncpg, httpx, certifi
from fastapi import FastAPI, Request

# =========================
# Settings / Environment
# =========================
BOT_TOKEN    = os.environ["BOT_TOKEN"]                    # e.g. 123456:ABC...
DATABASE_URL = os.environ["DATABASE_URL"]                 # Supabase session pooler URL + ?sslmode=require
ALLOWED_IDS  = set(int(x) for x in os.getenv("ALLOWED_TELEGRAM_IDS","").split(",") if x.strip())

# TLS controls for DB
DB_SSL_VERIFY = os.getenv("DB_SSL_VERIFY", "1")           # "1" verify (recommended), "0" disable verification
DB_SSL_MODE   = os.getenv("DB_SSL_MODE", "require")       # "require" | "disable"

# Category used by /vaksin shortcut
VACCINE_CATEGORY = os.getenv("VACCINE_CATEGORY", "vaccine")  # set to "Vaksin" if your data uses Indonesian

# Public URL for webhook; Render provides RENDER_EXTERNAL_URL at runtime
PUBLIC_URL   = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL  = (PUBLIC_URL + WEBHOOK_PATH) if PUBLIC_URL else None

# Optional secret so only Telegram hits the webhook
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =========================
# App + DB Pool + HTTP client
# =========================
app = FastAPI()
_pool: asyncpg.Pool | None = None
_http: httpx.AsyncClient | None = None

def _ssl_ctx():
    """Create SSL context that trusts public CAs (via certifi)."""
    if DB_SSL_MODE.lower() == "disable":
        return None
    ctx = ssl.create_default_context(cafile=certifi.where())
    if DB_SSL_VERIFY == "0":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx

async def _create_pool(dsn: str, retries: int = 6, delay: float = 1.0):
    last = None
    for _ in range(retries):
        try:
            if "sslmode=" not in dsn:
                dsn = dsn + (("&" if "?" in dsn else "?") + f"sslmode={DB_SSL_MODE}")
            return await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=4,
                ssl=_ssl_ctx(),
                command_timeout=30,
            )
        except Exception as e:
            last = e
            await asyncio.sleep(delay)
            delay *= 2
    raise last

@app.on_event("startup")
async def startup():
    global _pool, _http
    _pool = await _create_pool(DATABASE_URL)
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(10, connect=5, read=10),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )

@app.on_event("shutdown")
async def shutdown():
    global _http
    if _http:
        await _http.aclose()

async def db_fetch(sql: str, *params):
    async with _pool.acquire() as conn:
        return await conn.fetch(sql, *params)

# =========================
# Helpers
# =========================
PAGE_SIZE = 5

def rupiah(x: int | float) -> str:
    return "Rp" + format(int(x), ",").replace(",", ".")

def mdv2_escape(s: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', s or "")

async def _tg(method: str, payload: dict):
    r = await _http.post(f"{TG_API}/{method}", json=payload)
    try:
        data = r.json()
        ok = bool(data.get("ok"))
    except Exception:
        data, ok = {"raw": r.text}, False
    if not ok:
        print("Telegram error:", method, r.status_code, data)
    return ok, data

async def send(chat_id: int, text: str, parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    await _tg("sendMessage", payload)

async def send_with_keyboard(chat_id: int, text: str, buttons: list[list[dict]], parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": buttons}}
    if parse_mode: payload["parse_mode"] = parse_mode
    await _tg("sendMessage", payload)

async def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup: payload["reply_markup"] = reply_markup
    if parse_mode: payload["parse_mode"] = parse_mode
    await _tg("editMessageText", payload)

async def answer_callback(callback_query_id: str):
    await _tg("answerCallbackQuery", {"callback_query_id": callback_query_id})

async def send_long_text(chat_id: int, text: str, parse_mode: str | None = None):
    MAX = 4000  # under ~4096 cap
    while text:
        cut = text.rfind("\n", 0, MAX)
        if cut == -1 or cut < MAX * 0.6:
            cut = min(len(text), MAX)
        chunk, text = text[:cut], text[cut:]
        await send(chat_id, chunk, parse_mode=parse_mode)

# =========================
# SQL
# =========================
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
       "Aturan pakai"  as dosage,
       "URL"           as url
from "DataObat"
where id = $1;
"""

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

# =========================
# Data Access Helpers
# =========================
async def find_prices(qname: str, qpack: str = "", threshold: float = 0.30):
    qname = (qname or "").strip()
    qpack = (qpack or "").strip()
    try:
        rows = await db_fetch(SQL_LOOKUP_FUZZY, qname, qpack, threshold)
        rows = [dict(r) for r in rows]
    except Exception as e:
        print("Fuzzy search failed, falling back:", e)
        rows = []
    if not rows:
        rows = [dict(r) for r in await db_fetch(SQL_LOOKUP_SUBSTRING, qname, qpack)]
    return rows

async def list_products(q: str = "", category: str = "", page: int = 1, page_size: int = PAGE_SIZE):
    try:
        total = (await db_fetch(SQL_COUNT_PRODUCTS, q, category))[0]["cnt"]
        offset = (page - 1) * page_size
        rows = [dict(r) for r in await db_fetch(SQL_LIST_PRODUCTS, q, category, page_size, offset)]
        return total, rows
    except Exception as e:
        print("DB error list_products:", e)
        return 0, []

# =========================
# Catalog / Detail Senders (text-only)
# =========================
async def send_catalog(chat_id: int, q: str = "", category: str = "", page: int = 1):
    total, rows = await list_products(q, category, page)
    if not rows:
        msg = "Tidak ada produk untuk filter tersebut."
        if category:
            msg += f" (kategori={category})"
        await send(chat_id, msg)
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
    try:
        rows = await db_fetch(SQL_PRODUCT_DETAIL, pid)
    except Exception as e:
        print("DB error product_detail:", e)
        await send(chat_id, "Terjadi kesalahan saat mengambil detail produk.")
        return

    if not rows:
        await send(chat_id, "Produk tidak ditemukan.")
        return

    r = dict(rows[0])

    title = f"*{mdv2_escape(r.get('name',''))}*"
    bits = []
    if r.get("pack"):        bits.append(mdv2_escape(r["pack"]))
    if r.get("category"):    bits.append(mdv2_escape(r["category"]))
    if r.get("subcategory"): bits.append(mdv2_escape(r["subcategory"]))
    subtitle = "  •  ".join(bits)
    caption_lines = [title]
    if subtitle: caption_lines.append(subtitle)
    if r.get("price") is not None:
        caption_lines.append(f"Harga: *{mdv2_escape(rupiah(r['price']))}*")
    if r.get("sku"): caption_lines.append(f"SKU: {mdv2_escape(r['sku'])}")
    caption = "\n".join(caption_lines)

    details = []
    for label, key in [("Fungsi","function"), ("Deskripsi","description"),
                       ("Indikasi","indications"), ("Aturan pakai","dosage")]:
        if r.get(key):
            details.append(f"*{label}:*\n{mdv2_escape(str(r[key]))}")
    long_text = "\n\n".join(details)

    if r.get("url"):
        buttons = [[{"text": "Info produk", "url": r["url"]}]]
        await send_with_keyboard(chat_id, caption + ("\n\n"+long_text if long_text else ""), buttons, parse_mode="MarkdownV2")
    else:
        await send_long_text(chat_id, caption + ("\n\n"+long_text if long_text else ""), parse_mode="MarkdownV2")

# =========================
# Parsing helpers
# =========================
def _extract_arg(pattern: str, text: str) -> str:
    """Return the first group of pattern search or ''."""
    m = re.search(pattern, text, flags=re.I)
    return (m.group(1).strip() if m else "")

def _parse_produk_args(args: str):
    """
    Supports:
      /produk
      /produk vita
      /produk vita page 2
      /produk vita kategori Peternakan
      /produk kategori "Alat Kesehatan" page 3
      (kategori and page can be in any order)
    """
    if not args:
        return "", "", 1

    # quoted kategori first
    kat = _extract_arg(r'\bkategori\s+"([^"]+)"', args)
    if not kat:
        # unquoted, capture until end or before 'page <N>'
        m = re.search(r'\bkategori\s+(.+?)(?=\s+page\s+\d+\b|$)', args, flags=re.I)
        kat = (m.group(1).strip() if m else "")

    page_txt = _extract_arg(r'\bpage\s+(\d+)\b', args)
    page = max(1, int(page_txt)) if page_txt.isdigit() else 1

    # remove recognised parts to get free-text q
    q = re.sub(r'\bkategori\s+"[^"]+"\b', '', args, flags=re.I)
    q = re.sub(r'\bkategori\s+(.+?)(?=\s+page\s+\d+\b|$)', '', q, flags=re.I)
    q = re.sub(r'\bpage\s+\d+\b', '', q, flags=re.I).strip()

    return q, kat, page

# =========================
# HTTP Endpoints
# =========================
@app.get("/")
def health():
    return {"ok": True}

@app.get("/dbz")
async def db_ping():
    try:
        async with _pool.acquire() as conn:
            v = await conn.fetchval("select 1")
        return {"db": v}
    except Exception as e:
        print("DBZ error:", e)
        return {"db": None, "error": str(e)}

@app.get("/set-webhook")
async def set_webhook():
    if not WEBHOOK_URL:
        return {"ok": False, "error": "PUBLIC_URL or RENDER_EXTERNAL_URL not set"}
    payload = {"url": WEBHOOK_URL}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    ok, data = await _tg("setWebhook", payload)
    return {"ok": ok, "telegram": data, "url": WEBHOOK_URL}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Optional signature check
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return {"ok": True}

    try:
        update = await request.json()
    except Exception as e:
        print("Bad JSON from Telegram:", e)
        return {"ok": True}

    try:
        # Callback queries
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

        # /ping
        if re.match(r"^/?ping$", text, flags=re.I):
            await send(chat_id, "pong")
            return {"ok": True}

        # /produk [kata] [kategori X] [page N]
        m = re.match(r"^/?produk(?:\s+(.*))?$", text, flags=re.I)
        if m:
            args = (m.group(1) or "").strip()
            q, category, page = _parse_produk_args(args)
            await send_catalog(chat_id, q, category, page)
            return {"ok": True}

        # /vaksin [kata] [page N]  (Kategori = VACCINE_CATEGORY)
        m = re.match(r"^/?vaksin(?:\s+(.*))?$", text, flags=re.I)
        if m:
            args = (m.group(1) or "").strip()
            # only parse page from args; the free text is q
            page_txt = _extract_arg(r'\bpage\s+(\d+)\b', args)
            page = max(1, int(page_txt)) if page_txt.isdigit() else 1
            q = re.sub(r'\bpage\s+\d+\b', '', args, flags=re.I).strip()
            await send_catalog(chat_id, q, VACCINE_CATEGORY, page)
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
            "/ping\n"
            "/harga <nama> [pack <teks>]\n"
            "/produk [kata] [kategori <nama/kutip>] [page <N>]\n"
            "/vaksin [kata] [page <N>]\n"
            "Contoh: /produk vita kategori Peternakan page 2\n"
            "Contoh kategori dengan spasi: /produk kategori \"Alat Kesehatan\""
        )
        return {"ok": True}

    except Exception as e:
        # Never fail silently
        print("Webhook handler error:", e)
        try:
            chat_id = update.get("message", {}).get("chat", {}).get("id")
            if chat_id:
                await send(chat_id, "Terjadi kesalahan. Coba lagi sebentar.")
        except Exception:
            pass
        return {"ok": True}
