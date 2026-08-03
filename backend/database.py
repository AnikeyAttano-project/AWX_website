import random
import aiosqlite
from config import settings

# Алфавит реферальных кодов: без похожих символов O/0, I/1
_REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_referral_code(length: int = 8) -> str:
    """Случайный реферальный код: 8 символов A-Z, 0-9 (без O/0/I/1)."""
    return "".join(random.choice(_REFERRAL_ALPHABET) for _ in range(length))


def _mask_email(email: str) -> str:
    """Маскирует email для показа рефереру: user***@domain.com."""
    if not email:
        return ""
    local, _, domain = email.partition("@")
    if not local:
        return email
    if len(local) <= 2:
        masked = local[:1] + "***"
    else:
        masked = local[:2] + "***"
    return f"{masked}@{domain}"


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
    error_msg       TEXT,
    custom_name     TEXT
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

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id     TEXT NOT NULL,  -- кто пригласил
    referred_id     TEXT NOT NULL,  -- кто приглашён
    reward_days     INTEGER DEFAULT 0,  -- начисленные дни
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (referrer_id) REFERENCES users(id),
    FOREIGN KEY (referred_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS referral_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
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
            await db.execute("ALTER TABLE orders ADD COLUMN custom_name TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN user_id TEXT")
        except Exception:
            pass
        # Миграция: capability_token для защиты статуса заказа
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN capability_token TEXT")
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
        try:
            await db.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Миграция: users — реферальная система
        try:
            await db.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN referred_by TEXT")
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
        # Индексы для реферальной системы
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_referrals_pair "
                "ON referrals(referrer_id, referred_id)"
            )
        except Exception:
            pass
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code)"
        )
        # Настройки реферальной программы по умолчанию
        await db.execute(
            "INSERT OR IGNORE INTO referral_settings (key, value) VALUES ('referral_enabled', '1')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO referral_settings (key, value) VALUES ('bonus_percent', '10')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO referral_settings (key, value) VALUES ('level2_percent', '0')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO referral_settings (key, value) VALUES ('level3_percent', '0')"
        )
        await db.commit()


async def create_order(order_id: str, tariff: str, amount: float, capability_token: str = ""):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO orders (id, tariff, amount, capability_token) VALUES (?, ?, ?, ?)",
            (order_id, tariff, amount, capability_token),
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


async def set_order_custom_name(order_id: str, custom_name: str):
    """Set the user-facing custom name for a subscription."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE orders SET custom_name = ? WHERE id = ?",
            (custom_name, order_id),
        )
        await db.commit()


async def mark_order_deleted(order_id: str):
    """Mark an order as deleted (client removed from 3x-UI)."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE orders SET status = 'deleted' WHERE id = ?",
            (order_id,),
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


async def create_user(
    user_id: str,
    email: str,
    password_hash: str,
    verified: int = 1,
    referral_code: str = None,
):
    code = referral_code or generate_referral_code()
    async with aiosqlite.connect(settings.database_path) as db:
        # Гарантируем уникальность реферального кода
        for _ in range(30):
            cur = await db.execute(
                "SELECT 1 FROM users WHERE referral_code = ?", (code,)
            )
            if not await cur.fetchone():
                break
            code = generate_referral_code()
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified, referral_code) VALUES (?, ?, ?, ?, ?)",
            (user_id, email, password_hash, verified, code),
        )
        await db.commit()


async def set_user_verified(user_id: str):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("UPDATE users SET verified = 1 WHERE id = ?", (user_id,))
        await db.commit()


async def claim_trial(user_id: str, expires_at: str) -> bool:
    """Claim trial. Returns False if already used. Uses atomic UPDATE ... WHERE to avoid TOCTOU race."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "UPDATE users SET trial_started_at = datetime('now'), trial_expires_at = ? "
            "WHERE id = ? AND (trial_started_at IS NULL OR trial_started_at = '')",
            (expires_at, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ————————————————— Account queries —————————————————

async def get_user_subscriptions(user_id: str) -> list[dict]:
    """Get all orders (subscriptions) for a user, newest first. Deleted are hidden."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? AND status != 'deleted' ORDER BY created_at DESC",
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


