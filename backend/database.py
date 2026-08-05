import json
import random
import uuid
from datetime import datetime

import aiosqlite
from contextlib import asynccontextmanager
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
    is_test_account INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS device_addons (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    addon_type      TEXT NOT NULL,
    extra_devices   INTEGER NOT NULL,
    amount_paid     REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT DEFAULT (datetime('now')),
    expires_at      TEXT,
    platega_tx_id   TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS debug_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_name      TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_user_id  TEXT,
    details_json    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS site_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    level           TEXT NOT NULL DEFAULT 'info',
    action          TEXT NOT NULL,
    actor           TEXT,
    details         TEXT
);

CREATE TABLE IF NOT EXISTS promo_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'percent',   -- 'percent' | 'fixed'
    value           REAL NOT NULL DEFAULT 0,            -- 10 (процентов) или 100 (рублей)
    max_uses        INTEGER NOT NULL DEFAULT 0,         -- 0 = безлимит
    used_count      INTEGER NOT NULL DEFAULT 0,
    expires_at      TEXT,                                -- NULL = бессрочно
    tariff_group    TEXT,                                -- ограничение на группу тарифов (NULL = все)
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS renewals (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    days            INTEGER NOT NULL,
    amount          REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',    -- pending | active | failed | cancelled
    platega_tx_id   TEXT,
    provider        TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


@asynccontextmanager
async def _db():
    """Открывает соединение с настроенными PRAGMA.

    ВАЖНО: aiosqlite НЕ коммитит при выходе из контекста (только close()) —
    поэтому записи фиксируются здесь явно: commit на успех, rollback на ошибку.

    Это же закрывает per-connection PRAGMA: journal_mode=WAL хранится в файле БД,
    а synchronous / busy_timeout / cache_size / temp_store / mmap_size /
    foreign_keys действуют только на текущее соединение — их надо применять
    к каждому новому соединению, а не один раз в init_db().
    """
    conn = await aiosqlite.connect(settings.database_path)
    try:
        await conn.execute("PRAGMA synchronous=NORMAL")       # оптимум для WAL
        await conn.execute("PRAGMA busy_timeout=10000")        # ждать до 10с
        await conn.execute("PRAGMA cache_size=-32768")         # 32MB кэш страниц
        await conn.execute("PRAGMA temp_store=MEMORY")         # temp-таблицы в RAM
        await conn.execute("PRAGMA mmap_size=134217728")       # 128MB mmap I/O
        await conn.execute("PRAGMA foreign_keys=ON")           # целостность FK
        yield conn
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def init_db():
    async with _db() as db:
        # WAL mode — конкурентные чтения не блокируют запись.
        # Это персистентная настройка файла БД (в отличие от per-connection PRAGMA,
        # которые применяет _db()). Должна выполняться вне транзакции — на свежем
        # соединении от _db() её ещё нет.
        await db.execute("PRAGMA journal_mode=WAL")

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
        # Миграция: промо-коды — код и размер скидки на заказе
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN promo_code TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN promo_discount REAL")
        except Exception:
            pass
        # Миграция: платёжный провайдер заказа (для обратной совместимости polling)
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN provider TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE device_addons ADD COLUMN provider TEXT")
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
        # Миграция: users — дебаг-песочница
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_test_account INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Миграция: fulfill state machine — durable статус выдачи ключа.
        # Держим отдельно от status (оплата): payment 'paid' + fulfillment 'processing'.
        # После добавления колонки бэкфиллим существующие заказы.
        try:
            await db.execute(
                "ALTER TABLE orders ADD COLUMN fulfillment_status TEXT NOT NULL DEFAULT 'pending'"
            )
            await db.execute(
                "ALTER TABLE orders ADD COLUMN fulfillment_started_at TEXT"
            )
            # Бэкфилл: уже выданные ключи и заказы с ошибкой
            await db.execute(
                "UPDATE orders SET fulfillment_status='completed' "
                "WHERE sub_url IS NOT NULL AND sub_url != ''"
            )
            await db.execute(
                "UPDATE orders SET fulfillment_status='failed' WHERE status='error'"
            )
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
        # Индексы для платных продлений (renewals)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_renewals_order ON renewals(order_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_renewals_tx ON renewals(platega_tx_id)"
        )
        # Индексы для дебаг-аудита
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_debug_audit_user ON debug_audit_log(target_user_id)"
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


