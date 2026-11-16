# -*- coding: utf-8 -*-
"""
Веб-админка магазина (Flask).
Запускается вместе с ботом (python bot.py) или отдельно: python admin.py
Адрес: http://<сервер>:<ADMIN_PORT>, пароль — ADMIN_PASSWORD из .env.

Всё, что меняется здесь, сохраняется в catalog.json — бот подхватывает изменения сразу,
без перезапуска.
"""
import hmac
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from jinja2 import DictLoader
from markupsafe import Markup
from werkzeug.serving import make_server

import store as S

log = logging.getLogger("robux_admin")

TZ_OFFSET = int(os.getenv("TZ_OFFSET", "3") or 3)  # часовой пояс для дат в админке (МСК = 3)
BOT_USERNAME = ""
KINDS = {"sub": "Подписки", "svc": "Услуги"}

try:
    import telebot
    TG = telebot.TeleBot(S.BOT_TOKEN, parse_mode="HTML") if S.BOT_TOKEN else None
except ImportError:  # админку можно открыть и без telebot, но тогда она не пишет покупателям
    TG = None


def _secret_key() -> str:
    path = S.DATA_DIR / ".admin_secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


app = Flask(__name__)
app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

# ═══════════════════════════ ЗАЩИТА ═══════════════════════════

_fails: dict = {}  # ip -> (кол-во ошибок, заблокирован до)
MAX_FAILS, LOCK_SECONDS = 5, 600


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def _check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Страница устарела, обнови её и попробуй ещё раз")


@app.after_request
def _headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def check_kind(kind: str) -> None:
    if kind not in KINDS:
        abort(404)


# ═══════════════════════════ ШАБЛОНЫ: ФИЛЬТРЫ И КОНТЕКСТ ═══════════════════════════

def tg_html(text: str) -> Markup:
    """Текст сообщения Telegram → HTML для превью (переносы строк → <br>)."""
    return Markup((text or "").replace("\n", "<br>"))


def local_dt(iso: str) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso) + timedelta(hours=TZ_OFFSET)
    return dt.strftime("%d.%m %H:%M")


def order_paid(o) -> str:
    if o["currency"] == "XTR":
        return f"{o['amount']} ⭐"
    if o["currency"] == "USDT":
        return f"{S.fmt(o['price'])} · {S.CRYPTO_ASSET}"
    return f"{S.fmt(o['price'])} · карта"


app.jinja_env.filters.update(tg=tg_html, dt=local_dt, money=S.fmt)
app.jinja_env.globals.update(csrf_token=csrf_token, STATUS=S.STATUS_NAMES, order_paid=order_paid)


@app.context_processor
def _ctx():
    st = S.settings()
    data = {"shop_name": st.get("shop_name", "Магазин"), "currency_sign": st.get("currency_sign", "₽")}
    if session.get("ok"):
        data["todo_count"] = S.db_one(
            "SELECT COUNT(*) c FROM orders WHERE status IN ('paid', 'waiting')")["c"]
    return data


# ═══════════════════════════ ВХОД ═══════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    count, until = _fails.get(ip, (0, 0))
    error = None
    if request.method == "POST":
        if until > time.time():
            error = f"Слишком много попыток. Подожди {int((until - time.time()) // 60) + 1} мин."
        elif S.ADMIN_PASSWORD and hmac.compare_digest(
                request.form.get("password", "").encode(), S.ADMIN_PASSWORD.encode()):
            _fails.pop(ip, None)
            session.clear()
            session.permanent = True
            session["ok"] = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        else:
            count += 1
            _fails[ip] = (count, time.time() + LOCK_SECONDS if count >= MAX_FAILS else 0)
            error = "Неверный пароль"
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ═══════════════════════════ СВОДКА ═══════════════════════════

def _since(days: int = 0) -> str:
    """Начало сегодняшнего дня (по TZ_OFFSET) минус days, в UTC ISO."""
    local_now = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return (start - timedelta(hours=TZ_OFFSET)).isoformat(timespec="seconds")


def _revenue(since_iso: str) -> dict:
    row = S.db_one(
        "SELECT COUNT(*) n, COALESCE(SUM(price), 0) rub, "
        "COALESCE(SUM(CASE WHEN currency = 'XTR' THEN amount ELSE 0 END), 0) stars "
        "FROM orders WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ?",
        (since_iso,),
    )
    return dict(row)


@app.get("/")
@login_required
def dashboard():
    todo = S.db_all("SELECT * FROM orders WHERE status IN ('paid', 'waiting') ORDER BY id LIMIT 50")
    by_method = S.db_all(
        "SELECT currency, COUNT(*) n, COALESCE(SUM(price), 0) rub FROM orders "
        "WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ? GROUP BY currency",
        (_since(29),),
    )
    top = S.db_all(
        "SELECT title, COUNT(*) n, COALESCE(SUM(price), 0) rub FROM orders "
        "WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ? GROUP BY title ORDER BY rub DESC LIMIT 5",
        (_since(29),),
    )
    buyers = S.db_one("SELECT COUNT(DISTINCT user_id) c FROM orders WHERE status IN ('paid', 'waiting', 'done')")["c"]
    return render_template(
        "dashboard.html", active="dash", todo=todo,
        today=_revenue(_since(0)), week=_revenue(_since(6)), month=_revenue(_since(29)),
        by_method=by_method, top=top, buyers=buyers,
    )


# ═══════════════════════════ ЗАКАЗЫ ═══════════════════════════

ORDER_FILTERS = {
    "todo": ("Ждут выдачи", "status IN ('paid', 'waiting')"),
    "done": ("Выполнены", "status = 'done'"),
    "paid": ("Все оплаченные", "status IN ('paid', 'waiting', 'done')"),
    "other": ("Не оплачены / отменены", "status IN ('created', 'cancelled')"),
}


@app.get("/orders")
@login_required
def orders():
    f = request.args.get("f", "todo")
    if f not in ORDER_FILTERS:
        f = "todo"
    q = request.args.get("q", "").strip().lstrip("#@")
    sql, params = f"SELECT * FROM orders WHERE {ORDER_FILTERS[f][1]}", []
    if q:
        sql += " AND (CAST(id AS TEXT) = ? OR nick LIKE ? OR username LIKE ? OR CAST(user_id AS TEXT) = ?)"
        params += [q, f"%{q}%", f"%{q}%", q]
    sql += " ORDER BY id DESC LIMIT 200"
    return render_template("orders.html", active="orders", rows=S.db_all(sql, tuple(params)),
                           f=f, q=q, filters=ORDER_FILTERS)


