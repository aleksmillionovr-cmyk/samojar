# -*- coding: utf-8 -*-
"""
Telegram-бот магазина робуксов на pyTelegramBotAPI (telebot) + веб-админка.
Каталог, тексты и настройки — в catalog.json (правятся в админке), токены — в .env.
Оплата: ⭐ Telegram Stars, 💳 рубли (карта через платёжку Telegram), 💵 USDT (CryptoBot).
Запуск: python bot.py  (админка стартует вместе с ботом)
"""
import logging
import re
import threading
import time
from datetime import timedelta
from html import escape

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

import store as S
from store import (ADMIN_ID, CRYPTO_ASSET, CRYPTO_INVOICE_TTL, CURRENCY, PRIVATE_CHANNEL_ID,
                   PROVIDER_TOKEN, db_all, db_exec, fmt, get_item, get_order, item_title,
                   invoice_title, items, now_iso, price_for, set_status, settings, stars_for, texts)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("robux_bot")

if not S.BOT_TOKEN:
    raise SystemExit("❌ Укажи BOT_TOKEN в файле .env")

bot = telebot.TeleBot(S.BOT_TOKEN, parse_mode="HTML")
BOT_USERNAME = ""  # заполняется при запуске
NO_PREVIEW = types.LinkPreviewOptions(is_disabled=True)


def user_label(user) -> str:
    if user.username:
        return user.username
    return " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)


def create_order(user, kind, key, title, price, currency, amount) -> int:
    cur = db_exec(
        "INSERT INTO orders (user_id, username, kind, item_key, title, price, currency, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', ?)",
        (user.id, user_label(user), kind, key, title, price, currency, amount, now_iso()),
    )
    return cur.lastrowid


# ═══════════════════════════ CRYPTOBOT (USDT) ═══════════════════════════

_rate_cache = {"value": None, "ts": 0.0}


def crypto_call(method: str, **params):
    resp = requests.post(
        f"{S.CRYPTO_API}/{method}",
        json=params,
        headers={"Crypto-Pay-API-Token": S.CRYPTO_PAY_TOKEN},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"CryptoBot {method}: {data.get('error')}")
    return data["result"]


def usdt_rate():
    """Курс 1 USDT в валюте магазина (кэш 5 минут). None, если узнать не удалось."""
    if not S.CRYPTO_PAY_TOKEN:
        return None
    if _rate_cache["value"] and time.time() - _rate_cache["ts"] < 300:
        return _rate_cache["value"]
    try:
        for r in crypto_call("getExchangeRates"):
            if r.get("source") == CRYPTO_ASSET and r.get("target") == CURRENCY and r.get("is_valid", True):
                _rate_cache.update(value=float(r["rate"]), ts=time.time())
                return _rate_cache["value"]
    except Exception as e:  # noqa: BLE001
        log.warning("Курс USDT недоступен: %s", e)
    return None


def crypto_poller() -> None:
    """Каждые 20 секунд проверяет неоплаченные USDT-счета: покупателю не нужно жать «Проверить»."""
    while True:
        time.sleep(20)
        if not S.CRYPTO_PAY_TOKEN:
            continue
        try:
            rows = db_all(
                "SELECT id, crypto_invoice_id FROM orders WHERE currency = 'USDT' "
                "AND status IN ('created', 'cancelled') AND crypto_invoice_id IS NOT NULL "
                "AND created_at > ? ORDER BY id DESC LIMIT 100",
                (now_iso(timedelta(hours=-2)),),
            )
            if not rows:
                continue
            by_invoice = {str(r["crypto_invoice_id"]): r["id"] for r in rows}
            result = crypto_call("getInvoices", invoice_ids=",".join(by_invoice), count=100)
            for inv in result.get("items", []):
                order_id = by_invoice.get(str(inv.get("invoice_id")))
                if not order_id:
                    continue
                if inv.get("status") == "paid":
                    complete_order(order_id, f"cryptobot:{inv['invoice_id']}")
                elif inv.get("status") == "expired":
                    db_exec("UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'created'", (order_id,))
        except Exception as e:  # noqa: BLE001
            log.warning("Проверка USDT-счетов: %s", e)


# ═══════════════════════════ КЛАВИАТУРЫ ═══════════════════════════

