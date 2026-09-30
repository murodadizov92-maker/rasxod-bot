"""
Rasxod Telegram bot
-------------------
Golosovoy (yoki matn) xabar -> Gemini AI eshitib kimga / qancha / kategoriya ni ajratadi
-> tasdiqlash tugmalari -> SQLite bazaga saqlanadi -> /hisobot (bugun, kecha, 7 kun, sana oralig'i,
xodim va kategoriya filtri).

Sozlamalar sozlamalar.env faylida: TELEGRAM_TOKEN, GEMINI_API_KEY, ALLOWED_IDS
"""
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rasxod")
logging.getLogger("httpx").setLevel(logging.WARNING)  # token va keraksiz yozuvlar ko'rinmasin

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sozlamalar.env"))

# ----------------------------------------------------------------------------
# Sozlamalar
# ----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "rasxod.db")
# Render (yoki boshqa bulut) o'zi shu ikkitasini beradi -> webhook rejimi avtomatik yoqiladi.
PORT = int(os.environ.get("PORT", "10000"))
EXTERNAL_URL = os.environ.get("WEBHOOK_URL") or (
    f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}" if os.environ.get("RENDER_EXTERNAL_HOSTNAME") else None
)
BACKUP_CHAT_ID = int(os.environ["BACKUP_CHAT_ID"]) if os.environ.get("BACKUP_CHAT_ID") else None
# Birinchi model ishlamasa (eskirgan yoki limit tugagan bo'lsa), keyingisi sinab ko'riladi.
# Kerak bo'lsa sozlamalar.env da GEMINI_MODEL=... deb almashtirish mumkin.
GEMINI_MODELS = [m for m in (os.environ.get("GEMINI_MODEL"), "gemini-3.6-flash", "gemini-3.1-flash-lite") if m]
try:
    TZ = ZoneInfo("Asia/Tashkent")
except Exception:  # Windows'da tzdata bo'lmasa: Toshkent UTC+5 (yozgi vaqt yo'q)
    TZ = timezone(timedelta(hours=5))

CATEGORIES = [
    "ПРАДУКТА", "ПАЙНЕТ", "ЗАФТРИК", "КОМПУТЕР", "ПАКЕТ", "ДУКОН КРИДЕТ",
    "ДУКОН СОЛИК", "КОП", "ВАЙФАЙ", "КАЛБАСА ПРОБА", "ПЛАСТИК ФОЙЗ", "КВАРТИРА",
    "САНТЕХНИК", "ДУКОН РАСХОД", "АРАВА", "ЗАФТРИК -АБЕТ", "ИСУЗУ",
    "ТРАНСПОРТ РАСХОД", "ПОЧТА", "РЕКЛАМА", "АБЕТЬ", "УЖИН",
    "ОЙЛИКГА", "ГАЗГА",
]

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

BTN_REPORT = "📊 Hisobot"
BTN_LAST = "🕘 Oxirgilar"
MAIN_KB = ReplyKeyboardMarkup([[BTN_REPORT, BTN_LAST]], resize_keyboard=True)


