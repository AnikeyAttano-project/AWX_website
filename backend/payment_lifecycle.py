"""
Универсальный паттерн "pending -> confirm -> fulfill" для любой платной сущности
(order, device_addon, renewal, и любой будущей платной фичи).

Идея: за несколько раундов правок один и тот же скелет (создать pending-запись →
создать платёж → polling-эндпоинт с обязательной проверкой check_status ПЕРЕД
активацией → webhook-ветка с той же проверкой → идемпотентная fulfill-функция)
писался вручную для orders, потом для device_addons, потом для renewals. Этот
модуль выносит общую часть один раз, чтобы четвёртая копипаста была невозможна.

ГЛАВНОЕ ПРАВИЛО (не подлежит вольной интерпретации):
  confirm_and_fulfill  →  сначала проверка pending, потом check_status (или
  использование готового known_status), и только при "succeeded" — вызов fulfill.
  Никогда не активирует без подтверждения оплаты.

Использование:
    lifecycle = PaymentLifecycle(
        get_by_id=get_renewal_by_id,
        get_by_tx=get_renewal_by_tx,
        activate=activate_renewal,        # pending -> active, идемпотентно
        provision=_provision_renewal,     # реальная выдача (после подтверждения)
        lock_prefix="renewal",
    )

    # В polling-эндпоинте:
    await lifecycle.confirm_and_fulfill(entity_id, tx_id)

    # В webhook, при поиске нужной ветки:
    entity = await lifecycle.find_by_tx(tx_id) or await lifecycle.find_by_id(payload)
    if entity:
        await lifecycle.confirm_and_fulfill(entity["id"], tx_id, known_status=real_status)

Замечание про orders: у заказа две оси состояния — status (оплата) и
fulfillment_status (выдача ключа). activate для заказа не нужен (вся логика —
в provision: атомарный claim через begin_fulfillment + mark_paid + create_client),
поэтому activate=None. fulfill_statuses=("pending","paid","error") разрешает
re-claim брошенных/невыданных заказов при повторном webhook/polling.
"""
import asyncio
import logging
from typing import Awaitable, Callable, Optional

from payment_providers import get_provider, PaymentError

logger = logging.getLogger(__name__)

# Словарь per-entity asyncio.Lock. Живёт вечно (растёт медленно — только для
# сущностей, которые реально дошли до fulfill), как и _fulfill_locks раньше.
_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


class PaymentLifecycle:
    def __init__(
        self,
        get_by_id: Callable[[str], Awaitable[Optional[dict]]],
        get_by_tx: Callable[[str], Awaitable[Optional[dict]]],
        activate: Optional[Callable[[str], Awaitable[None]]],
        provision: Callable[[dict], Awaitable[None]],
        lock_prefix: str,
        status_field: str = "status",
        tx_field: str = "platega_tx_id",
        provider_field: str = "provider",
        fulfill_statuses: tuple[str, ...] = ("pending",),
    ):
        self.get_by_id = get_by_id
        self.get_by_tx = get_by_tx
        self.activate = activate
        self.provision = provision
        self.lock_prefix = lock_prefix
        self.status_field = status_field
        self.tx_field = tx_field
        self.provider_field = provider_field
        # Статусы, в которых fulfill() имеет право работать. Для аддонов/продлений
        # это только 'pending'; для заказов добавляем 'paid'/'error' (re-claim).
        self.fulfill_statuses = fulfill_statuses

    # ————————————————— Поиск (для webhook) —————————————————

    async def find_by_tx(self, tx_id: str) -> Optional[dict]:
        return await self.get_by_tx(tx_id)

    async def find_by_id(self, entity_id: str) -> Optional[dict]:
        return await self.get_by_id(entity_id)

    # ————————————————— Lock —————————————————

    async def _get_lock(self, entity_id: str) -> asyncio.Lock:
        key = f"{self.lock_prefix}:{entity_id}"
        async with _locks_guard:
            if key not in _locks:
                _locks[key] = asyncio.Lock()
        return _locks[key]

    # ————————————————— Fulfill —————————————————

    async def fulfill(self, entity_id: str, provision_kwargs: dict = None) -> bool:
        """
        Идемпотентная активация — вызывать ТОЛЬКО после того, как оплата уже
        подтверждена (см. confirm_and_fulfill). Возвращает True, если реально
        что-то активировал в этом вызове (для логов/тестов).

        provision_kwargs — доп. аргументы для provision (например, переопределение
        дней при ручной выдаче ключа админом); для addon/renewal не используется.
        """
        lock = await self._get_lock(entity_id)
        async with lock:
            entity = await self.get_by_id(entity_id)
            if not entity:
                logger.warning("%s: entity %s not found", self.lock_prefix, entity_id)
                return False
            if entity[self.status_field] not in self.fulfill_statuses:
                logger.info("%s: entity %s already %s", self.lock_prefix, entity_id,
                            entity[self.status_field])
                return False
            if self.activate is not None:
                await self.activate(entity_id)
            await self.provision(entity, **(provision_kwargs or {}))
            return True

    # ————————————————— Главная точка входа —————————————————

    async def confirm_and_fulfill(
        self,
        entity_id: str,
        tx_id: str,
        known_status: Optional[str] = None,
    ) -> dict:
        """
        Проверяет оплату и при "succeeded" активирует сущность.

        known_status передавай, если реальный статус уже вычислен вызывающей
        стороной (webhook) — тогда функция НЕ ходит в провайдер повторно.
        Иначе функция сама сходит в check_status активного провайдера.

        НИКОГДА не активирует без подтверждения — это тот самый шаг, который
        дважды забывали в прошлых раундах.
        """
        entity = await self.get_by_id(entity_id)
        if not entity:
            return {"ok": False, "error": "not found"}
        if entity[self.status_field] not in self.fulfill_statuses:
            return {"ok": True, "status": entity[self.status_field]}

        real_status = known_status
        if real_status is None:
            try:
                # get_provider внутри try (№35): неизвестное сохранённое имя
                # провайдера — тоже PaymentError, а не молчаливый фолбэк.
                provider = get_provider(entity.get(self.provider_field) or "")
                real_status = await provider.check_status(tx_id)
            except PaymentError as e:
                logger.error("%s: check_status failed for %s: %s",
                             self.lock_prefix, entity_id, e)
                return {"ok": False, "error": "status check failed",
                        "status": entity[self.status_field]}

        if real_status == "succeeded":
            await self.fulfill(entity_id)
            entity = await self.get_by_id(entity_id)
            return {"ok": True, "status": entity[self.status_field]}
        if real_status in ("cancelled", "expired"):
            return {"ok": True, "status": entity[self.status_field],
                    "final": real_status}
        return {"ok": True, "status": entity[self.status_field]}
