# -*- coding: utf-8 -*-
"""
Общее ядро для бота и веб-админки.

  • .env              — только запуск: пароль админки, порт, папка данных;
  • data/config.json  — токены и подключения (бот, платёжки, админы). Правятся в админке;
  • data/catalog.json — подписки, услуги, тексты и настройки магазина. Правятся в админке;
  • data/orders.db    — заказы, пользователи, рассылки (SQLite).

Всё, что меняется в админке, бот подхватывает сам. Перезапуск нужен только после смены токена бота
(админка делает его кнопкой).
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("store")
BASE = Path(__file__).resolve().parent


# ═══════════════════════════ .env ═══════════════════════════

def load_env(path: Path) -> None:
    """Мини-загрузчик .env, чтобы не тянуть лишние библиотеки."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


ENV_PATH = Path(os.getenv("ENV_FILE") or (BASE / ".env"))
load_env(ENV_PATH)


def env_on(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8080") or 8080)
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "3") or 3)          # часовой пояс для дат (МСК = 3)
TRUST_PROXY = env_on("TRUST_PROXY")                         # админка стоит за Caddy/nginx
COOKIE_SECURE = env_on("COOKIE_SECURE")                     # админка открывается только по https

LOG_PATH = DATA_DIR / "bot.log"


def setup_logging() -> None:
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    if getattr(root, "_robux_configured", False):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:  # нет прав на запись — работаем без файла
        log.warning("Лог-файл недоступен: %s", e)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("waitress").setLevel(logging.WARNING)
    root._robux_configured = True  # type: ignore[attr-defined]


# ═══════════════════════════ ПОДКЛЮЧЕНИЯ (config.json) ═══════════════════════════
# ключ: (переменная в .env для первого запуска, значение по умолчанию, секрет?)

CONFIG_SPEC = {
    "bot_token": ("BOT_TOKEN", "", True),
    "admin_ids": ("ADMIN_ID", "", False),
    "currency": ("CURRENCY", "RUB", False),
    "provider_token": ("PROVIDER_TOKEN", "", True),
    "crypto_pay_token": ("CRYPTO_PAY_TOKEN", "", True),
    "crypto_testnet": ("CRYPTO_TESTNET", "1", False),
    "crypto_asset": ("CRYPTO_ASSET", "USDT", False),
    "bk_api_key": ("BOOMKASSA_API_KEY", "", True),
    "bk_webhook_secret": ("BOOMKASSA_WEBHOOK_SECRET", "", True),
    "bk_base_url": ("BOOMKASSA_URL", "https://boomkassa.ru", False),
    "private_channel_id": ("PRIVATE_CHANNEL_ID", "", False),
    "public_url": ("PUBLIC_URL", "", False),
    "admin_password_hash": ("", "", True),
}

CONFIG_PATH = DATA_DIR / "config.json"
_cfg_lock = threading.RLock()
_cfg_cache: dict = {"mtime": None, "data": {}}


def _config_file() -> dict:
    with _cfg_lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            _cfg_cache.update(mtime=None, data={})
            return {}
        if _cfg_cache["mtime"] != mtime:
            try:
                _cfg_cache["data"] = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError) as e:
                log.error("config.json повреждён, использую прошлую версию: %s", e)
            _cfg_cache["mtime"] = mtime
        return _cfg_cache["data"]


def cfg(key: str) -> str:
    """Значение подключения: сначала config.json (из админки), потом .env."""
    data = _config_file()
    if key in data and data[key] is not None:
        return str(data[key])
    env, default, _ = CONFIG_SPEC[key]
    return (os.getenv(env, default) if env else default) or ""


def save_config(updates: dict) -> None:
    with _cfg_lock:
        data = dict(_config_file())
        data.update(updates)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _cfg_cache["mtime"] = None


def bot_token() -> str:
    return cfg("bot_token").strip()