# ----------------------------------------------------------------------------
# Baza
# ----------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                day TEXT NOT NULL,
                person TEXT NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT NOT NULL,
                raw_text TEXT,
                added_by INTEGER,
                note TEXT
            )"""
        )
        cols = [r[1] for r in con.execute("PRAGMA table_info(expenses)")]
        if "note" not in cols:  # eski baza uchun
            con.execute("ALTER TABLE expenses ADD COLUMN note TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_expenses_day ON expenses(day)")


def known_people() -> list[str]:
    with db() as con:
        rows = con.execute("SELECT DISTINCT person FROM expenses ORDER BY person").fetchall()
    return [r["person"] for r in rows]


def save_expense(person: str, amount: int, category: str, raw: str, uid: int, note: str = "") -> int:
    now = datetime.now(TZ)
    with db() as con:
        cur = con.execute(
            "INSERT INTO expenses(ts, day, person, amount, category, raw_text, added_by, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), now.date().isoformat(), person, amount, category, raw, uid, note),
        )
        return cur.lastrowid


def fetch_expenses(start: date, end: date, person=None, cat=None) -> list[sqlite3.Row]:
    q = "SELECT * FROM expenses WHERE day BETWEEN ? AND ?"
    args: list = [start.isoformat(), end.isoformat()]
    if person:
        q += " AND person = ?"
        args.append(person)
    if cat:
        q += " AND category = ?"
        args.append(cat)
    q += " ORDER BY ts"
    with db() as con:
        return con.execute(q, args).fetchall()


# ----------------------------------------------------------------------------
# Ruxsat tekshiruvi
# ----------------------------------------------------------------------------
def restricted(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid not in ALLOWED_IDS:
            if update.callback_query:
                await update.callback_query.answer("Ruxsat yo'q", show_alert=True)
            elif update.message:
                await update.message.reply_text(f"⛔ Ruxsat yo'q. Sizning ID: {uid}")
            return
        return await fn(update, context)

    return wrapper


# ----------------------------------------------------------------------------
# Ovoz / matn -> xarajat (Gemini)
# ----------------------------------------------------------------------------
def parse_system_prompt(people: list[str]) -> str:
    return f"""Sen xarajat yozuvlarini ajratib oluvchi yordamchisan.
Foydalanuvchi o'zbek tilida (lotin yoki kirill) xarajat haqida yozadi yoki golosovoy yuboradi.
Golosovoy bo'lsa, avval diqqat bilan eshit va nima deyilganini transcript maydoniga o'zbekcha yoz.
Yozma matn bo'lsa, transcript ga o'sha matnni ko'chir. Talaffuz xatolari bo'lishi mumkin.

Har bir xarajat uchun ajrat:
- person: kimga berilgan (ism). Lotin alifbosida, bosh harf bilan yoz (Samandar). Ism qo'shimchalarini
  olib tashla ("Samandarga" -> "Samandar", "Samandarni" -> "Samandar").
  Mavjud ismlar: {json.dumps(people, ensure_ascii=False)}. Agar ism shulardan biriga o'xshasa, aynan o'shani ishlat.
  Agar ism aytilmagan bo'lsa (masalan "dukon rasxod 50 ming skochga"), person = "Umumiy".
- note: izoh. Kategoriya va summadan tashqari qolgan, xarajat nima uchun ekanini bildiruvchi so'zlar
  (masalan "skochga" -> "skoch", "yog' olishga" -> "yog' olish"). Kategoriya nomini takrorlama, "-ga"
  qo'shimchasini olib tashla, qisqa va lotin alifbosida yoz. Izoh bo'lmasa bo'sh qator "".
- amount: butun son, so'mda. "ming" = 1000, "million" yoki "mln" = 1000000, "yarim million" = 500000.
  Masalan "50 ming" -> 50000, "1 million 200 ming" -> 1200000.
- category: quyidagi ro'yxatdan AYNAN bittasi, mos kelmasa null:
{json.dumps(CATEGORIES, ensure_ascii=False)}

