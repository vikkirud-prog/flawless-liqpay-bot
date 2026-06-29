import os

import json

import time

import base64

import hashlib

import hmac

import html

import re

import secrets

import uuid

import requests

import telebot

import psycopg

from decimal import Decimal, InvalidOperation

from flask import Flask, request, redirect

BOT_TOKEN = os.getenv("BOT_TOKEN")

LIQPAY_PUBLIC_KEY = os.getenv("LIQPAY_PUBLIC_KEY")

LIQPAY_PRIVATE_KEY = os.getenv("LIQPAY_PRIVATE_KEY")

CURRENCY = os.getenv("CURRENCY", "UAH")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL")

CHECKBOX_LICENSE_KEY = os.getenv("CHECKBOX_LICENSE_KEY", "")

CHECKBOX_PIN_CODE = os.getenv("CHECKBOX_PIN_CODE", "")

CHECKBOX_TAX_CODE = int(os.getenv("CHECKBOX_TAX_CODE", "8"))

CHECKBOX_API_URL = "https://api.checkbox.ua/api/v1"

ALLOWED_USER_IDS = {
    int(user_id.strip())
    for user_id in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if user_id.strip().isdigit()
}

PORT = int(os.getenv("PORT", "10000"))

PAY_DOMAIN = "https://pay.flawless-design.com.ua"

if not BOT_TOKEN:

    raise RuntimeError("BOT_TOKEN is missing")

if not LIQPAY_PUBLIC_KEY or not LIQPAY_PRIVATE_KEY:

    raise RuntimeError("LIQPAY_PUBLIC_KEY or LIQPAY_PRIVATE_KEY is missing")

if not WEBHOOK_URL:

    raise RuntimeError("WEBHOOK_URL is missing")

