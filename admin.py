# -*- coding: utf-8 -*-
"""
Веб-админка магазина (Flask).
Запускается вместе с ботом (python bot.py) или отдельно: python admin.py
Адрес: http(s)://<сервер>, пароль — ADMIN_PASSWORD из .env (потом можно сменить в админке → «Система»).

Всё, что меняется здесь, бот подхватывает сразу. Перезапуск нужен только после смены токена бота,
админка делает его сама.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape
from pathlib import Path

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

import boomkassa as BK
import store as S

log = logging.getLogger("robux_admin")
BASE = Path(__file__).resolve().parent
KINDS = {"sub": "Подписки", "svc": "Услуги"}
VERSION = "2.0"

# Бот при запуске передаёт сюда свои функции: завершение оплаты и уведомление админов.
HOOKS: dict = {"paid": None, "alert": None, "bot_username": ""}


def bot_username() -> str:
    return HOOKS.get("bot_username") or (S.kv_get("bot_status", {}) or {}).get("username") or ""


# ═══════════════════════════ TELEGRAM ИЗ АДМИНКИ ═══════════════════════════

_tg: dict = {"token": None, "bot": None}


def tg():
    """TeleBot для отправки сообщений из админки (свой экземпляр, polling не запускает)."""
    token = S.bot_token()
    if not token:
        return None
    if _tg["token"] != token:
        import telebot
        _tg.update(token=token, bot=telebot.TeleBot(token, parse_mode="HTML", threaded=False))
    return _tg["bot"]


def tg_send(chat_id: int, text: str, kb=None) -> tuple:
    """(успех, ошибка)."""
    bot = tg()
    if not bot:
        return False, "токен бота не задан"
    from telebot import types
    try:
        bot.send_message(chat_id, text, reply_markup=kb, link_preview_options=types.LinkPreviewOptions(is_disabled=True))
        return True, ""
    except Exception as e:  # noqa: BLE001
        log.warning("Админка: не удалось написать %s: %s", chat_id, e)
        if "403" in str(e):
            S.db_exec("UPDATE users SET bot_blocked = 1 WHERE id = ?", (chat_id,))
        return False, str(e)


def alert_admins(text: str) -> None:
    if HOOKS.get("alert"):
        HOOKS["alert"](text)
        return
    for admin_id in S.admin_ids():
        tg_send(admin_id, text)


def complete_paid(order_id: int, charge_id: str) -> bool:
    """Оплата подтверждена (вебхук или вручную): через бота, если он запущен, иначе только в базе."""
    if HOOKS.get("paid"):
        return HOOKS["paid"](order_id, charge_id)
    return S.mark_paid(order_id, charge_id)


# ═══════════════════════════ ПРИЛОЖЕНИЕ ═══════════════════════════

def _secret_key() -> str:
    path = S.DATA_DIR / ".admin_secret"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    return path.read_text(encoding="utf-8").strip()


app = Flask(__name__, template_folder=str(BASE / "templates"), static_folder=str(BASE / "static"))
app.secret_key = _secret_key()
app.config.update(
    SESSION_COOKIE_NAME="robux_admin",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=S.COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    TEMPLATES_AUTO_RELOAD=False,
)
if S.TRUST_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ═══════════════════════════ ЗАЩИТА ═══════════════════════════

_fails: dict = {}  # ip -> (кол-во ошибок, заблокирован до)
MAX_FAILS, LOCK_SECONDS = 5, 600
PUBLIC_PATHS = ("/login", "/healthz", "/webhook/", "/static/")


def password_stamp() -> str:
    """Меняется при смене пароля — старые сессии перестают работать."""
    h = S.cfg("admin_password_hash") or hashlib.sha256(S.ADMIN_PASSWORD.encode()).hexdigest()
    return hashlib.sha256(("stamp:" + h).encode()).hexdigest()[:16]


def check_password(password: str) -> bool:
    stored = S.cfg("admin_password_hash")
    if stored:
        return check_password_hash(stored, password)
    return bool(S.ADMIN_PASSWORD) and hmac.compare_digest(password.encode(), S.ADMIN_PASSWORD.encode())


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def _guard():
    path = request.path
    if path.startswith(PUBLIC_PATHS[1:]):
        return None
    if request.method == "POST":
        sent = request.form.get("csrf", "") or request.headers.get("X-CSRF", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Страница устарела, обнови её и попробуй ещё раз")
    if path == "/login":
        return None
    if not (session.get("ok") and session.get("pw") == password_stamp()):
        if request.method == "GET":
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        abort(401)
    return None


@app.after_request
def _headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
    if not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(404)
@app.errorhandler(500)
def _error(e):
    code = getattr(e, "code", 500)
    if request.path.startswith("/webhook/") or request.accept_mimetypes.best == "application/json":
        return jsonify(error=getattr(e, "description", str(e))), code
    titles = {400: "Запрос не принят", 401: "Нужно войти", 404: "Страница не найдена", 500: "Ошибка сервера"}
    return render_template("error.html", code=code, title=titles.get(code, "Ошибка"),
                           text=getattr(e, "description", "")), code


def check_kind(kind: str) -> None:
    if kind not in KINDS:
        abort(404)


# ═══════════════════════════ ШАБЛОНЫ: ФИЛЬТРЫ И КОНТЕКСТ ═══════════════════════════

def tg_html(text: str) -> Markup:
    """Текст сообщения Telegram → HTML для превью (переносы строк → <br>)."""
    return Markup((text or "").replace("\n", "<br>"))


def local_dt(iso: str, fmt: str = "%d.%m %H:%M") -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso) + timedelta(hours=S.TZ_OFFSET)
    except ValueError:
        return iso
    return dt.strftime(fmt)


def ago(iso: str) -> str:
    if not iso:
        return "никогда"
    try:
        sec = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return iso
    if sec < 60:
        return "только что"
    if sec < 3600:
        return f"{int(sec // 60)} мин назад"
    if sec < 86400:
        return f"{int(sec // 3600)} ч назад"
    return f"{int(sec // 86400)} дн назад"


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:6] + "…" + value[-4:] if len(value) > 14 else "•" * len(value)


app.jinja_env.filters.update(tg=tg_html, dt=local_dt, ago=ago, money=S.fmt, mask=mask)
app.jinja_env.globals.update(csrf_token=csrf_token, STATUS=S.STATUS_NAMES, METHODS=S.METHOD_NAMES,
                             order_paid=S.order_paid_line, VERSION=VERSION)


@app.context_processor
def _ctx():
    st = S.settings()
    data = {"shop_name": st.get("shop_name", "Магазин"), "currency_sign": st.get("currency_sign", "₽"),
            "maintenance": st.get("maintenance")}
    if session.get("ok"):
        data["todo_count"] = S.db_one("SELECT COUNT(*) c FROM orders WHERE status IN ('paid', 'waiting')")["c"]
    return data


def back_to(default: str):
    nxt = request.form.get("back") or request.args.get("back") or default
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else default)


# ═══════════════════════════ ВХОД ═══════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    count, until = _fails.get(ip, (0, 0))
    error = None
    if request.method == "POST":
        if until > time.time():
            error = f"Слишком много попыток. Подожди {int((until - time.time()) // 60) + 1} мин."
        elif check_password(request.form.get("password", "")):
            _fails.pop(ip, None)
            session.clear()
            session.permanent = True
            session.update(ok=True, pw=password_stamp())
            log.info("Вход в админку с %s", ip)
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        else:
            count += 1
            _fails[ip] = (count, time.time() + LOCK_SECONDS if count >= MAX_FAILS else 0)
            log.warning("Неверный пароль в админке с %s (%s)", ip, count)
            error = "Неверный пароль"
            time.sleep(0.6)
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/healthz")
def healthz():
    return "ok"


# ═══════════════════════════ СВОДКА ═══════════════════════════

def day_start(days_ago: int = 0) -> str:
    """Начало дня (по TZ_OFFSET) days_ago дней назад, в UTC ISO."""
    local_now = datetime.now(timezone.utc) + timedelta(hours=S.TZ_OFFSET)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return (start - timedelta(hours=S.TZ_OFFSET)).isoformat(timespec="seconds")


def revenue(since_iso: str) -> dict:
    row = S.db_one(
        "SELECT COUNT(*) n, COALESCE(SUM(price), 0) rub, "
        "COALESCE(SUM(CASE WHEN currency = 'XTR' THEN amount ELSE 0 END), 0) stars "
        "FROM orders WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ?",
        (since_iso,),
    )
    return dict(row)


def daily_chart(days: int = 14) -> list:
    rows = S.db_all("SELECT paid_at, price FROM orders WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ?",
                    (day_start(days - 1),))
    buckets: dict = {}
    for r in rows:
        d = (datetime.fromisoformat(r["paid_at"]) + timedelta(hours=S.TZ_OFFSET)).date()
        n, s = buckets.get(d, (0, 0))
        buckets[d] = (n + 1, s + r["price"])
    today = (datetime.now(timezone.utc) + timedelta(hours=S.TZ_OFFSET)).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        n, s = buckets.get(d, (0, 0))
        out.append({"label": d.strftime("%d.%m"), "wd": "пн вт ср чт пт сб вс".split()[d.weekday()], "n": n, "sum": s})
    peak = max((x["sum"] for x in out), default=0) or 1
    for x in out:
        x["h"] = round(x["sum"] / peak * 100)
    return out


def bot_state() -> dict:
    st = S.kv_get("bot_status", {}) or {}
    beat = S.kv_get("bot_heartbeat")
    alive = False
    if beat:
        try:
            alive = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() < 90
        except ValueError:
            pass
    state = st.get("state", "unknown")
    if state == "running" and not alive:
        state = "stale"
    labels = {
        "running": ("ok", "работает"),
        "stale": ("warn", "не отвечает"),
        "no_token": ("bad", "не задан токен"),
        "bad_token": ("bad", "Telegram не принял токен"),
        "no_network": ("warn", "нет связи с Telegram"),
        "unknown": ("warn", "запускается…"),
    }
    tone, text = labels.get(state, ("warn", state))
    return {"state": state, "tone": tone, "text": text, "username": st.get("username") or bot_username(),
            "since": st.get("at"), "error": S.kv_get("bot_error")}


def setup_checklist() -> list:
    st = S.settings()
    bk_on = st.get("enable_boomkassa")
    tariffs = BK.tariffs() if BK.configured() else []
    items = [
        ("Токен бота от @BotFather", bool(S.bot_token()) and bot_state()["state"] in ("running", "stale"),
         "/integrations#telegram"),
        ("Ваш Telegram ID для уведомлений о заказах", bool(S.admin_ids()), "/integrations#telegram"),
        ("Ник техподдержки", st.get("support_username") not in ("", "your_support"), "/settings#shop"),
    ]
    if bk_on:
        items += [
            ("BoomKassa: API-ключ", BK.configured(), "/integrations#boomkassa"),
            ("BoomKassa: тарифы для проекта", bool(tariffs), "/integrations#boomkassa"),
            ("BoomKassa: секрет вебхука (автоподтверждение оплат)", bool(S.cfg("bk_webhook_secret")),
             "/integrations#boomkassa"),
        ]
    return [{"text": t, "ok": ok, "url": u} for t, ok, u in items]


@app.get("/")
def dashboard():
    todo = S.db_all("SELECT * FROM orders WHERE status IN ('paid', 'waiting') ORDER BY id LIMIT 30")
    by_method = S.db_all(
        "SELECT method, COUNT(*) n, COALESCE(SUM(price), 0) rub FROM orders "
        "WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ? GROUP BY method ORDER BY rub DESC",
        (day_start(29),),
    )
    top = S.db_all(
        "SELECT title, COUNT(*) n, COALESCE(SUM(price), 0) rub FROM orders "
        "WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ? GROUP BY title ORDER BY rub DESC LIMIT 6",
        (day_start(29),),
    )
    people = S.db_one(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) today, "
        "SUM(CASE WHEN bot_blocked = 1 THEN 1 ELSE 0 END) blocked FROM users", (day_start(0),))
    buyers = S.db_one("SELECT COUNT(DISTINCT user_id) c FROM orders WHERE status IN ('paid', 'waiting', 'done')")["c"]
    checklist = setup_checklist()
    return render_template(
        "dashboard.html", active="dash", todo=todo,
        today=revenue(day_start(0)), week=revenue(day_start(6)), month=revenue(day_start(29)),
        total=revenue("2000-01-01"), by_method=by_method, top=top, buyers=buyers, people=people,
        chart=daily_chart(14), bot=bot_state(), checklist=checklist,
        checklist_left=sum(1 for c in checklist if not c["ok"]), methods=S.enabled_methods(),
    )


@app.post("/maintenance")
def toggle_maintenance():
    data = S.edit_catalog()
    data["settings"]["maintenance"] = not data["settings"].get("maintenance")
    S.save_catalog(data)
    flash("🛠 Технический перерыв включён: бот отвечает только админам" if data["settings"]["maintenance"]
          else "✅ Технический перерыв выключен, магазин снова работает")
    return back_to("/")


# ═══════════════════════════ ЗАКАЗЫ ═══════════════════════════

ORDER_FILTERS = {
    "todo": ("Ждут выдачи", "status IN ('paid', 'waiting')"),
    "done": ("Выполнены", "status = 'done'"),
    "paid": ("Все оплаченные", "status IN ('paid', 'waiting', 'done')"),
    "created": ("Не оплачены", "status = 'created'"),
    "cancelled": ("Отменены и возвраты", "status IN ('cancelled', 'refunded')"),
    "all": ("Все", "1 = 1"),
}
PER_PAGE = 40


def orders_query():
    f = request.args.get("f", "todo")
    if f not in ORDER_FILTERS:
        f = "todo"
    q = request.args.get("q", "").strip().lstrip("#@")
    method = request.args.get("m", "")
    where, params = [ORDER_FILTERS[f][1]], []
    if q:
        where.append("(CAST(id AS TEXT) = ? OR nick LIKE ? OR username LIKE ? OR CAST(user_id AS TEXT) = ? "
                     "OR title LIKE ? OR ext_id = ?)")
        params += [q, f"%{q}%", f"%{q}%", q, f"%{q}%", q]
    if method in S.METHOD_NAMES:
        where.append("method = ?")
        params.append(method)
    return f, q, method, " AND ".join(where), params


def page_args(total: int) -> tuple:
    pages = max(1, math.ceil(total / PER_PAGE))
    try:
        page = min(max(1, int(request.args.get("page", 1))), pages)
    except ValueError:
        page = 1
    return page, pages, (page - 1) * PER_PAGE


@app.get("/orders")
def orders():
    f, q, method, where, params = orders_query()
    total = S.db_one(f"SELECT COUNT(*) c FROM orders WHERE {where}", tuple(params))["c"]
    page, pages, offset = page_args(total)
    rows = S.db_all(f"SELECT * FROM orders WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*params, PER_PAGE, offset))
    counts = {k: S.db_one(f"SELECT COUNT(*) c FROM orders WHERE {v[1]}")["c"] for k, v in ORDER_FILTERS.items()}
    return render_template("orders.html", active="orders", rows=rows, f=f, q=q, m=method, filters=ORDER_FILTERS,
                           counts=counts, total=total, page=page, pages=pages)


@app.get("/orders.csv")
def orders_csv():
    _, _, _, where, params = orders_query()
    rows = S.db_all(f"SELECT * FROM orders WHERE {where} ORDER BY id DESC", tuple(params))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "Создан", "Оплачен", "Статус", "Позиция", "Цена", "Валюта", "Сумма в счёте", "Способ",
                "Покупатель", "Telegram ID", "Ник Roblox", "ID платежа", "Заметка"])
    for o in rows:
        w.writerow([o["id"], local_dt(o["created_at"], "%d.%m.%Y %H:%M"), local_dt(o["paid_at"], "%d.%m.%Y %H:%M"),
                    S.STATUS_NAMES.get(o["status"], o["status"]), o["title"], o["price"], o["currency"], o["amount"],
                    S.METHOD_NAMES.get(o["method"], o["method"]), o["username"], o["user_id"], o["nick"] or "",
                    o["charge_id"] or "", o["note"] or ""])
    name = f"orders_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def get_order_or_404(order_id: int):
    order = S.get_order(order_id)
    if not order:
        abort(404)
    return order


@app.get("/orders/<int:order_id>")
def order_page(order_id: int):
    o = get_order_or_404(order_id)
    user = S.get_user(o["user_id"])
    others = S.db_all("SELECT * FROM orders WHERE user_id = ? AND id != ? ORDER BY id DESC LIMIT 10",
                      (o["user_id"], order_id))
    item = S.get_item(o["kind"], o["item_key"], only_active=False)
    return render_template("order.html", active="orders", o=o, user=user, others=others, item=item)


@app.post("/orders/<int:order_id>/<action>")
def order_action(order_id: int, action: str):
    o = get_order_or_404(order_id)
    back = f"/orders/{order_id}"
    if action == "done":
        S.set_status(order_id, "done")
        ok, err = tg_send(o["user_id"], S.done_message(o))
        if ok:
            flash(f"✅ Заказ #{order_id} выполнен, покупатель получил уведомление")
        else:
            flash(f"Заказ #{order_id} отмечен выполненным, но написать покупателю не получилось: {err}", "error")
    elif action == "send":
        body = (request.form.get("text") or "").strip()
        if not body:
            flash("Пустое сообщение не отправлено", "error")
            return back_to(back)
        content = f"<code>{escape(body)}</code>" if request.form.get("as") == "code" else escape(body)
        ok, err = tg_send(o["user_id"], f"📬 <b>Заказ #{order_id}</b>\n📦 {escape(o['title'])}\n\n{content}")
        if ok:
            if request.form.get("close") == "on":
                S.set_status(order_id, "done")
            flash(f"📩 Отправлено покупателю по заказу #{order_id}")
        else:
            flash(f"Не получилось отправить: {err}", "error")
    elif action == "paid":
        if o["status"] not in ("created", "cancelled"):
            flash("Заказ уже оплачен", "error")
        elif complete_paid(order_id, f"manual:{datetime.now().strftime('%Y%m%d%H%M%S')}"):
            note = (o["note"] or "") + "\nОплата подтверждена вручную в админке"
            S.set_order_fields(order_id, note=note.strip())
            flash(f"💰 Заказ #{order_id} отмечен оплаченным. Покупатель получил уведомление, если бот запущен")
    elif action == "status":
        status = request.form.get("status", "")
        if status not in S.STATUS_NAMES:
            abort(400)
        if status in ("paid", "waiting", "done") and o["status"] in ("created", "cancelled") and not o["paid_at"]:
            S.db_exec("UPDATE orders SET paid_at = ? WHERE id = ?", (S.now_iso(), order_id))
        S.set_status(order_id, status)
        flash(f"Статус заказа #{order_id}: {S.STATUS_NAMES[status]}")
    elif action == "note":
        S.set_order_fields(order_id, note=(request.form.get("note") or "").strip()[:2000])
        flash("📝 Заметка сохранена")
    elif action == "nick":
        nick = (request.form.get("nick") or "").strip().lstrip("@")
        if nick and not re.fullmatch(r"[A-Za-z0-9_]{3,20}", nick):
            flash("Ник в Roblox: 3–20 латинских букв, цифр или _", "error")
        else:
            S.set_order_fields(order_id, nick=nick or None)
            if nick and o["status"] == "paid":
                S.set_status(order_id, "waiting")
            flash("🎮 Ник сохранён")
    elif action == "refund_stars":
        if o["currency"] != "XTR" or not o["charge_id"] or o["status"] == "refunded":
            flash("Вернуть звёзды можно только по оплаченному заказу в Stars", "error")
        else:
            bot = tg()
            try:
                bot.refund_star_payment(o["user_id"], o["charge_id"])
                S.set_status(order_id, "refunded")
                flash(f"↩️ {o['amount']} ⭐ возвращены покупателю, заказ #{order_id} помечен как возврат")
            except Exception as e:  # noqa: BLE001
                flash(f"Telegram не выполнил возврат: {e}", "error")
    else:
        abort(404)
    return back_to(back)


# ═══════════════════════════ ПОЛЬЗОВАТЕЛИ ═══════════════════════════

USER_FILTERS = {
    "all": ("Все", "1 = 1"),
    "buyers": ("Покупатели", "spent > 0"),
    "new": ("Новые за 7 дней", "u.created_at >= :week"),
    "banned": ("Заблокированы", "u.banned = 1"),
    "left": ("Остановили бота", "u.bot_blocked = 1"),
}
USERS_SQL = """
SELECT * FROM (
  SELECT u.*,
    (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id AND o.status IN ('paid','waiting','done')) AS paid_count,
    (SELECT COALESCE(SUM(price),0) FROM orders o WHERE o.user_id = u.id AND o.status IN ('paid','waiting','done'))
      AS spent
  FROM users u
) u WHERE {where}
"""


@app.get("/users")
def users():
    f = request.args.get("f", "all")
    if f not in USER_FILTERS:
        f = "all"
    q = request.args.get("q", "").strip().lstrip("@")
    where = USER_FILTERS[f][1]
    params: dict = {"week": day_start(6)}
    if q:
        where += " AND (CAST(u.id AS TEXT) = :q OR u.username LIKE :ql OR u.first_name LIKE :ql OR u.last_name LIKE :ql)"
        params.update(q=q, ql=f"%{q}%")
    sql = USERS_SQL.format(where=where)
    total = S.db_one(f"SELECT COUNT(*) c FROM ({sql})", params)["c"]
    page, pages, offset = page_args(total)
    rows = S.db_all(sql + " ORDER BY u.last_seen DESC LIMIT :lim OFFSET :off", {**params, "lim": PER_PAGE, "off": offset})
    return render_template("users.html", active="users", rows=rows, f=f, q=q, filters=USER_FILTERS, total=total,
                           page=page, pages=pages)


@app.get("/users/<int:user_id>")
def user_page(user_id: int):
    user = S.get_user(user_id)
    rows = S.db_all("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 100", (user_id,))
    if not user and not rows:
        abort(404)
    spent = sum(r["price"] for r in rows if r["status"] in S.PAID_STATUSES)
    return render_template("user.html", active="users", user=user, uid=user_id, rows=rows, spent=spent,
                           discount=S.user_discount(user_id), is_admin=S.is_admin(user_id))


@app.post("/users/<int:user_id>/<action>")
def user_action(user_id: int, action: str):
    if not S.get_user(user_id):
        S.db_exec("INSERT OR IGNORE INTO users (id, created_at, last_seen) VALUES (?, ?, ?)",
                  (user_id, S.now_iso(), S.now_iso()))
    back = f"/users/{user_id}"
    if action == "ban":
        user = S.get_user(user_id)
        S.set_user_flag(user_id, "banned", 0 if user["banned"] else 1)
        flash("⛔ Пользователь заблокирован в боте" if not user["banned"] else "✅ Пользователь разблокирован")
    elif action == "note":
        S.set_user_flag(user_id, "note", (request.form.get("note") or "").strip()[:2000])
        flash("📝 Заметка сохранена")
    elif action == "message":
        text = (request.form.get("text") or "").strip()
        errs = S.html_errors(text)
        if not text or errs:
            flash("Сообщение не отправлено: " + ("пустой текст" if not text else "; ".join(errs)), "error")
        else:
            ok, err = tg_send(user_id, text)
            flash("📩 Сообщение отправлено" if ok else f"Не получилось отправить: {err}", None if ok else "error")
    else:
        abort(404)
    return back_to(back)


# ═══════════════════════════ РАССЫЛКА ═══════════════════════════

AUDIENCES = {
    "all": ("Все пользователи", "SELECT id FROM users WHERE banned = 0 AND bot_blocked = 0"),
    "buyers": ("Только покупатели",
               "SELECT id FROM users WHERE banned = 0 AND bot_blocked = 0 AND id IN "
               "(SELECT user_id FROM orders WHERE status IN ('paid','waiting','done'))"),
    "nonbuyers": ("Кто ещё ничего не купил",
                  "SELECT id FROM users WHERE banned = 0 AND bot_blocked = 0 AND id NOT IN "
                  "(SELECT user_id FROM orders WHERE status IN ('paid','waiting','done'))"),
    "admins": ("Тест: только админам", None),
}
_bc_stop: set = set()


def audience_ids(aud: str) -> list:
    if aud == "admins":
        return S.admin_ids()
    return [r["id"] for r in S.db_all(AUDIENCES[aud][1])]


def run_broadcast(bid: int) -> None:
    from telebot import types
    from telebot.apihelper import ApiTelegramException

    row = S.db_one("SELECT * FROM broadcasts WHERE id = ?", (bid,))
    ids = audience_ids(row["audience"])
    S.db_exec("UPDATE broadcasts SET total = ? WHERE id = ?", (len(ids), bid))
    bot = tg()
    kb = None
    if row["button_text"] and row["button_url"]:
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton(row["button_text"], url=row["button_url"]))
    sent = failed = 0
    status = "done"
    for n, uid in enumerate(ids, 1):
        if bid in _bc_stop:
            status = "stopped"
            break
        for attempt in range(3):
            try:
                bot.send_message(uid, row["text"], reply_markup=kb,
                                 link_preview_options=types.LinkPreviewOptions(is_disabled=True))
                sent += 1
                break
            except ApiTelegramException as e:
                if e.error_code == 429 and attempt < 2:
                    time.sleep(int((e.result_json or {}).get("parameters", {}).get("retry_after", 5)) + 1)
                    continue
                if e.error_code == 403:
                    S.db_exec("UPDATE users SET bot_blocked = 1 WHERE id = ?", (uid,))
                failed += 1
                break
            except Exception as e:  # noqa: BLE001
                log.warning("Рассылка #%s: %s", bid, e)
                failed += 1
                time.sleep(2)
                break
        if n % 10 == 0:
            S.db_exec("UPDATE broadcasts SET sent = ?, failed = ? WHERE id = ?", (sent, failed, bid))
        time.sleep(0.05)  # ~20 сообщений в секунду, лимит Telegram — 30
    S.db_exec("UPDATE broadcasts SET sent = ?, failed = ?, status = ?, finished_at = ? WHERE id = ?",
              (sent, failed, status, S.now_iso(), bid))
    _bc_stop.discard(bid)
    log.info("Рассылка #%s: %s, отправлено %s, ошибок %s", bid, status, sent, failed)


@app.route("/broadcast", methods=["GET", "POST"])
def broadcast():
    form = {"text": "", "button_text": "", "button_url": "", "audience": "all"}
    errors = []
    if request.method == "POST":
        form = {k: (request.form.get(k) or "").replace("\r\n", "\n").strip() for k in form}
        if form["audience"] not in AUDIENCES:
            abort(400)
        if not form["text"]:
            errors.append("Напиши текст рассылки")
        errors += S.html_errors(form["text"])
        if len(form["text"]) > 4000:
            errors.append("Текст длиннее 4000 символов")
        if bool(form["button_text"]) != bool(form["button_url"]):
            errors.append("Для кнопки нужны и подпись, и ссылка")
        if form["button_url"] and not re.match(r"^(https?://|tg://)", form["button_url"]):
            errors.append("Ссылка кнопки должна начинаться с https:// или tg://")
        if not tg():
            errors.append("Бот не подключён: задай токен в «Подключениях»")
        if S.db_one("SELECT 1 FROM broadcasts WHERE status = 'running'"):
            errors.append("Уже идёт другая рассылка, дождись её окончания или останови")
        if not errors:
            cur = S.db_exec(
                "INSERT INTO broadcasts (text, button_text, button_url, audience, status, created_at) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (form["text"], form["button_text"] or None, form["button_url"] or None, form["audience"], S.now_iso()))
            threading.Thread(target=run_broadcast, args=(cur.lastrowid,), name="broadcast", daemon=True).start()
            flash(f"📣 Рассылка запущена: {AUDIENCES[form['audience']][0].lower()}")
            return redirect(url_for("broadcast"))
    counts = {k: len(audience_ids(k)) for k in AUDIENCES}
    history = S.db_all("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 20")
    return render_template("broadcast.html", active="broadcast", form=form, errors=errors, audiences=AUDIENCES,
                           counts=counts, history=history)


@app.get("/broadcast/<int:bid>.json")
def broadcast_status(bid: int):
    row = S.db_one("SELECT id, status, total, sent, failed FROM broadcasts WHERE id = ?", (bid,))
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.post("/broadcast/<int:bid>/stop")
def broadcast_stop(bid: int):
    _bc_stop.add(bid)
    flash("⏹ Рассылка останавливается")
    return redirect(url_for("broadcast"))


@app.post("/preview/message")
def preview_message():
    text = (request.form.get("value") or "").replace("\r\n", "\n").strip()
    buttons = [[request.form.get("button")]] if request.form.get("button") else []
    return jsonify(html=str(tg_html(text)), buttons=buttons, errors=S.html_errors(text))


# ═══════════════════════════ ПОДПИСКИ И УСЛУГИ ═══════════════════════════

NEW_DEFAULTS = {
    "sub": {"emoji": "⭐", "name": "", "period": "Навсегда", "price": "", "discount": 0, "days": None,
            "needs_nick": True, "active": True, "short": "", "about": "", "includes": [], "term": "", "how": "",
            "bk_tariff": ""},
    "svc": {"emoji": "🎁", "name": "", "amount": "", "price": "", "needs_nick": True, "nick_prompt": "",
            "active": True, "short": "", "text": "", "bk_tariff": ""},
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
    it["bk_tariff"] = g("bk_tariff")

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


def bk_status_for(item: dict) -> dict:
    """Как позиция будет оплачиваться через BoomKassa (для списка и карточки)."""
    if not S.settings().get("enable_boomkassa") or not BK.configured():
        return {"tone": "", "text": ""}
    t = BK.pick_tariff(item, int(item.get("price") or 0))
    if not t:
        if item.get("bk_tariff"):
            return {"tone": "bad", "text": f"тариф {item['bk_tariff']} не найден"}
        return {"tone": "warn", "text": "нет тарифа BoomKassa на эту сумму"}
    amount = BK.amount_of(t)
    if amount != int(item.get("price") or 0):
        return {"tone": "warn", "text": f"BoomKassa: {t['code']} · {S.fmt(amount)} (≠ цене)"}
    return {"tone": "ok", "text": f"BoomKassa: {t['code']}"}


@app.get("/items/<kind>")
def items_list(kind: str):
    check_kind(kind)
    screen = S.subs_text() if kind == "sub" else S.services_text(bot_username())
    lst = S.items(kind, only_active=False)
    return render_template("items.html", active=kind, kind=kind, title=KINDS[kind], lst=lst,
                           bk={it["key"]: bk_status_for(it) for it in lst},
                           screen=screen, buttons=list_buttons(kind))


@app.route("/items/<kind>/<key>", methods=["GET", "POST"])
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
                           item=base, is_new=is_new, key=key, errors=errors,
                           tariffs=BK.tariffs() if BK.configured() else [],
                           bk_on=S.settings().get("enable_boomkassa"))


@app.post("/items/<kind>/<key>/<action>")
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
        flash(f"👁 «{name}» снова в боте" if lst[idx]["active"] else f"🙈 «{name}» скрыто из бота")
    elif action == "copy":
        clone = json.loads(json.dumps(lst[idx]))
        existing = {x["key"] for x in lst}
        clone["key"] = next(k for k in iter(lambda: secrets.token_hex(4), None) if k not in existing)
        clone["name"] = (clone["name"] + " (копия)")[:40]
        clone["active"] = False
        lst.insert(idx + 1, clone)
        flash(f"📄 Создана копия «{name}» (скрыта из бота, пока не включишь)")
    elif action in ("up", "down"):
        j = idx - 1 if action == "up" else idx + 1
        if 0 <= j < len(lst):
            lst[idx], lst[j] = lst[j], lst[idx]
    else:
        abort(404)
    S.save_catalog(data)
    return redirect(url_for("items_list", kind=kind))


@app.post("/preview/item/<kind>")
def preview_item(kind: str):
    check_kind(kind)
    base = S.get_item(kind, request.form.get("key", ""), only_active=False) or dict(NEW_DEFAULTS[kind])
    item, errors = item_from_form(kind, request.form, base)
    return jsonify(html=str(tg_html(S.card_text(kind, item))), buttons=preview_buttons(kind, item), errors=errors,
                   bk=bk_status_for(item))


# ═══════════════════════════ ТЕКСТЫ ═══════════════════════════

TEXT_GROUPS = [
    ("Главные экраны", [
        ("welcome", "👋 Приветствие", "Показывается на /start и по кнопке «Меню». "
                                     "{name} — имя покупателя, {shop} — название магазина, {support} — ник поддержки"),
        ("subs_header", "💎 Шапка раздела «Подписки»", "Под ней бот сам добавит список тарифов с ценами и кнопки"),
        ("services_header", "🛍 Шапка раздела «Услуги»", "Под ней бот сам добавит кликабельный список услуг"),
        ("services_footer", "🛍 Подвал раздела «Услуги»", "{support} — ник поддержки"),
        ("support", "🆘 Техподдержка", "{support} — ник поддержки; кнопка «Написать в поддержку» добавится сама"),
    ]),
    ("Заказ и выдача", [
        ("paid", "✅ Оплата прошла", "{order} — номер заказа, {title} — что куплено, {shop} — магазин"),
        ("nick_prompt", "✍️ Просьба прислать ник после оплаты",
         "Для отдельной услуги можно задать свой текст в её карточке"),
        ("nick_saved", "🙌 Ник принят", "{nick} — ник, {order} — номер заказа"),
        ("in_work", "⏳ Заказ в работе (когда ник не нужен)", "{order} — номер заказа, {title} — что куплено"),
        ("done", "🎉 Заказ выполнен", "Уходит покупателю по кнопке «✅ Выдано». {order}, {title}, {shop}"),
        ("my_orders_empty", "🧾 «Мои заказы», когда заказов нет", ""),
    ]),
    ("Служебные", [
        ("maintenance", "🛠 Технический перерыв", "Включается в «Настройках» или кнопкой на сводке. {support}"),
        ("banned", "⛔ Пользователь заблокирован", "{support} — ник поддержки"),
        ("unknown", "🤔 Бот не понял сообщение", ""),
    ]),
]
TEXT_KEYS = [k for _, fields in TEXT_GROUPS for k, _, _ in fields]
SAMPLE_ORDER = {"order": 1234, "title": "⚡ Быстрые робуксы — 500 R$", "nick": "Builderman"}


def render_text_screen(field: str, tx: dict):
    """Текст и кнопки экрана, как их увидит покупатель."""
    if field == "welcome":
        return S.welcome_text("Гоша", tx), [["💎 Подписки", "🛍 Услуги"], ["🧾 Мои заказы", "🆘 Поддержка"]]
    if field == "subs_header":
        return S.subs_text(tx), list_buttons("sub")
    if field in ("services_header", "services_footer"):
        return S.services_text(bot_username(), tx), list_buttons("svc")
    if field == "support":
        return S.support_text(tx), [["✉️ Написать в поддержку"], ["⬅️ Назад"]]
    if field == "my_orders_empty":
        return S.render(field, tx), [["💎 Подписки", "🛍 Услуги"]]
    return S.render(field, tx, **SAMPLE_ORDER), []


@app.route("/texts", methods=["GET", "POST"])
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
    return render_template("texts.html", active="texts", groups=TEXT_GROUPS, tx=tx, errors=errors, screens=screens)


@app.post("/preview/text")
def preview_text():
    field = request.form.get("field", "")
    if field not in TEXT_KEYS:
        abort(404)
    tx = dict(S.texts())
    tx[field] = (request.form.get("value") or "").replace("\r\n", "\n").strip()
    html, buttons = render_text_screen(field, tx)
    return jsonify(html=str(tg_html(html)), buttons=buttons, errors=S.html_errors(tx[field]))


# ═══════════════════════════ НАСТРОЙКИ МАГАЗИНА ═══════════════════════════

BUTTON_FIELDS = [("btn_subs", "Подписки"), ("btn_services", "Услуги"), ("btn_orders", "Мои заказы"),
                 ("btn_support", "Поддержка"), ("btn_menu", "Меню")]


@app.route("/settings", methods=["GET", "POST"])
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
            "enable_boomkassa": f.get("enable_boomkassa") == "on",
            "enable_card": f.get("enable_card") == "on",
            "enable_crypto": f.get("enable_crypto") == "on",
            "bk_label": (f.get("bk_label") or "").strip() or "💳 Карта / СБП",
            "card_label": (f.get("card_label") or "").strip() or "💳 Картой в Telegram",
            "maintenance": f.get("maintenance") == "on",
            "notify_new_users": f.get("notify_new_users") == "on",
        }
        for key, label in BUTTON_FIELDS:
            new[key] = (f.get(key) or "").strip()
            if not new[key] or len(new[key]) > 30:
                errors.append(f"Кнопка «{label}»: от 1 до 30 символов")
        if len(set(new[k] for k, _ in BUTTON_FIELDS)) < len(BUTTON_FIELDS):
            errors.append("Подписи кнопок не должны повторяться")
        if not new["shop_name"] or len(new["shop_name"]) > 40 or re.search(r"[<>&]", new["shop_name"]):
            errors.append("Название магазина: от 1 до 40 символов, без &lt; &gt; &amp;")
        if not new["currency_sign"] or len(new["currency_sign"]) > 4:
            errors.append("Знак валюты: 1–4 символа")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", new["support_username"]):
            errors.append("Ник поддержки: 4–32 латинских буквы, цифры или _")
        for label_key in ("bk_label", "card_label"):
            if len(new[label_key]) > 30:
                errors.append("Подпись способа оплаты: до 30 символов")
        try:
            rate = float((f.get("rub_per_star") or "").replace(",", "."))
            if not 0.01 <= rate <= 1000:
                raise ValueError
            new["rub_per_star"] = rate
        except ValueError:
            errors.append("Курс звезды: число больше 0, например 1.8")
            new["rub_per_star"] = st.get("rub_per_star", 1.8)
        ttl = _int(f.get("order_ttl_hours"))
        if ttl is None or not 1 <= ttl <= 720:
            errors.append("Срок жизни неоплаченного заказа: от 1 до 720 часов")
            ttl = st.get("order_ttl_hours", 24)
        new["order_ttl_hours"] = ttl
        st.update(new)
        if not errors:
            S.save_catalog(data)
            flash("✅ Настройки сохранены, бот уже работает с ними")
            return redirect(url_for("settings_page"))
    conn = {"boomkassa": BK.configured(), "card": bool(S.cfg("provider_token")),
            "crypto": bool(S.cfg("crypto_pay_token"))}
    return render_template("settings.html", active="settings", st=st, errors=errors, conn=conn,
                           buttons=BUTTON_FIELDS)


# ═══════════════════════════ ПОДКЛЮЧЕНИЯ (ТОКЕНЫ) ═══════════════════════════

SECRET_FIELDS = ["bot_token", "provider_token", "crypto_pay_token", "bk_api_key", "bk_webhook_secret"]
PLAIN_FIELDS = ["admin_ids", "private_channel_id", "currency", "crypto_asset", "bk_base_url", "public_url"]


def check_bot_token(token: str) -> tuple:
    import requests
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    except Exception as e:  # noqa: BLE001
        return False, f"нет связи с Telegram: {e}"
    if r.get("ok"):
        return True, "@" + r["result"]["username"]
    return False, r.get("description") or "Telegram не принял токен"


@app.route("/integrations", methods=["GET", "POST"])
def integrations():
    errors = []
    if request.method == "POST":
        f = request.form
        updates = {}
        for key in SECRET_FIELDS:
            value = (f.get(key) or "").strip()
            if f.get(f"clear_{key}") == "on":
                updates[key] = ""
            elif value:
                updates[key] = value
        for key in PLAIN_FIELDS:
            updates[key] = (f.get(key) or "").strip()
        updates["crypto_testnet"] = "1" if f.get("crypto_testnet") == "on" else "0"
        updates["public_url"] = updates["public_url"].rstrip("/")
        updates["currency"] = (updates["currency"] or "RUB").upper()
        updates["crypto_asset"] = (updates["crypto_asset"] or "USDT").upper()
        updates["bk_base_url"] = (updates["bk_base_url"] or "https://boomkassa.ru").rstrip("/")

        token = updates.get("bot_token")
        if token and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,50}", token):
            errors.append("Токен бота выглядит неправильно. Он такой: 123456789:AAH…  (из @BotFather)")
        bad_ids = [p for p in re.split(r"[\s,;]+", updates["admin_ids"]) if p and not p.lstrip("-").isdigit()]
        if bad_ids:
            errors.append("Telegram ID — только цифры (узнать свой: @userinfobot). Несколько — через запятую")
        if updates["public_url"] and not updates["public_url"].startswith(("http://", "https://")):
            errors.append("Адрес админки должен начинаться с https://")
        if not re.fullmatch(r"[A-Z]{3}", updates["currency"]):
            errors.append("Валюта: три буквы, например RUB")
        if not errors:
            token_changed = token and token != S.bot_token()
            S.save_config(updates)
            if token_changed:
                ok, info = check_bot_token(token)
                flash(f"✅ Токен принят Telegram: {info}. Бот перезапускается, обнови страницу через 5 секунд"
                      if ok else f"⚠️ Токен сохранён, но Telegram ответил: {info}", None if ok else "error")
                restart_process()
            else:
                flash("✅ Подключения сохранены и уже действуют")
            return redirect(url_for("integrations"))
    current = {k: S.cfg(k) for k in SECRET_FIELDS + PLAIN_FIELDS + ["crypto_testnet"]}
    bk_info = BK.tariffs_info() if BK.configured() else None
    return render_template("integrations.html", active="integrations", c=current, errors=errors,
                           bk_info=bk_info, bk_error=BK.last_error(), webhook_url=BK.webhook_url(),
                           bot=bot_state())


@app.post("/integrations/test/<what>")
def integrations_test(what: str):
    if what == "telegram":
        token = S.bot_token()
        if not token:
            flash("Токен бота не задан", "error")
        else:
            ok, info = check_bot_token(token)
            flash(f"✅ Бот на связи: {info}" if ok else f"❌ {info}", None if ok else "error")
    elif what == "admins":
        ids = S.admin_ids()
        if not ids:
            flash("Telegram ID админов не заданы", "error")
        for admin_id in ids:
            ok, err = tg_send(admin_id, "✅ Проверка из админки: уведомления о заказах будут приходить сюда.")
            flash(f"✅ Сообщение для {admin_id} отправлено" if ok
                  else f"❌ {admin_id}: {err}. Напиши боту /start с этого аккаунта и попробуй снова", None if ok else "error")
    elif what == "boomkassa":
        try:
            info = BK.fetch_tariffs()
            n = len(info["tariffs"])
            if n:
                flash(f"✅ BoomKassa на связи, проект «{info['project'].get('name', '')}», тарифов: {n}")
            else:
                flash("⚠️ Ключ рабочий, но для проекта ещё нет тарифов. Их заводит поддержка BoomKassa "
                      f"{('(' + info['hint'] + ')') if info.get('hint') else ''}", "error")
        except BK.BKError as e:
            flash(f"❌ BoomKassa: {e.human()}", "error")
    elif what == "webhook":
        secret = S.cfg("bk_webhook_secret")
        if not secret:
            flash("Сначала укажи секрет вебхука BoomKassa", "error")
        else:
            body = json.dumps({"event": "test.ping", "order_id": "tg-0", "amount": 0}).encode()
            with app.test_client() as c:
                r = c.post("/webhook/boomkassa", data=body, content_type="application/json",
                           headers={"X-Payments-Boom-Signature": BK.sign(body),
                                    "X-Payments-Boom-Event-Id": "test-" + secrets.token_hex(6)})
            flash("✅ Вебхук принимает подписанные запросы" if r.status_code == 200
                  else f"❌ Вебхук ответил {r.status_code}", None if r.status_code == 200 else "error")
    elif what == "crypto":
        import requests
        try:
            r = requests.post(f"{S.crypto_api()}/getMe", timeout=15,
                              headers={"Crypto-Pay-API-Token": S.cfg("crypto_pay_token")}).json()
            flash(f"✅ CryptoBot на связи: {r['result'].get('name')}" if r.get("ok")
                  else f"❌ CryptoBot: {r.get('error')}", None if r.get("ok") else "error")
        except Exception as e:  # noqa: BLE001
            flash(f"❌ CryptoBot недоступен: {e}", "error")
    else:
        abort(404)
    return redirect(url_for("integrations") + f"#{'telegram' if what in ('telegram', 'admins') else what}")


# ═══════════════════════════ ВЕБХУК BOOMKASSA ═══════════════════════════

@app.post("/webhook/boomkassa")
def boomkassa_webhook():
    raw = request.get_data(cache=False)
    if not BK.verify_signature(raw, request.headers.get("X-Payments-Boom-Signature", "")):
        log.warning("BoomKassa: вебхук с неверной подписью от %s", request.remote_addr)
        return jsonify(error="bad signature"), 401
    try:
        ev = json.loads(raw)
    except ValueError:
        return jsonify(error="bad json"), 400
    event = ev.get("event", "")
    event_id = request.headers.get("X-Payments-Boom-Event-Id") or f"{event}:{ev.get('transaction_id')}"
    if S.event_seen(event_id):
        return jsonify(ok=True, duplicate=True)
    log.info("BoomKassa: %s по %s на %s", event, ev.get("order_id"), ev.get("amount"))

    order_id = BK.parse_order_id(ev.get("order_id"))
    order = S.get_order(order_id) if order_id else None
    if event == "test.ping":
        pass
    elif event == "payment.succeeded":
        if not order:
            alert_admins(f"⚠️ <b>BoomKassa</b>: оплата {escape(str(ev.get('amount')))} ₽ без заказа в боте "
                         f"(order_id <code>{escape(str(ev.get('order_id')))}</code>). Проверь в кабинете BoomKassa.")
        else:
            paid = float(ev.get("amount") or 0)
            if paid + 0.01 < order["price"]:
                S.set_order_fields(order_id, note=f"BoomKassa: оплачено {paid} вместо {order['price']}")
                alert_admins(f"⚠️ <b>BoomKassa</b>: по заказу #{order_id} пришло {paid} ₽ вместо {order['price']} ₽. "
                             f"Заказ не закрыт, проверь вручную.")
            else:
                complete_paid(order_id, f"boomkassa:{ev.get('transaction_id')}")
    elif event == "payment.refunded":
        if order:
            S.set_order_fields(order_id, status="refunded",
                               note=((order["note"] or "") + f"\nBoomKassa: возврат {ev.get('amount')} ₽").strip())
        alert_admins(f"↩️ <b>BoomKassa</b>: возврат {escape(str(ev.get('amount')))} ₽ по заказу "
                     f"#{order_id if order else escape(str(ev.get('order_id')))}")
    else:
        log.info("BoomKassa: неизвестное событие %s", event)
    S.remember_event("boomkassa", event_id, raw.decode("utf-8", "replace"))
    return jsonify(ok=True)


# ═══════════════════════════ СИСТЕМА ═══════════════════════════

def restart_process(delay: float = 1.5) -> None:
    """Перезапуск всего процесса (бот + админка) — подхватывает новый токен бота."""
    def _do():
        log.info("Перезапуск по команде из админки")
        logging.shutdown()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Timer(delay, _do).start()


def tail_log(lines: int = 400, level: str = "") -> list:
    if not S.LOG_PATH.exists():
        return []
    with open(S.LOG_PATH, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 300_000))
        data = f.read().decode("utf-8", "replace").splitlines()
    if level:
        data = [x for x in data if f" {level} " in x]
    return data[-lines:]


@app.get("/system")
def system():
    level = request.args.get("level", "")
    if level not in ("", "WARNING", "ERROR"):
        level = ""
    db_size = sum(p.stat().st_size for p in S.DATA_DIR.glob("orders.db*"))
    info = {
        "python": sys.version.split()[0], "data_dir": str(S.DATA_DIR), "db_size": db_size,
        "orders": S.db_one("SELECT COUNT(*) c FROM orders")["c"], "users": S.db_one("SELECT COUNT(*) c FROM users")["c"],
        "has_backup": S.BACKUP_PATH.exists(),
        "backup_time": datetime.fromtimestamp(S.BACKUP_PATH.stat().st_mtime).strftime("%d.%m %H:%M")
        if S.BACKUP_PATH.exists() else "", "pw_custom": bool(S.cfg("admin_password_hash")),
    }
    return render_template("system.html", active="system", info=info, logs=tail_log(level=level), level=level,
                           bot=bot_state())


@app.post("/system/password")
def change_password():
    cur, new, rep = (request.form.get(k, "") for k in ("current", "new", "repeat"))
    if not check_password(cur):
        flash("Текущий пароль указан неверно", "error")
    elif len(new) < 10:
        flash("Новый пароль: минимум 10 символов", "error")
    elif new != rep:
        flash("Пароли не совпадают", "error")
    else:
        S.save_config({"admin_password_hash": generate_password_hash(new)})
        session["pw"] = password_stamp()
        log.info("Пароль админки изменён")
        flash("🔐 Пароль изменён. На других устройствах нужно будет войти заново")
    return redirect(url_for("system"))


@app.post("/system/restart")
def system_restart():
    restart_process()
    flash("🔄 Бот перезапускается, это займёт несколько секунд")
    return redirect(url_for("system"))


@app.post("/system/restore-catalog")
def restore_catalog():
    if not S.BACKUP_PATH.exists():
        flash("Резервной копии каталога нет", "error")
    else:
        try:
            data = json.loads(S.BACKUP_PATH.read_text(encoding="utf-8"))
        except ValueError:
            flash("Резервная копия повреждена", "error")
            return redirect(url_for("system"))
        S.save_catalog(data)  # текущая версия уходит в backup — можно вернуть обратно той же кнопкой
        flash("↩️ Каталог возвращён к версии до последнего сохранения")
    return redirect(url_for("system"))


@app.get("/system/backup.zip")
def backup_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        tmp = S.DATA_DIR / ".backup.db"
        with S.DB_LOCK:
            dst = sqlite3.connect(tmp)
            S.DB.backup(dst)
            dst.close()
        z.write(tmp, "orders.db")
        tmp.unlink(missing_ok=True)
        for name in ("catalog.json", "catalog.backup.json", "boomkassa_tariffs.json"):
            p = S.DATA_DIR / name
            if p.exists():
                z.write(p, name)
        if request.args.get("secrets") == "1" and S.CONFIG_PATH.exists():
            z.write(S.CONFIG_PATH, "config.json")
    buf.seek(0)
    name = f"robux_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=name)


@app.get("/system/log.txt")
def download_log():
    return Response("\n".join(tail_log(5000)), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=bot.log"})


# ═══════════════════════════ ЗАПУСК ═══════════════════════════

_started = False


def _password_ready() -> bool:
    if S.cfg("admin_password_hash") or len(S.ADMIN_PASSWORD) >= 8:
        return True
    log.warning("ADMIN_PASSWORD короче 8 символов, админка не запущена. Задай надёжный пароль в .env")
    return False


def _make_server():
    try:
        from waitress import create_server
        srv = create_server(app, host=S.ADMIN_HOST, port=S.ADMIN_PORT, threads=8, ident="robux-admin")
        return srv.run
    except ImportError:
        from werkzeug.serving import make_server
        return make_server(S.ADMIN_HOST, S.ADMIN_PORT, app, threaded=True).serve_forever


def start_in_thread() -> None:
    """Запуск админки фоном вместе с ботом (повторный вызов ничего не делает)."""
    global _started
    if _started or not _password_ready():
        return
    _started = True
    S.db_exec("UPDATE broadcasts SET status = 'stopped', finished_at = ? WHERE status = 'running'", (S.now_iso(),))
    try:
        serve = _make_server()
    except OSError as e:
        log.error("Админка не запустилась: порт %s занят или недоступен (%s)", S.ADMIN_PORT, e)
        return
    threading.Thread(target=serve, name="admin", daemon=True).start()
    url = S.cfg("public_url") or f"http://{'localhost' if S.ADMIN_HOST in ('0.0.0.0', '127.0.0.1') else S.ADMIN_HOST}:{S.ADMIN_PORT}"
    log.info("Админка запущена: %s", url)


if __name__ == "__main__":
    S.setup_logging()
    S.load_catalog()
    if _password_ready():
        S.db_exec("UPDATE broadcasts SET status = 'stopped' WHERE status = 'running'")
        log.info("Админка: http://localhost:%s", S.ADMIN_PORT)
        _make_server()()