Moslashtirish yo'riqnomasi:
oyligiga/oylik -> ОЙЛИКГА; gazga/gaz -> ГАЗГА;
"dukon rasxod"/"дукон расход" (so'z "dukon" aniq aytilganda) -> ДУКОН РАСХОД;
faqat "rasxodga"/"rasxod"/"расходга" (dukon so'zisiz) -> ЗАФТРИК -АБЕТ;
"rasdodga" kabi imlo xatolarini ham shunday tushun;
kopga/копга/kop -> КОП; zaftrik/zavtrak/nonushta -> ЗАФТРИК; produkta/mahsulot -> ПРАДУКТА;
arava -> АРАВА; isuzu/isuzuga -> ИСУЗУ; pochta -> ПОЧТА; reklama -> РЕКЛАМА;
kvartira -> КВАРТИРА; paynet -> ПАЙНЕТ; ujin/kechki ovqat -> УЖИН.

Bitta xabarda bir nechta xarajat bo'lishi mumkin. Faqat JSON qaytar, boshqa hech narsa yozma:
{{"transcript":"Samandarga 50 ming gazga","items":[{{"person":"Samandar","amount":50000,"category":"ГАЗГА","note":""}}]}}
{{"transcript":"Dukon rasxod 50.000 skochga","items":[{{"person":"Umumiy","amount":50000,"category":"ДУКОН РАСХОД","note":"skoch"}}]}}
Xarajat topilmasa: {{"transcript":"...","items":[]}}"""


async def extract_expenses(text: str | None = None, audio: bytes | None = None, mime: str = "audio/ogg"):
    """Matn yoki ovozdan (transcript, xarajatlar ro'yxati) qaytaradi."""
    if audio is not None:
        contents = [
            types.Part.from_bytes(data=audio, mime_type=mime),
            "Bu golosovoy xabar. Eshitib, xarajatlarni ajrat.",
        ]
    else:
        contents = [text]
    config = types.GenerateContentConfig(
        system_instruction=parse_system_prompt(known_people()),
        response_mime_type="application/json",
        temperature=0,
    )
    resp = None
    last_err: Exception | None = None
    for model in GEMINI_MODELS:
        try:
            resp = await gemini.aio.models.generate_content(model=model, contents=contents, config=config)
            break
        except Exception as e:
            log.warning("model %s ishlamadi: %s", model, e)
            last_err = e
    if resp is None:
        raise last_err
    raw = re.sub(r"```json|```", "", resp.text or "").strip()
    data = json.loads(raw)
    transcript = (data.get("transcript") or text or "").strip()
    items = []
    for it in data.get("items", []):
        try:
            amount = int(it["amount"])
        except (KeyError, ValueError, TypeError):
            continue
        if amount <= 0:
            continue
        person = str(it.get("person") or "").strip() or "Umumiy"
        note = str(it.get("note") or "").strip()[:200]
        cat = it.get("category")
        items.append(
            {"person": person, "amount": amount, "category": cat if cat in CATEGORIES else None, "note": note}
        )
    return transcript, items


# ----------------------------------------------------------------------------
# Yordamchi formatlash
# ----------------------------------------------------------------------------
def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ----------------------------------------------------------------------------
# Xarajat kiritish (tasdiqlash oqimi)
# ----------------------------------------------------------------------------
def pending_text(item: dict) -> str:
    cat = item["category"] or "❓ kategoriyani tanlang"
    note = f"📝 {item['note']}\n" if item.get("note") else ""
    return (
        f"👤 {item['person']}\n"
        f"💰 {fmt(item['amount'])} so'm\n"
        f"🏷 {cat}\n"
        f"{note}\n"
        f"🗣 «{item['raw']}»"
    )


def category_grid(pid: str) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(c, callback_data=f"setcat:{pid}:{i}") for i, c in enumerate(CATEGORIES)]
    rows = list(chunks(buttons, 2))
    rows.append([InlineKeyboardButton("❌ Bekor", callback_data=f"no:{pid}")])
    return InlineKeyboardMarkup(rows)


def pending_kb(pid: str, item: dict) -> InlineKeyboardMarkup:
    if not item["category"]:
        return category_grid(pid)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Saqlash", callback_data=f"ok:{pid}"),
                InlineKeyboardButton("✏️ Kategoriya", callback_data=f"cat:{pid}"),
            ],
            [InlineKeyboardButton("❌ Bekor", callback_data=f"no:{pid}")],
        ]
    )


async def handle_expense(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str | None = None,
    audio: bytes | None = None,
    mime: str = "audio/ogg",
) -> None:
    msg = update.message
    try:
        transcript, items = await extract_expenses(text=text, audio=audio, mime=mime)
    except Exception as e:
        log.exception("extract failed")
        await msg.reply_text(
            "⚠️ Tushunib bo'lmadi. Bir ozdan keyin qayta yuboring.\n\n"
            f"Sabab (dasturchi uchun): {type(e).__name__}: {str(e)[:400]}"
        )
        return
    if not items:
        await msg.reply_text(
            f"🗣 «{transcript}»\n\nXarajat topilmadi. Masalan: «Samandarga 50 ming gazga»."
        )
        return
    pend = context.bot_data.setdefault("pending", {})
    for it in items:
        it["raw"] = transcript
        it["uid"] = update.effective_user.id
        pid = uuid.uuid4().hex[:8]
        pend[pid] = it
        await msg.reply_text(pending_text(it), reply_markup=pending_kb(pid, it))