if not DATABASE_URL:

    raise RuntimeError("DATABASE_URL is missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

app = Flask(__name__)

user_steps = {}

def get_db():

    return psycopg.connect(DATABASE_URL)

def init_db():

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    order_id TEXT PRIMARY KEY,
                    invoice_id TEXT,
                    phone TEXT NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL,
                    currency TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unpaid',
                    href TEXT,
                    short_code TEXT UNIQUE,
                    created_by BIGINT NOT NULL,
                    created_by_name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS invoices_created_at_idx
                ON invoices (created_at DESC)
                """
            )

            cursor.execute(
                """
                ALTER TABLE invoices
                    ADD COLUMN IF NOT EXISTS items JSONB
                        NOT NULL DEFAULT '[]'::jsonb,
                    ADD COLUMN IF NOT EXISTS checkbox_receipt_id UUID,
                    ADD COLUMN IF NOT EXISTS checkbox_status TEXT,
                    ADD COLUMN IF NOT EXISTS checkbox_error TEXT,
                    ADD COLUMN IF NOT EXISTS fiscalized_at TIMESTAMPTZ
                """
            )

def is_allowed(user_id: int) -> bool:

    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS

def require_access(message) -> bool:

    if is_allowed(message.from_user.id):

        return True

    bot.send_message(
        message.chat.id,
        "У вас нет доступа к этому боту.\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
    )

    return False

def clean_phone(phone: str) -> str:

    phone = re.sub(r"\D", "", phone)

    if phone.startswith("0") and len(phone) == 10:

        return "38" + phone

    if phone.startswith("380") and len(phone) == 12:

        return phone

    return phone

def display_phone(phone: str) -> str:

    digits = re.sub(r"\D", "", phone)

    if digits.startswith("380") and len(digits) == 12:

        return digits[2:]

    if digits.startswith("00") and len(digits) == 11:

        return digits[1:]

    return digits

def format_product_names(items, description: str) -> str:

    if isinstance(items, str):

        try:

            items = json.loads(items)

        except Exception:

            items = []

    if isinstance(items, list):

        names = [
            str(item.get("name", "")).strip()
            for item in items
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]

        if names:

            return ", ".join(names)

    return description

def extract_phone(text: str):

    phone_pattern = re.compile(
        r"(?<!\d)(?:\+?38[\s().-]*)?0(?:[\s().-]*\d){9}(?!\d)"
    )

    phones = []

    for match in phone_pattern.finditer(text):

        phone = clean_phone(match.group())

        if phone not in phones:

            phones.append(phone)

    if len(phones) == 1:

        return phones[0]

    return None

def make_signature(data_b64: str) -> str:

    raw = LIQPAY_PRIVATE_KEY + data_b64 + LIQPAY_PRIVATE_KEY

    return base64.b64encode(hashlib.sha1(raw.encode("utf-8")).digest()).decode("utf-8")

def liqpay_request(params: dict) -> dict:

    json_string = json.dumps(params, ensure_ascii=False, separators=(",", ":"))

    data_b64 = base64.b64encode(json_string.encode("utf-8")).decode("utf-8")

    signature = make_signature(data_b64)

    response = requests.post(

        "https://www.liqpay.ua/api/request",

        data={"data": data_b64, "signature": signature},

        timeout=30,

    )

    try:

        result = response.json()

    except Exception:

        response.raise_for_status()

        raise RuntimeError(response.text[:500])

    if response.status_code >= 400:

        raise RuntimeError(json.dumps(result, ensure_ascii=False))

    return result

def create_invoice(phone: str, amount: str, description: str) -> tuple[str, dict]:

    order_id = f"flawless_{int(time.time())}"

    params = {

        "version": 3,

        "public_key": LIQPAY_PUBLIC_KEY,

        "action": "invoice_send",

        "amount": amount,

        "currency": CURRENCY,

        "description": description,

        "order_id": order_id,

        "phone": phone,

        "language": "uk",

        "server_url": f"{WEBHOOK_URL}/liqpay/callback",

    }

    return order_id, liqpay_request(params)

def make_short_code() -> str:

    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]

def make_short_link(code: str) -> str:

    return f"{PAY_DOMAIN}/{code}"

def save_invoice(
    order_id: str,
    invoice_id,
    phone: str,
    amount: str,
    description: str,
    href,
    short_code,
    items: list,
    created_by: int,
    created_by_name: str,
):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                INSERT INTO invoices (
                    order_id, invoice_id, phone, amount, currency,
                    description, status, href, short_code,
                    items, created_by, created_by_name
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'unpaid',
                    %s, %s, %s::jsonb, %s, %s
                )
                """,
                (
                    order_id,
                    str(invoice_id) if invoice_id else None,
                    phone,
                    amount,
                    CURRENCY,
                    description,
                    str(href) if href else None,
                    short_code,
                    json.dumps(items, ensure_ascii=False),
                    created_by,
                    created_by_name,
                ),
            )

def checkbox_headers(token=None):

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Client-Name": "Flawless LiqPay Bot",
        "X-Client-Version": "1.0",
        "X-License-Key": CHECKBOX_LICENSE_KEY,
    }

    if token:

        headers["Authorization"] = f"Bearer {token}"

    return headers

def checkbox_signin() -> str:

    if not CHECKBOX_LICENSE_KEY or not CHECKBOX_PIN_CODE:

        raise RuntimeError("Checkbox integration is not configured")

    response = requests.post(
        f"{CHECKBOX_API_URL}/cashier/signinPinCode",
        headers=checkbox_headers(),
        json={"pin_code": CHECKBOX_PIN_CODE},
        timeout=20,
    )

    result = response.json()

    if response.status_code >= 400 or not result.get("access_token"):

        raise RuntimeError(
            result.get("message")
            or result.get("detail")
            or "Checkbox authorization failed"
        )

    return result["access_token"]

def checkbox_good_code(name: str) -> str:

    return hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()[:16]

