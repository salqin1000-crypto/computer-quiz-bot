from __future__ import annotations

import html
import json
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Poll, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = Path(os.getenv("QUESTIONS_PATH", BASE_DIR / "questions.json"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "quiz_bot.sqlite3"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

CATEGORY_LABELS = {
    "basics": "مفاهيم الحاسوب",
    "hardware": "المكونات المادية",
    "number_systems": "أنظمة العد وتمثيل البيانات",
    "software": "المكونات البرمجية",
    "algorithms": "الخوارزميات والبرمجة",
    "networks": "الشبكات والإنترنت",
    "security": "أمن المعلومات",
    "windows": "Windows 10",
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint",
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("computer_quiz_bot")


class QuestionBank:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.questions: list[dict[str, Any]] = []
        self.by_id: dict[int, dict[str, Any]] = {}
        self.reload()

    @staticmethod
    def validate_question(question: dict[str, Any]) -> None:
        required = {"id", "category", "type", "question", "options", "correct_index"}
        missing = required - question.keys()
        if missing:
            raise ValueError(f"حقول ناقصة: {sorted(missing)}")
        if question["type"] not in {"mcq", "true_false"}:
            raise ValueError("نوع السؤال يجب أن يكون mcq أو true_false")
        if not isinstance(question["options"], list) or len(question["options"]) < 2:
            raise ValueError("يجب أن يحتوي السؤال خيارين على الأقل")
        if not 0 <= int(question["correct_index"]) < len(question["options"]):
            raise ValueError("رقم الإجابة الصحيحة خارج نطاق الخيارات")

    def reload(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("questions.json يجب أن يحتوي قائمة أسئلة")
        ids: set[int] = set()
        for q in raw:
            self.validate_question(q)
            q["id"] = int(q["id"])
            if q["id"] in ids:
                raise ValueError(f"رقم سؤال مكرر: {q['id']}")
            ids.add(q["id"])
            q.setdefault("explanation", "")
            q.setdefault("source", "")
            q.setdefault("enabled", True)
        self.questions = raw
        self.by_id = {q["id"]: q for q in raw}
        logger.info("Loaded %s questions", len(self.questions))

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.questions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.reload()

    def enabled(self, *, category: str | None = None, qtype: str | None = None) -> list[dict[str, Any]]:
        result = [q for q in self.questions if q.get("enabled", True)]
        if category:
            result = [q for q in result if q["category"] == category]
        if qtype:
            result = [q for q in result if q["type"] == qtype]
        return result

    def add_many(self, incoming: list[dict[str, Any]]) -> int:
        next_id = max(self.by_id, default=0) + 1
        count = 0
        for item in incoming:
            q = dict(item)
            q["id"] = next_id
            q.setdefault("enabled", True)
            q.setdefault("explanation", "")
            q.setdefault("source", "إضافة إدارية")
            self.validate_question(q)
            self.questions.append(q)
            next_id += 1
            count += 1
        self.save()
        return count


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    last_seen INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    correct INTEGER NOT NULL DEFAULT 0,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    chosen_index INTEGER NOT NULL,
                    is_correct INTEGER NOT NULL,
                    answered_at INTEGER NOT NULL
                );
                """
            )

    def upsert_user(self, update: Update) -> None:
        user = update.effective_user
        if not user:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users(user_id, username, first_name, last_name, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    last_seen=excluded.last_seen
                """,
                (user.id, user.username, user.first_name, user.last_name, int(time.time())),
            )

    def start_attempt(self, user_id: int, category: str, mode: str, total: int) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO attempts(user_id, category, mode, total, started_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, category, mode, total, int(time.time())),
            )
            return int(cur.lastrowid)

    def record_response(self, attempt_id: int, question_id: int, chosen_index: int, is_correct: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO responses(attempt_id, question_id, chosen_index, is_correct, answered_at) VALUES (?, ?, ?, ?, ?)",
                (attempt_id, question_id, chosen_index, int(is_correct), int(time.time())),
            )

    def finish_attempt(self, attempt_id: int, correct: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE attempts SET correct=?, finished_at=? WHERE id=?",
                (correct, int(time.time()), attempt_id),
            )

    def user_stats(self, user_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) attempts,
                       COALESCE(SUM(total), 0) total,
                       COALESCE(SUM(correct), 0) correct,
                       COALESCE(MAX(finished_at), 0) last_finished
                FROM attempts
                WHERE user_id=? AND finished_at IS NOT NULL
                """,
                (user_id,),
            ).fetchone()
        return dict(row)

    def global_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            attempts = conn.execute("SELECT COUNT(*) FROM attempts WHERE finished_at IS NOT NULL").fetchone()[0]
            answers = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        return {"users": users, "attempts": attempts, "answers": answers}


BANK = QuestionBank(QUESTIONS_PATH)
DB = Database(DATABASE_PATH)


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 كويز تيليغرام — مثل الصورة", callback_data="menu:poll")],
            [InlineKeyboardButton("⚡ اختبار سريع — 10 أسئلة", callback_data="quiz:quick")],
            [InlineKeyboardButton("📝 امتحان شامل — 40 سؤالاً", callback_data="quiz:full")],
            [InlineKeyboardButton("📚 اختبار حسب القسم", callback_data="menu:categories")],
            [InlineKeyboardButton("✅ صح أو غلط", callback_data="quiz:true_false")],
            [InlineKeyboardButton("📈 نتائجي", callback_data="stats:me")],
            [InlineKeyboardButton("ℹ️ طريقة الاستخدام", callback_data="menu:help")],
        ]
    )


def poll_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚡ كويز سريع — 10 أسئلة", callback_data="poll:quick")],
            [InlineKeyboardButton("📝 كويز شامل — 40 سؤالاً", callback_data="poll:full")],
            [InlineKeyboardButton("📚 كويز حسب القسم", callback_data="menu:poll_categories")],
            [InlineKeyboardButton("✅ كويز صح أو غلط", callback_data="poll:true_false")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="menu:main")],
        ]
    )


def categories_menu(*, poll_mode: bool = False) -> InlineKeyboardMarkup:
    prefix = "pollcat" if poll_mode else "cat"
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{key}")]
        for key, label in CATEGORY_LABELS.items()
        if BANK.enabled(category=key)
    ]
    back = "menu:poll" if poll_mode else "menu:main"
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def quiz_session(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    session = context.user_data.get("quiz")
    return session if isinstance(session, dict) else None


def create_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool: list[dict[str, Any]],
    size: int,
    mode: str,
    category: str,
    *,
    delivery: str = "buttons",
) -> dict[str, Any]:
    selected = random.sample(pool, min(size, len(pool)))
    orders = {}
    for q in selected:
        order = list(range(len(q["options"])))
        random.shuffle(order)
        orders[str(q["id"])] = order
    attempt_id = DB.start_attempt(update.effective_user.id, category, mode, len(selected))
    session = {
        "question_ids": [q["id"] for q in selected],
        "orders": orders,
        "position": 0,
        "score": 0,
        "answered": False,
        "attempt_id": attempt_id,
        "mode": mode,
        "category": category,
        "delivery": delivery,
        "chat_id": update.effective_chat.id if update.effective_chat else update.effective_user.id,
        "started_at": int(time.time()),
    }
    context.user_data["quiz"] = session
    return session


def format_question(q: dict[str, Any], position: int, total: int) -> str:
    label = CATEGORY_LABELS.get(q["category"], q["category"])
    type_label = "صح أو غلط" if q["type"] == "true_false" else "اختيار من متعدد"
    return (
        f"<b>السؤال {position + 1} من {total}</b>\n"
        f"<i>{html.escape(label)} — {type_label}</i>\n\n"
        f"{html.escape(q['question'])}"
    )


def answer_keyboard(q: dict[str, Any], order: list[int], position: int) -> InlineKeyboardMarkup:
    letters = ["أ", "ب", "ج", "د", "هـ", "و"]
    rows = []
    for displayed_index, original_index in enumerate(order):
        option = q["options"][original_index]
        prefix = letters[displayed_index] if displayed_index < len(letters) else str(displayed_index + 1)
        rows.append(
            [InlineKeyboardButton(f"{prefix}) {option}", callback_data=f"ans:{position}:{displayed_index}")]
        )
    rows.append([InlineKeyboardButton("❌ إنهاء الاختبار", callback_data="quiz:cancel")])
    return InlineKeyboardMarkup(rows)


async def send_current_question(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
    session = quiz_session(context)
    if not session:
        await update.effective_message.reply_text("لا يوجد اختبار نشط.", reply_markup=main_menu())
        return
    pos = session["position"]
    ids = session["question_ids"]
    if pos >= len(ids):
        await finish_quiz(update, context)
        return
    q = BANK.by_id[ids[pos]]
    order = session["orders"][str(q["id"])]
    session["answered"] = False
    text = format_question(q, pos, len(ids))
    markup = answer_keyboard(q, order, pos)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def poll_explanation(q: dict[str, Any]) -> str | None:
    parts = []
    if q.get("explanation"):
        parts.append(str(q["explanation"]).strip())
    if q.get("source"):
        parts.append(f"المصدر: {str(q['source']).strip()}")
    text = " — ".join(part for part in parts if part)
    if not text:
        return None
    return text[:195]


async def send_current_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    session = quiz_session(context)
    if not session or session.get("delivery") != "poll":
        return
    pos = session["position"]
    ids = session["question_ids"]
    if pos >= len(ids):
        await finish_quiz(None, context)
        return

    q = BANK.by_id[ids[pos]]
    order = session["orders"][str(q["id"])]
    displayed_options = [str(q["options"][i])[:100] for i in order]
    correct_displayed_index = order.index(int(q["correct_index"]))
    label = CATEGORY_LABELS.get(q["category"], q["category"])
    question_text = f"{pos + 1}/{len(ids)} — {q['question']}\n{label}"[:300]

    message = await context.bot.send_poll(
        chat_id=session["chat_id"],
        question=question_text,
        options=displayed_options,
        type=Poll.QUIZ,
        correct_option_id=correct_displayed_index,
        is_anonymous=False,
        explanation=poll_explanation(q),
    )
    session["poll_id"] = message.poll.id
    session["poll_message_id"] = message.message_id
    session["answered"] = False


async def stop_active_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    session = quiz_session(context)
    if not session or session.get("delivery") != "poll":
        return
    message_id = session.get("poll_message_id")
    if not message_id:
        return
    try:
        await context.bot.stop_poll(chat_id=session["chat_id"], message_id=message_id)
    except Exception:
        logger.debug("Could not stop poll", exc_info=True)


async def finish_quiz(update: Update | None, context: ContextTypes.DEFAULT_TYPE, cancelled: bool = False) -> None:
    session = quiz_session(context)
    if not session:
        return
    total = len(session["question_ids"])
    correct = session["score"]
    DB.finish_attempt(session["attempt_id"], correct)
    percent = round((correct / total) * 100) if total else 0
    elapsed = max(0, int(time.time()) - session["started_at"])
    minutes, seconds = divmod(elapsed, 60)
    title = "تم إنهاء الاختبار" if cancelled else "انتهى الاختبار"
    text = (
        f"<b>{title} ✅</b>\n\n"
        f"النتيجة: <b>{correct} / {total}</b>\n"
        f"النسبة: <b>{percent}%</b>\n"
        f"الوقت: {minutes}:{seconds:02d}\n\n"
    )
    if percent >= 85:
        text += "ممتاز جداً! 🌟"
    elif percent >= 70:
        text += "نتيجة جيدة جداً 👏"
    elif percent >= 50:
        text += "جيد، راجع الأخطاء وجرّب مرة ثانية."
    else:
        text += "بدك مراجعة أكتر، وبالمحاولة الجاية بتتحسن."

    delivery = session.get("delivery", "buttons")
    chat_id = session.get("chat_id")
    control_message_id = session.get("control_message_id")
    context.user_data.pop("quiz", None)
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 اختبار جديد", callback_data="menu:main")],
            [InlineKeyboardButton("📈 نتائجي", callback_data="stats:me")],
        ]
    )

    if delivery == "poll" and chat_id:
        if control_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=control_message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                return
            except Exception:
                logger.debug("Could not edit poll control message", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if update and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    elif update and update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    elif chat_id:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    existing = quiz_session(context)
    if existing and existing.get("delivery") == "poll":
        await stop_active_poll(context)
    context.user_data.pop("quiz", None)
    DB.upsert_user(update)
    name = html.escape(update.effective_user.first_name or "")
    text = (
        f"أهلاً {name} 👋\n\n"
        "هذا بوت تدريبي لمادة <b>مهارات الحاسوب</b>. "
        f"يحتوي حالياً على <b>{len(BANK.enabled())}</b> سؤالاً من أسئلة الدورات والملخصات.\n\n"
        "اختار نوع الاختبار:"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    DB.upsert_user(update)
    data = query.data
    if data == "menu:main":
        context.user_data.pop("quiz", None)
        await query.edit_message_text("اختار نوع الاختبار:", reply_markup=main_menu())
    elif data == "menu:poll":
        await query.edit_message_text("اختار نوع كويز تيليغرام:", reply_markup=poll_menu())
    elif data == "menu:categories":
        await query.edit_message_text("اختار القسم:", reply_markup=categories_menu())
    elif data == "menu:poll_categories":
        await query.edit_message_text("اختار قسم كويز تيليغرام:", reply_markup=categories_menu(poll_mode=True))
    elif data == "menu:help":
        text = (
            "<b>طريقة الاستخدام</b>\n\n"
            "• نمط كويز تيليغرام يعرض السؤال كاستبيان Quiz مثل الصورة.\n"
            "• النمط العادي يعرض السؤال بأزرار مع شرح ومصدر.\n"
            "• الأسئلة والخيارات تتغير عشوائياً في كل محاولة.\n"
            "• استخدم /cancel لإنهاء الاختبار النشط، و/start للعودة للقائمة."
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="menu:main")]]),
        )


async def quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    DB.upsert_user(update)
    action = query.data.split(":", 1)[1]
    if action == "cancel":
        await finish_quiz(update, context, cancelled=True)
        return
    if action == "next":
        session = quiz_session(context)
        if not session:
            await query.edit_message_text("انتهت الجلسة. ابدأ اختباراً جديداً.", reply_markup=main_menu())
            return
        session["position"] += 1
        await send_current_question(update, context, edit=True)
        return
    if action == "quick":
        pool, size, mode, category = BANK.enabled(), 10, "quick", "all"
    elif action == "full":
        pool, size, mode, category = BANK.enabled(), 40, "full", "all"
    elif action == "true_false":
        pool, size, mode, category = BANK.enabled(qtype="true_false"), 15, "true_false", "all"
    else:
        await query.edit_message_text("خيار غير معروف.", reply_markup=main_menu())
        return
    if not pool:
        await query.edit_message_text("لا توجد أسئلة متاحة حالياً.", reply_markup=main_menu())
        return
    create_quiz(update, context, pool, size, mode, category)
    await send_current_question(update, context, edit=True)


async def poll_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    DB.upsert_user(update)
    action = query.data.split(":", 1)[1]

    if action == "cancel":
        await stop_active_poll(context)
        await finish_quiz(update, context, cancelled=True)
        return
    if action == "quick":
        pool, size, mode, category = BANK.enabled(), 10, "poll_quick", "all"
    elif action == "full":
        pool, size, mode, category = BANK.enabled(), 40, "poll_full", "all"
    elif action == "true_false":
        pool, size, mode, category = BANK.enabled(qtype="true_false"), 15, "poll_true_false", "all"
    else:
        await query.edit_message_text("خيار غير معروف.", reply_markup=poll_menu())
        return

    if not pool:
        await query.edit_message_text("لا توجد أسئلة متاحة حالياً.", reply_markup=poll_menu())
        return

    session = create_quiz(update, context, pool, size, mode, category, delivery="poll")
    session["control_message_id"] = query.message.message_id
    await query.edit_message_text(
        "بدأ كويز تيليغرام ✅\n\nجاوب على الاستبيان، والسؤال التالي بيطلع تلقائياً.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ إنهاء الكويز", callback_data="poll:cancel")]]
        ),
    )
    await send_current_poll(context)


async def poll_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    DB.upsert_user(update)
    category = query.data.split(":", 1)[1]
    pool = BANK.enabled(category=category)
    if not pool:
        await query.edit_message_text("لا توجد أسئلة بهذا القسم.", reply_markup=categories_menu(poll_mode=True))
        return

    session = create_quiz(update, context, pool, 20, "poll_category", category, delivery="poll")
    session["control_message_id"] = query.message.message_id
    await query.edit_message_text(
        f"بدأ كويز {CATEGORY_LABELS.get(category, category)} ✅\n\nجاوب على الاستبيان، والسؤال التالي بيطلع تلقائياً.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ إنهاء الكويز", callback_data="poll:cancel")]]
        ),
    )
    await send_current_poll(context)


async def poll_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    answer = update.poll_answer
    session = quiz_session(context)
    if not answer or not session or session.get("delivery") != "poll":
        return
    if answer.poll_id != session.get("poll_id") or session.get("answered"):
        return
    if not answer.option_ids:
        return

    displayed_index = int(answer.option_ids[0])
    pos = int(session["position"])
    qid = session["question_ids"][pos]
    q = BANK.by_id[qid]
    order = session["orders"][str(qid)]
    if displayed_index < 0 or displayed_index >= len(order):
        return

    original_index = order[displayed_index]
    is_correct_answer = original_index == int(q["correct_index"])
    session["answered"] = True
    if is_correct_answer:
        session["score"] += 1
    DB.record_response(session["attempt_id"], qid, original_index, is_correct_answer)
    await stop_active_poll(context)

    session["position"] += 1
    if session["position"] >= len(session["question_ids"]):
        await finish_quiz(None, context)
    else:
        await send_current_poll(context)


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    DB.upsert_user(update)
    category = query.data.split(":", 1)[1]
    pool = BANK.enabled(category=category)
    if not pool:
        await query.edit_message_text("لا توجد أسئلة بهذا القسم.", reply_markup=categories_menu())
        return
    create_quiz(update, context, pool, 20, "category", category)
    await send_current_question(update, context, edit=True)


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    session = quiz_session(context)
    if not session:
        await query.answer("انتهت جلسة الاختبار.", show_alert=True)
        return
    _, pos_text, displayed_text = query.data.split(":")
    pos = int(pos_text)
    displayed_index = int(displayed_text)
    if pos != session["position"] or session.get("answered"):
        await query.answer("تمت الإجابة عن هذا السؤال.", show_alert=False)
        return

    qid = session["question_ids"][pos]
    q = BANK.by_id[qid]
    order = session["orders"][str(qid)]
    if displayed_index >= len(order):
        await query.answer("إجابة غير صالحة.", show_alert=True)
        return
    original_index = order[displayed_index]
    is_correct_answer = original_index == q["correct_index"]
    session["answered"] = True
    if is_correct_answer:
        session["score"] += 1
    DB.record_response(session["attempt_id"], qid, original_index, is_correct_answer)
    await query.answer("إجابة صحيحة ✅" if is_correct_answer else "إجابة خاطئة ❌")

    correct_text = q["options"][q["correct_index"]]
    result = "✅ <b>إجابة صحيحة</b>" if is_correct_answer else "❌ <b>إجابة خاطئة</b>"
    text = format_question(q, pos, len(session["question_ids"]))
    text += f"\n\n{result}\nالإجابة الصحيحة: <b>{html.escape(correct_text)}</b>"
    if q.get("explanation"):
        text += f"\n\n💡 {html.escape(q['explanation'])}"
    if q.get("source"):
        text += f"\n\n<i>المصدر: {html.escape(q['source'])}</i>"
    last = pos + 1 >= len(session["question_ids"])
    button = InlineKeyboardButton("🏁 عرض النتيجة", callback_data="quiz:next") if last else InlineKeyboardButton("السؤال التالي ➡️", callback_data="quiz:next")
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[button]]))


async def stats_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    stats = DB.user_stats(update.effective_user.id)
    attempts = stats["attempts"]
    total = stats["total"]
    correct = stats["correct"]
    percent = round((correct / total) * 100) if total else 0
    text = (
        "<b>إحصاءاتك 📊</b>\n\n"
        f"الاختبارات المكتملة: <b>{attempts}</b>\n"
        f"الأسئلة المجابة: <b>{total}</b>\n"
        f"الإجابات الصحيحة: <b>{correct}</b>\n"
        f"النسبة العامة: <b>{percent}%</b>"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="menu:main")]]),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = quiz_session(context)
    if session:
        if session.get("delivery") == "poll":
            await stop_active_poll(context)
        await finish_quiz(update, context, cancelled=True)
    else:
        await update.effective_message.reply_text("لا يوجد اختبار نشط.", reply_markup=main_menu())


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(f"رقم حسابك في تيليغرام: `{user.id}`", parse_mode=ParseMode.MARKDOWN)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("هذا الأمر مخصص للإدارة.")
        return
    stats = DB.global_stats()
    text = (
        "<b>لوحة الإدارة</b>\n\n"
        f"الأسئلة: {len(BANK.questions)}\n"
        f"المستخدمون: {stats['users']}\n"
        f"الاختبارات المكتملة: {stats['attempts']}\n"
        f"الإجابات المسجلة: {stats['answers']}\n\n"
        "الأوامر:\n"
        "/exportq تصدير بنك الأسئلة\n"
        "/importq ثم أرسل ملف JSON\n"
        "/addq قسم | نوع | السؤال | خيار1 | خيار2 | ... | رقم الصحيح | الشرح\n"
        "/delq رقم_السؤال\n"
        "/reloadq إعادة تحميل البنك"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def export_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    await update.effective_message.reply_document(document=QUESTIONS_PATH.open("rb"), filename="questions.json")


async def import_questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    context.user_data["awaiting_question_import"] = True
    await update.effective_message.reply_text(
        "أرسل الآن ملف JSON يحتوي قائمة أسئلة. ستُضاف الأسئلة بأرقام جديدة دون حذف البنك الحالي."
    )


async def import_questions_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id) or not context.user_data.get("awaiting_question_import"):
        return
    document = update.effective_message.document
    if not document or not document.file_name.lower().endswith(".json"):
        await update.effective_message.reply_text("أرسل ملفاً بامتداد JSON.")
        return
    file = await document.get_file()
    target = BASE_DIR / "data" / "incoming_questions.json"
    await file.download_to_drive(custom_path=target)
    try:
        incoming = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(incoming, list):
            raise ValueError("الملف يجب أن يحتوي قائمة")
        count = BANK.add_many(incoming)
    except Exception as exc:
        logger.exception("Question import failed")
        await update.effective_message.reply_text(f"فشل الاستيراد: {exc}")
        return
    finally:
        target.unlink(missing_ok=True)
    context.user_data.pop("awaiting_question_import", None)
    await update.effective_message.reply_text(f"تمت إضافة {count} سؤالاً بنجاح ✅")


async def add_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    if not raw:
        await update.effective_message.reply_text(
            "الصيغة:\n/addq القسم | mcq | السؤال | خيار1 | خيار2 | خيار3 | خيار4 | رقم الإجابة الصحيحة | الشرح\n"
            "رقم الإجابة يبدأ من 1. للصح والخطأ استخدم true_false وخياري صح | خطأ."
        )
        return
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 7:
        await update.effective_message.reply_text("الصيغة ناقصة. استخدم /admin لعرض المثال.")
        return
    category, qtype, question = parts[0], parts[1], parts[2]
    try:
        correct_number = int(parts[-2])
    except ValueError:
        await update.effective_message.reply_text("رقم الإجابة الصحيحة يجب أن يكون رقماً.")
        return
    explanation = parts[-1]
    options = parts[3:-2]
    item = {
        "category": category,
        "type": qtype,
        "question": question,
        "options": options,
        "correct_index": correct_number - 1,
        "explanation": explanation,
        "source": "إضافة إدارية",
        "enabled": True,
    }
    try:
        BANK.add_many([item])
    except Exception as exc:
        await update.effective_message.reply_text(f"تعذر إضافة السؤال: {exc}")
        return
    await update.effective_message.reply_text("تمت إضافة السؤال ✅")


async def delete_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("استخدم: /delq رقم_السؤال")
        return
    qid = int(context.args[0])
    before = len(BANK.questions)
    BANK.questions = [q for q in BANK.questions if q["id"] != qid]
    if len(BANK.questions) == before:
        await update.effective_message.reply_text("لم أجد هذا السؤال.")
        return
    BANK.save()
    await update.effective_message.reply_text("تم حذف السؤال ✅")


async def reload_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    try:
        BANK.reload()
        await update.effective_message.reply_text(f"تم تحميل {len(BANK.questions)} سؤالاً ✅")
    except Exception as exc:
        await update.effective_message.reply_text(f"فشل التحميل: {exc}")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("استخدم /start لفتح قائمة الاختبارات.", reply_markup=main_menu())


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود. انسخ .env.example إلى .env وضع توكن BotFather.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("exportq", export_questions))
    app.add_handler(CommandHandler("importq", import_questions_command))
    app.add_handler(CommandHandler("addq", add_question))
    app.add_handler(CommandHandler("delq", delete_question))
    app.add_handler(CommandHandler("reloadq", reload_questions))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(quiz_callback, pattern=r"^quiz:"))
    app.add_handler(CallbackQueryHandler(poll_quiz_callback, pattern=r"^poll:"))
    app.add_handler(CallbackQueryHandler(category_callback, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(poll_category_callback, pattern=r"^pollcat:"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^ans:"))
    app.add_handler(CallbackQueryHandler(stats_me, pattern=r"^stats:me$"))
    app.add_handler(PollAnswerHandler(poll_answer_callback))
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), import_questions_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
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
        if secret_token and (len(secret_token) > 256 or any(not (ch.isalnum() or ch in "_-") for ch in secret_token)):
            logger.warning("Ignoring invalid WEBHOOK_SECRET; only letters, digits, underscore and hyphen are allowed")
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
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
