import aiosqlite
from config import settings

_CREATE = """
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    tariff          TEXT NOT NULL,
    amount          REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    platega_tx_id   TEXT,
    xui_email       TEXT,
    xui_sub_id      TEXT,
    sub_url         TEXT,
    inbound_ids     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    paid_at         TEXT,
    expires_at      TEXT,
    error_msg       TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    telegram_id     TEXT,
    verified        INTEGER NOT NULL DEFAULT 1,
    trial_started_at TEXT,
    trial_expires_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db():
    async with aiosqlite.connect(settings.database_path) as db:
        # WAL mode — конкурентные чтения не блокируют запись
        await db.execute("PRAGMA journal_mode=WAL")
        # Блокировка БД до 5 секунд при одновременной записи
        await db.execute("PRAGMA busy_timeout=5000")

        await db.executescript(_CREATE)
        # Миграция: добавляем новые колонки если их нет
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN inbound_ids TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN expires_at TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN error_msg TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN user_id TEXT")
        except Exception:
            pass
        # Миграция: users — verified, trial
        try:
            await db.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN trial_started_at TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN trial_expires_at TEXT")
        except Exception:
            pass
        # Индексы для частых запросов
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_tx ON orders(platega_tx_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)"
        )
        await db.commit()


async def create_order(order_id: str, tariff: str, amount: float):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO orders (id, tariff, amount) VALUES (?, ?, ?)",
            (order_id, tariff, amount),
        )
        await db.commit()


async def get_order(order_id: str) -> dict | None:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def save_platega_tx(order_id: str, tx_id: str):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE orders SET platega_tx_id = ? WHERE id = ?",
            (tx_id, order_id),
        )
        await db.commit()


async def mark_paid(order_id: str):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE orders SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
            (order_id,),
        )
        await db.commit()


async def save_subscription(
    order_id: str,
    email: str,
    sub_id: str,
    sub_url: str,
    inbound_ids: str = "",
    expires_at: str = "",
):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """UPDATE orders
               SET xui_email = ?, xui_sub_id = ?, sub_url = ?, inbound_ids = ?, expires_at = ?
               WHERE id = ?""",
            (email, sub_id, sub_url, inbound_ids, expires_at, order_id),
        )
        await db.commit()


async def mark_order_error(order_id: str, error_msg: str):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE orders SET status = 'error', error_msg = ? WHERE id = ?",
            (error_msg, order_id),
        )
        await db.commit()


async def get_order_by_tx(tx_id: str) -> dict | None:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE platega_tx_id = ?", (tx_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ————————————————— Users —————————————————

async def get_user_by_email(email: str) -> dict | None:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict | None:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_user(user_id: str, email: str, password_hash: str, verified: int = 1):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified) VALUES (?, ?, ?, ?)",
            (user_id, email, password_hash, verified),
        )
        await db.commit()


async def set_user_verified(user_id: str):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("UPDATE users SET verified = 1 WHERE id = ?", (user_id,))
        await db.commit()


async def claim_trial(user_id: str, expires_at: str) -> bool:
    """Atomically claim trial. Returns False if already used."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT trial_started_at FROM users WHERE id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row and row[0]:
            return False
        await db.execute(
            "UPDATE users SET trial_started_at = datetime('now'), trial_expires_at = ? WHERE id = ?",
            (expires_at, user_id),
        )
        await db.commit()
        return True


# ————————————————— Account queries —————————————————

async def get_user_subscriptions(user_id: str) -> list[dict]:
    """Get all orders (subscriptions) for a user, newest first."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def get_user_order(user_id: str, order_id: str) -> dict | None:
    """Get a specific order belonging to a user."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_order_user(order_id: str, user_id: str):
    """Link an order to a user."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE orders SET user_id = ? WHERE id = ?",
            (user_id, order_id),
        )
        await db.commit()
