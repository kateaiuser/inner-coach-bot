"""Слой памяти на Postgres.

Две таблицы:
  messages — вся переписка (история переживает перезапуск).
  memory   — долгая память: инсайты, паттерны, факты о человеке. Подтягивается
             в начало каждого разговора, чтобы коуч помнил между диалогами.

Подключение через переменную DATABASE_URL (Railway Postgres или Supabase).
Если её нет — память выключена, бот работает в лёгком режиме (см. bot.py).
Соединение открывается на каждую операцию: для личного бота это надёжнее пула
(не висят мёртвые коннекты) и достаточно быстро на фоне запроса к Claude.
"""
import os
import contextlib

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(DATABASE_URL)


@contextlib.contextmanager
def _cursor():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            yield cur


def init_db() -> None:
    """Создать таблицы, если их ещё нет. Вызывается один раз при старте."""
    with _cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT      NOT NULL,
                role       TEXT        NOT NULL,
                content    TEXT        NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (user_id, id)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT      NOT NULL,
                kind       TEXT        NOT NULL,
                content    TEXT        NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_user ON memory (user_id, id)"
        )


def save_message(user_id: int, role: str, content: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
            (user_id, role, content),
        )


def recent_messages(user_id: int, limit: int) -> list[dict]:
    """Последние `limit` сообщений в хронологическом порядке.

    Мы всегда пишем парами (user, assistant), поэтому чётный срез с конца
    начинается с user — это валидная последовательность для Claude.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT role, content FROM messages WHERE user_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (user_id, limit),
        )
        rows = cur.fetchall()
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]


def save_memory(user_id: int, kind: str, content: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO memory (user_id, kind, content) VALUES (%s, %s, %s)",
            (user_id, kind, content),
        )


def get_memory(user_id: int, limit: int = 50) -> list[tuple[str, str]]:
    """Долгая память человека (kind, content), от старых к новым."""
    with _cursor() as cur:
        cur.execute(
            "SELECT kind, content FROM memory WHERE user_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (user_id, limit),
        )
        rows = cur.fetchall()
    rows.reverse()
    return [(kind, content) for kind, content in rows]
