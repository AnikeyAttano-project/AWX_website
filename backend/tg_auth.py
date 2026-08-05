"""Проверка подписи данных Telegram Login Widget.

Официальный алгоритм Telegram (https://core.telegram.org/widgets/login):

  1. Из полученных данных убрать поле ``hash``.
  2. Оставшиеся поля отсортировать по ключу и склеить как ``key=value``,
     разделитель — перевод строки (ВСЕ поля, пустые НЕ отбрасывать —
     иначе подпись не сойдётся, Telegram хеширует все присланные поля).
  3. ``secret_key = SHA256(bot_token)``.
  4. ``expected = HMAC-SHA256(secret_key, data_check_string)`` (hex).
  5. Сравнить ``expected`` и ``hash`` constant-time + проверить свежесть ``auth_date``.
"""

import hashlib
import hmac
import time

from config import settings


class TelegramAuthError(Exception):
    """Ошибка валидации данных от Telegram (неверная подпись / истёкшие данные)."""


def verify_telegram_auth(data: dict, max_age_seconds: int = 86400) -> None:
    """Проверяет подпись данных виджета.

    Бросает TelegramAuthError при неверной подписи, истёкшем auth_date или
    если бот не настроен. Возвращает None при успехе.
    """
    if not settings.telegram_bot_token:
        raise TelegramAuthError("Telegram auth is not configured")

    received_hash = data.get("hash") or ""
    if not received_hash:
        raise TelegramAuthError("Missing Telegram hash")

    # Строка для проверки: все поля КРОМЕ hash, отсортированные, без пропуска пустых.
    check = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(check.items()))

    secret_key = hashlib.sha256(settings.telegram_bot_token.encode()).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramAuthError("Invalid Telegram signature")

    # Защита от replay: данные живут не дольше max_age_seconds.
    try:
        auth_date = int(data.get("auth_date", 0))
    except (TypeError, ValueError):
        raise TelegramAuthError("Invalid Telegram auth_date")
    if time.time() - auth_date > max_age_seconds:
        raise TelegramAuthError("Telegram auth data expired")