BTN_SUBS = "💎 Подписки"
BTN_SERVICES = "🛍 Услуги"
BTN_MENU = "🏠 Меню"
BTN_SUPPORT = "🆘 Поддержка"


def ib(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text, callback_data=data)


def ikb(rows) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        kb.row(*row)
    return kb


def reply_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, input_field_placeholder="Выбери раздел 👇")
    kb.row(types.KeyboardButton(BTN_SUBS), types.KeyboardButton(BTN_SERVICES))
    kb.row(types.KeyboardButton(BTN_MENU), types.KeyboardButton(BTN_SUPPORT))
    return kb


def main_kb():
    return ikb([
        [ib("💎 Подписки", "subs"), ib("🛍 Услуги", "svcs")],
        [ib("🆘 Техподдержка", "support")],
    ])


def subs_kb():
    rows = [[ib(S.sub_button(item), f"sub:{item['key']}")] for item in items("sub")]
    rows.append([ib("⬅️ Назад", "main")])
    return ikb(rows)


def services_kb():
    lst = items("svc")
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
    return ikb([
        [types.InlineKeyboardButton("✉️ Написать в поддержку", url=f"https://t.me/{support}")],
        [ib("⬅️ Назад", "main")],
    ])


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
    bot.answer_callback_query(call.id, text, show_alert=True)


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


# ═══════════════════════════ ХЕНДЛЕРЫ ═══════════════════════════
# Порядок важен: telebot берёт первый подходящий хендлер.

# ── /start, в том числе переходы по кликабельным ссылкам из списка услуг ──

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


# ── админ-команды в Telegram (дублируют веб-админку) ──

@bot.message_handler(commands=["give"], func=lambda m: m.from_user.id == ADMIN_ID)
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
    send(order["user_id"], f"📬 <b>Заказ #{order['id']}</b>\n📦 {escape(order['title'])}\n\n"
                           f"<code>{escape(parts[1])}</code>")
    set_status(order["id"], "done")
    send(message.chat.id, f"✅ Отправлено покупателю, заказ #{order['id']} закрыт")


@bot.message_handler(commands=["orders"], func=lambda m: m.from_user.id == ADMIN_ID)
def admin_orders(message: types.Message) -> None:
    rows = db_all("SELECT * FROM orders WHERE status NOT IN ('created', 'cancelled') ORDER BY id DESC LIMIT 15")
    if not rows:
        send(message.chat.id, "Оплаченных заказов пока нет")
        return
    lines = ["🧾 <b>Последние заказы</b>\n"]
    for o in rows:
        nick = f" • <code>{escape(o['nick'])}</code>" if o["nick"] else ""
        lines.append(f"#{o['id']} {S.STATUS_NAMES.get(o['status'], o['status'])} • {escape(o['title'])}{nick}")
    send(message.chat.id, "\n".join(lines))


@bot.message_handler(content_types=["sticker"], func=lambda m: m.from_user.id == ADMIN_ID)
def admin_sticker_id(message: types.Message) -> None:
    """Админ кидает стикер → бот отвечает его file_id (вставить в админке → Настройки)."""
    send(message.chat.id, f"ID стикера:\n<code>{message.sticker.file_id}</code>")


# ── нижняя клавиатура ──

@bot.message_handler(func=lambda m: m.text == BTN_MENU)
def r_menu(message: types.Message) -> None:
    send(message.chat.id, S.welcome_text(message.from_user.first_name), main_kb())


@bot.message_handler(func=lambda m: m.text == BTN_SUBS)
def r_subs(message: types.Message) -> None:
    send(message.chat.id, S.subs_text(), subs_kb())


@bot.message_handler(func=lambda m: m.text == BTN_SERVICES)
def r_services(message: types.Message) -> None:
    send(message.chat.id, S.services_text(BOT_USERNAME), services_kb())