def admin_ids() -> list:
    ids = []
    for part in re.split(r"[\s,;]+", cfg("admin_ids")):
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def is_admin(user_id) -> bool:
    return bool(user_id) and int(user_id) in admin_ids()


def currency() -> str:
    return (cfg("currency") or "RUB").upper()


def crypto_asset() -> str:
    return cfg("crypto_asset") or "USDT"


def crypto_api() -> str:
    testnet = cfg("crypto_testnet").strip().lower() in ("1", "true", "yes", "on")
    return "https://testnet-pay.crypt.bot/api" if testnet else "https://pay.crypt.bot/api"


CRYPTO_INVOICE_TTL = 3600


# ═══════════════════════════ БАЗА (SQLite) ═══════════════════════════

DB = sqlite3.connect(DATA_DIR / "orders.db", check_same_thread=False, timeout=15)
DB.row_factory = sqlite3.Row
DB_LOCK = threading.RLock()  # бот, админка и фоновые задачи работают в разных потоках

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    username          TEXT,
    kind              TEXT NOT NULL,      -- sub / svc
    item_key          TEXT NOT NULL,
    title             TEXT NOT NULL,
    price             INTEGER NOT NULL,   -- в рублях (со скидкой)
    currency          TEXT NOT NULL,      -- RUB / XTR / USDT
    amount            INTEGER NOT NULL,   -- сумма в счёте (копейки или звёзды; для USDT 0)
    status            TEXT NOT NULL,      -- created / paid / waiting / done / cancelled / refunded
    nick              TEXT,
    charge_id         TEXT,
    created_at        TEXT NOT NULL,
    paid_at           TEXT,
    crypto_invoice_id INTEGER
);
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    banned      INTEGER NOT NULL DEFAULT 0,
    bot_blocked INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    button_text TEXT,
    button_url  TEXT,
    audience    TEXT NOT NULL,
    status      TEXT NOT NULL,      -- running / done / stopped / failed
    total       INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