async def create_order(order_id: str, tariff: str, amount: float, capability_token: str = "",
                       promo_code: str = None, promo_discount: float = 0.0,
                       provider: str = ""):
    async with _db() as db:
        await db.execute(
            "INSERT INTO orders (id, tariff, amount, capability_token, promo_code, promo_discount, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, tariff, amount, capability_token, promo_code, promo_discount, provider),
        )
        await db.commit()


async def get_order(order_id: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def save_platega_tx(order_id: str, tx_id: str):
    async with _db() as db:
        await db.execute(
            "UPDATE orders SET platega_tx_id = ? WHERE id = ?",
            (tx_id, order_id),
        )
        await db.commit()


async def mark_paid(order_id: str):
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
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
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """UPDATE orders
               SET xui_email = ?, xui_sub_id = ?, sub_url = ?, inbound_ids = ?, expires_at = ?
               WHERE id = ?""",
            (email, sub_id, sub_url, inbound_ids, expires_at, order_id),
        )
        await db.commit()


async def mark_order_error(order_id: str, error_msg: str):
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE orders SET status = 'error', error_msg = ? WHERE id = ?",
            (error_msg, order_id),
        )
        await db.commit()


# ————————————————— Fulfill state machine —————————————————
# fulfillment_status — вторая ось состояния заказа, отдельная от status (оплата):
#   status='paid'            + fulfillment_status='pending'    — деньги пришли, ключ не выдавали
#   status='paid'            + fulfillment_status='processing' — ключ выдаётся прямо сейчас
#   status='paid'            + fulfillment_status='completed'  — ключ выдан (sub_url заполнен)
#   status='error'           + fulfillment_status='failed'     — выдача не удалась (можно ретраить)
# durable 'processing' даёт crash-recovery: если процесс упал посреди выдачи,
# claim остаётся в БД и пере-claim'ится после протухания (STALE_FULFILLMENT_SECONDS).

# 'processing' старше этого срока считается брошенным (сервер упал) — пере-claim'им.
STALE_FULFILLMENT_SECONDS = 120


async def begin_fulfillment(order_id: str) -> bool:
    """Атомарно занимает заказ на выдачу ключа (claim).

    pending/failed → processing. Также пере-claim'ит 'processing', если выдача
    была прервана (fulfillment_started_at старше STALE_FULFILLMENT_SECONDS).

    Возвращает True, если ЭТОТ вызов теперь ответственен за выдачу ключа;
    False — заказ уже выполняется/выполнен другим запросом.
    """
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """UPDATE orders
               SET fulfillment_status = 'processing',
                   fulfillment_started_at = datetime('now')
               WHERE (id = ? AND fulfillment_status IN ('pending', 'failed'))
                  OR (id = ? AND fulfillment_status = 'processing'
                      AND (fulfillment_started_at IS NULL
                           OR fulfillment_started_at <= datetime('now', ?)))
               """,
            (order_id, order_id, f'-{STALE_FULFILLMENT_SECONDS} seconds'),
        )
        return cur.rowcount > 0


async def complete_fulfillment(order_id: str) -> bool:
    """processing → completed (ключ выдан, sub_url сохранён)."""
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """UPDATE orders
               SET fulfillment_status = 'completed',
                   fulfillment_started_at = NULL
               WHERE id = ? AND fulfillment_status = 'processing'""",
            (order_id,),
        )
        return cur.rowcount > 0


async def fail_fulfillment(order_id: str, error: str) -> bool:
    """processing → failed (выдача не удалась; заказ при этом помечается 'error')."""
    async with _db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """UPDATE orders
               SET fulfillment_status = 'failed',
                   fulfillment_started_at = NULL,
                   error_msg = ?
               WHERE id = ? AND fulfillment_status = 'processing'""",
            (str(error)[:2000], order_id),
        )
        return cur.rowcount > 0