@bot.message_handler(func=lambda m: m.text == BTN_SUPPORT)
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
    st = settings()

    lines = ["🧾 <b>Оформление заказа</b>\n", f"📦 {item_title(kind, item)}"]
    if discount:
        lines.append(f"🏷 Скидка: −{discount}%")
    lines.append(f"💰 К оплате: <b>{fmt(final)}</b>\n")

    rows = []
    if st.get("enable_stars"):
        stars = stars_for(final)
        lines.append(f"⭐ Telegram Stars: <b>{stars} ⭐</b>")
        rows.append([ib(f"⭐ Telegram Stars • {stars} ⭐", f"stars:{kind}:{key}")])
    if st.get("enable_card"):
        lines.append(f"💳 Рубли (карта): <b>{fmt(final)}</b>")
        rows.append([ib(f"💳 Рубли (карта) • {fmt(final)}", f"card:{kind}:{key}")])
    if st.get("enable_crypto"):
        rate = usdt_rate()
        usdt = f"≈ {final / rate:.2f} {CRYPTO_ASSET}" if rate else f"по курсу {CRYPTO_ASSET}"
        lines.append(f"💵 {CRYPTO_ASSET} (крипта): <b>{usdt}</b>")
        rows.append([ib(f"💵 {CRYPTO_ASSET} (крипта) • {usdt}", f"crypto:{kind}:{key}")])

    if rows:
        lines.append("\nВыбери способ оплаты 👇")
    else:
        lines.append("⚠️ Оплата пока не подключена. Напиши в поддержку 🙏")
    rows.append([ib("⬅️ Назад", f"{kind}:{key}")])
    show(call, "\n".join(lines), ikb(rows))


# ── ⭐ Stars и 💳 рубли: встроенные счета Telegram ──

@bot.callback_query_handler(func=lambda c: c.data.startswith(("card:", "stars:")))
def cb_invoice(call: types.CallbackQuery) -> None:
    method, kind, key = parse_cb(call.data, 3)
    item = get_item(kind, key)
    if not item:
        alert(call, "Эта позиция больше недоступна 😔")
        return
    _, final, _ = price_for(kind, item, call.from_user.id)

    if method == "card":
        if not PROVIDER_TOKEN:
            alert(call, "💳 Оплата картой пока не подключена, выбери другой способ")
            log.warning("Нажата оплата картой, но PROVIDER_TOKEN пуст")
            return
        currency, amount, token = CURRENCY, final * 100, PROVIDER_TOKEN
        pay_text = f"💳 Оплатить {fmt(final)}"
    else:
        currency, amount, token = "XTR", stars_for(final), ""
        pay_text = f"⭐ Оплатить {amount} ⭐"

    order_id = create_order(call.from_user, kind, key, item_title(kind, item), final, currency, amount)
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