"""

NEW_ORDER_COLUMNS = {
    "method": "TEXT",       # stars / card / crypto / boomkassa / manual
    "ext_id": "TEXT",       # номер счёта во внешней платёжке
    "pay_url": "TEXT",
    "note": "TEXT",         # заметка админа
    "done_at": "TEXT",
    "updated_at": "TEXT",
}


def _migrate() -> None:
    with DB_LOCK:
        DB.execute("PRAGMA journal_mode=WAL")
        DB.executescript(SCHEMA)
        cols = {r["name"] for r in DB.execute("PRAGMA table_info(orders)")}
        for name, typ in NEW_ORDER_COLUMNS.items():
            if name not in cols:
                DB.execute(f"ALTER TABLE orders ADD COLUMN {name} {typ}")
        DB.execute("UPDATE orders SET method = CASE currency WHEN 'XTR' THEN 'stars' WHEN 'USDT' THEN 'crypto' "
                   "ELSE 'card' END WHERE method IS NULL")
        DB.execute("UPDATE orders SET ext_id = CAST(crypto_invoice_id AS TEXT) "
                   "WHERE ext_id IS NULL AND crypto_invoice_id IS NOT NULL")
        DB.execute("CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id)")
        DB.execute("CREATE INDEX IF NOT EXISTS orders_status ON orders(status)")
        DB.execute("CREATE INDEX IF NOT EXISTS orders_paid_at ON orders(paid_at)")
        # пользователи из старых заказов, чтобы рассылка дошла и до них
        DB.execute(
            "INSERT OR IGNORE INTO users (id, username, created_at, last_seen) "
            "SELECT user_id, MAX(username), MIN(created_at), MAX(created_at) FROM orders GROUP BY user_id"
        )
        DB.commit()


_migrate()

STATUS_NAMES = {
    "created": "🕓 не оплачен",
    "paid": "💰 оплачен, ждём ник",
    "waiting": "⏳ ждёт выдачи",
    "done": "✅ выполнен",
    "cancelled": "❌ отменён",
    "refunded": "↩️ возврат",
}
PAID_STATUSES = ("paid", "waiting", "done")
METHOD_NAMES = {
    "stars": "⭐ Stars",
    "card": "💳 Карта (Telegram)",
    "crypto": "💵 Крипта",
    "boomkassa": "💳 BoomKassa",
    "manual": "✍️ Вручную",
}


def db_exec(sql: str, params: tuple = ()):
    with DB_LOCK:
        cur = DB.execute(sql, params)
        DB.commit()
        return cur


def db_one(sql: str, params: tuple = ()):
    with DB_LOCK:
        return DB.execute(sql, params).fetchone()


def db_all(sql: str, params: tuple = ()):
    with DB_LOCK:
        return DB.execute(sql, params).fetchall()


def now_iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


# ── маленькое хранилище «ключ → JSON» (статус бота и т.п.) ──

def kv_get(key: str, default=None):
    row = db_one("SELECT value FROM kv WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def kv_set(key: str, value) -> None:
    db_exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)))


# ── заказы ──

def create_order(user_id: int, username: str, kind: str, key: str, title: str, price: int,
                 currency_code: str, amount: int, method: str) -> int:
    cur = db_exec(
        "INSERT INTO orders (user_id, username, kind, item_key, title, price, currency, amount, status, "
        "created_at, updated_at, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?)",
        (user_id, username, kind, key, title, price, currency_code, amount, now_iso(), now_iso(), method),
    )
    return cur.lastrowid


def get_order(order_id: int):
    return db_one("SELECT * FROM orders WHERE id = ?", (order_id,))


def set_status(order_id: int, status: str) -> None:
    done_at = now_iso() if status == "done" else None
    db_exec("UPDATE orders SET status = ?, updated_at = ?, done_at = COALESCE(?, done_at) WHERE id = ?",
            (status, now_iso(), done_at, order_id))


def set_order_fields(order_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    db_exec(f"UPDATE orders SET {cols}, updated_at = ? WHERE id = ?", (*fields.values(), now_iso(), order_id))


def mark_paid(order_id: int, charge_id: str) -> bool:
    """Атомарно помечает заказ оплаченным. False — если он уже был обработан (защита от дублей)."""
    cur = db_exec(
        "UPDATE orders SET status = 'paid', charge_id = ?, paid_at = ?, updated_at = ? "
        "WHERE id = ? AND status IN ('created', 'cancelled')",
        (charge_id, now_iso(), now_iso(), order_id),
    )
    return cur.rowcount == 1


def set_nick(order_id: int, nick: str) -> None:
    db_exec("UPDATE orders SET nick = ?, status = 'waiting', updated_at = ? WHERE id = ?", (nick, now_iso(), order_id))


def pending_nick_order(user_id: int):
    """Последний оплаченный заказ, по которому бот ещё ждёт ник."""
    return db_one(
        "SELECT * FROM orders WHERE user_id = ? AND status = 'paid' ORDER BY paid_at DESC, id DESC LIMIT 1",
        (user_id,),
    )


def user_orders(user_id: int, limit: int = 10):
    return db_all("SELECT * FROM orders WHERE user_id = ? AND status != 'created' ORDER BY id DESC LIMIT ?",
                  (user_id, limit))


def user_discount(user_id: int) -> int:
    """Лучшая действующая скидка пользователя по оплаченным подпискам."""
    rows = db_all(
        "SELECT item_key, paid_at FROM orders "
        "WHERE user_id = ? AND kind = 'sub' AND status IN ('paid', 'waiting', 'done')",
        (user_id,),
    )
    subs = {s["key"]: s for s in items("sub", only_active=False)}
    best, now = 0, datetime.now(timezone.utc)
    for row in rows:
        sub = subs.get(row["item_key"])
        if not sub:
            continue
        days = sub.get("days")
        if days and row["paid_at"] and now - datetime.fromisoformat(row["paid_at"]) > timedelta(days=int(days)):
            continue
        best = max(best, int(sub.get("discount") or 0))
    return best


# ── пользователи ──

_touched: dict = {}  # user_id -> время последней записи (чтобы не писать в базу на каждый клик)


def touch_user(user) -> bool:
    """Запоминает пользователя. True — если он пришёл впервые."""
    if not user or getattr(user, "is_bot", False):
        return False
    now = time.time()
    if now - _touched.get(user.id, 0) < 60:
        return False
    is_new = user.id not in _touched and get_user(user.id) is None
    _touched[user.id] = now
    db_exec(
        "INSERT INTO users (id, username, first_name, last_name, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name, "
        "last_name = excluded.last_name, last_seen = excluded.last_seen, bot_blocked = 0",
        (user.id, user.username, user.first_name, user.last_name, now_iso(), now_iso()),
    )
    return is_new


def get_user(user_id: int):
    return db_one("SELECT * FROM users WHERE id = ?", (user_id,))


def is_banned(user_id: int) -> bool:
    row = db_one("SELECT banned FROM users WHERE id = ?", (user_id,))
    return bool(row and row["banned"])


def set_user_flag(user_id: int, field: str, value) -> None:
    if field not in ("banned", "bot_blocked", "note"):
        raise ValueError(field)
    db_exec(f"UPDATE users SET {field} = ? WHERE id = ?", (value, user_id))


# ── события вебхуков (защита от повторной обработки) ──

def event_seen(event_id: str) -> bool:
    return db_one("SELECT 1 FROM events WHERE event_id = ?", (event_id,)) is not None


def remember_event(provider: str, event_id: str, payload: str = "") -> bool:
    cur = db_exec("INSERT OR IGNORE INTO events (event_id, provider, received_at, payload) VALUES (?, ?, ?, ?)",
                  (event_id, provider, now_iso(), payload[:4000]))
    return cur.rowcount == 1


# ═══════════════════════════ КАТАЛОГ (catalog.json) ═══════════════════════════

CATALOG_PATH = DATA_DIR / "catalog.json"
BACKUP_PATH = DATA_DIR / "catalog.backup.json"
_cat_lock = threading.RLock()
_cat_cache: dict = {"mtime": None, "data": None}


def _seed() -> dict:
    import catalog as D  # стартовые значения

    def to_list(d):
        return [{"key": k, "active": True, **v} for k, v in d.items()]

    return {
        "settings": dict(D.SETTINGS),
        "texts": dict(D.TEXTS),
        "subscriptions": to_list(D.SUBSCRIPTIONS),
        "services": to_list(D.SERVICES),
    }


def _with_defaults(data: dict) -> dict:
    """Добавляет новые настройки и тексты, которых не было в старом catalog.json (после обновления бота)."""
    import catalog as D

    data.setdefault("settings", {})
    data.setdefault("texts", {})
    data.setdefault("subscriptions", [])
    data.setdefault("services", [])
    for k, v in D.SETTINGS.items():
        data["settings"].setdefault(k, v)
    for k, v in D.TEXTS.items():
        data["texts"].setdefault(k, v)
    return data


def load_catalog() -> dict:
    """Текущий каталог. Перечитывается автоматически, когда файл изменился (например, из админки)."""
    with _cat_lock:
        if not CATALOG_PATH.exists():
            save_catalog(_seed())
        mtime = CATALOG_PATH.stat().st_mtime_ns
        if _cat_cache["mtime"] != mtime:
            try:
                _cat_cache["data"] = _with_defaults(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
            except ValueError as e:
                if _cat_cache["data"] is None:
                    raise SystemExit(f"❌ catalog.json повреждён ({e}). Восстанови его из catalog.backup.json")
                log.error("catalog.json повреждён, использую прошлую версию: %s", e)
            _cat_cache["mtime"] = mtime
        return _cat_cache["data"]


def edit_catalog() -> dict:
    """Копия каталога для изменения; потом передать в save_catalog()."""
    return copy.deepcopy(load_catalog())


def save_catalog(data: dict) -> None:
    with _cat_lock:
        if CATALOG_PATH.exists():
            shutil.copyfile(CATALOG_PATH, BACKUP_PATH)  # предыдущая версия на всякий случай
        tmp = CATALOG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CATALOG_PATH)
        _cat_cache["mtime"] = None


def settings() -> dict:
    return load_catalog()["settings"]


def texts() -> dict:
    return load_catalog()["texts"]


def list_name(kind: str) -> str:
    return "subscriptions" if kind == "sub" else "services"


def items(kind: str, only_active: bool = True) -> list:
    lst = load_catalog()[list_name(kind)]
    return [i for i in lst if i.get("active", True)] if only_active else lst


def get_item(kind: str, key: str, only_active: bool = True):
    for i in items(kind, only_active):
        if i["key"] == key:
            return i
    return None


# ═══════════════════════════ ПРОВЕРКА HTML ДЛЯ TELEGRAM ═══════════════════════════

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
                "code", "pre", "a", "tg-spoiler", "blockquote", "span", "tg-emoji"}


class _HtmlCheck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"тег &lt;{tag}&gt; Telegram не поддерживает")
        else:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"лишний или закрыт не по порядку &lt;/{tag}&gt;")


def html_errors(text: str) -> list:
    """Ошибки разметки, из-за которых Telegram не отправит сообщение."""
    errors = []
    if re.search(r"<(?![a-zA-Z/])", text):
        errors.append("знак &lt; без тега: пиши его как &amp;lt;")
    if re.search(r"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)", text):
        errors.append("знак &amp; без сущности: пиши его как &amp;amp;")
    check = _HtmlCheck()
    check.feed(text)
    check.close()
    errors += check.errors
    if check.stack:
        errors.append(f"не закрыт тег &lt;{check.stack[-1]}&gt;")
    return errors


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


# ═══════════════════════════ ТЕКСТЫ ЭКРАНОВ ═══════════════════════════

def fmt(n) -> str:
    return f"{int(round(float(n or 0))):,}".replace(",", " ") + f" {settings().get('currency_sign', '₽')}"


def item_title(kind: str, item: dict) -> str:
    if kind == "sub":
        return f"{item['emoji']} {item['name']} • {item['period']}"
    return f"{item['emoji']} {item['name']} — {item['amount']}"


def invoice_title(kind: str, item: dict) -> str:
    """Короткое название для счёта (Telegram ограничивает 32 символами)."""
    if kind == "sub":
        return f"{item['name']} • {item['period']}"[:32]
    return f"{item['name']} {item['amount']}"[:32]


def sub_button(item: dict) -> str:
    return f"{item['period']} {item['emoji']} {item['name']} • {fmt(item['price'])}"


def price_for(kind: str, item: dict, user_id=None):
    """(базовая цена, итоговая цена, скидка %). Скидка подписчиков действует только на услуги."""
    base = int(item["price"])
    discount = user_discount(user_id) if (kind == "svc" and user_id) else 0
    final = math.ceil(base * (100 - discount) / 100)
    return base, final, discount


def stars_for(price: int) -> int:
    rate = float(settings().get("rub_per_star") or 1.8)
    return max(1, math.ceil(price / rate))


def enabled_methods() -> list:
    """Способы оплаты, которые включены в админке И подключены (есть токен)."""
    s = settings()
    out = []
    if s.get("enable_stars"):
        out.append("stars")
    if s.get("enable_boomkassa") and cfg("bk_api_key"):
        out.append("boomkassa")
    if s.get("enable_card") and cfg("provider_token"):
        out.append("card")
    if s.get("enable_crypto") and cfg("crypto_pay_token"):
        out.append("crypto")
    return out


def method_label(method: str) -> str:
    s = settings()
    return {
        "stars": "⭐ Telegram Stars",
        "boomkassa": s.get("bk_label") or "💳 Карта / СБП",
        "card": s.get("card_label") or "💳 Картой в Telegram",
        "crypto": f"💵 {crypto_asset()} (крипта)",
    }.get(method, method)


def methods_line() -> str:
    names = {"stars": "⭐ Stars", "boomkassa": "💳 карта/СБП", "card": "💳 карта", "crypto": f"💵 {crypto_asset()}"}
    return " · ".join(names[m] for m in enabled_methods())


def fill(text: str, **values) -> str:
    """Подставляет {name}, {shop}, {support}, {order}…; остальные фигурные скобки не трогает."""
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def common_values() -> dict:
    s = settings()
    return {"shop": escape(s.get("shop_name", "")), "support": s.get("support_username", "")}


def render(key: str, tx: dict = None, **values) -> str:
    """Текст из раздела «Тексты» с подстановками."""
    tx = tx or texts()
    return fill(tx.get(key, ""), **common_values(), **values)


def welcome_text(name: str, tx: dict = None) -> str:
    return render("welcome", tx, name=escape(name or "друг"))


def subs_text(tx: dict = None) -> str:
    lines = [render("subs_header", tx)]
    for item in items("sub"):
        lines.append(f"{item['emoji']} <b>{item['name']} • {item['period']}</b>: {fmt(item['price'])}")
    return "\n".join(lines)


def services_text(bot_username: str = "", tx: dict = None) -> str:
    lines = [render("services_header", tx)]
    for item in items("svc"):
        label = escape(f"{item['emoji']} {item['name'].upper()} — {item['amount']}")
        if bot_username:
            lines.append(f'👉 <a href="https://t.me/{bot_username}?start=svc_{item["key"]}">{label}</a>')
        else:
            lines.append(f'👉 <a href="#">{label}</a>')
    lines.append("")
    lines.append(render("services_footer", tx))
    return "\n".join(lines)


def support_text(tx: dict = None) -> str:
    return render("support", tx)


def order_values(order) -> dict:
    return {"order": order["id"], "title": escape(order["title"]), "nick": escape(order["nick"] or "")}


def done_message(order) -> str:
    return render("done", **order_values(order))


def card_text(kind: str, item: dict, user_id=None) -> str:
    base, final, discount = price_for(kind, item, user_id)
    if kind == "sub":
        includes = "\n".join(x for x in item.get("includes", []) if x.strip())
        body = f"<i>{item.get('about', '')}</i>\n\n" if item.get("about") else ""
        if includes:
            body += f"✅ <b>Что входит в подписку:</b>\n{includes}\n\n"
        if item.get("term"):
            body += f"⏳ <b>Срок:</b> {item['term']}\n"
        if item.get("how"):
            body += f"📥 <b>Как получить:</b> {item['how']}\n"
    else:
        body = (item.get("text") or "").rstrip("\n") + "\n"

    if discount:
        price_line = f"💰 Цена: <s>{fmt(base)}</s> <b>{fmt(final)}</b>  🏷 твоя скидка −{discount}%"
    else:
        price_line = f"💰 <b>Цена: {fmt(final)}</b>"
    methods = methods_line()
    pay_line = f"\n💳 Оплата: {methods}" if methods else ""
    return f"<b>{item_title(kind, item)}</b>\n\n{body}\n{price_line}{pay_line}"


def order_paid_line(o) -> str:
    """Как оплачен заказ — для админки и уведомлений."""
    method = o["method"] if "method" in o.keys() else None
    if o["currency"] == "XTR":
        return f"{o['amount']} ⭐ Stars"
    if method == "crypto" or o["currency"] == "USDT":
        return f"{fmt(o['price'])} в {crypto_asset()}"
    if method == "boomkassa":
        return f"{fmt(o['price'])} · BoomKassa"
    if method == "manual":
        return f"{fmt(o['price'])} · вручную"
    return f"{fmt(o['price'])} · карта"
