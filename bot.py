"""Inner Coach Bot — личный коуч в Telegram на Claude, С ПАМЯТЬЮ.

Помнит переписку и держит связь между разговорами:
  - вся история хранится в Postgres (переживает перезапуск и передеплой);
  - коуч сам сохраняет важное в долгую память (инструмент remember) и видит её
    в начале каждого нового разговора — узнаёт человека, возвращает паттерны.

Память включается переменной DATABASE_URL (Railway Postgres или Supabase).
Без неё бот работает в лёгком режиме: помнит только текущий запуск.

Личность коуча: переменная COACH_PROMPT (приватно) или файл personality.md.
"""
import os
import logging
from collections import defaultdict, deque

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("inner-coach-bot")

# ---- настройки из переменных окружения ----
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.getenv("MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
HISTORY = int(os.getenv("HISTORY", "16"))  # сколько последних реплик подгружать

_allowed = os.getenv("ALLOWED_USER_IDS", "").replace(" ", "")
ALLOWED_USER_IDS = {int(x) for x in _allowed.split(",") if x} if _allowed else set()

MEMORY_ON = db.enabled()  # уточняется в main() после попытки поднять базу


def load_personality() -> str:
    """Личность коуча: COACH_PROMPT в приоритете, иначе personality.md."""
    env_prompt = os.getenv("COACH_PROMPT", "").strip()
    if env_prompt:
        return env_prompt
    try:
        with open("personality.md", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    except FileNotFoundError:
        pass
    return (
        "Ты тёплый и честный коуч. Рядом и держишь, без сахара. Возвращаешь в тело "
        "(три вдоха, где это в теле) и задаёшь один точный вопрос."
    )


BASE_PROMPT = load_personality()

REMEMBER_TOOL = {
    "name": "remember",
    "description": (
        "Сохрани важное о человеке в долгую память, чтобы помнить это в будущих "
        "разговорах: повторяющийся паттерн, значимый инсайт или важный факт о нём. "
        "Вызывай, когда замечаешь то, что стоит держать в памяти надолго. "
        "Не сохраняй мелочи и не дублируй уже известное."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["pattern", "insight", "fact"],
                "description": "pattern — повторяющийся цикл; insight — осознание; fact — факт о человеке",
            },
            "content": {"type": "string", "description": "одно ёмкое предложение"},
        },
        "required": ["kind", "content"],
    },
}

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# лёгкий режим: история в памяти процесса (сбрасывается при перезапуске)
_mem_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY * 2))


def allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def build_system(user_id: int) -> str:
    """Личность + блок долгой памяти (если включена)."""
    if not MEMORY_ON:
        return BASE_PROMPT
    items = db.get_memory(user_id)
    if not items:
        return (
            BASE_PROMPT
            + "\n\n## ЧТО ТЫ ПОМНИШЬ О ЧЕЛОВЕКЕ\nпока ничего, это начало. когда "
            "заметишь важное (паттерн, инсайт, факт) — сохрани через remember."
        )
    lines = "\n".join(f"- [{kind}] {content}" for kind, content in items)
    return (
        BASE_PROMPT
        + "\n\n## ЧТО ТЫ ПОМНИШЬ О ЧЕЛОВЕКЕ (из прошлых разговоров)\n"
        + lines
        + "\n\nопирайся на это и возвращай связь («в прошлый раз ты…»), но не "
        "пересказывай списком. заметишь новое важное — сохрани через remember."
    )


def history_for(user_id: int) -> list[dict]:
    if MEMORY_ON:
        return db.recent_messages(user_id, HISTORY * 2)
    return list(_mem_history[user_id])


def run_turn(user_id: int, system: str, convo: list[dict], tools: list[dict]) -> str:
    """Запрос к Claude с обработкой инструмента remember. Возвращает финальный текст."""
    resp = None
    for _ in range(4):  # защита от зацикливания на инструментах
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tools,
            messages=convo,
        )
        if resp.stop_reason != "tool_use":
            break
        convo.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use" and block.name == "remember":
                try:
                    db.save_memory(
                        user_id,
                        block.input.get("kind", "fact"),
                        block.input.get("content", ""),
                    )
                    out = "сохранено в долгую память"
                except Exception:
                    log.exception("не удалось сохранить память")
                    out = "не удалось сохранить"
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": out}
                )
        convo.append({"role": "user", "content": results})
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "привет. я рядом. с чем ты сейчас — что в теле и что на сегодня?"
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        log.warning("сообщение от чужого user_id=%s, пропускаю", update.effective_user.id)
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_text = update.message.text

    # история + текущая реплика (в базу пишем только при успехе, без сирот)
    convo = history_for(user_id) + [{"role": "user", "content": user_text}]
    system = build_system(user_id)
    tools = [REMEMBER_TOOL] if MEMORY_ON else []

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        answer = run_turn(user_id, system, convo, tools)
    except Exception:
        log.exception("ошибка при запросе к Claude")
        await update.message.reply_text(
            "что-то сломалось на моей стороне. попробуй ещё раз через минуту."
        )
        return

    if not answer:
        answer = "я тебя услышала, но не нашла слов. скажи иначе?"

    # сохраняем удачный обмен парой (user + assistant)
    if MEMORY_ON:
        try:
            db.save_message(user_id, "user", user_text)
            db.save_message(user_id, "assistant", answer)
        except Exception:
            log.exception("не удалось сохранить переписку (ответ всё равно отправлю)")
    else:
        _mem_history[user_id].append({"role": "user", "content": user_text})
        _mem_history[user_id].append({"role": "assistant", "content": answer})

    await update.message.reply_text(answer)


def main() -> None:
    global MEMORY_ON
    if MEMORY_ON:
        try:
            db.init_db()
            log.info("память включена (Postgres)")
        except Exception:
            log.exception("не удалось подключиться к базе — память выключена, лёгкий режим")
            MEMORY_ON = False
    else:
        log.warning(
            "DATABASE_URL не задан — память выключена (лёгкий режим). "
            "добавь базу, чтобы бот помнил между перезапусками."
        )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Inner Coach Bot запущен (model=%s, memory=%s)", MODEL, MEMORY_ON)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