def _back_to_orders():
    return redirect(request.form.get("back") or url_for("orders"))


@app.post("/orders/<int:order_id>/done")
@login_required
def order_done(order_id: int):
    order = S.get_order(order_id)
    if not order:
        abort(404)
    S.set_status(order_id, "done")
    if _tg_send(order["user_id"], S.done_message(order)):
        flash(f"✅ Заказ #{order_id} выполнен, покупатель получил уведомление")
    else:
        flash(f"Заказ #{order_id} отмечен выполненным, но написать покупателю не получилось", "error")
    return _back_to_orders()


@app.post("/orders/<int:order_id>/send")
@login_required
def order_send(order_id: int):
    order = S.get_order(order_id)
    if not order:
        abort(404)
    body = (request.form.get("text") or "").strip()
    if not body:
        flash("Пустое сообщение не отправлено", "error")
        return _back_to_orders()
    content = f"<code>{escape(body)}</code>" if request.form.get("as") == "code" else escape(body)
    text = f"📬 <b>Заказ #{order_id}</b>\n📦 {escape(order['title'])}\n\n{content}"
    if _tg_send(order["user_id"], text):
        if request.form.get("close") == "on":
            S.set_status(order_id, "done")
        flash(f"📩 Отправлено покупателю по заказу #{order_id}")
    else:
        flash("Не получилось отправить: покупатель мог заблокировать бота", "error")
    return _back_to_orders()


def _tg_send(chat_id: int, text: str) -> bool:
    if not TG:
        return False
    try:
        TG.send_message(chat_id, text)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Админка: не удалось написать %s: %s", chat_id, e)
        return False


# ═══════════════════════════ ПОДПИСКИ И УСЛУГИ ═══════════════════════════

NEW_DEFAULTS = {
    "sub": {"emoji": "⭐", "name": "", "period": "Навсегда", "price": "", "discount": 0, "days": None,
            "needs_nick": True, "active": True, "short": "", "about": "", "includes": [], "term": "", "how": ""},
    "svc": {"emoji": "🎁", "name": "", "amount": "", "price": "", "needs_nick": True, "nick_prompt": "",
            "active": True, "short": "", "text": ""},
}


def _int(value, default=None):
    try:
        return int(str(value).replace(" ", "").replace(" ", ""))
    except (TypeError, ValueError):
        return default


def item_from_form(kind: str, form, base: dict):
    """Собирает позицию из формы и проверяет её. Возвращает (позиция, список ошибок)."""
    def g(name):
        return (form.get(name) or "").replace("\r\n", "\n").strip()

    it, errs = dict(base), []
    it["emoji"], it["name"] = g("emoji"), g("name")
    if not it["emoji"]:
        errs.append("Укажи эмодзи")
    if not it["name"]:
        errs.append("Укажи название")
    elif len(it["name"]) > 40:
        errs.append("Название длиннее 40 символов")

    price = _int(g("price"))
    if price is None or not 1 <= price <= 10_000_000:
        errs.append("Цена: целое число больше 0")
        price = 0
    it["price"] = price

    it["short"] = g("short")
    if not it["short"]:
        errs.append("Заполни короткое описание для счёта")
    elif len(it["short"]) > 200:
        errs.append("Короткое описание длиннее 200 символов")
    it["needs_nick"] = form.get("needs_nick") == "on"
    it["active"] = form.get("active") == "on"

    plain = [("Эмодзи", it["emoji"]), ("Название", it["name"])]
    if kind == "sub":
        it["period"] = g("period")
        if not it["period"]:
            errs.append("Укажи период (например, «1 месяц» или «Навсегда»)")
        plain.append(("Период", it["period"]))
        discount = _int(g("discount") or 0)
        if discount is None or not 0 <= discount <= 90:
            errs.append("Скидка: число от 0 до 90")
            discount = 0
        it["discount"] = discount
        days_raw = g("days")
        days = _int(days_raw) if days_raw else None
        if days_raw and (days is None or not 1 <= days <= 3650):
            errs.append("Срок скидки: число дней от 1 до 3650 или пусто (навсегда)")
            days = None
        it["days"] = days
        it["about"], it["term"], it["how"] = g("about"), g("term"), g("how")
        it["includes"] = [line.strip() for line in g("includes").split("\n") if line.strip()]
        rich = [("Описание", it["about"]), ("Что входит", "\n".join(it["includes"])),
                ("Срок", it["term"]), ("Как получить", it["how"])]
    else:
        it["amount"] = g("amount")
        if not it["amount"]:
            errs.append("Укажи, что получает покупатель (например, «500 R$»)")
        plain.append(("Количество", it["amount"]))
        it["text"] = g("text")
        if not it["text"]:
            errs.append("Заполни описание услуги")
        it["nick_prompt"] = g("nick_prompt")
        rich = [("Описание", it["text"]), ("Вопрос про ник", it["nick_prompt"])]

    for label, value in plain:
        if re.search(r"[<>&]", value):
            errs.append(f"{label}: без символов &lt; &gt; &amp;")
    for label, value in rich:
        errs += [f"{label}: {e}" for e in S.html_errors(value)]

    if len(S.card_text(kind, it)) > 3900:
        errs.append("Карточка получилась длиннее лимита Telegram (≈4000 символов), сократи текст")
    return it, errs


def preview_buttons(kind: str, item: dict):
    return [[f"💳 Оплатить • {S.fmt(item.get('price') or 0)}"], ["⬅️ Назад", "🏠 Меню"]]


def list_buttons(kind: str):
    if kind == "sub":
        rows = [[S.sub_button(i)] for i in S.items("sub")]
    else:
        lst = S.items("svc")
        rows = [[f"{i['emoji']} {i['name']}" for i in lst[n:n + 2]] for n in range(0, len(lst), 2)]
    return rows + [["⬅️ Назад"]]


@app.get("/items/<kind>")
@login_required
def items_list(kind: str):
    check_kind(kind)
    screen = S.subs_text() if kind == "sub" else S.services_text(BOT_USERNAME)
    return render_template("items.html", active=kind, kind=kind, title=KINDS[kind],
                           lst=S.items(kind, only_active=False), screen=screen, buttons=list_buttons(kind))