# ————————————————— Referrals —————————————————

async def get_setting(key: str, default: str = "") -> str:
    """Читает настройку реферальной программы (таблица referral_settings)."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT value FROM referral_settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    """Записывает настройку реферальной программы."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO referral_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def ensure_referral_code(user_id: str) -> str | None:
    """Возвращает реферальный код пользователя, генерируя его при необходимости."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT referral_code FROM users WHERE id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        if row[0]:
            return row[0]
        # Старые пользователи без кода — генерируем уникальный
        for _ in range(50):
            code = generate_referral_code()
            cur = await db.execute(
                "SELECT 1 FROM users WHERE referral_code = ?", (code,)
            )
            if not await cur.fetchone():
                break
        await db.execute(
            "UPDATE users SET referral_code = ? WHERE id = ?", (code, user_id)
        )
        await db.commit()
        return code


async def get_user_by_referral_code(code: str) -> dict | None:
    """Находит пользователя по реферальному коду."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE UPPER(referral_code) = ?", (code.upper(),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_referrer(user_id: str) -> str | None:
    """Возвращает ID пригласившего пользователя (referrer) или None."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT referred_by FROM users WHERE id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def apply_referral_code(user_id: str, referrer_id: str) -> bool:
    """
    Привязывает приглашённого пользователя к рефереру.
    Однократно: повторный вызов возвращает False.
    Защищает от самоприглашения и циклов в цепочке.
    """
    if user_id == referrer_id:
        return False

    async with aiosqlite.connect(settings.database_path) as db:
        # Пользователь уже привязан к рефереру
        cur = await db.execute(
            "SELECT referred_by FROM users WHERE id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row and row[0]:
            return False

        # Защита от циклов: referrer не должен быть потомком пользователя
        current_id = referrer_id
        for _ in range(10):
            if current_id == user_id:
                return False
            cur = await db.execute(
                "SELECT referred_by FROM users WHERE id = ?", (current_id,)
            )
            row = await cur.fetchone()
            current_id = row[0] if row else None
            if not current_id:
                break

        cur = await db.execute(
            "UPDATE users SET referred_by = ? WHERE id = ? AND referred_by IS NULL",
            (referrer_id, user_id),
        )
        if cur.rowcount == 0:
            return False

        await db.execute(
            "INSERT INTO referrals (referrer_id, referred_id, reward_days) VALUES (?, ?, 0)",
            (referrer_id, user_id),
        )
        await db.commit()
        return True


async def add_referral_reward(referrer_id: str, referred_id: str, reward_days: int) -> bool:
    """Фиксирует начисление бонусных дней (суммируется по паре реферер/реферал)."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO referrals (referrer_id, referred_id, reward_days)
               VALUES (?, ?, ?)
               ON CONFLICT(referrer_id, referred_id) DO UPDATE SET
                   reward_days = reward_days + excluded.reward_days""",
            (referrer_id, referred_id, reward_days),
        )
        await db.commit()
        return True


async def count_referrals(referrer_id: str) -> int:
    """Сколько пользователей пригласил реферер."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def sum_reward_days(referrer_id: str) -> int:
    """Суммарное количество начисленных бонусных дней."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(reward_days), 0) FROM referrals WHERE referrer_id = ?",
            (referrer_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_referral_list(referrer_id: str) -> list[dict]:
    """Список приглашённых: referred_id, masked_email, reward_days, created_at."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT r.referred_id, r.reward_days, r.created_at, u.email
               FROM referrals r
               JOIN users u ON u.id = r.referred_id
               WHERE r.referrer_id = ?
               ORDER BY r.created_at DESC""",
            (referrer_id,),
        )
        rows = await cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["masked_email"] = _mask_email(d.pop("email", ""))
            result.append(d)
        return result


async def get_referral_levels() -> list[tuple[int, int]]:
    """
    Возвращает включённые уровни реферальной программы.
    Каждый уровень — (номер, процент). Уровень 2 и 3 опциональны (0 = выключен).
    """
    if (await get_setting("referral_enabled", "1")) != "1":
        return []
    levels = []
    p1 = int(await get_setting("bonus_percent", "10") or 0)
    if p1 > 0:
        levels.append((1, p1))
    p2 = int(await get_setting("level2_percent", "0") or 0)
    if p2 > 0:
        levels.append((2, p2))
    p3 = int(await get_setting("level3_percent", "0") or 0)
    if p3 > 0:
        levels.append((3, p3))
    return levels


async def get_active_subscription(user_id: str) -> dict | None:
    """Последняя оплаченная подписка пользователя с привязкой к 3x-UI."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM orders
               WHERE user_id = ? AND status = 'paid'
                     AND xui_email IS NOT NULL AND xui_email != ''
               ORDER BY created_at DESC LIMIT 1""",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ————————————————— Runtime settings (persisted to settings table) —————————————————

async def load_runtime_settings():
    """Load admin-editable settings from the settings table into the in-memory settings object."""
    import json
    try:
        async with aiosqlite.connect(settings.database_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT key, value FROM settings")
            rows = await cur.fetchall()
        for row in rows:
            key = row["key"]
            try:
                value = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                value = row["value"]
            if key == "tariffs" and isinstance(value, dict):
                settings.tariffs = value
            elif key == "trial" and isinstance(value, dict):
                settings.trial_enabled = bool(value.get("enabled", True))
                settings.trial_days = int(value.get("days", settings.trial_days))
                settings.trial_gb = int(value.get("gb", settings.trial_gb))
                settings.trial_devices = int(value.get("devices", settings.trial_devices))
            elif key == "demo_mode" and isinstance(value, dict):
                settings.demo_mode = bool(value.get("enabled", False))
    except Exception:
        pass


async def save_settings_value(key: str, value) -> None:
    """Write a JSON-serializable value to the settings table."""
    import json
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, serialized),
        )
        await db.commit()


async def get_settings_value(key: str, default: str = "") -> str:
    """Read a raw string from the settings table."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


# ————————————————— Admin helpers —————————————————

async def set_user_blocked(user_id: str, blocked: bool):
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE users SET blocked = ? WHERE id = ?",
            (1 if blocked else 0, user_id),
        )
        await db.commit()


