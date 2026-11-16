# -*- coding: utf-8 -*-
"""
Общее хранилище для бота и веб-админки:
  • настройки из .env (токены и секреты);
  • заказы (SQLite, orders.db);
  • каталог, тексты и настройки магазина (catalog.json, правится в админке);
  • сборка текстов экранов (одинаково для бота и превью в админке).
"""
import copy
import json
import math
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path

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


load_env(BASE / ".env")


def env_on(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip() == "1"


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CURRENCY = os.getenv("CURRENCY", "RUB")

PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_TESTNET = env_on("CRYPTO_TESTNET")
CRYPTO_ASSET = os.getenv("CRYPTO_ASSET", "USDT")
CRYPTO_API = "https://testnet-pay.crypt.bot/api" if CRYPTO_TESTNET else "https://pay.crypt.bot/api"
CRYPTO_INVOICE_TTL = 3600

PRIVATE_CHANNEL_ID = os.getenv("PRIVATE_CHANNEL_ID", "")

# Папка для данных (заказы, каталог). На хостинге укажи постоянное хранилище, например DATA_DIR=/data
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE)
DATA_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8080") or 8080)

# ═══════════════════════════ ЗАКАЗЫ (SQLite) ═══════════════════════════

DB = sqlite3.connect(DATA_DIR / "orders.db", check_same_thread=False, timeout=10)
DB.row_factory = sqlite3.Row
DB_LOCK = threading.RLock()  # бот и админка работают в разных потоках

with DB_LOCK:
    DB.execute(
        """
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
            status            TEXT NOT NULL,      -- created / paid / waiting / done / cancelled
            nick              TEXT,
            charge_id         TEXT,
            created_at        TEXT NOT NULL,
            paid_at           TEXT,
            crypto_invoice_id INTEGER
        )
        """
    )
    if "crypto_invoice_id" not in {r["name"] for r in DB.execute("PRAGMA table_info(orders)")}:
        DB.execute("ALTER TABLE orders ADD COLUMN crypto_invoice_id INTEGER")
    DB.commit()

STATUS_NAMES = {
    "created": "🕓 создан",
    "paid": "💰 оплачен, ждём ник",
    "waiting": "⏳ ждёт выдачи",
    "done": "✅ выполнен",
    "cancelled": "❌ отменён",
}
PAID_STATUSES = ("paid", "waiting", "done")


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


def get_order(order_id: int):
    return db_one("SELECT * FROM orders WHERE id = ?", (order_id,))


def set_status(order_id: int, status: str) -> None:
    db_exec("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


def mark_paid(order_id: int, charge_id: str) -> bool:
    """Атомарно помечает заказ оплаченным. False — если он уже был обработан (защита от дублей)."""
    cur = db_exec(
        "UPDATE orders SET status = 'paid', charge_id = ?, paid_at = ? "
        "WHERE id = ? AND status IN ('created', 'cancelled')",
        (charge_id, now_iso(), order_id),
    )
    return cur.rowcount == 1


def set_nick(order_id: int, nick: str) -> None:
    db_exec("UPDATE orders SET nick = ?, status = 'waiting' WHERE id = ?", (nick, order_id))


def pending_nick_order(user_id: int):
    """Последний оплаченный заказ, по которому бот ещё ждёт ник."""
    return db_one(
        "SELECT * FROM orders WHERE user_id = ? AND status = 'paid' ORDER BY paid_at DESC, id DESC LIMIT 1",
        (user_id,),
    )


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


# ═══════════════════════════ КАТАЛОГ (catalog.json) ═══════════════════════════

CATALOG_PATH = DATA_DIR / "catalog.json"
BACKUP_PATH = DATA_DIR / "catalog.backup.json"
_cat_lock = threading.RLock()
_cat_cache = {"mtime": None, "data": None}


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


def load_catalog() -> dict:
    """Текущий каталог. Перечитывается автоматически, когда файл изменился (например, из админки)."""
    with _cat_lock:
        if not CATALOG_PATH.exists():
            save_catalog(_seed())
        mtime = CATALOG_PATH.stat().st_mtime_ns
        if _cat_cache["mtime"] != mtime:
            _cat_cache["data"] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
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
                "code", "pre", "a", "tg-spoiler", "blockquote", "span"}


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


# ═══════════════════════════ ТЕКСТЫ ЭКРАНОВ ═══════════════════════════

def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ") + f" {settings().get('currency_sign', '₽')}"


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


def methods_line() -> str:
    s = settings()
    names = []
    if s.get("enable_stars"):
        names.append("⭐ Stars")
    if s.get("enable_card"):
        names.append("💳 рубли")
    if s.get("enable_crypto"):
        names.append(f"💵 {CRYPTO_ASSET}")
    return " · ".join(names)


def fill(text: str, **values) -> str:
    """Подставляет {name}, {shop}, {support}; остальные фигурные скобки не трогает."""
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def welcome_text(name: str, tx: dict = None) -> str:
    s, tx = settings(), tx or texts()
    return fill(tx["welcome"], name=escape(name or "друг"), shop=escape(s.get("shop_name", "")),
                support=s.get("support_username", ""))


def subs_text(tx: dict = None) -> str:
    lines = [(tx or texts())["subs_header"]]
    for item in items("sub"):
        lines.append(f"{item['emoji']} <b>{item['name']} • {item['period']}</b>: {fmt(item['price'])}")
    return "\n".join(lines)


def services_text(bot_username: str = "", tx: dict = None) -> str:
    tx = tx or texts()
    lines = [tx["services_header"]]
    for item in items("svc"):
        label = escape(f"{item['emoji']} {item['name'].upper()} — {item['amount']}")
        if bot_username:
            lines.append(f'👉 <a href="https://t.me/{bot_username}?start=svc_{item["key"]}">{label}</a>')
        else:
            lines.append(f"👉 <a href=\"#\">{label}</a>")
    lines.append("")
    lines.append(fill(tx["services_footer"], support=settings().get("support_username", "")))
    return "\n".join(lines)


def support_text(tx: dict = None) -> str:
    return fill((tx or texts())["support"], support=settings().get("support_username", ""))


def done_message(order) -> str:
    return (f"🎉 Заказ <b>#{order['id']}</b> выполнен!\n📦 {escape(order['title'])}\n\n"
            f"Спасибо, что выбрал {escape(settings().get('shop_name', ''))} 💎")


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