@app.route("/items/<kind>/<key>", methods=["GET", "POST"])
@login_required
def item_edit(kind: str, key: str):
    check_kind(kind)
    data = S.edit_catalog()
    lst = data[S.list_name(kind)]
    is_new = key == "new"
    idx = None if is_new else next((n for n, x in enumerate(lst) if x["key"] == key), None)
    if not is_new and idx is None:
        abort(404)
    base = dict(NEW_DEFAULTS[kind]) if is_new else lst[idx]
    errors = []

    if request.method == "POST":
        item, errors = item_from_form(kind, request.form, base)
        if not errors:
            if is_new:
                existing = {x["key"] for x in lst}
                item["key"] = next(k for k in iter(lambda: secrets.token_hex(4), None) if k not in existing)
                lst.append(item)
            else:
                lst[idx] = item
            S.save_catalog(data)
            state = "показывается в боте" if item["active"] else "скрыто из бота"
            flash(f"✅ «{item['name']}» сохранено и {state}")
            return redirect(url_for("items_list", kind=kind))
        base = item

    return render_template("item_form.html", active=kind, kind=kind, title=KINDS[kind],
                           item=base, is_new=is_new, key=key, errors=errors)


@app.post("/items/<kind>/<key>/<action>")
@login_required
def item_action(kind: str, key: str, action: str):
    check_kind(kind)
    data = S.edit_catalog()
    lst = data[S.list_name(kind)]
    idx = next((n for n, x in enumerate(lst) if x["key"] == key), None)
    if idx is None:
        abort(404)
    name = lst[idx]["name"]
    if action == "delete":
        lst.pop(idx)
        flash(f"🗑 «{name}» удалено")
    elif action == "toggle":
        lst[idx]["active"] = not lst[idx].get("active", True)
        flash(f"{'👁 «' + name + '» снова в боте' if lst[idx]['active'] else '🙈 «' + name + '» скрыто из бота'}")
    elif action in ("up", "down"):
        j = idx - 1 if action == "up" else idx + 1
        if 0 <= j < len(lst):
            lst[idx], lst[j] = lst[j], lst[idx]
    else:
        abort(404)
    S.save_catalog(data)
    return redirect(url_for("items_list", kind=kind))


@app.post("/preview/item/<kind>")
@login_required
def preview_item(kind: str):
    check_kind(kind)
    base = S.get_item(kind, request.form.get("key", ""), only_active=False) or dict(NEW_DEFAULTS[kind])
    item, errors = item_from_form(kind, request.form, base)
    return jsonify(html=str(tg_html(S.card_text(kind, item))), buttons=preview_buttons(kind, item), errors=errors)


# ═══════════════════════════ ТЕКСТЫ ═══════════════════════════

TEXT_FIELDS = [
    ("welcome", "👋 Приветствие", "Показывается на /start и по кнопке «Меню». "
                                 "Подстановки: {name} — имя покупателя, {shop} — название магазина, {support} — ник поддержки"),
    ("subs_header", "💎 Шапка раздела «Подписки»", "Под ней бот сам добавит список тарифов с ценами и кнопки"),
    ("services_header", "🛍 Шапка раздела «Услуги»", "Под ней бот сам добавит кликабельный список услуг"),
    ("services_footer", "🛍 Подвал раздела «Услуги»", "{support} — ник поддержки"),
    ("support", "🆘 Техподдержка", "{support} — ник поддержки; кнопка «Написать в поддержку» добавится сама"),
    ("nick_prompt", "✍️ Просьба прислать ник после оплаты", "Для отдельной услуги можно задать свой текст в её карточке"),
]
TEXT_KEYS = [k for k, _, _ in TEXT_FIELDS]


def render_text_screen(field: str, tx: dict):
    """Текст и кнопки экрана, как их увидит покупатель."""
    if field == "welcome":
        return S.welcome_text("Гоша", tx), [["💎 Подписки", "🛍 Услуги"], ["🆘 Техподдержка"]]
    if field == "subs_header":
        return S.subs_text(tx), list_buttons("sub")
    if field in ("services_header", "services_footer"):
        return S.services_text(BOT_USERNAME, tx), list_buttons("svc")
    if field == "support":
        return S.support_text(tx), [["✉️ Написать в поддержку"], ["⬅️ Назад"]]
    return tx[field], []


@app.route("/texts", methods=["GET", "POST"])
@login_required
def texts_page():
    data = S.edit_catalog()
    tx = data["texts"]
    errors = {}
    if request.method == "POST":
        new = {k: (request.form.get(k) or "").replace("\r\n", "\n").strip() for k in TEXT_KEYS}
        for k, v in new.items():
            errs = S.html_errors(v) + ([] if v else ["текст не может быть пустым"])
            if errs:
                errors[k] = errs
        tx.update(new)
        if not errors:
            S.save_catalog(data)
            flash("✅ Тексты сохранены, бот уже показывает новые")
            return redirect(url_for("texts_page"))
    screens = {k: render_text_screen(k, tx) for k in TEXT_KEYS}
    return render_template("texts.html", active="texts", fields=TEXT_FIELDS, tx=tx, errors=errors, screens=screens)


@app.post("/preview/text")
@login_required
def preview_text():
    field = request.form.get("field", "")
    if field not in TEXT_KEYS:
        abort(404)
    tx = dict(S.texts())
    tx[field] = (request.form.get("value") or "").replace("\r\n", "\n").strip()
    html, buttons = render_text_screen(field, tx)
    return jsonify(html=str(tg_html(html)), buttons=buttons, errors=S.html_errors(tx[field]))


# ═══════════════════════════ НАСТРОЙКИ ═══════════════════════════

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    data = S.edit_catalog()
    st = data["settings"]
    errors = []
    if request.method == "POST":
        f = request.form
        new = {
            "shop_name": (f.get("shop_name") or "").strip(),
            "currency_sign": (f.get("currency_sign") or "").strip(),
            "support_username": (f.get("support_username") or "").strip().lstrip("@"),
            "welcome_sticker": (f.get("welcome_sticker") or "").strip(),
            "enable_stars": f.get("enable_stars") == "on",
            "enable_card": f.get("enable_card") == "on",
            "enable_crypto": f.get("enable_crypto") == "on",
        }
        if not new["shop_name"] or len(new["shop_name"]) > 40 or re.search(r"[<>&]", new["shop_name"]):
            errors.append("Название магазина: от 1 до 40 символов, без &lt; &gt; &amp;")
        if not new["currency_sign"] or len(new["currency_sign"]) > 4:
            errors.append("Знак валюты: 1–4 символа")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", new["support_username"]):
            errors.append("Ник поддержки: 4–32 латинских буквы, цифры или _")
        try:
            rate = float((f.get("rub_per_star") or "").replace(",", "."))
            if not 0.01 <= rate <= 1000:
                raise ValueError
            new["rub_per_star"] = rate
        except ValueError:
            errors.append("Курс звезды: число больше 0, например 1.8")
            new["rub_per_star"] = st.get("rub_per_star", 1.8)
        st.update(new)
        if not errors:
            S.save_catalog(data)
            flash("✅ Настройки сохранены")
            return redirect(url_for("settings_page"))
    env = {
        "card": bool(S.PROVIDER_TOKEN),
        "crypto": bool(S.CRYPTO_PAY_TOKEN),
        "testnet": S.CRYPTO_TESTNET,
        "channel": bool(S.PRIVATE_CHANNEL_ID),
        "admin_id": bool(S.ADMIN_ID),
    }
    return render_template("settings.html", active="settings", st=st, errors=errors, env=env)