def fiscalize_checkbox_receipt(order_id: str, items: list, amount) -> str:

    receipt_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"flawless-checkbox:{order_id}")
    )

    goods = []

    for item in items:

        price_cents = int(
            (Decimal(str(item["price"])) * 100).quantize(Decimal("1"))
        )

        goods.append(
            {
                "good": {
                    "code": checkbox_good_code(item["name"]),
                    "name": item["name"][:255],
                    "price": price_cents,
                    "tax": [CHECKBOX_TAX_CODE],
                },
                "quantity": 1000,
                "is_return": False,
            }
        )

    total_cents = int(
        (Decimal(str(amount)) * 100).quantize(Decimal("1"))
    )

    token = checkbox_signin()
    response = requests.post(
        f"{CHECKBOX_API_URL}/receipts/sell",
        headers=checkbox_headers(token),
        json={
            "id": receipt_id,
            "goods": goods,
            "payments": [
                {
                    "type": "CASHLESS",
                    "label": "Картка",
                    "value": total_cents,
                }
            ],
        },
        timeout=30,
    )

    try:

        result = response.json()

    except Exception:

        result = {}

    if response.status_code >= 400:

        raise RuntimeError(
            result.get("message")
            or result.get("detail")
            or response.text[:300]
            or "Checkbox receipt creation failed"
        )

    return receipt_id

def claim_invoice_for_fiscalization(order_id: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET checkbox_status = 'processing',
                    checkbox_error = NULL,
                    updated_at = NOW()
                WHERE order_id = %s
                  AND checkbox_receipt_id IS NULL
                  AND COALESCE(checkbox_status, 'new')
                      IN ('new', 'error')
                RETURNING items, amount
                """,
                (order_id,),
            )

            return cursor.fetchone()

def mark_checkbox_receipt(
    order_id: str,
    status: str,
    receipt_id=None,
    error=None,
):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET checkbox_status = %s,
                    checkbox_receipt_id = COALESCE(%s, checkbox_receipt_id),
                    checkbox_error = %s,
                    fiscalized_at = CASE
                        WHEN %s = 'created' THEN NOW()
                        ELSE fiscalized_at
                    END,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (status, receipt_id, error, status, order_id),
            )

def get_invoice_url(code: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT href
                FROM invoices
                WHERE short_code = %s
                  AND status NOT IN ('cancelled', 'canceled')
                """,
                (code,),
            )

            row = cursor.fetchone()

    return row[0] if row else None

def get_recent_invoices(limit: int = 10):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id, phone, amount, currency, description,
                       status, short_code, created_by_name, created_at
                FROM invoices
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )

            return cursor.fetchall()

def get_invoice_status(order_id: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                "SELECT status FROM invoices WHERE order_id = %s",
                (order_id,),
            )

            row = cursor.fetchone()

    return row[0] if row else None

