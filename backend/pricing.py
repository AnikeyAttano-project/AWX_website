"""Расчёт proration для add-on устройств.

Единственный источник правды формулы P = ceil(B * (1 - D) / T * R),
используется и в main.py (get_addon_price / purchase_addon), и в
дебаг-калькуляторе (/admin/debug/proration-calc). Держим формулу в одном
месте, чтобы она физически не могла разъехаться между копипастами.
"""

import math


def compute_addon_proration(
    base_price: float,
    discount_pct: float,
    total_days: int,
    remaining_days: float,
) -> dict:
    """Proration для покупки доп. устройств.

    Args:
        base_price: базовая цена add-on пакета (settings.device_addons[type]["base_price"]).
        discount_pct: скидка тарифа в процентах (0-100), как в settings.tariffs[slug]["discount"].
        total_days: длительность тарифа в днях (settings.tariffs[slug]["days"]).
        remaining_days: сколько дней осталось до конца текущего периода.

    Returns:
        {"raw": raw, "price_now": price_now} — raw это значение ДО округления
        (нужно для дебаг-калькулятора), price_now итоговая сумма к оплате.

    ВАЖНО: не менять логику округления — она критична, чтобы суммы в дебаге
    и в реальном purchase совпадали численно.
    """
    discount = discount_pct / 100
    remaining = max(0, remaining_days)
    # ceil(B * (1 - D) / T * R); если срок истёк — 0
    raw = base_price * (1 - discount) / total_days * remaining if total_days > 0 else 0
    price_now = math.ceil(raw) if remaining > 0 else 0
    price_now = max(1, price_now) if remaining > 0 else 0
    return {"raw": raw, "price_now": price_now}