@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query: types.PreCheckoutQuery) -> None:
    order = None
    try:
        order = get_order(int(query.invoice_payload.rsplit(":", 1)[1]))
    except (ValueError, IndexError):
        pass
    if not order or order["status"] != "created" or order["user_id"] != query.from_user.id:
        bot.answer_pre_checkout_query(query.id, ok=False, error_message="Счёт устарел, оформи заказ заново 🙏")
        return
    bot.answer_pre_checkout_query(query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def on_telegram_paid(message: types.Message) -> None:
    sp = message.successful_payment
    order_id = int(sp.invoice_payload.rsplit(":", 1)[1])
    complete_order(order_id, sp.telegram_payment_charge_id)


# ── 💵 USDT через CryptoBot ──

@bot.callback_query_handler(func=lambda c: c.data.startswith("crypto:"))
def cb_crypto(call: types.CallbackQuery) -> None:
    _, kind, key = parse_cb(call.data, 3)
    item = get_item(kind, key)
    if not item:
        alert(call, "Эта позиция больше недоступна 😔")
        return
    if not S.CRYPTO_PAY_TOKEN:
        alert(call, f"💵 Оплата в {CRYPTO_ASSET} пока не подключена, выбери другой способ")
        log.warning("Нажата оплата USDT, но CRYPTO_PAY_TOKEN пуст")
        return
    _, final, _ = price_for(kind, item, call.from_user.id)
    order_id = create_order(call.from_user, kind, key, item_title(kind, item), final, "USDT", 0)

    try:
        params = dict(
            currency_type="fiat",
            fiat=CURRENCY,
            amount=str(final),
            accepted_assets=CRYPTO_ASSET,
            description=f"{settings().get('shop_name', '')}: {invoice_title(kind, item)} (заказ #{order_id})",
            payload=str(order_id),
            expires_in=CRYPTO_INVOICE_TTL,
        )
        if BOT_USERNAME:
            params.update(paid_btn_name="openBot", paid_btn_url=f"https://t.me/{BOT_USERNAME}")
        invoice = crypto_call("createInvoice", **params)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось создать USDT-счёт: %s", e)
        set_status(order_id, "cancelled")
        alert(call, "😔 Не получилось создать счёт, попробуй позже или выбери другой способ")
        return

    db_exec("UPDATE orders SET crypto_invoice_id = ? WHERE id = ?", (invoice["invoice_id"], order_id))
    pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")

    rate = usdt_rate()
    approx = f" (≈ {final / rate:.2f} {CRYPTO_ASSET})" if rate else ""
    text = (
        f"💵 <b>Оплата в {CRYPTO_ASSET}</b>\n\n"
        f"🧾 Заказ <b>#{order_id}</b>\n"
        f"📦 {item_title(kind, item)}\n"
        f"💰 Сумма: <b>{fmt(final)}</b>{approx}\n\n"
        f"1️⃣ Нажми «Оплатить», откроется @CryptoBot\n"
        f"2️⃣ Оплати счёт в {CRYPTO_ASSET}\n"
        f"3️⃣ Вернись сюда: бот сам увидит оплату в течение минуты "
        f"(или нажми «Проверить оплату»)\n\n"
        f"⏳ Счёт действует {CRYPTO_INVOICE_TTL // 60} минут"
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
        result = crypto_call("getInvoices", invoice_ids=str(order["crypto_invoice_id"]))
        status = (result.get("items") or [{}])[0].get("status")
    except Exception as e:  # noqa: BLE001
        log.warning("Проверка USDT-счёта #%s: %s", order_id, e)
        alert(call, "😔 Не удалось проверить, попробуй через минуту")
        return

    if status == "paid":
        bot.answer_callback_query(call.id, "✅ Оплата найдена!")
        complete_order(order_id, f"cryptobot:{order['crypto_invoice_id']}")
    elif status == "expired":
        set_status(order_id, "cancelled")
        alert(call, "⌛ Счёт истёк. Оформи заказ заново 🙏")
    else:
        alert(call, "⏳ Оплата пока не поступила. Если уже оплатил, подожди минуту и нажми ещё раз")


# ── отмена любого счёта ──

@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(call: types.CallbackQuery) -> None:
    order_id = int(parse_cb(call.data, 2)[1])
    order = get_order(order_id)
    if order and order["user_id"] == call.from_user.id and order["status"] == "created":
        set_status(order_id, "cancelled")
        if order["crypto_invoice_id"]:
            try:
                crypto_call("deleteInvoice", invoice_id=order["crypto_invoice_id"])
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
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id)
        return
    order_id = int(parse_cb(call.data, 2)[1])
    order = get_order(order_id)
    if not order:
        alert(call, "Заказ не найден")
        return
    set_status(order_id, "done")
    try:
        send(order["user_id"], S.done_message(order), main_kb())
    except ApiTelegramException as e:
        log.warning("Не удалось написать покупателю: %s", e)
    try:
        bot.edit_message_text(
            call.message.html_text + "\n✅ <b>Выдано</b>",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
        )
    except (ApiTelegramException, AttributeError):
        pass
    bot.answer_callback_query(call.id, "Покупатель уведомлён ✅")


# ── общий финал после любой оплаты ──

def complete_order(order_id: int, charge_id: str) -> None:
    if not S.mark_paid(order_id, charge_id):
        return  # уже обработан (например, проверка и авто-проверка сработали одновременно)
    order = get_order(order_id)
    user_id, kind = order["user_id"], order["kind"]
    item = get_item(kind, order["item_key"], only_active=False) or {}

    send(user_id, f"✅ <b>Оплата прошла!</b>\n\n🧾 Заказ <b>#{order_id}</b>\n📦 {escape(order['title'])}\n\n"
                  f"Спасибо за покупку 💎")

    # подписка → одноразовая ссылка в закрытый канал (если канал настроен)
    if kind == "sub" and PRIVATE_CHANNEL_ID:
        try:
            chat = int(PRIVATE_CHANNEL_ID) if PRIVATE_CHANNEL_ID.lstrip("-").isdigit() else PRIVATE_CHANNEL_ID
            link = bot.create_chat_invite_link(chat, name=f"Заказ {order_id}", member_limit=1)
            send(user_id, f"🔒 Твой вход в закрытый канал (ссылка одноразовая):\n{link.invite_link}")
        except ApiTelegramException as e:
            log.warning("Не удалось создать ссылку в канал: %s", e)

    if item.get("needs_nick"):
        send(user_id, item.get("nick_prompt") or texts()["nick_prompt"])  # статус 'paid' = ждём ник
    else:
        set_status(order_id, "waiting")
        send(user_id, "⏳ Заказ передан в работу, всё пришлём сюда в течение 15 минут.")

    notify_admin(order_id)


def notify_admin(order_id: int) -> None:
    if not ADMIN_ID:
        return
    o = get_order(order_id)
    if o["currency"] == "XTR":
        paid = f"{o['amount']} ⭐ Stars"
    elif o["currency"] == "USDT":
        paid = f"{fmt(o['price'])} в {CRYPTO_ASSET} (CryptoBot)"
    else:
        paid = f"{fmt(o['price'])} картой"
    buyer = f'<a href="tg://user?id={o["user_id"]}">{escape(o["username"] or str(o["user_id"]))}</a>'
    text = (
        f"🔔 <b>Заказ #{o['id']}</b>: {S.STATUS_NAMES.get(o['status'], o['status'])}\n\n"
        f"📦 {escape(o['title'])}\n"
        f"💰 {paid}\n"
        f"👤 {buyer}\n"
    )
    if o["nick"]:
        text += f"🎮 Ник: <code>{escape(o['nick'])}</code>\n"
    try:
        send(ADMIN_ID, text, ikb([[ib("✅ Выдано", f"done:{o['id']}")]]))
    except ApiTelegramException as e:
        log.warning("Не удалось уведомить админа: %s", e)


# ── ник после оплаты + всё остальное ──

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@bot.message_handler(content_types=["text"])
def text_input(message: types.Message) -> None:
    order = S.pending_nick_order(message.from_user.id)
    if not order or message.text.startswith("/"):
        send(message.chat.id, "🤔 Не понял. Выбери раздел в меню 👇", main_kb())
        return
    nick = message.text.strip().lstrip("@")
    if not NICK_RE.match(nick):
        send(message.chat.id, "😕 Ник в Roblox: 3–20 латинских букв, цифр или _. Попробуй ещё раз:")
        return
    S.set_nick(order["id"], nick)
    bot.send_message(
        message.chat.id,
        f"🙌 Принято! Ник: <code>{escape(nick)}</code>\n"
        f"⏳ Заказ <b>#{order['id']}</b> в очереди на выдачу, напишем сюда, как только всё будет готово.",
        reply_markup=reply_kb(),
    )
    notify_admin(order["id"])
    nxt = S.pending_nick_order(message.from_user.id)  # если оплачено несколько заказов подряд
    if nxt:
        send(message.chat.id, f"🧾 Есть ещё заказ <b>#{nxt['id']}</b> ({escape(nxt['title'])}).\n"
                              f"Пришли ник в Roblox для него:")


@bot.message_handler(content_types=[
    "photo", "video", "document", "sticker", "voice", "audio", "animation", "video_note", "location", "contact",
])
def fallback(message: types.Message) -> None:
    send(message.chat.id, "🤔 Не понял. Выбери раздел в меню 👇", main_kb())


# ═══════════════════════════ ЗАПУСК ═══════════════════════════

def main() -> None:
    global BOT_USERNAME
    S.load_catalog()  # создаст catalog.json при первом запуске
    BOT_USERNAME = bot.get_me().username
    bot.set_my_commands([types.BotCommand("start", "🏠 Главное меню")])
    threading.Thread(target=crypto_poller, daemon=True).start()

    if S.ADMIN_PASSWORD:
        import admin
        admin.start_in_thread(bot_username=BOT_USERNAME)
    else:
        log.warning("ADMIN_PASSWORD не задан в .env, веб-админка выключена")

    log.info("Бот @%s запущен. Оплата: %s", BOT_USERNAME, S.methods_line() or "не подключена")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