async def set_order_custom_name(order_id: str, custom_name: str):
    """Set the user-facing custom name for a subscription."""
    async with _db() as db:
        await db.execute(
            "UPDATE orders SET custom_name = ? WHERE id = ?",
            (custom_name, order_id),
        )
        await db.commit()


async def mark_order_deleted(order_id: str):
    """Mark an order as deleted (client removed from 3x-UI)."""
    async with _db() as db:
        await db.execute(
            "UPDATE orders SET status = 'deleted' WHERE id = ?",
            (order_id,),
        )
        await db.commit()


async def get_order_by_tx(tx_id: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE platega_tx_id = ?", (tx_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ————————————————— Users —————————————————

async def get_user_by_email(email: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict | None:
    async with _db() as db:
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
    async with _db() as db:
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
    async with _db() as db:
        await db.execute("UPDATE users SET verified = 1 WHERE id = ?", (user_id,))
        await db.commit()


# ————————————————— TELEGRAM LOGIN —————————————————

async def get_user_by_telegram(telegram_id) -> dict | None:
    """Находит пользователя по telegram_id (для входа через Telegram)."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (str(telegram_id),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_telegram_user(user_id: str, telegram_id) -> str:
    """Создаёт пользователя, вошедшего через Telegram.

    Email генерируется как ``tg_{telegram_id}@t.me`` (уникальный), password_hash —
    случайная заглушка (вход по паролю для такого юзера невозможен), verified=1.
    Возвращает email. Коллизию email нужно проверять ДО вызова (route).
    """
    email = f"tg_{telegram_id}@t.me"
    password_hash = "!telegram:" + uuid.uuid4().hex
    code = generate_referral_code()
    async with _db() as db:
        for _ in range(30):
            cur = await db.execute(
                "SELECT 1 FROM users WHERE referral_code = ?", (code,)
            )
            if not await cur.fetchone():
                break
            code = generate_referral_code()
        await db.execute(
            "INSERT INTO users (id, email, password_hash, telegram_id, verified, referral_code) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (user_id, email, password_hash, str(telegram_id), code),
        )
        await db.commit()
    return email


async def set_user_telegram(user_id: str, telegram_id) -> None:
    """Привязывает telegram_id к существующему пользователю (OAuth-привязка в ЛК)."""
    async with _db() as db:
        await db.execute(
            "UPDATE users SET telegram_id = ? WHERE id = ?",
            (str(telegram_id), user_id),
        )
        await db.commit()


async def set_devices_admin_addon(order_id: str, user_id: str, extra_devices: int,
                                  expires_at: str = None) -> None:
    """Тестовый инструмент админки «Устройства»: приводит admin-аддон к нужному extra.

    Реальные купленные addon'ы не трогаем — пересоздаём только ``devices_admin``
    так, чтобы суммарный extra (реальные + admin) совпал с желаемым.
    ``extra_devices`` — желаемый extra СВЕРХ базового лимита тарифа (реальные
    addon'ы уже учтены в вызове). Если реальные уже покрывают — admin-аддон удаляется.
    """
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COALESCE(SUM(extra_devices), 0) AS s FROM device_addons "
            "WHERE order_id = ? AND status IN ('active','pending') AND addon_type != 'devices_admin'",
            (order_id,),
        )
        row = await cur.fetchone()
        real_extra = row["s"] if row else 0
        admin_extra = max(0, extra_devices - real_extra)

        await db.execute(
            "DELETE FROM device_addons WHERE order_id = ? AND addon_type = 'devices_admin'",
            (order_id,),
        )
        if admin_extra > 0:
            await db.execute(
                "INSERT INTO device_addons (id, user_id, order_id, addon_type, extra_devices, "
                "amount_paid, status, expires_at) VALUES (?, ?, ?, 'devices_admin', ?, 0, 'active', ?)",
                (uuid.uuid4().hex[:12], user_id, order_id, admin_extra,
                 expires_at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            )
        await db.commit()


# ————————————————— ОБЩИЙ ЛОГ ДЕЙСТВИЙ (site_log) —————————————————

async def add_site_log(action: str, actor: str = None, level: str = "info",
                       details: str = None) -> None:
    """Пишет запись в общий лог действий сайта.

    Никогда не бросает исключений (лог не должен ломать бизнес-логику):
    при ошибке записи только логирует предупреждение.
    """
    try:
        async with _db() as db:
            await db.execute(
                "INSERT INTO site_log (level, action, actor, details) VALUES (?, ?, ?, ?)",
                (level, action, actor, details),
            )
            await db.commit()
    except Exception:
        pass


async def get_site_logs(limit: int = 200) -> list[dict]:
    """Возвращает последние ``limit`` записей site_log (свежие сверху)."""
    limit = max(10, min(limit, 5000))
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM site_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def prune_site_log(keep: int = 5000) -> int:
    """Удаляет записи site_log сверх ``keep``; возвращает число удалённых."""
    async with _db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM (SELECT id FROM site_log ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        row = await cur.fetchone()
        count = row[0] if row else 0
        cur = await db.execute(
            "DELETE FROM site_log WHERE id NOT IN ("
            "SELECT id FROM (SELECT id FROM site_log ORDER BY id DESC LIMIT ?))",
            (keep,),
        )
        await db.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


# ————————————————— АНАЛИТИКА (витрина на основе site_log) —————————————————
# Все агрегации — SQL GROUP BY, без загрузки таблиц в Python.

async def get_analytics_funnel(days: int = 7) -> dict:
    """
    Воронка продаж за последние `days` дней, по дням:
    order_create → order_paid → fulfill.

    order_create/fulfill берутся из site_log; order_paid — из orders.paid_at
    (отдельного события в логе нет — оплата помечается напрямую в таблице).
    """
    window = f"-{days} days"
    async with _db() as db:
        db.row_factory = aiosqlite.Row

        async def _group(sql: str, param: str) -> dict[str, int]:
            cur = await db.execute(sql, (param,))
            return {r["d"]: r["n"] for r in await cur.fetchall()}

        create_rows = await _group(
            "SELECT date(ts) AS d, COUNT(*) AS n FROM site_log "
            "WHERE action = 'order_create' AND ts >= datetime('now', ?) "
            "GROUP BY date(ts)", window)
        paid_rows = await _group(
            "SELECT date(paid_at) AS d, COUNT(*) AS n FROM orders "
            "WHERE paid_at IS NOT NULL AND paid_at >= datetime('now', ?) "
            "GROUP BY date(paid_at)", window)
        fulfill_rows = await _group(
            "SELECT date(ts) AS d, COUNT(*) AS n FROM site_log "
            "WHERE action = 'fulfill' AND ts >= datetime('now', ?) "
            "GROUP BY date(ts)", window)

    all_dates = sorted(set(create_rows) | set(paid_rows) | set(fulfill_rows))
    rows = []
    for d in all_dates:
        c = create_rows.get(d, 0)
        p = paid_rows.get(d, 0)
        f = fulfill_rows.get(d, 0)
        rows.append({
            "date": d,
            "order_create": c,
            "order_paid": p,
            "fulfill": f,
            "conv_create_to_paid": round(p / c * 100, 1) if c else 0,
            "conv_paid_to_fulfill": round(f / p * 100, 1) if p else 0,
        })

    tc = sum(create_rows.values())
    tp = sum(paid_rows.values())
    tf = sum(fulfill_rows.values())
    return {
        "days": days,
        "rows": rows,
        "totals": {
            "order_create": tc,
            "order_paid": tp,
            "fulfill": tf,
            "conv_create_to_paid": round(tp / tc * 100, 1) if tc else 0,
            "conv_paid_to_fulfill": round(tf / tp * 100, 1) if tp else 0,
            "conv_overall": round(tf / tc * 100, 1) if tc else 0,
        },
    }


async def get_analytics_by_tariff(days: int = 30) -> list[dict]:
    """Оплаченные заказы за последние `days` дней по тарифам: количество и выручка."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT tariff, COUNT(*) AS cnt, ROUND(SUM(amount), 2) AS revenue "
            "FROM orders "
            "WHERE paid_at IS NOT NULL AND paid_at >= datetime('now', ?) "
            "GROUP BY tariff ORDER BY revenue DESC, cnt DESC",
            (f"-{days} days",),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_analytics_anomalies() -> dict:
    """
    Простая эвристика злоупотреблений/багов:
      - renew_heavy: >3 платных продлений за 7 дней на одного actor;
      - addon_burst: >5 покупок add-on за один день на одного actor.
    """
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT actor, COUNT(*) AS cnt FROM site_log "
            "WHERE action = 'renew' AND ts >= datetime('now', '-7 days') "
            "GROUP BY actor HAVING COUNT(*) > 3 ORDER BY cnt DESC",
        )
        renew_heavy = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT actor, date(ts) AS day, COUNT(*) AS cnt FROM site_log "
            "WHERE action = 'addon_purchase' AND ts >= datetime('now', '-7 days') "
            "GROUP BY actor, date(ts) HAVING COUNT(*) > 5 ORDER BY cnt DESC",
        )
        addon_burst = [dict(r) for r in await cur.fetchall()]
    return {"renew_heavy": renew_heavy, "addon_burst": addon_burst}


# ————————————————— ПРОМО-КОДЫ (promo_codes) —————————————————

async def create_promo_code(code: str, kind: str, value: float, max_uses: int = 0,
                            expires_at: str = None, tariff_group: str = None) -> dict:
    """Создаёт промо-код. Код хранится в верхнем регистре."""
    code = code.strip().upper()
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute(
                "INSERT INTO promo_codes (code, kind, value, max_uses, expires_at, tariff_group) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (code, kind, value, int(max_uses), expires_at, tariff_group),
            )
            await db.commit()
            promo_id = cur.lastrowid
        except Exception:
            raise ValueError(f"Код '{code}' уже существует")
        cur = await db.execute("SELECT * FROM promo_codes WHERE id = ?", (promo_id,))
        row = await cur.fetchone()
        return dict(row)


async def get_promo_code(code: str) -> dict | None:
    """Возвращает промо-код по его коду (без учёта регистра) или None."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM promo_codes WHERE upper(code) = upper(?)", (code.strip(),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_promo_code_by_id(promo_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promo_codes WHERE id = ?", (promo_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_promo_codes() -> list[dict]:
    """Все промо-коды (свежие сверху)."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM promo_codes ORDER BY id DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def delete_promo_code(promo_id: int) -> bool:
    async with _db() as db:
        cur = await db.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
        await db.commit()
        return cur.rowcount > 0


async def toggle_promo_code(promo_id: int) -> dict | None:
    """Переключает is_active промо-кода; возвращает обновлённый или None."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE promo_codes SET is_active = 1 - is_active WHERE id = ?", (promo_id,)
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM promo_codes WHERE id = ?", (promo_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def validate_promo_code(code: str, tariff_slug: str = None,
                              tariff_group: str = None) -> tuple[dict | None, str | None]:
    """Проверяет применимость промо-кода.

    Returns: (promo, None) если применим, иначе (None, причина_отказа).
    """
    promo = await get_promo_code(code)
    if not promo:
        return None, "Промокод не найден"
    if not promo.get("is_active"):
        return None, "Промокод неактивен"
    if promo.get("max_uses") and promo.get("used_count", 0) >= promo["max_uses"]:
        return None, "Промокод исчерпал лимит использований"
    if promo.get("expires_at"):
        try:
            from datetime import datetime as _dt
            exp = _dt.strptime(promo["expires_at"], "%Y-%m-%d %H:%M:%S")
            if exp < _dt.utcnow():
                return None, "Промокод истёк"
        except ValueError:
            pass
    if promo.get("tariff_group"):
        if tariff_group and promo["tariff_group"] != tariff_group:
            return None, "Промокод не действует на этот тариф"
    return promo, None


def compute_promo_discount(promo: dict, total: float) -> tuple[float, float]:
    """Возвращает (discount, final). percent — доля, fixed — вычет."""
    if promo.get("kind") == "fixed":
        discount = min(promo.get("value", 0), total)
    else:
        discount = total * (promo.get("value", 0) / 100.0)
    discount = round(discount, 2)
    return discount, round(max(0, total - discount), 2)


async def use_promo_code(promo_id: int) -> None:
    """Инкрементирует счётчик использований промо-кода."""
    async with _db() as db:
        await db.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 WHERE id = ?", (promo_id,)
        )
        await db.commit()


async def claim_trial(user_id: str, expires_at: str) -> bool:
    """Claim trial. Returns False if already used. Uses atomic UPDATE ... WHERE to avoid TOCTOU race."""
    async with _db() as db:
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
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? AND status != 'deleted' ORDER BY created_at DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def get_user_order(user_id: str, order_id: str) -> dict | None:
    """Get a specific order belonging to a user."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_order_user(order_id: str, user_id: str):
    """Link an order to a user."""
    async with _db() as db:
        await db.execute(
            "UPDATE orders SET user_id = ? WHERE id = ?",
            (user_id, order_id),
        )
        await db.commit()


# ————————————————— Referrals —————————————————

async def get_setting(key: str, default: str = "") -> str:
    """Читает настройку реферальной программы (таблица referral_settings)."""
    async with _db() as db:
        cur = await db.execute(
            "SELECT value FROM referral_settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    """Записывает настройку реферальной программы."""
    async with _db() as db:
        await db.execute(
            "INSERT INTO referral_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def ensure_referral_code(user_id: str) -> str | None:
    """Возвращает реферальный код пользователя, генерируя его при необходимости."""
    async with _db() as db:
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
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE UPPER(referral_code) = ?", (code.upper(),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_referrer(user_id: str) -> str | None:
    """Возвращает ID пригласившего пользователя (referrer) или None."""
    async with _db() as db:
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

    async with _db() as db:
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
    async with _db() as db:
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
    async with _db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def sum_reward_days(referrer_id: str) -> int:
    """Суммарное количество начисленных бонусных дней."""
    async with _db() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(reward_days), 0) FROM referrals WHERE referrer_id = ?",
            (referrer_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_referral_list(referrer_id: str) -> list[dict]:
    """Список приглашённых: referred_id, masked_email, reward_days, created_at."""
    async with _db() as db:
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
    async with _db() as db:
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
        async with _db() as db:
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
            elif key == "tariff_groups" and isinstance(value, dict):
                settings.tariff_groups = value
            elif key == "branding" and isinstance(value, dict):
                settings.branding = value
            elif key == "payment" and isinstance(value, dict):
                if "provider" in value:
                    settings.payment_provider = str(value["provider"])
                if "yookassa_shop_id" in value:
                    settings.yookassa_shop_id = str(value["yookassa_shop_id"])
                if "yookassa_secret_key" in value:
                    settings.yookassa_secret_key = str(value["yookassa_secret_key"])
            elif key == "trial" and isinstance(value, dict):
                settings.trial_enabled = bool(value.get("enabled", True))
                settings.trial_days = int(value.get("days", settings.trial_days))
                settings.trial_gb = int(value.get("gb", settings.trial_gb))
                settings.trial_devices = int(value.get("devices", settings.trial_devices))
            elif key == "demo_mode" and isinstance(value, dict):
                settings.demo_mode = bool(value.get("enabled", False))
            elif key == "available_inbounds" and isinstance(value, list):
                settings.available_inbounds = [int(x) for x in value if isinstance(x, (int, str)) and str(x).strip().isdigit()]
    except Exception:
        pass


async def save_settings_value(key: str, value) -> None:
    """Write a JSON-serializable value to the settings table."""
    import json
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    async with _db() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, serialized),
        )
        await db.commit()


async def get_settings_value(key: str, default: str = "") -> str:
    """Read a raw string from the settings table."""
    async with _db() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


# ————————————————— Admin helpers —————————————————

async def set_user_blocked(user_id: str, blocked: bool):
    async with _db() as db:
        await db.execute(
            "UPDATE users SET blocked = ? WHERE id = ?",
            (1 if blocked else 0, user_id),
        )
        await db.commit()


async def count_users(search: str = "") -> int:
    async with _db() as db:
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
    async with _db() as db:
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
    """Physically remove an order from the database (and its add-ons).

    Порядок важен: device_addons ссылается на orders через FK без
    ON DELETE CASCADE, а PRAGMA foreign_keys=ON включён — поэтому
    сначала удаляем дочерние add-ons, затем сам заказ.
    """
    async with _db() as db:
        await db.execute("DELETE FROM device_addons WHERE order_id = ?", (order_id,))
        await db.execute("DELETE FROM orders WHERE id = ?", (order_id,))


async def cleanup_expired_orders(grace_days: int = 14) -> list[dict]:
    """Find and delete orders expired more than grace_days ago. Returns list of {id, xui_email}."""
    async with _db() as db:
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
            # Сначала add-ons (FK без CASCADE, foreign_keys=ON), затем заказы
            await db.execute(
                f"DELETE FROM device_addons WHERE order_id IN ({placeholders})", ids
            )
            await db.execute(f"DELETE FROM orders WHERE id IN ({placeholders})", ids)

        return expired


async def cleanup_expired_trials(grace_days: int = 14) -> int:
    """Reset trial fields for users whose trial expired more than grace_days ago. Returns count."""
    async with _db() as db:
        cur = await db.execute(
            "UPDATE users SET trial_started_at = NULL, trial_expires_at = NULL "
            "WHERE trial_expires_at IS NOT NULL AND trial_expires_at < datetime('now', ?)",
            (f'-{grace_days} days',)
        )
        await db.commit()
        return cur.rowcount


# ————————————————— DEVICE ADD-ONS —————————————————

async def create_device_addon(addon_id: str, user_id: str, order_id: str,
                               addon_type: str, extra_devices: int, amount_paid: float,
                               expires_at: str, platega_tx_id: str = "", provider: str = ""):
    async with _db() as db:
        await db.execute(
            "INSERT INTO device_addons (id, user_id, order_id, addon_type, extra_devices, "
            "amount_paid, status, expires_at, platega_tx_id, provider) VALUES "
            "(?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (addon_id, user_id, order_id, addon_type, extra_devices, amount_paid, expires_at,
             platega_tx_id, provider),
        )
        await db.commit()


async def get_device_addons_for_order(order_id: str) -> list[dict]:
    """Get all add-ons for a specific subscription order."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM device_addons WHERE order_id = ? ORDER BY created_at DESC",
            (order_id,),
        )
        return [dict(row) for row in await cur.fetchall()]


async def get_active_addon_for_order(order_id: str) -> dict | None:
    """Get the active (or cancel_pending) add-on for a subscription."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM device_addons WHERE order_id = ? AND status IN ('active', 'cancel_pending') "
            "ORDER BY created_at DESC LIMIT 1",
            (order_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def activate_addon(addon_id: str):
    async with _db() as db:
        await db.execute(
            "UPDATE device_addons SET status = 'active' WHERE id = ?", (addon_id,)
        )
        await db.commit()


async def activate_pending_addons_for_order(order_id: str) -> int:
    """Активировать все pending add-ons заказа (куплены вместе с подпиской).

    Вызывается из fulfill_order после успешной выдачи ключа — платёж уже
    подтверждён, значит доп. устройства оплачены. Возвращает кол-во активированных.
    """
    async with _db() as db:
        cur = await db.execute(
            "UPDATE device_addons SET status = 'active' "
            "WHERE order_id = ? AND status = 'pending'",
            (order_id,),
        )
        await db.commit()
        return cur.rowcount


async def cancel_pending_addon(addon_id: str):
    """Mark add-on as cancel_pending — will be removed at next renewal."""
    async with _db() as db:
        await db.execute(
            "UPDATE device_addons SET status = 'cancel_pending' WHERE id = ? AND status = 'active'",
            (addon_id,),
        )
        await db.commit()


async def finalize_addon_cancellation(addon_id: str):
    """Actually cancel and remove add-on (called during renewal)."""
    async with _db() as db:
        await db.execute(
            "UPDATE device_addons SET status = 'cancelled' WHERE id = ?", (addon_id,)
        )
        await db.commit()


async def get_addon_by_tx(tx_id: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM device_addons WHERE platega_tx_id = ?", (tx_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_addon_by_id(addon_id: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM device_addons WHERE id = ?", (addon_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_total_extra_devices(order_id: str) -> int:
    """Sum of active extra devices for a subscription."""
    async with _db() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(extra_devices), 0) FROM device_addons "
            "WHERE order_id = ? AND status = 'active'",
            (order_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


# ————————————————— Платные продления (renewals) —————————————————
# Та же схема, что и у add-ons: create_* → get_*_by_id / get_*_by_tx →
# activate_* (pending → active). Переиспользуется PaymentLifecycle'ом
# (Часть 1), чтобы порядок pending → confirm → fulfill не нарушался.

async def create_renewal(renewal_id: str, order_id: str, user_id: str,
                         days: int, amount: float, platega_tx_id: str = "",
                         provider: str = ""):
    async with _db() as db:
        await db.execute(
            "INSERT INTO renewals (id, order_id, user_id, days, amount, "
            "status, platega_tx_id, provider) VALUES "
            "(?, ?, ?, ?, ?, 'pending', ?, ?)",
            (renewal_id, order_id, user_id, days, amount, platega_tx_id, provider),
        )
        await db.commit()


async def get_renewal_by_id(renewal_id: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM renewals WHERE id = ?", (renewal_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_renewal_by_tx(tx_id: str) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM renewals WHERE platega_tx_id = ?", (tx_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_pending_renewal_for_order(order_id: str) -> dict | None:
    """Действующая (pending) заявка на продление — защита от двойного клика."""
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM renewals WHERE order_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (order_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def activate_renewal(renewal_id: str):
    """pending → active. Идемпотентно: повторный вызов ничего не меняет."""
    async with _db() as db:
        await db.execute(
            "UPDATE renewals SET status = 'active' WHERE id = ? AND status = 'pending'",
            (renewal_id,),
        )
        await db.commit()


async def set_renewal_status(renewal_id: str, status: str):
    """Принудительно сменить статус (failed/cancelled) — для учёта ошибок
    провижининга и отменённых/протухших платежей."""
    async with _db() as db:
        await db.execute(
            "UPDATE renewals SET status = ? WHERE id = ?", (status, renewal_id)
        )
        await db.commit()


# ————————————————— Debug Sandbox —————————————————

async def set_test_account(user_id: str, is_test: bool):
    """Помечает/снимает флаг тестового аккаунта (is_test_account)."""
    async with _db() as db:
        await db.execute(
            "UPDATE users SET is_test_account = ? WHERE id = ?",
            (1 if is_test else 0, user_id),
        )
        await db.commit()


async def log_debug_action(
    admin_name: str,
    action: str,
    target_user_id: str | None,
    details: dict,
):
    """Пишет строку в debug_audit_log после успешного разрушительного действия.

    admin_name — значение заголовка X-Admin-Name (не настоящая аутентификация по
    ролям, а указание «кто отвечает за это действие» при единственном общем ключе).
    Если в будущем появятся несколько реальных людей с раздельным доступом —
    это место нужно будет заменить на настоящие именные учётки.
    """
    async with _db() as db:
        await db.execute(
            "INSERT INTO debug_audit_log (admin_name, action, target_user_id, details_json) "
            "VALUES (?, ?, ?, ?)",
            (admin_name, action, target_user_id, json.dumps(details, ensure_ascii=False)),
        )
        await db.commit()