async def count_users(search: str = "") -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        if search:
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE email LIKE ?",
                (f"%{search}%",),
            )
        else:
            cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0] if row else 0


async def list_users_page(search: str, limit: int, offset: int) -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        if search:
            cur = await db.execute(
                """SELECT * FROM users WHERE email LIKE ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (f"%{search}%", limit, offset),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def delete_order(order_id: str):
    """Physically remove an order from the database."""
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        await db.commit()


async def cleanup_expired_orders(grace_days: int = 14) -> list[dict]:
    """Find and delete orders expired more than grace_days ago. Returns list of {id, xui_email}."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, xui_email FROM orders "
            "WHERE expires_at IS NOT NULL AND expires_at < datetime('now', ?) "
            "AND status IN ('paid', 'error')",
            (f'-{grace_days} days',)
        )
        rows = await cur.fetchall()
        expired = [dict(row) for row in rows]

        if expired:
            ids = [r["id"] for r in expired]
            placeholders = ",".join("?" * len(ids))
            await db.execute(f"DELETE FROM orders WHERE id IN ({placeholders})", ids)
            await db.commit()

        return expired


async def cleanup_expired_trials(grace_days: int = 14) -> int:
    """Reset trial fields for users whose trial expired more than grace_days ago. Returns count."""
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "UPDATE users SET trial_started_at = NULL, trial_expires_at = NULL "
            "WHERE trial_expires_at IS NOT NULL AND trial_expires_at < datetime('now', ?)",
            (f'-{grace_days} days',)
        )
        await db.commit()
        return cur.rowcount
