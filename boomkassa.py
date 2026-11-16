# -*- coding: utf-8 -*-
"""
BoomKassa: оплата картой и через СБП.

Как это работает (по документации из личного кабинета BoomKassa → «API и интеграция»):
  1. Бот создаёт ссылку на оплату: POST /api/v1/payment-links с кодом тарифа.
     Сумму задаёт тариф: тарифы и цены для проекта заводит BoomKassa, своих сумм API не принимает.
  2. Покупатель платит на странице BoomKassa.
  3. BoomKassa присылает вебхук payment.succeeded на https://<сервер>/webhook/boomkassa
     с подписью X-Payments-Boom-Signature = hex(HMAC-SHA256(тело, секрет вебхука)).
     Адрес вебхука и секрет выдаёт поддержка BoomKassa.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

import requests

import store as S

log = logging.getLogger("boomkassa")

ORDER_PREFIX = "tg-"   # order_id в BoomKassa: tg-<номер заказа>
TARIFFS_FILE = S.DATA_DIR / "boomkassa_tariffs.json"
CACHE_TTL = 600
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None, "error": ""}


class BKError(Exception):
    def __init__(self, message: str, code: str = "", status: int = 0, hint: str = ""):
        super().__init__(message)
        self.code, self.status, self.hint = code, status, hint

    def human(self) -> str:
        known = {
            "feature_locked": "В BoomKassa не включены «Ссылки на оплату». Отправь запрос в личном кабинете → API.",
            "tariffs_not_configured": "Для проекта в BoomKassa ещё нет тарифов: их заводит поддержка BoomKassa.",
            "unknown_tariff": "Такого тарифа нет. Обнови список тарифов и выбери заново.",
            "tariff_not_available": "Тариф не закреплён за терминалом проекта, напиши в поддержку BoomKassa.",
            "partner_paused": "Приём платежей в BoomKassa приостановлен до проверки, напиши в их поддержку.",
        }
        text = known.get(self.code) or str(self)
        if self.status == 401:
            text = "Неверный или отозванный API-ключ BoomKassa."
        return f"{text}{' (' + self.hint + ')' if self.hint else ''}"


def configured() -> bool:
    return bool(S.cfg("bk_api_key"))


def _url(path: str) -> str:
    return (S.cfg("bk_base_url") or "https://boomkassa.ru").rstrip("/") + "/api/v1" + path


def _call(method: str, path: str, body: dict = None, api_key: str = None) -> dict:
    key = api_key or S.cfg("bk_api_key")
    if not key:
        raise BKError("API-ключ BoomKassa не задан", "no_key")
    try:
        r = requests.request(method, _url(path), json=body, timeout=20,
                             headers={"X-Api-Key": key, "Accept": "application/json"})
    except requests.RequestException as e:
        raise BKError(f"BoomKassa недоступна: {e}", "network") from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not r.ok:
        raise BKError(data.get("error") or f"HTTP {r.status_code}", data.get("code", ""), r.status_code,
                      data.get("hint", ""))
    return data


# ═══════════════════════════ ТАРИФЫ ═══════════════════════════

def fetch_tariffs(api_key: str = None) -> dict:
    """Свежий список тарифов с сервера BoomKassa (и сохранение его на диск)."""
    data = _call("GET", "/tariffs", api_key=api_key)
    result = {
        "project": data.get("project") or {},
        "tariffs": data.get("tariffs") or [],
        "code": data.get("code", ""),
        "hint": data.get("hint", ""),
        "fetched_at": S.now_iso(),
    }
    if api_key is None:
        with _lock:
            _cache.update(ts=time.time(), data=result, error="")
        try:
            TARIFFS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return result


def tariffs_info(force: bool = False) -> dict:
    """Тарифы из кэша (10 минут). Если BoomKassa не ответила — последний сохранённый список."""
    with _lock:
        fresh = _cache["data"] is not None and time.time() - _cache["ts"] < CACHE_TTL
        if fresh and not force:
            return _cache["data"]
    if configured():
        try:
            return fetch_tariffs()
        except BKError as e:
            log.warning("Тарифы BoomKassa недоступны: %s", e.human())
            with _lock:
                _cache["error"] = e.human()
                _cache["ts"] = time.time()  # не долбим API при каждом клике
    with _lock:
        if _cache["data"] is None and TARIFFS_FILE.exists():
            try:
                _cache["data"] = json.loads(TARIFFS_FILE.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return _cache["data"] or {"project": {}, "tariffs": [], "code": "", "hint": "", "fetched_at": None}


def last_error() -> str:
    return _cache.get("error", "")


def tariffs(force: bool = False) -> list:
    return [t for t in tariffs_info(force).get("tariffs", []) if (t.get("currency") or "RUB") == "RUB"]


def amount_of(t: dict) -> int:
    try:
        return int(round(float(t.get("amount") or 0)))
    except (TypeError, ValueError):
        return 0


def pick_tariff(item: dict, price: int):
    """Какой тариф BoomKassa использовать для позиции.
    Если в карточке выбран тариф — он (сумму берёт из тарифа), иначе тариф с той же суммой, что и цена."""
    lst = tariffs()
    code = (item.get("bk_tariff") or "").strip()
    if code:
        return next((t for t in lst if t.get("code") == code), None)
    return next((t for t in lst if amount_of(t) == int(price)), None)


# ═══════════════════════════ ОПЛАТА ═══════════════════════════

def create_link(order_id: int, tariff: dict, description: str, customer: str, success_url: str = "") -> dict:
    body = {
        "tariff": tariff["code"],
        "order_id": f"{ORDER_PREFIX}{order_id}",
        "customer": str(customer),
        "description": description[:255],
    }
    if success_url.startswith(("http://", "https://")):
        body["success_url"] = success_url
    return _call("POST", "/payment-links", body)


def parse_order_id(value) -> int | None:
    s = str(value or "")
    if s.startswith(ORDER_PREFIX):
        s = s[len(ORDER_PREFIX):]
    return int(s) if s.isdigit() else None


def verify_signature(raw: bytes, given: str) -> bool:
    secret = S.cfg("bk_webhook_secret")
    if not secret:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (given or "").strip().lower())


def sign(raw: bytes, secret: str = None) -> str:
    """Подпись тела запроса (для теста вебхука из админки)."""
    return hmac.new((secret or S.cfg("bk_webhook_secret")).encode(), raw, hashlib.sha256).hexdigest()


def webhook_url() -> str:
    base = (S.cfg("public_url") or "").rstrip("/")
    return f"{base}/webhook/boomkassa" if base else ""
