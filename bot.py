# -*- coding: utf-8 -*-
"""
Telegram-бот магазина робуксов на pyTelegramBotAPI (telebot) + веб-админка.

Каталог, тексты и настройки — в data/catalog.json, токены и подключения — в data/config.json.
И то и другое правится в веб-админке.
Оплата: ⭐ Telegram Stars, 💳 карта / СБП (BoomKassa), 💳 карта через платёжку Telegram, 💵 USDT (CryptoBot).

Запуск: python bot.py  (админка стартует вместе с ботом; без токена работает только админка)
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from telebot.handler_backends import BaseMiddleware, CancelUpdate

import boomkassa as BK
import store as S
from store import fmt, get_item, get_order, item_title, invoice_title, price_for, settings, stars_for

S.setup_logging()
log = logging.getLogger("robux_bot")


class ErrorHandler(telebot.ExceptionHandler):
    """Ошибки в хендлерах не роняют бота: пишем в лог и показываем в админке."""

    def handle(self, exception) -> bool:
        log.error("Ошибка бота: %r", exception, exc_info=exception)
        try:
            S.kv_set("bot_error", {"text": repr(exception)[:500], "at": S.now_iso()})
        except Exception:  # noqa: BLE001
            pass
        return True


TOKEN = S.bot_token()
bot = telebot.TeleBot(TOKEN or "0:not-configured", parse_mode="HTML", use_class_middlewares=True,
                      num_threads=4, exception_handler=ErrorHandler())
BOT_USERNAME = ""  # заполняется при запуске
NO_PREVIEW = types.LinkPreviewOptions(is_disabled=True)


def user_label(user) -> str:
    if user.username:
        return user.username
    return " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)


# ═══════════════════════════ УВЕДОМЛЕНИЯ АДМИНАМ ═══════════════════════════

_alerts_sent: dict = {}


def alert_admins(text: str, kb=None, throttle_key: str = "", throttle_sec: int = 600) -> None:
    """Сообщение всем админам (ADMIN_ID в «Подключениях»). throttle_key — не чаще раза в throttle_sec."""
    if throttle_key:
        if time.time() - _alerts_sent.get(throttle_key, 0) < throttle_sec:
            return
        _alerts_sent[throttle_key] = time.time()
    for admin_id in S.admin_ids():
        try:
            bot.send_message(admin_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        except ApiTelegramException as e:
            log.warning("Не удалось написать админу %s: %s", admin_id, e)


# ═══════════════════════════ CRYPTOBOT (USDT) ═══════════════════════════

_rate_cache = {"value": None, "ts": 0.0}


def crypto_call(method: str, **params):
    resp = requests.post(
        f"{S.crypto_api()}/{method}",
        json=params,
        headers={"Crypto-Pay-API-Token": S.cfg("crypto_pay_token")},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"CryptoBot {method}: {data.get('error')}")
    return data["result"]


def usdt_rate():
    """Курс 1 USDT в валюте магазина (кэш 5 минут). None, если узнать не удалось."""
    if not S.cfg("crypto_pay_token"):
        return None
    if _rate_cache["value"] and time.time() - _rate_cache["ts"] < 300:
        return _rate_cache["value"]
    try:
        for r in crypto_call("getExchangeRates"):
            if r.get("source") == S.crypto_asset() and r.get("target") == S.currency() and r.get("is_valid", True):
                _rate_cache.update(value=float(r["rate"]), ts=time.time())
                return _rate_cache["value"]
    except Exception as e:  # noqa: BLE001
        log.warning("Курс %s недоступен: %s", S.crypto_asset(), e)
    return None


def check_crypto_invoices() -> None:
    """Проверяет неоплаченные USDT-счета: покупателю не нужно жать «Проверить»."""
    if not S.cfg("crypto_pay_token"):
        return
    rows = S.db_all(
        "SELECT id, ext_id FROM orders WHERE method = 'crypto' AND status IN ('created', 'cancelled') "
        "AND ext_id IS NOT NULL AND created_at > ? ORDER BY id DESC LIMIT 100",
        (S.now_iso(timedelta(hours=-2)),),
    )
    if not rows:
        return
    by_invoice = {str(r["ext_id"]): r["id"] for r in rows}
    result = crypto_call("getInvoices", invoice_ids=",".join(by_invoice), count=100)
    for inv in result.get("items", []):
        order_id = by_invoice.get(str(inv.get("invoice_id")))
        if not order_id:
            continue
        if inv.get("status") == "paid":
            complete_order(order_id, f"cryptobot:{inv['invoice_id']}")
        elif inv.get("status") == "expired":
            S.db_exec("UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'created'", (order_id,))


def expire_old_orders() -> None:
    hours = float(settings().get("order_ttl_hours") or 24)
    S.db_exec("UPDATE orders SET status = 'cancelled', updated_at = ? WHERE status = 'created' AND created_at < ?",
              (S.now_iso(), S.now_iso(timedelta(hours=-hours))))


def background_worker() -> None:
    """Фоновые задачи: проверка крипто-счетов, отмена старых неоплаченных заказов, «пульс» для админки."""
    n = 0
    while True:
        time.sleep(20)
        n += 1
        S.kv_set("bot_heartbeat", S.now_iso())
        try:
            check_crypto_invoices()
        except Exception as e:  # noqa: BLE001
            log.warning("Проверка USDT-счетов: %s", e)
        if n % 30 == 1:
            try:
                expire_old_orders()
            except Exception as e:  # noqa: BLE001
                log.warning("Отмена старых заказов: %s", e)


# ═══════════════════════════ КЛАВИАТУРЫ ═══════════════════════════

DEFAULT_BTNS = {
    "btn_subs": "💎 Подписки",
    "btn_services": "🛍 Услуги",
    "btn_orders": "🧾 Мои заказы",
    "btn_support": "🆘 Поддержка",
    "btn_menu": "🏠 Меню",
}


def btn(name: str) -> str:
    return settings().get(name) or DEFAULT_BTNS[name]


def is_btn(name: str):
    """Фильтр для нижней клавиатуры: подходит и текущая подпись кнопки, и стандартная (старые клавиатуры)."""
    return lambda m: bool(m.text) and m.text in (btn(name), DEFAULT_BTNS[name])


def ib(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text, callback_data=data)


def ikb(rows) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        kb.row(*row)
    return kb


def reply_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, input_field_placeholder="Выбери раздел 👇")
    kb.row(types.KeyboardButton(btn("btn_subs")), types.KeyboardButton(btn("btn_services")))
    kb.row(types.KeyboardButton(btn("btn_orders")), types.KeyboardButton(btn("btn_menu")),
           types.KeyboardButton(btn("btn_support")))
    return kb


def main_kb():
    return ikb([
        [ib(btn("btn_subs"), "subs"), ib(btn("btn_services"), "svcs")],
        [ib(btn("btn_orders"), "myorders"), ib(btn("btn_support"), "support")],
    ])


def subs_kb():
    rows = [[ib(S.sub_button(item), f"sub:{item['key']}")] for item in S.items("sub")]
    rows.append([ib("⬅️ Назад", "main")])
    return ikb(rows)


def services_kb():
    lst = S.items("svc")
    rows = [
        [ib(f"{item['emoji']} {item['name']}", f"svc:{item['key']}") for item in lst[i:i + 2]]
        for i in range(0, len(lst), 2)
    ]
    rows.append([ib("⬅️ Назад", "main")])
    return ikb(rows)


def card_kb(kind: str, key: str, final: int):
    back = "subs" if kind == "sub" else "svcs"
    return ikb([
        [ib(f"💳 Оплатить • {fmt(final)}", f"pay:{kind}:{key}")],
        [ib("⬅️ Назад", back), ib("🏠 Меню", "main")],
    ])


def support_kb():
    support = settings().get("support_username", "")
    rows = []
    if support:
        rows.append([types.InlineKeyboardButton("✉️ Написать в поддержку", url=f"https://t.me/{support}")])
    rows.append([ib("⬅️ Назад", "main")])
    return ikb(rows)


# ═══════════════════════════ ПОМОЩНИКИ ОТПРАВКИ ═══════════════════════════

def send(chat_id: int, text: str, kb=None):
    return bot.send_message(chat_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)


def show(call: types.CallbackQuery, text: str, kb) -> None:
    """Перерисовать текущее сообщение (чтобы чат не засорялся), либо прислать новое."""
    msg = call.message
    try:
        if msg is not None and getattr(msg, "text", None) is not None:
            bot.edit_message_text(
                text, chat_id=msg.chat.id, message_id=msg.message_id,
                reply_markup=kb, link_preview_options=NO_PREVIEW,
            )
        else:
            send(call.from_user.id, text, kb)
    except ApiTelegramException as e:
        if "not modified" not in str(e):
            send(call.from_user.id, text, kb)
    bot.answer_callback_query(call.id)


def alert(call: types.CallbackQuery, text: str) -> None:
    bot.answer_callback_query(call.id, text[:200], show_alert=True)


def send_welcome(message: types.Message) -> None:
    # 1-е сообщение ставит нижнюю клавиатуру (стикер, если задан, иначе анимированный 👋)
    sent = False
    sticker = settings().get("welcome_sticker")
    if sticker:
        try:
            bot.send_sticker(message.chat.id, sticker, reply_markup=reply_kb())
            sent = True
        except ApiTelegramException:
            log.warning("Стикер приветствия не подошёл, проверь file_id в админке")
    if not sent:
        bot.send_message(message.chat.id, "👋", reply_markup=reply_kb())
    send(message.chat.id, S.welcome_text(message.from_user.first_name), main_kb())


def send_card(chat_id: int, user_id: int, kind: str, item: dict) -> None:
    _, final, _ = price_for(kind, item, user_id)
    send(chat_id, S.card_text(kind, item, user_id), card_kb(kind, item["key"], final))


def parse_cb(data: str, parts: int):
    """'pay:sub:vip_month' → ['pay', 'sub', 'vip_month']"""
    return data.split(":", parts - 1)


def my_orders_text(user_id: int) -> str:
    rows = S.user_orders(user_id, 10)
    if not rows:
        return S.render("my_orders_empty")
    lines = ["🧾 <b>Мои заказы</b>\n"]
    for o in rows:
        when = (datetime.fromisoformat(o["paid_at"] or o["created_at"]) + timedelta(hours=S.TZ_OFFSET))
        lines.append(f"<b>#{o['id']}</b> · {escape(o['title'])}\n"
                     f"     {S.STATUS_NAMES.get(o['status'], o['status'])} · {fmt(o['price'])} · "
                     f"{when.strftime('%d.%m.%Y')}")
    lines.append("\nВопрос по заказу? Нажми «Поддержка» и укажи номер 🙏")
    return "\n".join(lines)


# ═══════════════════════════ ДОСТУП: БАН, ТЕХПЕРЕРЫВ, УЧЁТ ПОЛЬЗОВАТЕЛЕЙ ═══════════════════════════

class Gate(BaseMiddleware):
    def __init__(self):
        super().__init__()
        self.update_types = ["message", "callback_query"]

    def pre_process(self, obj, data):
        user = getattr(obj, "from_user", None)
        if user is None:
            return None
        if isinstance(obj, types.Message) and obj.chat.type != "private":
            return CancelUpdate()  # бот работает только в личке
        try:
            if S.touch_user(user) and settings().get("notify_new_users"):
                alert_admins(f"👋 Новый пользователь: <a href=\"tg://user?id={user.id}\">"
                             f"{escape(user_label(user))}</a> (<code>{user.id}</code>)")
        except Exception as e:  # noqa: BLE001
            log.warning("Учёт пользователя: %s", e)
        if S.is_admin(user.id):
            return None
        if isinstance(obj, types.Message) and obj.content_type == "successful_payment":
            return None  # оплату обрабатываем всегда
        if S.is_banned(user.id):
            self._reply(obj, S.render("banned"))
            return CancelUpdate()
        if settings().get("maintenance"):
            self._reply(obj, S.render("maintenance"))
            return CancelUpdate()
        return None

    def post_process(self, obj, data, exception):
        pass

    @staticmethod
    def _reply(obj, text: str) -> None:
        try:
            if isinstance(obj, types.CallbackQuery):
                bot.answer_callback_query(obj.id, S.strip_tags(text)[:200], show_alert=True)
            else:
                send(obj.chat.id, text)
        except ApiTelegramException:
            pass


bot.setup_middleware(Gate())


@bot.my_chat_member_handler()
def on_member_change(update: types.ChatMemberUpdated) -> None:
    """Пользователь заблокировал / разблокировал бота: учитываем в рассылках."""
    if update.chat.type != "private":
        return
    status = update.new_chat_member.status
    S.db_exec("UPDATE users SET bot_blocked = ? WHERE id = ?", (1 if status == "kicked" else 0, update.chat.id))


# ═══════════════════════════ ХЕНДЛЕРЫ ═══════════════════════════
# Порядок важен: telebot берёт первый подходящий хендлер.

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message) -> None:
    arg = telebot.util.extract_arguments(message.text) or ""
    kind, _, key = arg.partition("_")
    item = get_item(kind, key) if kind in ("svc", "sub") else None
    if item:
        try:
            bot.delete_message(message.chat.id, message.message_id)  # убираем «/start svc_...»
        except ApiTelegramException:
            pass
        send_card(message.chat.id, message.from_user.id, kind, item)
    else:
        send_welcome(message)


@bot.message_handler(commands=["orders"], func=lambda m: not S.is_admin(m.from_user.id))
def cmd_my_orders(message: types.Message) -> None:
    send(message.chat.id, my_orders_text(message.from_user.id), main_kb())


@bot.message_handler(commands=["help", "support"])
def cmd_help(message: types.Message) -> None:
    send(message.chat.id, S.support_text(), support_kb())


# ── админ-команды в Telegram (дублируют веб-админку) ──

@bot.message_handler(commands=["give"], func=lambda m: S.is_admin(m.from_user.id))
def admin_give(message: types.Message) -> None:
    """/give 15 ABCD-1234-EFGH — отправить покупателю код/текст и закрыть заказ."""
    parts = (telebot.util.extract_arguments(message.text) or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        send(message.chat.id, "Формат: <code>/give номер_заказа текст или код</code>")
        return
    order = get_order(int(parts[0]))
    if not order:
        send(message.chat.id, "Заказ не найден")
        return
    try:
        send(order["user_id"], f"📬 <b>Заказ #{order['id']}</b>\n📦 {escape(order['title'])}\n\n"
                               f"<code>{escape(parts[1])}</code>")
    except ApiTelegramException as e:
        send(message.chat.id, f"😔 Не удалось отправить покупателю: {escape(str(e))}")
        return
    S.set_status(order["id"], "done")
    send(message.chat.id, f"✅ Отправлено покупателю, заказ #{order['id']} закрыт")


@bot.message_handler(commands=["orders"], func=lambda m: S.is_admin(m.from_user.id))
def admin_orders(message: types.Message) -> None:
    rows = S.db_all("SELECT * FROM orders WHERE status IN ('paid', 'waiting') ORDER BY id LIMIT 20")
    if not rows:
        send(message.chat.id, "🎉 Всё выдано, заказов в очереди нет.\n\nСвои заказы как покупатель: /myorders")
        return
    lines = ["⏳ <b>Ждут выдачи</b>\n"]
    for o in rows:
        nick = f" • <code>{escape(o['nick'])}</code>" if o["nick"] else ""
        lines.append(f"#{o['id']} {S.STATUS_NAMES.get(o['status'], o['status'])} • {escape(o['title'])}{nick}")
    send(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["myorders"])
def cmd_myorders_alias(message: types.Message) -> None:
    send(message.chat.id, my_orders_text(message.from_user.id), main_kb())


@bot.message_handler(commands=["admin"], func=lambda m: S.is_admin(m.from_user.id))
def admin_link(message: types.Message) -> None:
    url = S.cfg("public_url")
    send(message.chat.id, f"🖥 Админка: {url}" if url else "Адрес админки не задан (PUBLIC_URL в «Подключениях»)")


@bot.message_handler(commands=["stats"], func=lambda m: S.is_admin(m.from_user.id))
def admin_stats(message: types.Message) -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    day = S.db_one("SELECT COUNT(*) n, COALESCE(SUM(price), 0) s FROM orders "
                   "WHERE status IN ('paid', 'waiting', 'done') AND paid_at >= ?", (since,))
    todo = S.db_one("SELECT COUNT(*) c FROM orders WHERE status IN ('paid', 'waiting')")["c"]
    users = S.db_one("SELECT COUNT(*) c FROM users")["c"]
    send(message.chat.id, f"📊 <b>За 24 часа</b>: {day['n']} заказ(ов) на {fmt(day['s'])}\n"
                          f"⏳ Ждут выдачи: {todo}\n👥 Пользователей: {users}")


@bot.message_handler(content_types=["sticker"], func=lambda m: S.is_admin(m.from_user.id))
def admin_sticker_id(message: types.Message) -> None:
    """Админ кидает стикер → бот отвечает его file_id (вставить в админке → Настройки)."""
    send(message.chat.id, f"ID стикера:\n<code>{message.sticker.file_id}</code>")


# ── нижняя клавиатура ──

@bot.message_handler(func=is_btn("btn_menu"))
def r_menu(message: types.Message) -> None:
    send(message.chat.id, S.welcome_text(message.from_user.first_name), main_kb())


@bot.message_handler(func=is_btn("btn_subs"))
def r_subs(message: types.Message) -> None:
    send(message.chat.id, S.subs_text(), subs_kb())


@bot.message_handler(func=is_btn("btn_services"))
def r_services(message: types.Message) -> None:
    send(message.chat.id, S.services_text(BOT_USERNAME), services_kb())


@bot.message_handler(func=is_btn("btn_orders"))
def r_orders(message: types.Message) -> None:
    send(message.chat.id, my_orders_text(message.from_user.id), main_kb())


@bot.message_handler(func=is_btn("btn_support"))
def r_support(message: types.Message) -> None:
    send(message.chat.id, S.support_text(), support_kb())


# ── инлайн-навигация ──

@bot.callback_query_handler(func=lambda c: c.data == "main")
def cb_main(call: types.CallbackQuery) -> None:
    show(call, S.welcome_text(call.from_user.first_name), main_kb())


@bot.callback_query_handler(func=lambda c: c.data == "subs")
def cb_subs(call: types.CallbackQuery) -> None:
    show(call, S.subs_text(), subs_kb())


@bot.callback_query_handler(func=lambda c: c.data == "svcs")
def cb_services(call: types.CallbackQuery) -> None:
    show(call, S.services_text(BOT_USERNAME), services_kb())


@bot.callback_query_handler(func=lambda c: c.data == "support")
def cb_support(call: types.CallbackQuery) -> None:
    show(call, S.support_text(), support_kb())


@bot.callback_query_handler(func=lambda c: c.data == "myorders")
def cb_my_orders(call: types.CallbackQuery) -> None:
    show(call, my_orders_text(call.from_user.id), ikb([[ib("⬅️ Назад", "main")]]))


@bot.callback_query_handler(func=lambda c: c.data.startswith(("sub:", "svc:")))
def cb_card(call: types.CallbackQuery) -> None:
    kind, key = parse_cb(call.data, 2)
    item = get_item(kind, key)
    if not item:
        alert(call, "Эта позиция больше недоступна 😔")
        return
    _, final, _ = price_for(kind, item, call.from_user.id)
    show(call, S.card_text(kind, item, call.from_user.id), card_kb(kind, key, final))


# ── выбор способа оплаты ──

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay:"))
def cb_checkout(call: types.CallbackQuery) -> None:
    _, kind, key = parse_cb(call.data, 3)
    item = get_item(kind, key)
    if not item:
        alert(call, "Эта позиция больше недоступна 😔")
        return
    _, final, discount = price_for(kind, item, call.from_user.id)

    lines = ["🧾 <b>Оформление заказа</b>\n", f"📦 {item_title(kind, item)}"]
    if discount:
        lines.append(f"🏷 Скидка: −{discount}%")
    lines.append(f"💰 К оплате: <b>{fmt(final)}</b>\n")

    rows = []
    for method in S.enabled_methods():
        label = S.method_label(method)
        if method == "stars":
            stars = stars_for(final)
            lines.append(f"{label}: <b>{stars} ⭐</b>")
            rows.append([ib(f"{label} • {stars} ⭐", f"stars:{kind}:{key}")])
        elif method == "boomkassa":
            tariff = BK.pick_tariff(item, final)
            if not tariff:
                log.info("BoomKassa: нет тарифа для «%s» на %s ₽ — способ скрыт", item["name"], final)
                continue
            amount = BK.amount_of(tariff)
            lines.append(f"{label}: <b>{fmt(amount)}</b>")
            rows.append([ib(f"{label} • {fmt(amount)}", f"bk:{kind}:{key}")])
        elif method == "card":
            lines.append(f"{label}: <b>{fmt(final)}</b>")
            rows.append([ib(f"{label} • {fmt(final)}", f"card:{kind}:{key}")])
        elif method == "crypto":
            rate = usdt_rate()
            asset = S.crypto_asset()
            usdt = f"≈ {final / rate:.2f} {asset}" if rate else f"по курсу {asset}"
            lines.append(f"{label}: <b>{usdt}</b>")
            rows.append([ib(f"{label} • {usdt}", f"crypto:{kind}:{key}")])

    if rows:
        lines.append("\nВыбери способ оплаты 👇")
    else:
        lines.append("⚠️ Оплата пока не подключена. Напиши в поддержку 🙏")
    rows.append([ib("⬅️ Назад", f"{kind}:{key}")])
    show(call, "\n".join(lines), ikb(rows))


def new_order(call, kind: str, key: str, item: dict, price: int, currency: str, amount: int, method: str) -> int:
    return S.create_order(call.from_user.id, user_label(call.from_user), kind, key, item_title(kind, item),
                          price, currency, amount, method)


# ── ⭐ Stars и 💳 карта через Telegram: встроенные счета ──

@bot.callback_query_handler(func=lambda c: c.data.startswith(("card:", "stars:")))
def cb_invoice(call: types.CallbackQuery) -> None:
    method, kind, key = parse_cb(call.data, 3)
    item = get_item(kind, key)
    if not item:
        alert(call, "Эта позиция больше недоступна 😔")
        return
    _, final, _ = price_for(kind, item, call.from_user.id)

    if method == "card":
        token = S.cfg("provider_token")
        if not token:
            alert(call, "💳 Оплата картой пока не подключена, выбери другой способ")
            log.warning("Нажата оплата картой, но PROVIDER_TOKEN пуст")
            return
        currency, amount = S.currency(), final * 100
        pay_text = f"💳 Оплатить {fmt(final)}"
    else:
        currency, amount, token = "XTR", stars_for(final), ""
        pay_text = f"⭐ Оплатить {amount} ⭐"

    order_id = new_order(call, kind, key, item, final, currency, amount, method)
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(pay_text, pay=True))
    kb.row(ib("❌ Отменить", f"cancel:{order_id}"))
    bot.send_invoice(
        chat_id=call.from_user.id,
        title=invoice_title(kind, item),
        description=f"{item['short']}\nЗаказ #{order_id}"[:255],
        invoice_payload=f"{kind}:{key}:{order_id}",
        provider_token=token,
        currency=currency,
        prices=[types.LabeledPrice(invoice_title(kind, item), amount)],
        reply_markup=kb,
    )
    bot.answer_callback_query(call.id)


def order_from_payload(payload: str):
    try:
        return get_order(int(payload.rsplit(":", 1)[1]))
    except (ValueError, IndexError, AttributeError):
        return None


@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query: types.PreCheckoutQuery) -> None:
    order = order_from_payload(query.invoice_payload)
    if (not order or order["status"] != "created" or order["user_id"] != query.from_user.id
            or order["amount"] != query.total_amount):
        bot.answer_pre_checkout_query(query.id, ok=False, error_message="Счёт устарел, оформи заказ заново 🙏")
        return
    bot.answer_pre_checkout_query(query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def on_telegram_paid(message: types.Message) -> None:
    sp = message.successful_payment
    order = order_from_payload(sp.invoice_payload)
    if not order:
        log.error("Оплата без заказа: %s", sp.invoice_payload)
        alert_admins(f"⚠️ Пришла оплата, но заказ не найден: <code>{escape(sp.invoice_payload)}</code>, "
                     f"покупатель <code>{message.from_user.id}</code>")
        return
    complete_order(order["id"], sp.telegram_payment_charge_id)


# ── 💳 карта / СБП через BoomKassa ──

@bot.callback_query_handler(func=lambda c: c.data.startswith("bk:"))
def cb_boomkassa(call: types.CallbackQuery) -> None:
    _, kind, key = parse_cb(call.data, 3)
    item = get_item(kind, key)
    if not item:
        alert(call, "Эта позиция больше недоступна 😔")
        return
    _, final, _ = price_for(kind, item, call.from_user.id)
    tariff = BK.pick_tariff(item, final) if BK.configured() else None
    if not tariff:
        alert(call, "💳 Оплата картой для этой позиции временно недоступна, выбери другой способ")
        return
    amount = BK.amount_of(tariff)
    order_id = new_order(call, kind, key, item, amount, "RUB", amount * 100, "boomkassa")
    try:
        link = BK.create_link(
            order_id, tariff,
            description=f"{settings().get('shop_name', '')}: {invoice_title(kind, item)} (заказ #{order_id})",
            customer=str(call.from_user.id),
            success_url=f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "",
        )
    except BK.BKError as e:
        log.warning("BoomKassa: не удалось создать ссылку для заказа #%s: %s", order_id, e.human())
        S.set_order_fields(order_id, status="cancelled", note=f"BoomKassa: {e.human()}"[:300])
        alert_admins(f"⚠️ <b>BoomKassa</b> не создала ссылку на оплату:\n{escape(e.human())}",
                     throttle_key=f"bk:{e.code or e.status}")
        alert(call, "😔 Не получилось создать счёт, попробуй позже или выбери другой способ")
        return

    pay_url = link.get("url")
    S.set_order_fields(order_id, ext_id=str(link.get("invoice_id") or ""), pay_url=pay_url)
    if not S.cfg("bk_webhook_secret"):
        log.warning("BoomKassa: секрет вебхука не задан — оплату заказа #%s придётся подтвердить вручную", order_id)

    text = (
        f"💳 <b>Оплата картой или через СБП</b>\n\n"
        f"🧾 Заказ <b>#{order_id}</b>\n"
        f"📦 {item_title(kind, item)}\n"
        f"💰 К оплате: <b>{fmt(amount)}</b>\n\n"
        f"1️⃣ Нажми «Оплатить», откроется защищённая страница оплаты\n"
        f"2️⃣ Оплати картой или через СБП\n"
        f"3️⃣ Возвращайся сюда: подтверждение придёт автоматически\n\n"
        f"⏳ После открытия страницы на оплату есть 20 минут"
    )
    kb = ikb([
        [types.InlineKeyboardButton(f"💳 Оплатить {fmt(amount)}", url=pay_url)],
        [ib("❌ Отменить", f"cancel:{order_id}")],
    ])
    send(call.from_user.id, text, kb)
    bot.answer_callback_query(call.id)


# ── 💵 USDT через CryptoBot ──

@bot.callback_query_handler(func=lambda c: c.data.startswith("crypto:"))
def cb_crypto(call: types.CallbackQuery) -> None:
    _, kind, key = parse_cb(call.data, 3)
    item = get_item(kind, key)
    asset = S.crypto_asset()
    if not item:
        alert(call, "Эта позиция больше недоступна 😔")
        return
    if not S.cfg("crypto_pay_token"):
        alert(call, f"💵 Оплата в {asset} пока не подключена, выбери другой способ")
        return
    _, final, _ = price_for(kind, item, call.from_user.id)
    order_id = new_order(call, kind, key, item, final, "USDT", 0, "crypto")

    try:
        params = dict(
            currency_type="fiat",
            fiat=S.currency(),
            amount=str(final),
            accepted_assets=asset,
            description=f"{settings().get('shop_name', '')}: {invoice_title(kind, item)} (заказ #{order_id})",
            payload=str(order_id),
            expires_in=S.CRYPTO_INVOICE_TTL,
        )
        if BOT_USERNAME:
            params.update(paid_btn_name="openBot", paid_btn_url=f"https://t.me/{BOT_USERNAME}")
        invoice = crypto_call("createInvoice", **params)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось создать USDT-счёт: %s", e)
        S.set_status(order_id, "cancelled")
        alert(call, "😔 Не получилось создать счёт, попробуй позже или выбери другой способ")
        return

    pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
    S.set_order_fields(order_id, ext_id=str(invoice["invoice_id"]), crypto_invoice_id=invoice["invoice_id"],
                       pay_url=pay_url)

    rate = usdt_rate()
    approx = f" (≈ {final / rate:.2f} {asset})" if rate else ""
    text = (
        f"💵 <b>Оплата в {asset}</b>\n\n"
        f"🧾 Заказ <b>#{order_id}</b>\n"
        f"📦 {item_title(kind, item)}\n"
        f"💰 Сумма: <b>{fmt(final)}</b>{approx}\n\n"
        f"1️⃣ Нажми «Оплатить», откроется @CryptoBot\n"
        f"2️⃣ Оплати счёт в {asset}\n"
        f"3️⃣ Вернись сюда: бот сам увидит оплату в течение минуты "
        f"(или нажми «Проверить оплату»)\n\n"
        f"⏳ Счёт действует {S.CRYPTO_INVOICE_TTL // 60} минут"
    )
    kb = ikb([
        [types.InlineKeyboardButton("💵 Оплатить в CryptoBot", url=pay_url)],
        [ib("🔄 Проверить оплату", f"ccheck:{order_id}")],
        [ib("❌ Отменить", f"cancel:{order_id}")],
    ])
    send(call.from_user.id, text, kb)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ccheck:"))
def cb_crypto_check(call: types.CallbackQuery) -> None:
    order_id = int(parse_cb(call.data, 2)[1])
    order = get_order(order_id)
    if not order or order["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id)
        return
    if order["status"] not in ("created", "cancelled"):
        alert(call, "✅ Этот заказ уже оплачен")
        return
    try:
        result = crypto_call("getInvoices", invoice_ids=str(order["ext_id"]))
        status = (result.get("items") or [{}])[0].get("status")
    except Exception as e:  # noqa: BLE001
        log.warning("Проверка USDT-счёта #%s: %s", order_id, e)
        alert(call, "😔 Не удалось проверить, попробуй через минуту")
        return

    if status == "paid":
        bot.answer_callback_query(call.id, "✅ Оплата найдена!")
        complete_order(order_id, f"cryptobot:{order['ext_id']}")
    elif status == "expired":
        S.set_status(order_id, "cancelled")
        alert(call, "⌛ Счёт истёк. Оформи заказ заново 🙏")
    else:
        alert(call, "⏳ Оплата пока не поступила. Если уже оплатил, подожди минуту и нажми ещё раз")


# ── отмена любого счёта ──

@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(call: types.CallbackQuery) -> None:
    order_id = int(parse_cb(call.data, 2)[1])
    order = get_order(order_id)
    if order and order["user_id"] == call.from_user.id and order["status"] == "created":
        S.set_status(order_id, "cancelled")
        if order["method"] == "crypto" and order["ext_id"]:
            try:
                crypto_call("deleteInvoice", invoice_id=int(order["ext_id"]))
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось удалить USDT-счёт: %s", e)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except (ApiTelegramException, AttributeError):
        pass
    bot.answer_callback_query(call.id, "Заказ отменён")


# ── админ: «✅ Выдано» в Telegram ──

@bot.callback_query_handler(func=lambda c: c.data.startswith("done:"))
def cb_done(call: types.CallbackQuery) -> None:
    if not S.is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    order_id = int(parse_cb(call.data, 2)[1])
    order = get_order(order_id)
    if not order:
        alert(call, "Заказ не найден")
        return
    if order["status"] == "done":
        alert(call, "Этот заказ уже отмечен выданным")
        return
    S.set_status(order_id, "done")
    try:
        send(order["user_id"], S.done_message(order), main_kb())
    except ApiTelegramException as e:
        log.warning("Не удалось написать покупателю: %s", e)
    try:
        bot.edit_message_text(
            call.message.html_text + f"\n✅ <b>Выдано</b> ({escape(user_label(call.from_user))})",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
        )
    except (ApiTelegramException, AttributeError):
        pass
    bot.answer_callback_query(call.id, "Покупатель уведомлён ✅")


# ── общий финал после любой оплаты ──

_complete_lock = threading.Lock()


def complete_order(order_id: int, charge_id: str) -> bool:
    """Оплата получена: уведомить покупателя, спросить ник, сообщить админам. False — если уже обработано."""
    with _complete_lock:
        if not S.mark_paid(order_id, charge_id):
            return False  # уже обработан (например, вебхук пришёл повторно)
    order = get_order(order_id)
    user_id, kind = order["user_id"], order["kind"]
    item = get_item(kind, order["item_key"], only_active=False) or {}
    values = S.order_values(order)

    try:
        send(user_id, S.render("paid", **values))

        # подписка → одноразовая ссылка в закрытый канал (если канал настроен)
        channel = S.cfg("private_channel_id").strip()
        if kind == "sub" and channel:
            try:
                chat = int(channel) if channel.lstrip("-").isdigit() else channel
                link = bot.create_chat_invite_link(chat, name=f"Заказ {order_id}", member_limit=1)
                send(user_id, f"🔒 Твой вход в закрытый канал (ссылка одноразовая):\n{link.invite_link}")
            except ApiTelegramException as e:
                log.warning("Не удалось создать ссылку в канал: %s", e)

        if item.get("needs_nick"):
            send(user_id, item.get("nick_prompt") or S.render("nick_prompt"))  # статус 'paid' = ждём ник
        else:
            S.set_status(order_id, "waiting")
            send(user_id, S.render("in_work", **values))
    except ApiTelegramException as e:
        log.warning("Покупатель %s не получил сообщение об оплате: %s", user_id, e)
        if not item.get("needs_nick"):
            S.set_status(order_id, "waiting")

    notify_admin(order_id)
    return True


def notify_admin(order_id: int) -> None:
    o = get_order(order_id)
    buyer = f'<a href="tg://user?id={o["user_id"]}">{escape(o["username"] or str(o["user_id"]))}</a>'
    text = (
        f"🔔 <b>Заказ #{o['id']}</b>: {S.STATUS_NAMES.get(o['status'], o['status'])}\n\n"
        f"📦 {escape(o['title'])}\n"
        f"💰 {S.order_paid_line(o)}\n"
        f"👤 {buyer} (<code>{o['user_id']}</code>)\n"
    )
    if o["nick"]:
        text += f"🎮 Ник: <code>{escape(o['nick'])}</code>\n"
    alert_admins(text, ikb([[ib("✅ Выдано", f"done:{o['id']}")]]))


# ── ник после оплаты + всё остальное ──

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@bot.message_handler(content_types=["text"])
def text_input(message: types.Message) -> None:
    order = S.pending_nick_order(message.from_user.id)
    if not order or message.text.startswith("/"):
        send(message.chat.id, S.render("unknown"), main_kb())
        return
    nick = message.text.strip().lstrip("@")
    if not NICK_RE.match(nick):
        send(message.chat.id, "😕 Ник в Roblox: 3–20 латинских букв, цифр или _. Попробуй ещё раз:")
        return
    S.set_nick(order["id"], nick)
    order = get_order(order["id"])
    bot.send_message(message.chat.id, S.render("nick_saved", **S.order_values(order)), reply_markup=reply_kb())
    notify_admin(order["id"])
    nxt = S.pending_nick_order(message.from_user.id)  # если оплачено несколько заказов подряд
    if nxt:
        send(message.chat.id, f"🧾 Есть ещё заказ <b>#{nxt['id']}</b> ({escape(nxt['title'])}).\n"
                              f"Пришли ник в Roblox для него:")


@bot.message_handler(content_types=[
    "photo", "video", "document", "sticker", "voice", "audio", "animation", "video_note", "location", "contact",
])
def fallback(message: types.Message) -> None:
    send(message.chat.id, S.render("unknown"), main_kb())


# ═══════════════════════════ ЗАПУСК ═══════════════════════════

def set_status(state: str, **extra) -> None:
    S.kv_set("bot_status", {"state": state, "username": BOT_USERNAME, "at": S.now_iso(), **extra})


def wait_forever() -> None:
    """Бот не запущен (нет токена или он неверный), но админка работает: ждём перезапуска из админки."""
    while True:
        time.sleep(3600)


def main() -> None:
    global BOT_USERNAME
    S.load_catalog()  # создаст catalog.json при первом запуске
    import admin

    if not TOKEN:
        set_status("no_token")
        admin.start_in_thread()
        log.warning("Токен бота не задан. Открой админку → «Подключения» и вставь токен от @BotFather")
        wait_forever()

    me = None
    for attempt in range(1, 1000000):
        try:
            me = bot.get_me()
            break
        except ApiTelegramException as e:
            if e.error_code in (401, 404):
                set_status("bad_token", error=str(e))
                admin.start_in_thread()
                log.error("Telegram не принял токен бота (%s). Исправь его в админке → «Подключения»", e)
                wait_forever()
            log.warning("Telegram ответил ошибкой (%s), пробую ещё раз", e)
        except requests.RequestException as e:
            log.warning("Нет связи с Telegram (%s), попытка %s", e, attempt)
            if attempt == 1:
                set_status("no_network", error=str(e))
                admin.start_in_thread()
        time.sleep(min(60, 5 * attempt))

    BOT_USERNAME = me.username
    admin.HOOKS.update(paid=complete_order, alert=alert_admins, bot_username=BOT_USERNAME)
    admin.start_in_thread()
    try:
        bot.set_my_commands([
            types.BotCommand("start", "🏠 Главное меню"),
            types.BotCommand("orders", "🧾 Мои заказы"),
            types.BotCommand("help", "🆘 Поддержка"),
        ])
    except ApiTelegramException as e:
        log.warning("Не удалось задать команды бота: %s", e)
    threading.Thread(target=background_worker, name="background", daemon=True).start()

    set_status("running", pid=__import__("os").getpid())
    S.kv_set("bot_heartbeat", S.now_iso())
    log.info("Бот @%s запущен. Оплата: %s. Админы: %s", BOT_USERNAME, S.methods_line() or "не подключена",
             S.admin_ids() or "не заданы")
    if not S.admin_ids():
        log.warning("ADMIN_ID не задан: уведомления о заказах никому не придут. Укажи его в админке → «Подключения»")
    # skip_pending=False: оплаты, пришедшие пока бот перезапускался, не теряются
    bot.infinity_polling(skip_pending=False, timeout=30, long_polling_timeout=25,
                         allowed_updates=["message", "callback_query", "pre_checkout_query", "my_chat_member"])


if __name__ == "__main__":
    main()