@restricted
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    media = msg.voice or msg.audio
    await msg.reply_chat_action("typing")
    try:
        tg_file = await media.get_file()
        data = bytes(await tg_file.download_as_bytearray())
    except Exception:
        log.exception("download failed")
        await msg.reply_text("⚠️ Ovozni yuklab bo'lmadi. Qayta yuboring.")
        return
    mime = getattr(media, "mime_type", None) or "audio/ogg"
    await handle_expense(update, context, audio=data, mime=mime)


@restricted
async def on_expense_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = q.data.split(":")
    action = parts[0]

    if action == "del":
        with db() as con:
            con.execute("DELETE FROM expenses WHERE id = ?", (int(parts[1]),))
        await q.answer("O'chirildi")
        await q.edit_message_text("🗑 O'chirildi")
        return

    pend = context.bot_data.setdefault("pending", {})
    pid = parts[1]
    item = pend.get(pid)
    if not item:
        await q.answer("Eskirgan, qayta yuboring", show_alert=True)
        return

    if action == "no":
        pend.pop(pid, None)
        await q.answer()
        await q.edit_message_text("❌ Bekor qilindi")
    elif action == "cat":
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=category_grid(pid))
    elif action == "setcat":
        item["category"] = CATEGORIES[int(parts[2])]
        await q.answer()
        await q.edit_message_text(pending_text(item), reply_markup=pending_kb(pid, item))
    elif action == "ok":
        if not item["category"]:
            await q.answer("Avval kategoriyani tanlang", show_alert=True)
            return
        pend.pop(pid, None)
        save_expense(item["person"], item["amount"], item["category"], item["raw"], item["uid"], item.get("note", ""))
        await q.answer("Saqlandi")
        note = f"\n📝 {item['note']}" if item.get("note") else ""
        await q.edit_message_text(
            f"✅ Saqlandi\n👤 {item['person']}\n💰 {fmt(item['amount'])} so'm\n🏷 {item['category']}{note}"
        )


# ----------------------------------------------------------------------------
# Oxirgi yozuvlar (o'chirish uchun)
# ----------------------------------------------------------------------------
@restricted
async def oxirgilar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db() as con:
        rows = con.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        await update.message.reply_text("Hali yozuv yo'q.")
        return
    for r in rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 O'chirish", callback_data=f"del:{r['id']}")]])
        await update.message.reply_text(
            f"{r['ts'][8:10]}.{r['ts'][5:7]} {r['ts'][11:16]}\n👤 {r['person']} — {fmt(r['amount'])}\n🏷 {r['category']}"
            + (f"\n📝 {r['note']}" if r["note"] else ""),
            reply_markup=kb,
        )


# ----------------------------------------------------------------------------
# Hisobot
# ----------------------------------------------------------------------------
def new_report_state() -> dict:
    today = datetime.now(TZ).date()
    return {"start": today, "end": today, "person": None, "cat": None, "detail": False}


