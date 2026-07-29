from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # التشغيل المحلي من دون PostgreSQL
    psycopg = None
    dict_row = None

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
ARABIC_PATH = Path(os.getenv("ARABIC_PATH", BASE_DIR / "arabic_bank.json"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "arabic_bot.sqlite3"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

CATEGORY_LABELS = {
    "verbs": "بناء الأفعال",
    "subject": "الفاعل ونائب الفاعل",
    "nominal": "المبتدأ والخبر",
    "poem_exile": "قصيدة لا تعذليه",
    "cup_story": "قصيدة كوب",
    "defective": "الأفعال الناقصة",
    "vocative": "النداء",
    "exception": "الاستثناء",
    "styles": "الأساليب",
    "poem_hamdan": "أراك عصي الدمع",
    "number": "العدد",
    "correction": "صحح الخطأ",
}

BTN_HOME = "🏠 الرئيسية"
BTN_WRITTEN = "✍️ تسميع كتابي"
BTN_PAPER = "📝 حل على الورقة"
BTN_FILL = "🧩 أكمل الفراغ"
BTN_CATEGORIES = "📚 حسب القسم"
BTN_EXAM = "⏱ امتحان تجريبي"
BTN_REVIEW = "🔁 مراجعة أخطائي"
BTN_STATS = "📊 مستوى الحفظ"
BTN_ID = "🆔 رقم حسابي"
BTN_HELP = "ℹ️ المساعدة"
BTN_CANCEL = "❌ إنهاء الجلسة"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("arabic_study_bot")


class ArabicBank:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[dict[str, Any]] = []
        self.by_id: dict[int, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("arabic_bank.json يجب أن يحتوي قائمة")

        seen: set[int] = set()
        for item in raw:
            for key in ("id", "category", "kind", "prompt", "answer"):
                if key not in item:
                    raise ValueError(f"حقل ناقص في بنك العربي: {key}")
            item["id"] = int(item["id"])
            if item["id"] in seen:
                raise ValueError(f"رقم مكرر في بنك العربي: {item['id']}")
            if item["kind"] not in {"written", "fill", "card"}:
                raise ValueError(f"نوع غير مدعوم: {item['kind']}")
            seen.add(item["id"])
            item.setdefault("key_points", [])
            item.setdefault("accepted", [])
            item.setdefault("source", "")
            item.setdefault("enabled", True)

        self.items = raw
        self.by_id = {item["id"]: item for item in raw}
        logger.info("Loaded %s Arabic study items", len(self.items))

    def enabled(self, category: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        result = [item for item in self.items if item.get("enabled", True)]
        if category:
            result = [item for item in result if item["category"] == category]
        if kind:
            result = [item for item in result if item["kind"] == kind]
        return result


class Database:
    """تخزين دائم عبر PostgreSQL/Neon، مع SQLite للتجربة المحلية."""

    def __init__(self, path: Path, database_url: str = "") -> None:
        self.path = path
        self.database_url = database_url
        self.is_postgres = bool(database_url)
        self.initialize()
        logger.info(
            "Database backend: %s",
            "PostgreSQL" if self.is_postgres else f"SQLite ({self.path})",
        )

    def connect_sqlite(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def connect_postgres(self):
        if psycopg is None:
            raise RuntimeError("ثبّت psycopg أو اترك DATABASE_URL فارغاً للتشغيل المحلي")
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def initialize(self) -> None:
        users_sql = """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                first_seen TIMESTAMP NOT NULL,
                last_seen TIMESTAMP NOT NULL
            )
        """
        progress_sql = """
            CREATE TABLE IF NOT EXISTS arabic_progress (
                user_id BIGINT NOT NULL,
                item_id INTEGER NOT NULL,
                best_score INTEGER NOT NULL DEFAULT 0,
                last_score INTEGER NOT NULL DEFAULT 0,
                review_count INTEGER NOT NULL DEFAULT 0,
                last_review TIMESTAMP NOT NULL,
                PRIMARY KEY (user_id, item_id)
            )
        """
        if self.is_postgres:
            with self.connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(users_sql)
                    cur.execute(progress_sql)
                conn.commit()
        else:
            with self.connect_sqlite() as conn:
                conn.execute(users_sql)
                conn.execute(progress_sql)
                conn.commit()

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def upsert_user(self, update: Update) -> None:
        user = update.effective_user
        if not user:
            return
        now = self.now()
        values = (user.id, user.username, user.first_name, user.last_name, now, now)
        if self.is_postgres:
            with self.connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET
                            username=EXCLUDED.username,
                            first_name=EXCLUDED.first_name,
                            last_name=EXCLUDED.last_name,
                            last_seen=EXCLUDED.last_seen
                        """,
                        values,
                    )
                conn.commit()
        else:
            with self.connect_sqlite() as conn:
                conn.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username=excluded.username,
                        first_name=excluded.first_name,
                        last_name=excluded.last_name,
                        last_seen=excluded.last_seen
                    """,
                    values,
                )
                conn.commit()

    def record_progress(self, user_id: int, item_id: int, score: int) -> None:
        score = max(0, min(2, int(score)))
        now = self.now()
        if self.is_postgres:
            with self.connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO arabic_progress
                            (user_id, item_id, best_score, last_score, review_count, last_review)
                        VALUES (%s, %s, %s, %s, 1, %s)
                        ON CONFLICT (user_id, item_id) DO UPDATE SET
                            best_score=GREATEST(arabic_progress.best_score, EXCLUDED.best_score),
                            last_score=EXCLUDED.last_score,
                            review_count=arabic_progress.review_count + 1,
                            last_review=EXCLUDED.last_review
                        """,
                        (user_id, item_id, score, score, now),
                    )
                conn.commit()
        else:
            with self.connect_sqlite() as conn:
                conn.execute(
                    """
                    INSERT INTO arabic_progress
                        (user_id, item_id, best_score, last_score, review_count, last_review)
                    VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(user_id, item_id) DO UPDATE SET
                        best_score=MAX(arabic_progress.best_score, excluded.best_score),
                        last_score=excluded.last_score,
                        review_count=arabic_progress.review_count + 1,
                        last_review=excluded.last_review
                    """,
                    (user_id, item_id, score, score, now),
                )
                conn.commit()

    def weak_ids(self, user_id: int) -> list[int]:
        query = """
            SELECT item_id FROM arabic_progress
            WHERE user_id = {placeholder} AND last_score < 2
            ORDER BY last_score ASC, last_review ASC
        """
        if self.is_postgres:
            with self.connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(query.format(placeholder="%s"), (user_id,))
                    return [int(row["item_id"]) for row in cur.fetchall()]
        with self.connect_sqlite() as conn:
            rows = conn.execute(query.format(placeholder="?"), (user_id,)).fetchall()
            return [int(row["item_id"]) for row in rows]

    def stats(self, user_id: int) -> dict[str, int]:
        query = """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN best_score = 2 THEN 1 ELSE 0 END) AS learned,
                SUM(CASE WHEN last_score = 1 THEN 1 ELSE 0 END) AS partial,
                SUM(CASE WHEN last_score = 0 THEN 1 ELSE 0 END) AS weak,
                COALESCE(SUM(review_count), 0) AS reviews
            FROM arabic_progress WHERE user_id = {placeholder}
        """
        if self.is_postgres:
            with self.connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(query.format(placeholder="%s"), (user_id,))
                    row = cur.fetchone() or {}
        else:
            with self.connect_sqlite() as conn:
                row = conn.execute(query.format(placeholder="?"), (user_id,)).fetchone() or {}
        return {key: int((row[key] if row[key] is not None else 0)) for key in ("total", "learned", "partial", "weak", "reviews")}

    def global_stats(self) -> dict[str, int]:
        if self.is_postgres:
            with self.connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS count FROM users")
                    users = int(cur.fetchone()["count"])
                    cur.execute("SELECT COUNT(*) AS count, COALESCE(SUM(review_count),0) AS reviews FROM arabic_progress")
                    row = cur.fetchone()
        else:
            with self.connect_sqlite() as conn:
                users = int(conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"])
                row = conn.execute("SELECT COUNT(*) AS count, COALESCE(SUM(review_count),0) AS reviews FROM arabic_progress").fetchone()
        return {"users": users, "studied": int(row["count"]), "reviews": int(row["reviews"])}

    def recent_users(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        if self.is_postgres:
            with self.connect_postgres() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT user_id, username, first_name, last_name, last_seen FROM users ORDER BY last_seen DESC LIMIT %s",
                        (limit,),
                    )
                    return list(cur.fetchall())
        with self.connect_sqlite() as conn:
            rows = conn.execute(
                "SELECT user_id, username, first_name, last_name, last_seen FROM users ORDER BY last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]


BANK = ArabicBank(ARABIC_PATH)
DB = Database(DATABASE_PATH, DATABASE_URL)


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def quick_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_WRITTEN, BTN_PAPER],
            [BTN_FILL, BTN_CATEGORIES],
            [BTN_EXAM, BTN_REVIEW],
            [BTN_STATS, BTN_ID],
            [BTN_HELP, BTN_HOME],
            [BTN_CANCEL],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="اختار طريقة الدراسة أو اكتب جوابك…",
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(BTN_WRITTEN, callback_data="mode:written")],
            [InlineKeyboardButton(BTN_PAPER, callback_data="mode:paper")],
            [InlineKeyboardButton(BTN_FILL, callback_data="mode:fill")],
            [InlineKeyboardButton(BTN_CATEGORIES, callback_data="menu:categories")],
            [InlineKeyboardButton(BTN_EXAM, callback_data="mode:exam")],
            [InlineKeyboardButton(BTN_REVIEW, callback_data="mode:review")],
            [InlineKeyboardButton(BTN_STATS, callback_data="menu:stats")],
            [InlineKeyboardButton(BTN_HELP, callback_data="menu:help")],
        ]
    )


def categories_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"category:{key}")]
        for key, label in CATEGORY_LABELS.items()
        if BANK.enabled(category=key)
    ]
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def category_modes_menu(category: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✍️ تسميع كتابي", callback_data=f"catmode:{category}:written")],
            [InlineKeyboardButton("📝 حل على الورقة", callback_data=f"catmode:{category}:paper")],
            [InlineKeyboardButton("🧩 أكمل الفراغ", callback_data=f"catmode:{category}:fill")],
            [InlineKeyboardButton("🎯 اختبار مختلط", callback_data=f"catmode:{category}:exam")],
            [InlineKeyboardButton("⬅️ الأقسام", callback_data="menu:categories")],
        ]
    )


def session(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    value = context.user_data.get("arabic_session")
    return value if isinstance(value, dict) else None


def normalize_arabic(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    replacements = str.maketrans({
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
        "ى": "ي", "ؤ": "و", "ئ": "ي", "ة": "ه",
        "ـ": " ",
    })
    text = text.translate(replacements)
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def evaluate_answer(item: dict[str, Any], user_answer: str) -> tuple[int, list[str], int, int]:
    normalized = normalize_arabic(user_answer)
    if not normalized:
        return 0, [], 0, 1

    accepted = [item.get("answer", ""), *item.get("accepted", [])]
    if item["kind"] == "fill":
        ok = any(
            normalize_arabic(value) == normalized
            or normalize_arabic(value) in normalized
            for value in accepted
            if value
        )
        return (2 if ok else 0), ([] if ok else [item["answer"]]), (1 if ok else 0), 1

    groups = item.get("key_points", [])
    if not groups:
        answer_norm = normalize_arabic(item.get("answer", ""))
        ok = bool(answer_norm and (answer_norm in normalized or normalized in answer_norm))
        return (2 if ok else 1), ([] if ok else [item["answer"]]), (1 if ok else 0), 1

    missing: list[str] = []
    matched = 0
    for group in groups:
        alternatives = group if isinstance(group, list) else [str(group)]
        if any(normalize_arabic(value) in normalized for value in alternatives if value):
            matched += 1
        else:
            missing.append(" / ".join(alternatives))

    ratio = matched / max(1, len(groups))
    score = 2 if ratio >= 0.8 else 1 if ratio >= 0.45 else 0
    return score, missing, matched, len(groups)


def choose_items(pool: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    pool = list(pool)
    random.shuffle(pool)
    return pool[: min(size, len(pool))]


def start_study_session(
    context: ContextTypes.DEFAULT_TYPE,
    pool: list[dict[str, Any]],
    mode: str,
    chat_id: int,
    size: int,
) -> bool:
    items = choose_items(pool, size)
    if not items:
        return False
    context.user_data["arabic_session"] = {
        "ids": [item["id"] for item in items],
        "pos": 0,
        "mode": mode,
        "scores": [],
        "chat_id": chat_id,
        "awaiting": False,
        "revealed": False,
    }
    return True


def current_item(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    active = session(context)
    if not active or active["pos"] >= len(active["ids"]):
        return None
    return BANK.by_id.get(active["ids"][active["pos"]])


def item_delivery_mode(active: dict[str, Any], item: dict[str, Any]) -> str:
    mode = active["mode"]
    if mode == "paper":
        return "paper"
    if mode == "written":
        return "typed"
    if mode == "fill":
        return "typed"
    if mode in {"exam", "review"}:
        return "paper" if item["kind"] == "card" else "typed"
    return "typed"


async def send_current(update: Update | None, context: ContextTypes.DEFAULT_TYPE) -> None:
    active = session(context)
    if not active:
        return
    if active["pos"] >= len(active["ids"]):
        await finish_session(update, context)
        return

    item = current_item(context)
    if not item:
        active["pos"] += 1
        await send_current(update, context)
        return

    label = CATEGORY_LABELS.get(item["category"], item["category"])
    progress = f"السؤال {active['pos'] + 1} من {len(active['ids'])}"
    text = (
        f"<b>{html.escape(label)}</b>\n"
        f"<i>{progress}</i>\n\n"
        f"{html.escape(item['prompt'])}"
    )

    delivery = item_delivery_mode(active, item)
    active["revealed"] = False
    if delivery == "paper":
        active["awaiting"] = False
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("👁 عرض الحل", callback_data="study:reveal")],
                [InlineKeyboardButton("❌ إنهاء الجلسة", callback_data="study:cancel")],
            ]
        )
    else:
        active["awaiting"] = True
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ إنهاء الجلسة", callback_data="study:cancel")]]
        )
        text += "\n\n<b>اكتب جوابك برسالة.</b>"

    chat_id = active["chat_id"]
    if update and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def finish_session(update: Update | None, context: ContextTypes.DEFAULT_TYPE, cancelled: bool = False) -> None:
    active = session(context)
    if not active:
        return
    scores = active.get("scores", [])
    total = len(active.get("ids", []))
    perfect = sum(1 for value in scores if value == 2)
    partial = sum(1 for value in scores if value == 1)
    weak = sum(1 for value in scores if value == 0)
    chat_id = active["chat_id"]
    context.user_data.pop("arabic_session", None)

    if cancelled:
        text = "تم إنهاء الجلسة."
    else:
        text = (
            "<b>انتهت جلسة العربي ✅</b>\n\n"
            f"عدد الأسئلة: <b>{total}</b>\n"
            f"محفوظ: <b>{perfect}</b>\n"
            f"ناقص شوي: <b>{partial}</b>\n"
            f"ضعيف: <b>{weak}</b>"
        )

    if update and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=main_menu())
    await context.bot.send_message(chat_id=chat_id, text="رجعناك للقائمة الرئيسية.", reply_markup=quick_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    DB.upsert_user(update)
    context.user_data.pop("arabic_session", None)
    name = html.escape(update.effective_user.first_name or "طالب")
    text = (
        f"أهلاً <b>{name}</b> 👋\n\n"
        "هاد البوت مخصص لحفظ ومراجعة <b>اللغة العربية فقط</b>.\n"
        "اختار طريقة الدراسة:"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())
    await update.effective_message.reply_text("الأزرار السريعة صارت تحت مكان الكتابة.", reply_markup=quick_keyboard())


async def start_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, category: str | None = None) -> None:
    DB.upsert_user(update)
    user_id = update.effective_user.id

    if mode == "written":
        pool = BANK.enabled(category=category, kind="written")
        size = 10
    elif mode == "paper":
        pool = BANK.enabled(category=category)
        size = 10
    elif mode == "fill":
        pool = BANK.enabled(category=category, kind="fill")
        size = 10
    elif mode == "exam":
        pool = BANK.enabled(category=category)
        size = 10
    elif mode == "review":
        ids = DB.weak_ids(user_id)
        pool = [BANK.by_id[item_id] for item_id in ids if item_id in BANK.by_id]
        size = 15
        if not pool:
            message = "ما عندك أسئلة ضعيفة مسجلة حالياً. ابدأ جلسة دراسة أولاً."
            if update.callback_query:
                await update.callback_query.edit_message_text(message, reply_markup=main_menu())
            else:
                await update.effective_message.reply_text(message, reply_markup=main_menu())
            return
    else:
        pool = []
        size = 10

    ok = start_study_session(context, pool, mode, update.effective_chat.id, size)
    if not ok:
        message = "ما في أسئلة متاحة بهالقسم أو بهالنوع حالياً."
        if update.callback_query:
            await update.callback_query.edit_message_text(message, reply_markup=main_menu())
        else:
            await update.effective_message.reply_text(message, reply_markup=main_menu())
        return
    await send_current(update, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    DB.upsert_user(update)
    data = query.data
    if data == "menu:home":
        context.user_data.pop("arabic_session", None)
        await query.edit_message_text("اختار طريقة الدراسة:", reply_markup=main_menu())
    elif data == "menu:categories":
        await query.edit_message_text("اختار القسم:", reply_markup=categories_menu())
    elif data == "menu:stats":
        await query.edit_message_text(stats_text(update.effective_user.id), parse_mode=ParseMode.HTML, reply_markup=main_menu())
    elif data == "menu:help":
        await query.edit_message_text(help_text(), parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    await start_mode(update, context, mode)


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    category = query.data.split(":", 1)[1]
    label = CATEGORY_LABELS.get(category, category)
    await query.edit_message_text(f"قسم: <b>{html.escape(label)}</b>\nاختار طريقة الدراسة:", parse_mode=ParseMode.HTML, reply_markup=category_modes_menu(category))


async def category_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, category, mode = query.data.split(":", 2)
    await start_mode(update, context, mode, category)


async def study_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    active = session(context)
    if not active:
        await query.edit_message_text("انتهت الجلسة. ابدأ جلسة جديدة.", reply_markup=main_menu())
        return

    action = query.data.split(":", 1)[1]
    if action == "cancel":
        await finish_session(update, context, cancelled=True)
        return
    if action == "next":
        await send_current(update, context)
        return
    if action == "finish":
        await finish_session(update, context)
        return
    if action == "reveal":
        item = current_item(context)
        if not item:
            await finish_session(update, context)
            return
        active["revealed"] = True
        answer = html.escape(item["answer"])
        source = html.escape(item.get("source", ""))
        text = (
            f"<b>الجواب النموذجي:</b>\n{answer}"
            + (f"\n\n<i>المصدر: {source}</i>" if source else "")
            + "\n\nقيّم حالك بصراحة:"
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ حفظته", callback_data="score:2"),
                    InlineKeyboardButton("🟡 ناقص شوي", callback_data="score:1"),
                ],
                [InlineKeyboardButton("❌ ما عرفته", callback_data="score:0")],
                [InlineKeyboardButton("❌ إنهاء الجلسة", callback_data="study:cancel")],
            ]
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def score_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    active = session(context)
    item = current_item(context)
    if not active or not item:
        await query.edit_message_text("انتهت الجلسة.", reply_markup=main_menu())
        return

    score = int(query.data.split(":", 1)[1])
    DB.record_progress(update.effective_user.id, item["id"], score)
    active["scores"].append(score)
    active["pos"] += 1
    active["awaiting"] = False

    last = active["pos"] >= len(active["ids"])
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏁 عرض النتيجة" if last else "السؤال التالي ➡️", callback_data="study:finish" if last else "study:next")]]
    )
    await query.edit_message_reply_markup(reply_markup=markup)


async def text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    active = session(context)
    if not active or not active.get("awaiting"):
        return False
    item = current_item(context)
    if not item:
        await finish_session(None, context)
        return True

    score, missing, matched, total = evaluate_answer(item, update.effective_message.text or "")
    DB.record_progress(update.effective_user.id, item["id"], score)
    active["scores"].append(score)
    active["awaiting"] = False
    active["pos"] += 1

    if score == 2:
        heading = "✅ جوابك ممتاز"
    elif score == 1:
        heading = "🟡 جوابك فيه جزء صحيح"
    else:
        heading = "❌ الجواب ناقص أو غير صحيح"

    feedback = [f"<b>{heading}</b>"]
    if total > 1:
        feedback.append(f"ذكرت <b>{matched}</b> من أصل <b>{total}</b> نقاط أساسية.")
    if missing and item["kind"] != "fill":
        feedback.append("<b>النقاط الناقصة:</b>\n• " + "\n• ".join(html.escape(value) for value in missing[:8]))
    feedback.append(f"<b>الجواب النموذجي:</b>\n{html.escape(item['answer'])}")
    if item.get("source"):
        feedback.append(f"<i>المصدر: {html.escape(item['source'])}</i>")

    last = active["pos"] >= len(active["ids"])
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏁 عرض النتيجة" if last else "السؤال التالي ➡️", callback_data="study:finish" if last else "study:next")]]
    )
    await update.effective_message.reply_text("\n\n".join(feedback), parse_mode=ParseMode.HTML, reply_markup=markup)
    return True


def stats_text(user_id: int) -> str:
    stats = DB.stats(user_id)
    total_bank = len(BANK.enabled())
    return (
        "<b>مستوى حفظك 📊</b>\n\n"
        f"درست: <b>{stats['total']}</b> من <b>{total_bank}</b>\n"
        f"محفوظ: <b>{stats['learned']}</b>\n"
        f"ناقص شوي: <b>{stats['partial']}</b>\n"
        f"ضعيف: <b>{stats['weak']}</b>\n"
        f"إجمالي المراجعات: <b>{stats['reviews']}</b>"
    )


def help_text() -> str:
    return (
        "<b>طريقة استخدام بوت العربي</b>\n\n"
        "✍️ <b>تسميع كتابي:</b> اكتب الجواب والبوت يقارن النقاط الأساسية.\n"
        "📝 <b>حل على الورقة:</b> جاوب لحالك ثم اعرض الحل وقيّم نفسك.\n"
        "🧩 <b>أكمل الفراغ:</b> أسئلة قصيرة للحفظ السريع.\n"
        "📚 <b>حسب القسم:</b> اختار القاعدة أو النص وطريقة الدراسة.\n"
        "⏱ <b>امتحان تجريبي:</b> عشرة أسئلة مختلطة.\n"
        "🔁 <b>مراجعة أخطائي:</b> يعيد الأسئلة التي كانت نتيجتك فيها أقل من ممتاز.\n\n"
        "اختلاف الصياغة البسيط مقبول، لكن دائماً راجع الجواب النموذجي."
    )


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    DB.upsert_user(update)
    await update.effective_message.reply_text(stats_text(update.effective_user.id), parse_mode=ParseMode.HTML, reply_markup=quick_keyboard())


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML, reply_markup=quick_keyboard())


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    DB.upsert_user(update)
    await update.effective_message.reply_text(
        f"رقم حسابك في تيليغرام: `{update.effective_user.id}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=quick_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if session(context):
        await finish_session(None, context, cancelled=True)
    else:
        await update.effective_message.reply_text("ما في جلسة نشطة حالياً.", reply_markup=quick_keyboard())


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("هذا الأمر مخصص للإدارة.")
        return
    stats = DB.global_stats()
    text = (
        "<b>لوحة إدارة بوت العربي</b>\n\n"
        f"البطاقات والأسئلة: <b>{len(BANK.items)}</b>\n"
        f"المستخدمون: <b>{stats['users']}</b>\n"
        f"العناصر المدروسة: <b>{stats['studied']}</b>\n"
        f"إجمالي المراجعات: <b>{stats['reviews']}</b>\n\n"
        "الأوامر:\n"
        "/users عرض آخر المستخدمين\n"
        "/exportarabic تصدير بنك العربي\n"
        "/reloadarabic إعادة تحميل البنك"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("هذا الأمر مخصص للإدارة.")
        return
    rows = DB.recent_users(20)
    if not rows:
        await update.effective_message.reply_text("ما في مستخدمين مسجلين بعد.")
        return
    lines = ["<b>آخر المستخدمين نشاطاً</b>"]
    for row in rows:
        full_name = " ".join(filter(None, [row.get("first_name"), row.get("last_name")])) or "بدون اسم"
        username = f"@{row['username']}" if row.get("username") else "بدون معرف"
        lines.append(f"\n• {html.escape(full_name)} — {html.escape(username)}\n<code>{row['user_id']}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def export_arabic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    await update.effective_message.reply_document(document=ARABIC_PATH.open("rb"), filename="arabic_bank.json")


async def reload_arabic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    try:
        BANK.reload()
        await update.effective_message.reply_text(f"تم تحميل {len(BANK.items)} عنصراً ✅")
    except Exception as exc:
        logger.exception("Arabic bank reload failed")
        await update.effective_message.reply_text(f"فشل التحميل: {exc}")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    DB.upsert_user(update)
    text = (update.effective_message.text or "").strip()

    if await text_answer(update, context):
        return
    if text == BTN_HOME:
        await start(update, context)
    elif text == BTN_WRITTEN:
        await start_mode(update, context, "written")
    elif text == BTN_PAPER:
        await start_mode(update, context, "paper")
    elif text == BTN_FILL:
        await start_mode(update, context, "fill")
    elif text == BTN_CATEGORIES:
        await update.effective_message.reply_text("اختار القسم:", reply_markup=categories_menu())
    elif text == BTN_EXAM:
        await start_mode(update, context, "exam")
    elif text == BTN_REVIEW:
        await start_mode(update, context, "review")
    elif text == BTN_STATS:
        await show_stats(update, context)
    elif text == BTN_ID:
        await show_id(update, context)
    elif text == BTN_HELP:
        await show_help(update, context)
    elif text == BTN_CANCEL:
        await cancel(update, context)
    else:
        await update.effective_message.reply_text("اختار من الأزرار أو استخدم /start.", reply_markup=quick_keyboard())


async def setup_commands(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "فتح القائمة الرئيسية"),
            BotCommand("written", "تسميع كتابي"),
            BotCommand("paper", "حل على الورقة"),
            BotCommand("fill", "أكمل الفراغ"),
            BotCommand("exam", "امتحان تجريبي"),
            BotCommand("review", "مراجعة أخطائي"),
            BotCommand("stats", "مستوى الحفظ"),
            BotCommand("id", "رقم حسابي"),
            BotCommand("help", "المساعدة"),
            BotCommand("cancel", "إنهاء الجلسة"),
        ]
    )


async def command_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = (update.effective_message.text or "").split()[0].lstrip("/").split("@")[0]
    mapping = {
        "written": "written",
        "paper": "paper",
        "fill": "fill",
        "exam": "exam",
        "review": "review",
    }
    await start_mode(update, context, mapping[command])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود. ضعه في Environment على Render.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(setup_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["written", "paper", "fill", "exam", "review"], command_mode))
    app.add_handler(CommandHandler("stats", show_stats))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("users", users))
    app.add_handler(CommandHandler("exportarabic", export_arabic))
    app.add_handler(CommandHandler("reloadarabic", reload_arabic))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(mode_callback, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(category_callback, pattern=r"^category:"))
    app.add_handler(CallbackQueryHandler(category_mode_callback, pattern=r"^catmode:"))
    app.add_handler(CallbackQueryHandler(study_callback, pattern=r"^study:"))
    app.add_handler(CallbackQueryHandler(score_callback, pattern=r"^score:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_application()
    webhook_url = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
    if not webhook_url:
        render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip().strip("/")
        if render_hostname:
            webhook_url = f"https://{render_hostname}"

    if webhook_url:
        port = int(os.getenv("PORT", "10000"))
        path = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip("/")
        secret_token = os.getenv("WEBHOOK_SECRET", "").strip() or None
        if secret_token and (
            len(secret_token) > 256
            or any(not (ch.isalnum() or ch in "_-") for ch in secret_token)
        ):
            logger.warning("Ignoring invalid WEBHOOK_SECRET")
            secret_token = None
        logger.info("Starting webhook on port %s", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=path,
            webhook_url=f"{webhook_url}/{path}",
            secret_token=secret_token,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