# ═══════════════════════════ HTML-ШАБЛОНЫ ═══════════════════════════

TEMPLATES = {}

TEMPLATES["base.html"] = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}Админка{% endblock %} · {{ shop_name }}</title>
<style>
:root{--bg:#0d1016;--panel:#151a24;--panel2:#1c2330;--line:#283142;--text:#e9edf5;--mute:#8a94a7;
--acc:#7b61ff;--acc2:#3aa8ff;--ok:#31c97a;--warn:#f0b04a;--bad:#ff5d6e;--r:14px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc2);text-decoration:none}
.top{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;padding:10px 18px;
background:rgba(13,16,22,.9);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.brand{font-weight:800;white-space:nowrap;letter-spacing:.2px}
.brand small{display:block;color:var(--mute);font-weight:500;font-size:11.5px;letter-spacing:.4px;text-transform:uppercase}
nav{display:flex;gap:4px;overflow-x:auto;flex:1;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav a{color:var(--mute);padding:8px 12px;border-radius:10px;white-space:nowrap;font-weight:500}
nav a.on{background:var(--panel2);color:var(--text)}nav a:hover{color:var(--text)}
.pill{display:inline-block;background:var(--bad);color:#fff;border-radius:999px;padding:0 7px;font-size:12px;margin-left:5px;line-height:18px}
main{max-width:1180px;margin:0 auto;padding:22px 18px 70px}
h1{font-size:24px;margin:2px 0 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1 .sp{flex:1}
h2{font-size:17px;margin:0 0 12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px}
.grid{display:grid;gap:14px}.stack{display:grid;gap:12px}
.stats{grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin-bottom:18px}
.stat .l{color:var(--mute);font-size:13px}.stat .v{font-size:25px;font-weight:800;margin-top:2px}.stat .s{color:var(--mute);font-size:12.5px}
.stat.hot{border-color:#5a3a1a;background:linear-gradient(180deg,#2a1e12,#151a24)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:0;border-radius:10px;padding:9px 14px;
font:inherit;font-weight:650;cursor:pointer;background:var(--acc);color:#fff;white-space:nowrap}
.btn:hover{filter:brightness(1.1)}
.btn.sec{background:var(--panel2);color:var(--text);border:1px solid var(--line)}
.btn.ok{background:var(--ok);color:#05140b}.btn.bad{background:transparent;color:var(--bad);border:1px solid #5b2a33}
.btn.sm{padding:6px 10px;font-size:13px;border-radius:9px}
.ghost{background:none;border:1px solid var(--line);color:var(--mute);border-radius:10px;padding:7px 12px;cursor:pointer;font:inherit}
form.inline{display:inline}
label{display:block;font-size:13px;color:var(--mute);margin:14px 0 6px;font-weight:500}
input[type=text],input[type=number],input[type=password],input[type=search],textarea,select{width:100%;background:var(--bg);
border:1px solid var(--line);color:var(--text);border-radius:10px;padding:10px 12px;font:inherit}
textarea{min-height:120px;resize:vertical;font:13.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
input:focus,textarea:focus,select:focus{outline:2px solid var(--acc);border-color:transparent}
.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.check{display:flex;align-items:center;gap:10px;margin:14px 0 0;color:var(--text);font-size:15px;font-weight:500;cursor:pointer}
.check input{width:20px;height:20px;accent-color:var(--acc)}
.hint{color:var(--mute);font-size:12.5px;margin-top:6px}
.flash{padding:12px 14px;border-radius:12px;margin-bottom:14px;background:#12301f;border:1px solid #22573a}
.flash.error,.errbox{background:#35161d;border:1px solid #6b2a36;padding:12px 14px;border-radius:12px;margin-bottom:14px}
.errbox ul{margin:6px 0 0 18px;padding:0}
.split{display:grid;gap:20px;grid-template-columns:minmax(0,1fr) 370px;align-items:start}
@media(max-width:920px){.split{grid-template-columns:1fr}.sticky{position:static!important}}
.sticky{position:sticky;top:78px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}
.muted{color:var(--mute)}.small{font-size:13px}
/* превью Telegram */
.phone{background:#0a0d13;border:1px solid var(--line);border-radius:22px;overflow:hidden}
.phone-head{padding:10px 14px;font-size:12.5px;color:var(--mute);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:8px}
.chat{padding:14px;min-height:160px;background:radial-gradient(circle at 15% 0%,#1d2547 0,transparent 55%),radial-gradient(circle at 100% 100%,#2a1d40 0,transparent 50%),#0f131d}
.bubble{background:#202939;border-radius:16px 16px 16px 5px;padding:10px 12px;font-size:14.5px;line-height:1.42;overflow-wrap:anywhere}
.bubble a{color:#6cb9ff}.bubble code{background:#0004;padding:1px 4px;border-radius:5px;font-size:13px}
.bubble s{opacity:.7}.bubble blockquote{border-left:3px solid var(--acc2);margin:4px 0;padding-left:8px}
.btns{display:grid;gap:4px;margin-top:4px}.btns .r{display:grid;gap:4px;grid-auto-flow:column;grid-auto-columns:1fr}
.btns span{background:#2a3446d9;border-radius:10px;text-align:center;padding:8px 6px;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.errs{color:var(--bad);font-size:13px;margin-top:10px}
/* списки */
.item{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.item.off{opacity:.55}
.em{font-size:24px;width:46px;height:46px;flex:none;display:grid;place-items:center;background:var(--panel2);border-radius:12px}
.info{flex:1;min-width:170px}.nm{font-weight:700}.sub{color:var(--mute);font-size:13px}
.acts{display:flex;gap:6px;flex-wrap:wrap}
.badge{font-size:12px;padding:2px 9px;border-radius:999px;background:var(--panel2);color:var(--mute);border:1px solid var(--line);white-space:nowrap}
.badge.ok{color:var(--ok);border-color:#22573a}.badge.warn{color:var(--warn);border-color:#5a4520}.badge.bad{color:var(--bad);border-color:#5b2a33}
/* заказы */
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tabs a{padding:7px 13px;border-radius:999px;background:var(--panel);border:1px solid var(--line);color:var(--mute);font-size:14px}
.tabs a.on{color:var(--text);border-color:var(--acc);background:#1d1a33}
.order .h{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center}
.order .meta{display:flex;gap:6px 16px;flex-wrap:wrap;color:var(--mute);font-size:13.5px;margin:8px 0 4px}
.order .meta b{color:var(--text);font-weight:600}
.nick{font-family:ui-monospace,Menlo,Consolas,monospace;background:var(--bg);border:1px solid var(--line);padding:1px 7px;border-radius:7px;color:var(--text)}
details{margin-top:8px}details summary{cursor:pointer;color:var(--acc2);font-size:14px;list-style:none}
details summary::-webkit-details-marker{display:none}
details textarea{min-height:80px;margin-top:8px}
.radios{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:14px}.radios label{display:flex;gap:6px;align-items:center;margin:0;color:var(--text)}
.toolbar{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px}
.toolbar button{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:3px 10px;cursor:pointer;font:inherit;font-size:13px}
.search{display:flex;gap:8px;margin-bottom:14px}.search input{max-width:360px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--mute);font-weight:500}
.login{max-width:380px;margin:12vh auto}
.status{display:flex;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}.status:last-child{border:0}
.dot{width:9px;height:9px;border-radius:50%;background:var(--bad);flex:none}.dot.ok{background:var(--ok)}
.empty{color:var(--mute);text-align:center;padding:28px}
@media(max-width:760px){.top{flex-wrap:wrap;gap:8px;padding:10px 12px}.top .brand{flex:1}.top nav{order:3;flex-basis:100%;margin:0 -4px}main{padding:16px 12px 70px}h1{font-size:21px}}
</style></head>
<body>
{% if session.get('ok') %}
<header class="top">
  <div class="brand">💎 {{ shop_name }}<small>админка</small></div>
  <nav>
    <a href="/" class="{{ 'on' if active == 'dash' }}">📊 Сводка</a>
    <a href="/orders" class="{{ 'on' if active == 'orders' }}">🧾 Заказы{% if todo_count %}<span class="pill">{{ todo_count }}</span>{% endif %}</a>
    <a href="/items/sub" class="{{ 'on' if active == 'sub' }}">💎 Подписки</a>
    <a href="/items/svc" class="{{ 'on' if active == 'svc' }}">🛍 Услуги</a>
    <a href="/texts" class="{{ 'on' if active == 'texts' }}">✍️ Тексты</a>
    <a href="/settings" class="{{ 'on' if active == 'settings' }}">⚙️ Настройки</a>
  </nav>
  <form method="post" action="/logout" class="inline"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="ghost">Выйти</button></form>
</header>
{% endif %}
<main>
{% with msgs = get_flashed_messages(with_categories=true) %}{% for cat, m in msgs %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}{% endwith %}
{% block content %}{% endblock %}
</main>
<script>
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function debounce(fn,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)}}
function renderButtons(el,rows){el.innerHTML=(rows||[]).map(r=>'<div class="r">'+r.map(t=>'<span>'+esc(t)+'</span>').join('')+'</div>').join('')}
document.addEventListener('click',e=>{const b=e.target.closest('[data-wrap]');if(!b)return;e.preventDefault();
  const ta=document.getElementById(b.dataset.for),[o,c]=b.dataset.wrap.split('|'),s=ta.selectionStart,en=ta.selectionEnd;
  ta.setRangeText(o+ta.value.slice(s,en)+c,s,en,'end');ta.focus();ta.dispatchEvent(new Event('input',{bubbles:true}))});
document.addEventListener('submit',e=>{const m=e.target.dataset.confirm;if(m&&!confirm(m))e.preventDefault()});
</script>
{% block scripts %}{% endblock %}
</body></html>"""

TEMPLATES["macros.html"] = r"""
{% macro phone(id, html='', buttons=[], head='Так увидит покупатель') %}
<div class="phone"><div class="phone-head"><span>📱 {{ head }}</span><span>бот</span></div>
<div class="chat"><div class="bubble" id="{{ id }}">{{ html|tg }}</div>
<div class="btns" id="{{ id }}-btns">{% for r in buttons %}<div class="r">{% for b in r %}<span>{{ b }}</span>{% endfor %}</div>{% endfor %}</div>
</div></div>
{% endmacro %}
{% macro toolbar(id) %}
<div class="toolbar">
<button type="button" data-for="{{ id }}" data-wrap="<b>|</b>"><b>Ж</b></button>
<button type="button" data-for="{{ id }}" data-wrap="<i>|</i>"><i>К</i></button>
<button type="button" data-for="{{ id }}" data-wrap="<u>|</u>"><u>Ч</u></button>
<button type="button" data-for="{{ id }}" data-wrap="<s>|</s>"><s>З</s></button>
<button type="button" data-for="{{ id }}" data-wrap="<code>|</code>">код</button>
<button type="button" data-for="{{ id }}" data-wrap="<tg-spoiler>|</tg-spoiler>">спойлер</button>
</div>
{% endmacro %}
{% macro csrf() %}<input type="hidden" name="csrf" value="{{ csrf_token() }}">{% endmacro %}
"""

TEMPLATES["login.html"] = r"""{% extends "base.html" %}{% block title %}Вход{% endblock %}
{% block content %}
<div class="login card">
  <h1>💎 Вход в админку</h1>
  {% if error %}<div class="errbox">{{ error }}</div>{% endif %}
  <form method="post">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <label>Пароль</label>
    <input type="password" name="password" autofocus autocomplete="current-password">
    <div class="actions"><button class="btn" style="width:100%">Войти</button></div>
  </form>
</div>
{% endblock %}"""

TEMPLATES["order_card.html"] = r"""{% from "macros.html" import csrf %}
<div class="card order">
  <div class="h">
    <div><b>#{{ o.id }}</b> · {{ o.title }}</div>
    <span class="badge {{ 'ok' if o.status == 'done' else 'warn' if o.status in ('paid','waiting') else 'bad' }}">{{ STATUS[o.status] }}</span>
  </div>
  <div class="meta">
    <span>💰 <b>{{ order_paid(o) }}</b></span>
    <span>👤 <a href="tg://user?id={{ o.user_id }}">{{ o.username or o.user_id }}</a></span>
    {% if o.nick %}<span>🎮 <span class="nick">{{ o.nick }}</span></span>{% elif o.status == 'paid' %}<span>🎮 ждём ник</span>{% endif %}
    <span>🕓 {{ (o.paid_at or o.created_at)|dt }}</span>
  </div>
  {% if o.status in ('paid', 'waiting', 'done') %}
  <div class="acts" style="margin-top:6px">
    {% if o.status != 'done' %}
    <form method="post" action="/orders/{{ o.id }}/done" class="inline">{{ csrf() }}<input type="hidden" name="back" value="{{ back }}">
      <button class="btn ok sm">✅ Выдано</button></form>
    {% endif %}
  </div>
  <details>
    <summary>📩 Отправить код или сообщение покупателю</summary>
    <form method="post" action="/orders/{{ o.id }}/send">{{ csrf() }}<input type="hidden" name="back" value="{{ back }}">
      <textarea name="text" placeholder="Например: код подарочной карты или ссылка на группу"></textarea>
      <div class="radios">
        <label><input type="radio" name="as" value="code" checked> как код</label>
        <label><input type="radio" name="as" value="text"> обычным текстом</label>
        <label><input type="checkbox" name="close" {{ 'checked' if o.status != 'done' }}> закрыть заказ</label>
      </div>
      <div class="actions" style="margin-top:10px"><button class="btn sm">Отправить</button></div>
    </form>
  </details>
  {% endif %}
</div>"""

TEMPLATES["dashboard.html"] = r"""{% extends "base.html" %}{% block title %}Сводка{% endblock %}
{% block content %}
<h1>📊 Сводка</h1>
<div class="grid stats">
  <div class="card stat {{ 'hot' if todo }}"><div class="l">Ждут выдачи</div><div class="v">{{ todo|length }}</div><div class="s"><a href="/orders">открыть заказы →</a></div></div>
  <div class="card stat"><div class="l">Сегодня</div><div class="v">{{ today.rub|money }}</div><div class="s">{{ today.n }} заказ(ов){% if today.stars %} · {{ today.stars }} ⭐{% endif %}</div></div>
  <div class="card stat"><div class="l">7 дней</div><div class="v">{{ week.rub|money }}</div><div class="s">{{ week.n }} заказ(ов){% if week.stars %} · {{ week.stars }} ⭐{% endif %}</div></div>
  <div class="card stat"><div class="l">30 дней</div><div class="v">{{ month.rub|money }}</div><div class="s">{{ month.n }} заказ(ов) · покупателей всего: {{ buyers }}</div></div>
</div>
<div class="split">
  <div class="stack">
    <h2>⏳ Нужно выдать</h2>
    {% for o in todo %}{% set back = '/' %}{% include "order_card.html" %}{% else %}<div class="card empty">🎉 Всё выдано, новых заказов нет</div>{% endfor %}
  </div>
  <div class="stack">
    <div class="card"><h2>💳 Способы оплаты · 30 дней</h2>
      {% set names = {'XTR': '⭐ Stars', 'RUB': '💳 Рубли', 'USDT': '💵 USDT'} %}
      {% if by_method %}<table>{% for m in by_method %}<tr><td>{{ names.get(m.currency, m.currency) }}</td><td>{{ m.n }} шт.</td><td style="text-align:right">{{ m.rub|money }}</td></tr>{% endfor %}</table>
      {% else %}<div class="muted small">Пока нет оплат</div>{% endif %}
    </div>
    <div class="card"><h2>🏆 Топ позиций · 30 дней</h2>
      {% if top %}<table>{% for t in top %}<tr><td>{{ t.title }}</td><td>{{ t.n }}</td><td style="text-align:right">{{ t.rub|money }}</td></tr>{% endfor %}</table>
      {% else %}<div class="muted small">Пока нет продаж</div>{% endif %}
    </div>
    <div class="muted small">Суммы в рублях по цене заказа; для Stars дополнительно показано число звёзд.</div>
  </div>
</div>
{% endblock %}"""

TEMPLATES["orders.html"] = r"""{% extends "base.html" %}{% block title %}Заказы{% endblock %}
{% block content %}
<h1>🧾 Заказы</h1>
<div class="tabs">{% for k, v in filters.items() %}<a href="?f={{ k }}" class="{{ 'on' if f == k }}">{{ v[0] }}</a>{% endfor %}</div>
<form class="search" method="get"><input type="hidden" name="f" value="{{ f }}">
  <input type="search" name="q" value="{{ q }}" placeholder="№ заказа, ник Roblox, ник или ID в Telegram">
  <button class="btn sec">Найти</button></form>
<div class="stack">
{% set back = request.full_path %}
{% for o in rows %}{% include "order_card.html" %}{% else %}<div class="card empty">Здесь пусто</div>{% endfor %}
</div>
{% endblock %}"""

TEMPLATES["items.html"] = r"""{% extends "base.html" %}{% from "macros.html" import phone, csrf %}
{% block title %}{{ title }}{% endblock %}
{% block content %}
<h1>{{ '💎' if kind == 'sub' else '🛍' }} {{ title }}<span class="sp"></span><a class="btn" href="/items/{{ kind }}/new">＋ Добавить</a></h1>
<div class="split">
  <div class="stack">
  {% for it in lst %}
    <div class="card item {{ '' if it.get('active', True) else 'off' }}">
      <div class="em">{{ it.emoji }}</div>
      <div class="info">
        <div class="nm">{{ it.name }} {% if kind == 'sub' %}• {{ it.period }}{% else %}— {{ it.amount }}{% endif %}</div>
        <div class="sub">{{ it.price|money }}{% if kind == 'sub' and it.discount %} · скидка {{ it.discount }}%{% if it.days %} на {{ it.days }} дн.{% endif %}{% endif %}
          · {% if it.get('active', True) %}<span class="badge ok">в боте</span>{% else %}<span class="badge warn">скрыто</span>{% endif %}</div>
      </div>
      <div class="acts">
        <a class="btn sm" href="/items/{{ kind }}/{{ it.key }}">✏️ Изменить</a>
        <form method="post" action="/items/{{ kind }}/{{ it.key }}/up" class="inline">{{ csrf() }}<button class="btn sec sm" title="Выше" {{ 'disabled' if loop.first }}>↑</button></form>
        <form method="post" action="/items/{{ kind }}/{{ it.key }}/down" class="inline">{{ csrf() }}<button class="btn sec sm" title="Ниже" {{ 'disabled' if loop.last }}>↓</button></form>
        <form method="post" action="/items/{{ kind }}/{{ it.key }}/toggle" class="inline">{{ csrf() }}<button class="btn sec sm">{{ '🙈 Скрыть' if it.get('active', True) else '👁 Показать' }}</button></form>
        <form method="post" action="/items/{{ kind }}/{{ it.key }}/delete" class="inline" data-confirm="Удалить «{{ it.name }}»? Это нельзя отменить (можно просто скрыть).">{{ csrf() }}<button class="btn bad sm">🗑</button></form>
      </div>
    </div>
  {% else %}<div class="card empty">Пока пусто, нажми «Добавить»</div>{% endfor %}
  </div>
  <div class="sticky">{{ phone('pv', screen, buttons, 'Список в боте') }}
    <div class="hint">Скрытые позиции в боте не показываются. Порядок меняется стрелками ↑ ↓.</div></div>
</div>
{% endblock %}"""

TEMPLATES["item_form.html"] = r"""{% extends "base.html" %}{% from "macros.html" import phone, toolbar, csrf %}
{% block title %}{{ 'Новая позиция' if is_new else item.name }}{% endblock %}
{% block content %}
<h1><a href="/items/{{ kind }}" class="muted">← {{ title }}</a> / {{ 'Новая позиция' if is_new else item.name }}</h1>
{% if errors %}<div class="errbox"><b>Не сохранено, исправь:</b><ul>{% for e in errors %}<li>{{ e|safe }}</li>{% endfor %}</ul></div>{% endif %}
<div class="split">
<form method="post" id="itemForm" class="card" data-preview="/preview/item/{{ kind }}">
  {{ csrf() }}<input type="hidden" name="key" value="{{ '' if is_new else key }}">
  <div class="row">
    <div><label>Эмодзи</label><input type="text" name="emoji" value="{{ item.emoji }}" maxlength="8"></div>
    <div style="grid-column:span 2"><label>Название</label><input type="text" name="name" value="{{ item.name }}" maxlength="40" placeholder="{{ 'VIP' if kind == 'sub' else 'Быстрые робуксы' }}"></div>
  </div>
  <div class="row">
    {% if kind == 'sub' %}
    <div><label>Период</label><input type="text" name="period" value="{{ item.period }}" placeholder="Навсегда / 1 месяц"></div>
    {% else %}
    <div><label>Что получает покупатель</label><input type="text" name="amount" value="{{ item.amount }}" placeholder="500 R$"></div>
    {% endif %}
    <div><label>Цена, {{ currency_sign }}</label><input type="number" name="price" value="{{ item.price }}" min="1" inputmode="numeric"></div>
  </div>
  {% if kind == 'sub' %}
  <div class="row">
    <div><label>Скидка на все услуги, %</label><input type="number" name="discount" value="{{ item.discount or 0 }}" min="0" max="90" inputmode="numeric"></div>
    <div><label>Срок скидки, дней</label><input type="number" name="days" value="{{ item.days or '' }}" min="1" placeholder="пусто = навсегда" inputmode="numeric"></div>
  </div>
  <div class="hint">Бот сам считает скидку подписчику и показывает в услугах зачёркнутую старую цену.</div>
  {% endif %}

  <label>Короткое описание для счёта (до 200 символов, без разметки)</label>
  <input type="text" name="short" value="{{ item.short }}" maxlength="200">

  {% if kind == 'sub' %}
  <label>Описание тарифа (курсивом под заголовком)</label>
  {{ toolbar('f_about') }}<textarea id="f_about" name="about" style="min-height:70px">{{ item.about }}</textarea>
  <label>Что входит в подписку: каждый пункт с новой строки</label>
  {{ toolbar('f_includes') }}<textarea id="f_includes" name="includes" style="min-height:140px">{{ item.includes|join('\n') }}</textarea>
  <label>Срок</label>
  <input type="text" name="term" value="{{ item.term }}" placeholder="навсегда, без продлений">
  <label>Как получить</label>
  {{ toolbar('f_how') }}<textarea id="f_how" name="how" style="min-height:80px">{{ item.how }}</textarea>
  {% else %}
  <label>Описание услуги</label>
  {{ toolbar('f_text') }}<textarea id="f_text" name="text" style="min-height:170px">{{ item.text }}</textarea>
  {% endif %}

  <label class="check"><input type="checkbox" name="needs_nick" {{ 'checked' if item.needs_nick }}> Спрашивать ник в Roblox после оплаты</label>
  {% if kind == 'svc' %}
  <label>Свой текст вопроса про ник (необязательно)</label>
  <input type="text" name="nick_prompt" value="{{ item.nick_prompt or '' }}" placeholder="По умолчанию: текст из раздела «Тексты»">
  {% endif %}
  <label class="check"><input type="checkbox" name="active" {{ 'checked' if item.get('active', True) }}> Показывать в боте</label>

  <div class="hint" style="margin-top:14px">Разметка Telegram: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;, &lt;s&gt;зачёркнутый&lt;/s&gt;, &lt;code&gt;код&lt;/code&gt;. Выдели текст и нажми кнопку над полем.</div>
  <div class="actions">
    <button class="btn">💾 Сохранить</button>
    <a class="btn sec" href="/items/{{ kind }}">Отмена</a>
  </div>
</form>
<div class="sticky">{{ phone('pv') }}<div class="errs" id="pv-errs"></div></div>
</div>
{% endblock %}
{% block scripts %}<script>
(()=>{const f=document.getElementById('itemForm');
const upd=debounce(async()=>{try{const r=await fetch(f.dataset.preview,{method:'POST',body:new FormData(f)});const j=await r.json();
document.getElementById('pv').innerHTML=j.html;renderButtons(document.getElementById('pv-btns'),j.buttons);
document.getElementById('pv-errs').innerHTML=(j.errors||[]).map(x=>'• '+x).join('<br>');}catch(e){}},250);
f.addEventListener('input',upd);f.addEventListener('change',upd);upd();})();
</script>{% endblock %}"""

TEMPLATES["texts.html"] = r"""{% extends "base.html" %}{% from "macros.html" import phone, toolbar, csrf %}
{% block title %}Тексты{% endblock %}
{% block content %}
<h1>✍️ Тексты бота</h1>
{% if errors %}<div class="errbox"><b>Не сохранено: проверь поля, отмеченные красным</b></div>{% endif %}
<form method="post" id="textsForm" class="stack">{{ csrf() }}
{% for key, label, hint in fields %}
  <div class="card split" style="grid-template-columns:minmax(0,1fr) 340px">
    <div>
      <h2>{{ label }}</h2>
      {{ toolbar('t_' ~ key) }}
      <textarea id="t_{{ key }}" name="{{ key }}" data-field="{{ key }}" style="min-height:190px">{{ tx[key] }}</textarea>
      <div class="hint">{{ hint }}</div>
      {% if errors.get(key) %}<div class="errs">{% for e in errors[key] %}• {{ e|safe }}<br>{% endfor %}</div>{% endif %}
      <div class="errs" id="pv_{{ key }}-errs"></div>
    </div>
    <div>{{ phone('pv_' ~ key, screens[key][0], screens[key][1]) }}</div>
  </div>
{% endfor %}
<div class="actions" style="position:sticky;bottom:12px"><button class="btn">💾 Сохранить все тексты</button></div>
</form>
{% endblock %}
{% block scripts %}<script>
(()=>{const csrf=document.querySelector('#textsForm [name=csrf]').value;
document.querySelectorAll('textarea[data-field]').forEach(ta=>{const id='pv_'+ta.dataset.field;
ta.addEventListener('input',debounce(async()=>{const fd=new FormData();fd.append('csrf',csrf);fd.append('field',ta.dataset.field);fd.append('value',ta.value);
try{const r=await fetch('/preview/text',{method:'POST',body:fd});const j=await r.json();document.getElementById(id).innerHTML=j.html;
renderButtons(document.getElementById(id+'-btns'),j.buttons);document.getElementById(id+'-errs').innerHTML=(j.errors||[]).map(x=>'• '+x).join('<br>');}catch(e){}},250));});})();
</script>{% endblock %}"""

TEMPLATES["settings.html"] = r"""{% extends "base.html" %}{% from "macros.html" import csrf %}
{% block title %}Настройки{% endblock %}
{% block content %}
<h1>⚙️ Настройки</h1>
{% if errors %}<div class="errbox"><b>Не сохранено:</b><ul>{% for e in errors %}<li>{{ e|safe }}</li>{% endfor %}</ul></div>{% endif %}
<div class="split">
<form method="post" class="card">{{ csrf() }}
  <h2>🏪 Магазин</h2>
  <div class="row">
    <div><label>Название магазина</label><input type="text" name="shop_name" value="{{ st.shop_name }}" maxlength="40"></div>
    <div><label>Знак валюты</label><input type="text" name="currency_sign" value="{{ st.currency_sign }}" maxlength="4"></div>
  </div>
  <label>Аккаунт техподдержки (без @)</label>
  <input type="text" name="support_username" value="{{ st.support_username }}">
  <label>Стикер на /start (file_id)</label>
  <input type="text" name="welcome_sticker" value="{{ st.welcome_sticker }}" placeholder="пусто: анимированный 👋">
  <div class="hint">Чтобы узнать file_id, отправь боту стикер со своего админ-аккаунта, он пришлёт ID.</div>

  <h2 style="margin-top:22px">💳 Оплата</h2>
  <label class="check"><input type="checkbox" name="enable_stars" {{ 'checked' if st.enable_stars }}> ⭐ Telegram Stars</label>
  <label class="check"><input type="checkbox" name="enable_card" {{ 'checked' if st.enable_card }}> 💳 Рубли (карта)</label>
  <label class="check"><input type="checkbox" name="enable_crypto" {{ 'checked' if st.enable_crypto }}> 💵 USDT через CryptoBot</label>
  <label>Курс: сколько рублей стоит 1 ⭐</label>
  <input type="text" name="rub_per_star" value="{{ st.rub_per_star }}" inputmode="decimal">
  <div class="hint">Цена в звёздах = цена в рублях ÷ курс, с округлением вверх.</div>
  <div class="actions"><button class="btn">💾 Сохранить</button></div>
</form>
<div class="card">
  <h2>🔌 Подключения (файл .env)</h2>
  <div class="status"><span class="dot {{ 'ok' if env.admin_id }}"></span>Уведомления о заказах в Telegram (ADMIN_ID)</div>
  <div class="status"><span class="dot ok"></span>⭐ Stars работают без токена</div>
  <div class="status"><span class="dot {{ 'ok' if env.card }}"></span>💳 Платёжка картой (PROVIDER_TOKEN)</div>
  <div class="status"><span class="dot {{ 'ok' if env.crypto }}"></span>💵 CryptoBot (CRYPTO_PAY_TOKEN){% if env.crypto %} · {{ 'тестовая сеть' if env.testnet else 'боевой режим' }}{% endif %}</div>
  <div class="status"><span class="dot {{ 'ok' if env.channel }}"></span>🔒 Закрытый канал для подписчиков (PRIVATE_CHANNEL_ID)</div>
  <div class="hint" style="margin-top:10px">Токены хранятся только в .env на сервере и в админке не показываются. После правки .env перезапусти бота.</div>
</div>
</div>
{% endblock %}"""

app.jinja_loader = DictLoader(TEMPLATES)


# ═══════════════════════════ ЗАПУСК ═══════════════════════════

def _check_password() -> bool:
    if len(S.ADMIN_PASSWORD) < 8:
        log.warning("ADMIN_PASSWORD короче 8 символов, админка не запущена. Задай надёжный пароль в .env")
        return False
    return True


def start_in_thread(bot_username: str = "") -> None:
    """Запуск админки фоном вместе с ботом."""
    global BOT_USERNAME
    BOT_USERNAME = bot_username
    if not _check_password():
        return
    server = make_server(S.ADMIN_HOST, S.ADMIN_PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Админка: http://%s:%s", "localhost" if S.ADMIN_HOST in ("0.0.0.0", "127.0.0.1") else S.ADMIN_HOST,
             S.ADMIN_PORT)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    S.load_catalog()
    if TG:
        try:
            BOT_USERNAME = TG.get_me().username
        except Exception:  # noqa: BLE001
            pass
    if _check_password():
        log.info("Админка: http://localhost:%s", S.ADMIN_PORT)
        make_server(S.ADMIN_HOST, S.ADMIN_PORT, app, threaded=True).serve_forever()