def build_report(rep: dict) -> tuple[str, InlineKeyboardMarkup]:
    start, end, person, cat = rep["start"], rep["end"], rep["person"], rep["cat"]
    rows = fetch_expenses(start, end, person, cat)

    title = fmt_date(start) if start == end else f"{fmt_date(start)} — {fmt_date(end)}"
    lines = [
        f"📊 Hisobot: {title}",
        f"👤 Xodim: {person or 'hammasi'}   🏷 Kategoriya: {cat or 'hammasi'}",
        "",
    ]

    if not rows:
        lines.append("Bu davrda yozuv yo'q.")
    elif rep["detail"]:
        current_day = None
        for r in rows:
            if r["day"] != current_day:
                current_day = r["day"]
                lines.append(f"— {fmt_date(date.fromisoformat(current_day))} —")
            extra = f" — {r['note']}" if r["note"] else ""
            lines.append(f"{r['ts'][11:16]}  {r['person']} — {fmt(r['amount'])} — {r['category']}{extra}")
        lines += ["", f"💰 JAMI: {fmt(sum(r['amount'] for r in rows))} so'm"]
    else:
        by_person: dict[str, dict[str, int]] = {}
        by_cat: dict[str, int] = {}
        by_note: dict[tuple[str, str], dict[str, int]] = {}
        for r in rows:
            by_person.setdefault(r["person"], {}).setdefault(r["category"], 0)
            by_person[r["person"]][r["category"]] += r["amount"]
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["amount"]
            note = (r["note"] or "").strip()
            if note:
                d = by_note.setdefault((r["person"], r["category"]), {})
                d[note] = d.get(note, 0) + r["amount"]
        for p in sorted(by_person):
            cats = by_person[p]
            lines.append(f"👤 {p} — {fmt(sum(cats.values()))}")
            for c, s in sorted(cats.items(), key=lambda x: -x[1]):
                lines.append(f"   {c} — {fmt(s)}")
                for n, amt in by_note.get((p, c), {}).items():
                    lines.append(f"      📝 {n} — {fmt(amt)}")
            lines.append("")
        lines.append("🏷 Kategoriya bo'yicha jami:")
        for c, s in sorted(by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"   {c} — {fmt(s)}")
        lines += ["", f"💰 JAMI: {fmt(sum(by_cat.values()))} so'm"]

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…(qisqartirildi, davrni yoki filtrni toraytiring)"

    last_row = [InlineKeyboardButton("📊 Jamlama" if rep["detail"] else "📋 Batafsil", callback_data="rp:detail")]
    if person or cat:  # tozalash tugmasi faqat filtr tanlangan bo'lsa ko'rinadi
        last_row.append(InlineKeyboardButton("♻️ Filtrni tozalash", callback_data="rp:clear"))

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Bugun", callback_data="rp:today"),
                InlineKeyboardButton("Kecha", callback_data="rp:yest"),
                InlineKeyboardButton("7 kun", callback_data="rp:week"),
            ],
            [InlineKeyboardButton("📆 Sana oralig'i", callback_data="rp:range")],
            [
                InlineKeyboardButton("👤 Xodim", callback_data="rp:pickp"),
                InlineKeyboardButton("🏷 Kategoriya", callback_data="rp:pickc"),
            ],
            last_row,
        ]
    )
    return text, kb


@restricted
async def hisobot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rep = context.user_data["rep"] = new_report_state()
    text, kb = build_report(rep)
    await update.message.reply_text(text, reply_markup=kb)


@restricted
async def on_report_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    rep = context.user_data.get("rep") or new_report_state()
    context.user_data["rep"] = rep
    parts = q.data.split(":")
    action = parts[1]
    today = datetime.now(TZ).date()

    if action == "today":
        rep["start"] = rep["end"] = today
    elif action == "yest":
        rep["start"] = rep["end"] = today - timedelta(days=1)
    elif action == "week":
        rep["start"], rep["end"] = today - timedelta(days=6), today
    elif action == "range":
        context.user_data["awaiting_range"] = True
        await q.answer()
        await q.message.reply_text("Sana oralig'ini yozing. Masalan:\n10.09.2026 - 18.09.2026")
        return
    elif action == "detail":
        rep["detail"] = not rep["detail"]
    elif action == "clear":
        rep["person"] = rep["cat"] = None
    elif action == "pickp":
        people = known_people()
        buttons = [InlineKeyboardButton("Hammasi", callback_data="rp:setp:-1")]
        buttons += [InlineKeyboardButton(p, callback_data=f"rp:setp:{i}") for i, p in enumerate(people)]
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(list(chunks(buttons, 2))))
        return
    elif action == "setp":
        i = int(parts[2])
        people = known_people()
        rep["person"] = people[i] if 0 <= i < len(people) else None
    elif action == "pickc":
        buttons = [InlineKeyboardButton("Hammasi", callback_data="rp:setc:-1")]
        buttons += [InlineKeyboardButton(c, callback_data=f"rp:setc:{i}") for i, c in enumerate(CATEGORIES)]
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(list(chunks(buttons, 2))))
        return
    elif action == "setc":
        i = int(parts[2])
        rep["cat"] = CATEGORIES[i] if 0 <= i < len(CATEGORIES) else None

    text, kb = build_report(rep)
    await q.answer("Filtr tozalandi" if action == "clear" else None)
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():  # matn o'zgarmagan bo'lsa, xato emas
            raise


DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?")


def parse_range(text: str) -> tuple[date, date] | None:
    today = datetime.now(TZ).date()
    found = []
    for d, m, y in DATE_RE.findall(text):
        year = int(y) if y else today.year
        if year < 100:
            year += 2000
        try:
            found.append(date(year, int(m), int(d)))
        except ValueError:
            return None
    if len(found) < 2:
        return None
    a, b = found[0], found[1]
    return (a, b) if a <= b else (b, a)


# ----------------------------------------------------------------------------
# Matn xabarlar, /start
# ----------------------------------------------------------------------------
@restricted
async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("Hali yozuv yo'q.")
        return
    await update.message.reply_document(
        document=open(DB_PATH, "rb"), filename=f"rasxod_{datetime.now(TZ):%Y-%m-%d_%H%M}.db",
        caption="📦 Bazaning zaxira nusxasi",
    )


async def daily_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Har kuni tunda bazani BACKUP_CHAT_ID'ga jo'natadi — server o'chib/qayta ishga tushib qolsa ham
    xarajatlar yo'qolib ketmasin uchun."""
    if not BACKUP_CHAT_ID or not os.path.exists(DB_PATH):
        return
    try:
        await context.bot.send_document(
            chat_id=BACKUP_CHAT_ID,
            document=open(DB_PATH, "rb"),
            filename=f"rasxod_{datetime.now(TZ):%Y-%m-%d}.db",
            caption="📦 Kunlik avtomatik zaxira",
        )
    except Exception:
        log.exception("daily_backup failed")


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Assalomu alaykum! Golosovoy yoki matn yuboring, masalan:\n"
        "«Samandarga 50 ming gazga»\n"
        "«Samandarni oyligiga 100 ming»\n\n"
        "Hisobot: /hisobot\nOxirgi yozuvlar (o'chirish): /oxirgilar",
        reply_markup=MAIN_KB,
    )


@restricted
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if text == BTN_REPORT:
        return await hisobot(update, context)
    if text == BTN_LAST:
        return await oxirgilar(update, context)

    if context.user_data.pop("awaiting_range", False):
        rng = parse_range(text)
        if not rng:
            context.user_data["awaiting_range"] = True
            await update.message.reply_text("Tushunmadim. Shunday yozing: 10.09.2026 - 18.09.2026")
            return
        rep = context.user_data.setdefault("rep", new_report_state())
        rep["start"], rep["end"] = rng
        t, kb = build_report(rep)
        await update.message.reply_text(t, reply_markup=kb)
        return

    await handle_expense(update, context, text=text)


def main() -> None:
    init_db()
    proxy = os.environ.get("PROXY_URL") or None  # kerak bo'lsa sozlamalar.env da: PROXY_URL=...

    def make_request() -> HTTPXRequest:
        return HTTPXRequest(
            connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30, proxy=proxy
        )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(make_request())
        .get_updates_request(make_request())
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("hisobot", hisobot))
    app.add_handler(CommandHandler("oxirgilar", oxirgilar))
    app.add_handler(CommandHandler("backup", backup))
    if BACKUP_CHAT_ID and app.job_queue:
        app.job_queue.run_daily(daily_backup, time=dt_time(hour=23, minute=55, tzinfo=TZ))
    app.add_handler(CallbackQueryHandler(on_expense_cb, pattern=r"^(ok|no|cat|setcat|del):"))
    app.add_handler(CallbackQueryHandler(on_report_cb, pattern=r"^rp:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Rasxod bot ishga tushdi")
    if EXTERNAL_URL:
        log.info("Webhook rejimi: %s", EXTERNAL_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{EXTERNAL_URL}/{TELEGRAM_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        log.info("Polling rejimi (lokal kompyuter)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
