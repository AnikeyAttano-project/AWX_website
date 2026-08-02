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
"""


async def init_db():
    async with aiosqlite.connect(settings.database_path) as db:
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
):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """UPDATE orders
               SET xui_email = ?, xui_sub_id = ?, sub_url = ?, inbound_ids = ?
               WHERE id = ?""",
            (email, sub_id, sub_url, inbound_ids, order_id),
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