def mark_invoice_cancelled(order_id: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET status = 'cancelled', updated_at = NOW()
                WHERE order_id = %s
                """,
                (order_id,),
            )

def cancel_liqpay_invoice(order_id: str) -> dict:

    return liqpay_request(
        {
            "version": 3,
            "public_key": LIQPAY_PUBLIC_KEY,
            "action": "invoice_cancel",
            "order_id": order_id,
        }
    )

def status_label(status: str) -> str:

    labels = {
        "unpaid": "🕓 Ожидает оплаты",
        "invoice_wait": "🕓 Ожидает оплаты",
        "wait_accept": "🕓 Ожидает оплаты",
        "processing": "🕓 Обрабатывается",
        "success": "✅ Оплачен",
        "failure": "❌ Не оплачен",
        "error": "❌ Ошибка",
        "reversed": "↩️ Возврат",
        "cancelled": "🚫 Скасований",
        "canceled": "🚫 Скасований",
    }

    return labels.get(status, f"ℹ️ {status}")

def main_menu():

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add(telebot.types.KeyboardButton("Создать инвойс"))

    markup.add(telebot.types.KeyboardButton("История"))

    return markup

def item_action_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    markup.row(
        telebot.types.KeyboardButton("➕ Добавить ещё товар"),
        telebot.types.KeyboardButton("✅ Создать инвойс"),
    )

    return markup

@bot.message_handler(commands=["start"])

def start(message):

    if not require_access(message):

        return

    bot.send_message(

        message.chat.id,

        "Привет! Я бот Flawless для создания LiqPay-инвойсов.\n\n"

        "Нажми <b>Создать инвойс</b> или отправь /invoice.",

        reply_markup=main_menu()

    )

@bot.message_handler(commands=["invoice"])

def invoice_command(message):

    if not require_access(message):

        return

    ask_phone(message)

@bot.message_handler(func=lambda message: message.text == "Создать инвойс")

def invoice_button(message):

    if not require_access(message):

        return

    ask_phone(message)

@bot.message_handler(commands=["id"])

def show_telegram_id(message):

    bot.send_message(
        message.chat.id,
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
    )

@bot.message_handler(commands=["history"])

def history_command(message):

    show_history(message)

@bot.message_handler(func=lambda message: message.text == "История")

def history_button(message):

    show_history(message)

def show_history(message):

    if not require_access(message):

        return

    invoices = get_recent_invoices()

    if not invoices:

        bot.send_message(message.chat.id, "История инвойсов пока пустая.")

        return

    bot.send_message(
        message.chat.id,
        "📋 <b>Последние инвойсы</b>",
        reply_markup=main_menu(),
    )

    for (
        order_id,
        phone,
        amount,
        currency,
        description,
        status,
        short_code,
        created_by_name,
        created_at,
    ) in invoices:

        phone_for_display = display_phone(phone)
        payment_link = make_short_link(short_code) if short_code else None

        item = (
            f"<b>{created_at.astimezone().strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"{status_label(status)}\n"
            f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
            f"Телефон: <code>{html.escape(phone_for_display)}</code>\n"
            f"Описание: {html.escape(description)}\n"
            f"Создал: {html.escape(created_by_name)}\n"
            f"ID: <code>{html.escape(order_id)}</code>"
        )

        if payment_link:

            item += f"\n{html.escape(payment_link)}"

        copy_markup = telebot.types.InlineKeyboardMarkup()
        copy_markup.add(
            telebot.types.InlineKeyboardButton(
                text=f"📋 Копировать {phone_for_display}",
                copy_text=telebot.types.CopyTextButton(
                    text=phone_for_display,
                ),
            )
        )

        if status in {"unpaid", "invoice_wait", "wait_accept"}:

            copy_markup.add(
                telebot.types.InlineKeyboardButton(
                    text="❌ Скасувати інвойс",
                    callback_data=f"cancel:{order_id}",
                )
            )

        bot.send_message(
            message.chat.id,
            item,
            reply_markup=copy_markup,
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel:"))

def ask_cancel_invoice(call):

    if not is_allowed(call.from_user.id):

        bot.answer_callback_query(call.id, "У вас нет доступа.", show_alert=True)

        return

    order_id = call.data.split(":", 1)[1]
    status = get_invoice_status(order_id)

    if status not in {"unpaid", "invoice_wait", "wait_accept"}:

        bot.answer_callback_query(
            call.id,
            "Этот инвойс уже оплачен или отменён.",
            show_alert=True,
        )

        return

    confirm_markup = telebot.types.InlineKeyboardMarkup()
    confirm_markup.row(
        telebot.types.InlineKeyboardButton(
            text="Да, скасувати",
            callback_data=f"confirm_cancel:{order_id}",
        ),
        telebot.types.InlineKeyboardButton(
            text="Ні, залишити",
            callback_data="keep_invoice",
        ),
    )

    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "Точно скасувати цей інвойс?\n"
        f"ID: <code>{html.escape(order_id)}</code>",
        reply_markup=confirm_markup,
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_cancel:")
)

def confirm_cancel_invoice(call):

    if not is_allowed(call.from_user.id):

        bot.answer_callback_query(call.id, "У вас нет доступа.", show_alert=True)

        return

    order_id = call.data.split(":", 1)[1]
    status = get_invoice_status(order_id)

    if status not in {"unpaid", "invoice_wait", "wait_accept"}:

        bot.answer_callback_query(
            call.id,
            "Этот инвойс уже оплачен или отменён.",
            show_alert=True,
        )

        return

    try:

        result = cancel_liqpay_invoice(order_id)

        if result.get("result") != "ok":

            raise RuntimeError(
                result.get("err_description")
                or result.get("status")
                or str(result)
            )

        mark_invoice_cancelled(order_id)

    except Exception as error:

        bot.answer_callback_query(
            call.id,
            "LiqPay не смог отменить инвойс.",
            show_alert=True,
        )
        bot.send_message(
            call.message.chat.id,
            "❌ Не получилось отменить инвойс.\n"
            f"Ошибка: <code>{html.escape(str(error))}</code>",
        )

        return

    bot.answer_callback_query(call.id, "Инвойс отменён")
    bot.edit_message_text(
        "🚫 <b>Інвойс скасовано</b>\n"
        "Клієнт більше не зможе оплатити це посилання.\n"
        f"ID: <code>{html.escape(order_id)}</code>",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

@bot.callback_query_handler(func=lambda call: call.data == "keep_invoice")

def keep_invoice(call):

    bot.answer_callback_query(call.id, "Инвойс оставлен без изменений")
    bot.delete_message(call.message.chat.id, call.message.message_id)

def ask_phone(message):

    user_steps[message.chat.id] = {"step": "phone"}

    bot.send_message(

        message.chat.id,

        "Введите номер телефона клиента:\n"

        "<code>0939325197</code>\n\n"

        "Или вставьте целиком реквизиты Новой почты — "
        "бот сам найдет в них номер.\n\n"

        "Можно с плюсом, пробелами или 380 — бот сам исправит.\n"

        "Для отмены напиши: <code>отмена</code>"

    )

@bot.message_handler(func=lambda message: message.chat.id in user_steps)

def handle_invoice_steps(message):

    chat_id = message.chat.id

    text = (message.text or "").strip()

    if text.lower() in ["/cancel", "отмена", "скасувати", "cancel"]:

        user_steps.pop(chat_id, None)

        bot.send_message(chat_id, "Ок, создание инвойса отменено.", reply_markup=main_menu())

        return

    data = user_steps.get(chat_id, {})

    step = data.get("step")

    if step == "phone":

        phone = extract_phone(text)

        if not phone:

            bot.send_message(
                chat_id,
                "Не получилось найти один номер телефона.\n"
                "Проверьте реквизиты или отправьте номер отдельно, например: "
                "<code>380671234567</code>",
            )

            return

        data["phone"] = phone

        data["items"] = []

        data["step"] = "item_name"

        user_steps[chat_id] = data

        bot.send_message(
            chat_id,
            "Введите название первого товара, например:\n"
            "<code>Комбинезон Flawless, размер M, чёрный</code>",
            reply_markup=telebot.types.ReplyKeyboardRemove(),
        )

        return

    if step == "item_name":

        if not text:

            bot.send_message(chat_id, "Введите название товара.")

            return

        data["pending_item_name"] = text

        data["step"] = "item_price"

        user_steps[chat_id] = data

        bot.send_message(
            chat_id,
            f"Введите цену товара «{html.escape(text)}» в грн, например:\n"
            "<code>2490</code>",
        )

        return

    if step == "item_price":

        price_text = text.replace(" ", "").replace(",", ".")

        try:

            price = Decimal(price_text)

            if price <= 0:

                raise InvalidOperation

        except (InvalidOperation, ValueError):

            bot.send_message(chat_id, "Цена должна быть числом. Например: <code>2490</code>")

            return

        price = price.quantize(Decimal("0.01"))

        price_display = (
            str(int(price))
            if price == price.to_integral_value()
            else f"{price:.2f}"
        )

        data["items"].append(
            {
                "name": data.pop("pending_item_name"),
                "price": price_display,
            }
        )

        data["step"] = "item_action"

        user_steps[chat_id] = data

        items_summary = "\n".join(
            f"{index}. {html.escape(item['name'])} — "
            f"<b>{html.escape(item['price'])} UAH</b>"
            for index, item in enumerate(data["items"], start=1)
        )

        bot.send_message(
            chat_id,
            "Добавлено ✅\n\n"
            f"{items_summary}\n\n"
            "Добавить ещё один товар или создать инвойс?",
            reply_markup=item_action_menu(),
        )

        return

    if step == "item_action":

        if text == "➕ Добавить ещё товар":

            data["step"] = "item_name"

            user_steps[chat_id] = data

            bot.send_message(
                chat_id,
                f"Введите название товара №{len(data['items']) + 1}:",
                reply_markup=telebot.types.ReplyKeyboardRemove(),
            )

            return

        if text != "✅ Создать инвойс":

            bot.send_message(
                chat_id,
                "Выберите одну из кнопок ниже.",
                reply_markup=item_action_menu(),
            )

            return

        total = sum(
            (Decimal(item["price"]) for item in data["items"]),
            Decimal("0"),
        )

        amount = (
            str(int(total))
            if total == total.to_integral_value()
            else f"{total:.2f}"
        )

        description = "; ".join(
            f"{index}. {item['name']} — {item['price']} грн"
            for index, item in enumerate(data["items"], start=1)
        )

        phone = data["phone"]

        bot.send_message(
            chat_id,
            f"Общая сумма: <b>{html.escape(amount)} UAH</b>\n"
            "Создаю инвойс LiqPay…",
            reply_markup=telebot.types.ReplyKeyboardRemove(),
        )

        try:

            order_id, result = create_invoice(
                phone=phone,
                amount=amount,
                description=description,
            )

        except Exception as e:

            user_steps.pop(chat_id, None)

            bot.send_message(

                chat_id,

                "❌ Не получилось создать инвойс.\n\n"

                f"Ошибка: <code>{html.escape(str(e))}</code>",

                reply_markup=main_menu()

            )

            return

        user_steps.pop(chat_id, None)

        status = html.escape(str(result.get("status", "unknown")))

        href = result.get("href") or result.get("url") or result.get("checkout_url")

        invoice_id = result.get("invoice_id") or result.get("id")

        short_code = make_short_code() if href else None

        creator_name = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else message.from_user.first_name
        )

        invoice_saved = True

        try:

            save_invoice(
                order_id=order_id,
                invoice_id=invoice_id,
                phone=phone,
                amount=amount,
                description=description,
                href=href,
                short_code=short_code,
                items=data["items"],
                created_by=message.from_user.id,
                created_by_name=creator_name,
            )

        except Exception as e:

            invoice_saved = False

            bot.send_message(
                chat_id,
                "⚠️ Инвойс создан в LiqPay, но не сохранился в истории.\n"
                f"Ошибка: <code>{html.escape(str(e))}</code>",
            )

        phone_for_display = display_phone(phone)

        msg = (

            "✅ <b>Инвойс создан</b>\n\n"

            f"Телефон: <code>{html.escape(phone_for_display)}</code>\n"

            f"Сумма: <b>{html.escape(amount)} {html.escape(CURRENCY)}</b>\n"

            f"Описание: {html.escape(description)}\n"

            f"Статус: <code>{status}</code>\n"

        )

        if invoice_id:

            msg += f"ID: <code>{html.escape(str(invoice_id))}</code>\n"

        if href:

            short_link = (
                make_short_link(short_code)
                if invoice_saved
                else str(href)
            )

        else:

            msg += "\nLiqPay отправил счет клиенту по телефону, но ссылку в ответе не вернул."

        bot.send_message(chat_id, msg, reply_markup=main_menu())

        if href:

            client_message = (
                "Ваше замовлення сформоване 🌸\n"
                "Оплатити можна за посиланням:\n"
                f"{short_link}"
            )

            bot.send_message(
                chat_id,
                client_message,
                disable_web_page_preview=True,
            )

@bot.message_handler(func=lambda message: True)

def fallback(message):

    bot.send_message(

        message.chat.id,

        "Я умею создавать инвойсы LiqPay.\nНажми <b>Создать инвойс</b> или отправь /invoice.",

        reply_markup=main_menu()

    )

@app.route("/", methods=["GET"])

def index():

    return "Flawless LiqPay Telegram bot is running", 200

@app.route("/<code>", methods=["GET"])

def short_link_redirect(code):

    liqpay_url = get_invoice_url(code)

    if not liqpay_url:

        return "Ссылка не найдена или уже недействительна", 404

    user_agent = request.headers.get("User-Agent", "").lower()
    meta_preview_bots = (
        "facebookexternalhit",
        "facebot",
        "meta-externalagent",
        "meta-externalfetcher",
    )

    if any(bot_name in user_agent for bot_name in meta_preview_bots):

        return "", 204, {"Cache-Control": "no-store"}

    return redirect(liqpay_url, code=302)

@app.route("/liqpay/callback", methods=["POST"])

def liqpay_callback():

    data_b64 = request.form.get("data", "")
    received_signature = request.form.get("signature", "")

    if not data_b64 or not received_signature:

        return "Missing callback data", 400

    expected_signature = make_signature(data_b64)

    if not hmac.compare_digest(received_signature, expected_signature):

        return "Invalid signature", 403

    try:

        callback_data = json.loads(
            base64.b64decode(data_b64).decode("utf-8")
        )

    except Exception:

        return "Invalid callback data", 400

    order_id = str(callback_data.get("order_id", ""))
    status = str(callback_data.get("status", "unknown"))

    if not order_id:

        return "Missing order_id", 400

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET status = %s, updated_at = NOW()
                WHERE order_id = %s
                RETURNING amount, currency, phone, description, items
                """,
                (status, order_id),
            )

            updated_invoice = cursor.fetchone()

    if status == "success" and updated_invoice:

        amount, currency, phone, description, items = updated_invoice
        phone_for_display = display_phone(phone)
        product_names = format_product_names(items, description)

        checkbox_message = ""
        invoice_to_fiscalize = claim_invoice_for_fiscalization(order_id)

        if invoice_to_fiscalize:

            items, invoice_amount = invoice_to_fiscalize

            if items:

                try:

                    receipt_id = fiscalize_checkbox_receipt(
                        order_id,
                        items,
                        invoice_amount,
                    )
                    mark_checkbox_receipt(
                        order_id,
                        "created",
                        receipt_id=receipt_id,
                    )
                    checkbox_message = "\n🧾 Чек Checkbox створено автоматично."

                except Exception as error:

                    mark_checkbox_receipt(
                        order_id,
                        "error",
                        error=str(error)[:500],
                    )
                    checkbox_message = (
                        "\n⚠️ Чек Checkbox не створено автоматично.\n"
                        f"Ошибка: <code>{html.escape(str(error))}</code>"
                    )

            else:

                mark_checkbox_receipt(
                    order_id,
                    "error",
                    error="Invoice has no structured items",
                )
                checkbox_message = (
                    "\n⚠️ Чек Checkbox не створено: "
                    "в старому інвойсі немає списку товарів."
                )

        for user_id in ALLOWED_USER_IDS:

            try:

                bot.send_message(
                    user_id,
                    "✅ <b>Инвойс оплачен</b>\n"
                    f"Телефон: <code>{html.escape(phone_for_display)}</code>\n"
                    f"Товар: {html.escape(product_names)}\n"
                    f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
                    f"{checkbox_message}",
                )

            except Exception:

                pass

    return "ok", 200

@app.route(f"/{BOT_TOKEN}", methods=["POST"])

def telegram_webhook():

    if request.headers.get("content-type") == "application/json":

        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))

        bot.process_new_updates([update])

        return "", 200

    return "Unsupported Media Type", 415

def setup_webhook():

    webhook_full_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"

    bot.remove_webhook()

    time.sleep(0.5)

    success = bot.set_webhook(url=webhook_full_url)

    if not success:

        raise RuntimeError("Telegram webhook setup failed")

    print("Webhook set successfully")

init_db()

setup_webhook()

if __name__ == "__main__":

    print(f"Flawless LiqPay bot запущен на порту {PORT}")

    app.run(host="0.0.0.0", port=PORT)
