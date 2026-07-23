import os

import json

import time

import threading

import base64

import hashlib

import hmac

import html

import re

import secrets

import uuid

import urllib.parse

import requests

import telebot

import psycopg

from decimal import Decimal, InvalidOperation

from flask import Flask, request, redirect

from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")

LIQPAY_PUBLIC_KEY = os.getenv("LIQPAY_PUBLIC_KEY")

LIQPAY_PRIVATE_KEY = os.getenv("LIQPAY_PRIVATE_KEY")

CURRENCY = os.getenv("CURRENCY", "UAH")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL")

CHECKBOX_LICENSE_KEY = os.getenv("CHECKBOX_LICENSE_KEY", "")

KYIV_TZ = ZoneInfo("Europe/Kyiv")

CHECKBOX_PIN_CODE = os.getenv("CHECKBOX_PIN_CODE", "")

CHECKBOX_TAX_CODE = int(os.getenv("CHECKBOX_TAX_CODE", "8"))

CHECKBOX_API_URL = "https://api.checkbox.ua/api/v1"

KEYCRM_API_KEY = os.getenv("KEYCRM_API_KEY", "")

KEYCRM_API_URL = "https://openapi.keycrm.app/v1"
PRODUCT_CATALOG_CACHE = {"expires_at": 0, "payload": None}

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
                    ADD COLUMN IF NOT EXISTS fiscalized_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS liqpay_payment_id TEXT,
                    ADD COLUMN IF NOT EXISTS refund_status TEXT,
                    ADD COLUMN IF NOT EXISTS refund_amount NUMERIC(12, 2),
                    ADD COLUMN IF NOT EXISTS refund_checkbox_receipt_id UUID,
                    ADD COLUMN IF NOT EXISTS refund_error TEXT,
                    ADD COLUMN IF NOT EXISTS refund_requested_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ
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

def phone_message_line(phone: str) -> str:

    phone_for_display = display_phone(phone)

    if not phone_for_display:

        return ""

    return f"Телефон: <code>{html.escape(phone_for_display)}</code>\n"

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

def extract_liqpay_callback_phone(callback_data: dict) -> str:

    if not isinstance(callback_data, dict):

        return ""

    phone_keys = {
        "phone",
        "sender_phone",
        "customer_phone",
        "payer_phone",
        "client_phone",
        "card_phone",
    }

    def walk(value):

        if isinstance(value, dict):

            for key, nested_value in value.items():

                if str(key).lower() in phone_keys:

                    phone = clean_phone(str(nested_value or ""))

                    if phone:

                        return phone

                phone = walk(nested_value)

                if phone:

                    return phone

        if isinstance(value, list):

            for item in value:

                phone = walk(item)

                if phone:

                    return phone

        return ""

    return walk(callback_data)

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

def liqpay_checkout_url(params: dict) -> str:

    json_string = json.dumps(params, ensure_ascii=False, separators=(",", ":"))

    data_b64 = base64.b64encode(json_string.encode("utf-8")).decode("utf-8")

    signature = make_signature(data_b64)

    return "https://www.liqpay.ua/api/3/checkout?" + urllib.parse.urlencode({
        "data": data_b64,
        "signature": signature,
    })

def create_invoice(
    amount: str,
    description: str,
    phone: str = "",
    order_id: str = None,
) -> tuple[str, dict]:

    order_id = order_id or f"flawless_{int(time.time())}_{secrets.token_hex(4)}"

    params = {

        "version": 3,

        "public_key": LIQPAY_PUBLIC_KEY,

        "action": "pay",

        "amount": amount,

        "currency": CURRENCY,

        "description": description,

        "order_id": order_id,

        "language": "uk",

        "server_url": f"{WEBHOOK_URL}/liqpay/callback",

        "paytypes": "card,apay,gpay",

    }

    return order_id, {
        "status": "checkout_url",
        "href": liqpay_checkout_url(params),
    }

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

def checkbox_shift_is_open() -> bool:

    if not CHECKBOX_LICENSE_KEY or not CHECKBOX_PIN_CODE:

        return False

    token = checkbox_signin()
    response = requests.get(
        f"{CHECKBOX_API_URL}/cashier/shift",
        headers=checkbox_headers(token),
        timeout=20,
    )

    if response.status_code == 404:

        return False

    try:

        result = response.json()

    except Exception:

        response.raise_for_status()
        return False

    if response.status_code >= 400:

        return False

    shift = result.get("shift") if isinstance(result, dict) else None
    shift_status = (
        shift.get("status")
        if isinstance(shift, dict)
        else result.get("status") if isinstance(result, dict) else None
    )

    return str(shift_status).upper() == "OPENED"

def checkbox_good_code(name: str) -> str:

    return hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()[:16]

def fiscalize_checkbox_receipt(order_id: str, items: list, amount) -> str:

    receipt_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"flawless-checkbox:{order_id}")
    )

    goods = []

    for item in items:

        fiscal_name = item.get("fiscal_name") or item["name"]
        price_cents = int(
            (Decimal(str(item["price"])) * 100).quantize(Decimal("1"))
        )

        goods.append(
            {
                "good": {
                    "code": checkbox_good_code(fiscal_name),
                    "name": fiscal_name[:255],
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

def fiscalize_checkbox_return(order_id: str, items: list, amount) -> str:

    receipt_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"flawless-checkbox-return:{order_id}")
    )
    goods = []

    for item in items:

        fiscal_name = item.get("fiscal_name") or item["name"]
        price_cents = int(
            (Decimal(str(item["price"])) * 100).quantize(Decimal("1"))
        )
        goods.append(
            {
                "good": {
                    "code": checkbox_good_code(fiscal_name),
                    "name": fiscal_name[:255],
                    "price": price_cents,
                    "tax": [CHECKBOX_TAX_CODE],
                },
                "quantity": 1000,
                "is_return": True,
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
                    "label": "Повернення на картку",
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
            or "Checkbox return receipt creation failed"
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

def get_pending_checkbox_invoices(limit: int = 50):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id
                FROM invoices
                WHERE status = 'success'
                  AND checkbox_receipt_id IS NULL
                  AND checkbox_status = 'error'
                ORDER BY updated_at
                LIMIT %s
                """,
                (limit,),
            )

            return [row[0] for row in cursor.fetchall()]

def get_pending_checkbox_returns(limit: int = 50):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id, items, refund_amount
                FROM invoices
                WHERE refund_status IN ('receipt_pending', 'receipt_error')
                  AND refund_checkbox_receipt_id IS NULL
                ORDER BY refund_requested_at
                LIMIT %s
                """,
                (limit,),
            )

            return cursor.fetchall()

def mark_refund_receipt(
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
                SET refund_status = %s,
                    refund_checkbox_receipt_id =
                        COALESCE(%s, refund_checkbox_receipt_id),
                    refund_error = %s,
                    refunded_at = CASE
                        WHEN %s = 'completed' THEN NOW()
                        ELSE refunded_at
                    END,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (status, receipt_id, error, status, order_id),
            )

def retry_pending_checkbox_receipts():

    if not checkbox_shift_is_open():

        return

    for order_id in get_pending_checkbox_invoices():

        invoice_to_fiscalize = claim_invoice_for_fiscalization(order_id)

        if not invoice_to_fiscalize:

            continue

        items, invoice_amount = invoice_to_fiscalize

        if not items:

            mark_checkbox_receipt(
                order_id,
                "error",
                error="Invoice has no structured items",
            )
            continue

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

        except Exception as error:

            mark_checkbox_receipt(
                order_id,
                "error",
                error=str(error)[:500],
            )

    for order_id, items, refund_amount in get_pending_checkbox_returns():

        try:

            receipt_id = fiscalize_checkbox_return(
                order_id,
                items,
                refund_amount,
            )
            mark_refund_receipt(
                order_id,
                "completed",
                receipt_id=receipt_id,
            )

        except Exception as error:

            mark_refund_receipt(
                order_id,
                "receipt_error",
                error=str(error)[:500],
            )

def checkbox_retry_worker():

    while True:

        try:

            retry_pending_checkbox_receipts()

        except Exception as error:

            print(f"Checkbox retry failed: {error}")

        time.sleep(60)

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
                       status, short_code, created_by_name, created_at,
                       liqpay_payment_id
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

def refund_liqpay_payment(order_id: str, amount) -> dict:

    return liqpay_request(
        {
            "version": 3,
            "public_key": LIQPAY_PUBLIC_KEY,
            "action": "refund",
            "order_id": order_id,
            "amount": str(amount),
        }
    )

def get_paid_invoices_by_phone(phone: str, limit: int = 10):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id, amount, currency, description, created_at,
                       checkbox_receipt_id, refund_status, status,
                       liqpay_payment_id
                FROM invoices
                WHERE phone = %s
                  AND status IN ('success', 'reversed')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (phone, limit),
            )

            return cursor.fetchall()

def get_paid_invoices_by_phone_for_refund(order_id: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT amount, currency, description
                FROM invoices
                WHERE order_id = %s
                  AND status = 'success'
                  AND refund_status IS NULL
                """,
                (order_id,),
            )

            return cursor.fetchone()

def claim_invoice_for_refund(order_id: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET refund_status = 'processing',
                    refund_amount = amount,
                    refund_error = NULL,
                    refund_requested_at = NOW(),
                    updated_at = NOW()
                WHERE order_id = %s
                  AND status = 'success'
                  AND refund_status IS NULL
                RETURNING phone, amount, currency, description, items,
                          checkbox_receipt_id
                """,
                (order_id,),
            )

            return cursor.fetchone()

def mark_refund_failed(order_id: str, error: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET refund_status = 'failed',
                    refund_error = %s,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (error[:500], order_id),
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

    markup.add(telebot.types.KeyboardButton("Возврат"))

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

def amount_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    markup.row(
        telebot.types.KeyboardButton("150"),
        telebot.types.KeyboardButton("590"),
    )

    for left_amount, right_amount in (
        ("990", "891"),
        ("890", "801"),
        ("650", "585"),
        ("690", "621"),
        ("790", "711"),
        ("1590", "1431"),
    ):

        markup.row(
            telebot.types.KeyboardButton(left_amount),
            telebot.types.KeyboardButton(right_amount),
        )

    return markup

def product_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    for product_name in (
        "Штани шовк на резиночці",
        "Комбінезон",
        "Боді з мереживом літо",
        "Футболка бавовна",
        "Сукня з комірцем",
        "Комплект піджак брюки та жилет",
        "Боді принтоване",
    ):

        markup.add(telebot.types.KeyboardButton(product_name))

    return markup

def jumpsuit_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    for product_name in (
        "Комбінезон - сукня трикотаж",
        "Комбінезон кльош майкою",
        "Комбінезон короткий рукав трикотаж",
        "Комбінезон біфлекс",
        "Комбінезон з вирізом",
    ):

        markup.add(telebot.types.KeyboardButton(product_name))

    markup.add(telebot.types.KeyboardButton("⬅️ До списку товарів"))

    return markup

def ask_item_price(chat_id: int, item_number: int):

    bot.send_message(
        chat_id,
        f"Выберите сумму товара №{item_number} или введите другую вручную:",
        reply_markup=amount_menu(),
    )

def ask_item_name(chat_id: int):

    bot.send_message(
        chat_id,
        "Выберите товар из списка или введите другое название вручную:",
        reply_markup=product_menu(),
    )

def add_item_and_show_actions(
    chat_id: int,
    data: dict,
    product_name: str,
    fiscal_name: str = None,
):

    item = {
        "name": product_name,
        "price": data.pop("pending_item_price"),
    }

    if fiscal_name:

        item["fiscal_name"] = fiscal_name

    data["items"].append(item)
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

@bot.message_handler(commands=["refund"])

def refund_command(message):

    ask_refund_phone(message)

@bot.message_handler(func=lambda message: message.text == "Возврат")

def refund_button(message):

    ask_refund_phone(message)

def ask_refund_phone(message):

    if not require_access(message):

        return

    user_steps[message.chat.id] = {"step": "refund_phone"}
    bot.send_message(
        message.chat.id,
        "Введите номер телефона клиента для поиска оплаченных инвойсов:\n"
        "<code>0939325197</code>\n\n"
        "Для выхода напишите: <code>отмена</code>",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )

def show_refund_search_results(chat_id: int, phone: str):

    invoices = get_paid_invoices_by_phone(phone)

    if not invoices:

        bot.send_message(
            chat_id,
            "Оплаченные инвойсы по этому номеру не найдены.",
            reply_markup=main_menu(),
        )
        return

    bot.send_message(
        chat_id,
        "Найдены оплаты. Выберите нужную:",
        reply_markup=main_menu(),
    )

    for (
        order_id,
        amount,
        currency,
        description,
        created_at,
        checkbox_receipt_id,
        refund_status,
        payment_status,
        liqpay_payment_id,
    ) in invoices:

        markup = telebot.types.InlineKeyboardMarkup()

        if (
            payment_status == "success"
            and refund_status is None
        ):

            markup.add(
                telebot.types.InlineKeyboardButton(
                    text=(
                        "↩️ Вернуть деньги и чек"
                        if checkbox_receipt_id
                        else "↩️ Вернуть деньги"
                    ),
                    callback_data=f"refund:{order_id}",
                )
            )

        refund_label = {
            "processing": "⏳ Возврат обрабатывается",
            "receipt_pending": "⏳ Деньги возвращены, создаётся чек",
            "receipt_error": "⚠️ Деньги возвращены, чек ожидает смену",
            "completed": "✅ Возврат завершён",
            "failed": "❌ Ошибка возврата",
        }.get(refund_status)

        text = (
            f"<b>{created_at.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
            f"Товар: {html.escape(description)}\n"
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id or '—')}</code>\n"
            f"Чек Checkbox: {'найден' if checkbox_receipt_id else 'не найден'}"
        )

        if refund_label:

            text += f"\n{refund_label}"

        bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("refund:"))

def ask_refund_confirmation(call):

    if not is_allowed(call.from_user.id):

        bot.answer_callback_query(call.id, "У вас нет доступа.", show_alert=True)
        return

    order_id = call.data.split(":", 1)[1]
    invoice = get_paid_invoices_by_phone_for_refund(order_id)

    if not invoice:

        bot.answer_callback_query(
            call.id,
            "Эта оплата недоступна для возврата.",
            show_alert=True,
        )
        return

    amount, currency, description = invoice
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(
            text="Да, вернуть деньги",
            callback_data=f"confirm_refund:{order_id}",
        ),
        telebot.types.InlineKeyboardButton(
            text="Нет",
            callback_data="keep_invoice",
        ),
    )
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "Подтвердите полный возврат:\n"
        f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
        f"Товар: {html.escape(description)}\n\n"
        "Деньги будут возвращены через LiqPay, "
        "а бот создаст чек возврата Checkbox, если исходный чек найден.",
        reply_markup=markup,
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_refund:")
)

def confirm_refund_payment(call):

    if not is_allowed(call.from_user.id):

        bot.answer_callback_query(call.id, "У вас нет доступа.", show_alert=True)
        return

    order_id = call.data.split(":", 1)[1]
    invoice = claim_invoice_for_refund(order_id)

    if not invoice:

        bot.answer_callback_query(
            call.id,
            "Возврат уже запускался или оплата недоступна.",
            show_alert=True,
        )
        return

    phone, amount, currency, description, items, checkbox_receipt_id = invoice
    bot.answer_callback_query(call.id, "Запускаю возврат")
    bot.edit_message_text(
        "⏳ <b>Оформляю возврат…</b>\n"
        f"Сумма: <b>{amount} {html.escape(currency)}</b>",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

    try:

        result = refund_liqpay_payment(order_id, amount)

        if result.get("result") != "ok":

            raise RuntimeError(
                result.get("err_description")
                or result.get("status")
                or str(result)
            )

    except Exception as error:

        mark_refund_failed(order_id, str(error))
        bot.send_message(
            call.message.chat.id,
            "❌ LiqPay не принял возврат.\n"
            f"Ошибка: <code>{html.escape(str(error))}</code>",
            reply_markup=main_menu(),
        )
        return

    if checkbox_receipt_id:

        mark_refund_receipt(order_id, "receipt_pending")
        checkbox_message = "🧾 Чек возврата Checkbox создан."

        try:

            receipt_id = fiscalize_checkbox_return(order_id, items, amount)
            mark_refund_receipt(
                order_id,
                "completed",
                receipt_id=receipt_id,
            )

        except Exception as error:

            mark_refund_receipt(
                order_id,
                "receipt_error",
                error=str(error)[:500],
            )
            checkbox_message = (
                "⚠️ Деньги отправлены на возврат.\n"
                "Чек Checkbox будет создан автоматически после открытия смены."
            )

    else:

        mark_refund_receipt(
            order_id,
            "completed",
            error="Original Checkbox receipt was not found",
        )
        checkbox_message = (
            "ℹ️ Исходный чек Checkbox не найден, "
            "поэтому чек возврата не создавался."
        )

    wait_message = (
        "\nВозврат будет выполнен за счёт будущих оплат."
        if str(result.get("wait_amount")).lower() == "true"
        else ""
    )
    bot.send_message(
        call.message.chat.id,
        "✅ <b>Возврат принят LiqPay</b>\n"
        f"{phone_message_line(phone)}"
        f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
        f"{checkbox_message}"
        f"{wait_message}",
        reply_markup=main_menu(),
    )

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
        liqpay_payment_id,
    ) in invoices:

        phone_for_display = display_phone(phone)
        payment_link = make_short_link(short_code) if short_code else None

        item = (
            f"<b>{created_at.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"{status_label(status)}\n"
            f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
            f"{phone_message_line(phone)}"
            f"Описание: {html.escape(description)}\n"
            f"Создал: {html.escape(created_by_name)}\n"
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id or '—')}</code>\n"
            f"ID: <code>{html.escape(order_id)}</code>"
        )

        if payment_link:

            item += f"\n{html.escape(payment_link)}"

        copy_markup = telebot.types.InlineKeyboardMarkup()

        if phone_for_display:

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

    user_steps[message.chat.id] = {
        "step": "item_price",
        "phone": "",
        "items": [],
    }

    bot.send_message(
        message.chat.id,
        "Номер телефона клиента сейчас не спрашиваем.\n"
        "Создаём инвойс без привязки к телефону.",
    )

    ask_item_price(message.chat.id, 1)

@bot.message_handler(func=lambda message: message.chat.id in user_steps)

def handle_invoice_steps(message):

    chat_id = message.chat.id

    text = (message.text or "").strip()

    if text.lower() in ["/cancel", "отмена", "скасувати", "cancel"]:

        user_steps.pop(chat_id, None)

        bot.send_message(chat_id, "Ок, действие отменено.", reply_markup=main_menu())

        return

    data = user_steps.get(chat_id, {})

    step = data.get("step")

    if step == "refund_phone":

        phone = extract_phone(text)

        if not phone:

            bot.send_message(
                chat_id,
                "Не получилось найти один номер телефона. "
                "Отправьте его отдельно, например: "
                "<code>380671234567</code>",
            )
            return

        user_steps.pop(chat_id, None)
        show_refund_search_results(chat_id, phone)
        return

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

        data["step"] = "item_price"

        user_steps[chat_id] = data

        ask_item_price(chat_id, 1)

        return

    if step == "item_price":

        price_text = text.replace(" ", "").replace(",", ".")

        try:

            price = Decimal(price_text)

            if price <= 0:

                raise InvalidOperation

        except (InvalidOperation, ValueError):

            bot.send_message(
                chat_id,
                "Сумма должна быть числом. Выберите кнопку или введите сумму вручную.",
                reply_markup=amount_menu(),
            )

            return

        price = price.quantize(Decimal("0.01"))

        price_display = (
            str(int(price))
            if price == price.to_integral_value()
            else f"{price:.2f}"
        )

        data["pending_item_price"] = price_display

        if price_display == "150":

            add_item_and_show_actions(
                chat_id,
                data,
                product_name="Одяг",
                fiscal_name="Шкарпетки",
            )

            return

        data["step"] = "item_name"

        user_steps[chat_id] = data

        ask_item_name(chat_id)

        return

    if step == "item_name":

        if not text:

            bot.send_message(chat_id, "Введите название товара.")

            return

        if text == "Комбінезон":

            bot.send_message(
                chat_id,
                "Выберите вариант комбинезона:",
                reply_markup=jumpsuit_menu(),
            )

            return

        if text == "⬅️ До списку товарів":

            ask_item_name(chat_id)

            return

        add_item_and_show_actions(chat_id, data, product_name=text)

        return

    if step == "item_action":

        if text == "➕ Добавить ещё товар":

            data["step"] = "item_price"

            user_steps[chat_id] = data

            ask_item_price(chat_id, len(data["items"]) + 1)

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

        msg = (

            "✅ <b>Инвойс создан</b>\n\n"

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

        created_invoice_markup = telebot.types.InlineKeyboardMarkup()

        if invoice_saved:

            created_invoice_markup.add(
                telebot.types.InlineKeyboardButton(
                    text="❌ Скасувати інвойс",
                    callback_data=f"cancel:{order_id}",
                )
            )

        bot.send_message(
            chat_id,
            msg,
            reply_markup=(
                created_invoice_markup
                if invoice_saved
                else main_menu()
            ),
        )

        if href:

            client_message = (
                "Ваше замовлення сформоване 🌸 "
                f"Швидка оплата за посиланням : {short_link}\n"
                "Або можемо надати реквізити iban"
            )

            bot.send_message(
                chat_id,
                client_message,
                disable_web_page_preview=True,
                reply_markup=main_menu(),
            )

        elif invoice_saved:

            bot.send_message(
                chat_id,
                "Инвойс можно отменить кнопкой выше или через раздел «История».",
                reply_markup=main_menu(),
            )

@bot.message_handler(func=lambda message: True)

def fallback(message):

    bot.send_message(

        message.chat.id,

        "Я умею создавать инвойсы LiqPay.\nНажми <b>Создать инвойс</b> или отправь /invoice.",

        reply_markup=main_menu()

    )

def keycrm_request(path: str, params=None):

    if not KEYCRM_API_KEY:

        raise RuntimeError("KEYCRM_API_KEY is missing")

    response = requests.get(
        f"{KEYCRM_API_URL}/{path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {KEYCRM_API_KEY}",
        },
        params=params or {},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()

def keycrm_product_image(product: dict) -> str:

    for field in (
        "thumbnail_url",
        "image",
        "image_url",
        "picture",
        "picture_url",
        "thumbnail",
    ):

        value = product.get(field)

        if isinstance(value, str) and value.startswith("http"):

            return value

        if isinstance(value, dict):

            url = value.get("url") or value.get("src")

            if isinstance(url, str) and url.startswith("http"):

                return url

    for field in ("attachments_data", "images", "pictures", "photos"):

        for image in product.get(field) or []:

            if isinstance(image, str) and image.startswith("http"):

                return image

            if isinstance(image, dict):

                url = image.get("url") or image.get("src") or image.get("thumbnail")

                if isinstance(url, str) and url.startswith("http"):

                    return url

    return ""

def keycrm_number(value) -> float:

    try:

        return float(str(value or "").replace(" ", "").replace(",", "."))

    except (TypeError, ValueError):

        return 0

def keycrm_product_price(product: dict) -> float:

    for field in ("price", "price_min", "min_price", "sale_price"):

        value = keycrm_number(product.get(field))

        if value > 0:

            return value

    prices = []

    for offer in product.get("offers") or []:

        for field in ("price", "sale_price", "price_min"):

            value = keycrm_number(offer.get(field))

            if value > 0:

                prices.append(value)
                break

    return min(prices) if prices else 0

def keycrm_product_category(product: dict) -> str:

    category = product.get("category") or {}
    raw_name = (
        category.get("name")
        if isinstance(category, dict)
        else category
    ) or product.get("category_name") or ""
    name = str(raw_name).lower()

    if "сук" in name or "dress" in name:

        return "dresses"

    if "костюм" in name or "комплект" in name or "set" in name:

        return "sets"

    if "топ" in name or "бод" in name or "футбол" in name:

        return "tops"

    return "all"

def keycrm_product_subcategory(name: str) -> str:

    normalized = name.lower()
    groups = (
        (("комбінез", "комбинез"), "Комбінезони"),
        (("боді", "боди"), "Боді"),
        (("сукн", "плать"), "Сукні"),
        (("костюм", "комплект"), "Костюми та комплекти"),
        (("футбол",), "Футболки"),
        (("сороч", "рубаш"), "Сорочки"),
        (("штан", "брюк", "палац"), "Штани"),
        (("спідниц", "юбк"), "Спідниці"),
        (("топ",), "Топи"),
        (("піджак", "жакет", "блейзер"), "Піджаки та жилети"),
    )

    for fragments, label in groups:

        if any(fragment in normalized for fragment in fragments):

            return label

    return "Інше"

def keycrm_product_gender(product: dict) -> str:

    category = product.get("category") or {}
    category_name = (
        category.get("name")
        if isinstance(category, dict)
        else category
    ) or product.get("category_name") or ""
    searchable = f"{category_name} {product.get('name') or ''}".lower()

    if any(marker in searchable for marker in ("чолов", "муж", "для нього", "men")):

        return "men"

    return "women"

def keycrm_offer_options(offers: list) -> tuple[list, list, list]:

    sizes = []
    colors = []
    variants = []

    for offer in offers:

        properties = offer.get("properties") or []
        variant = {
            "id": str(offer.get("id") or ""),
            "sku": str(offer.get("sku") or ""),
            "price": keycrm_number(offer.get("price")),
            "quantity": keycrm_number(offer.get("quantity")),
            "image": str(offer.get("thumbnail_url") or ""),
            "size": "",
            "color": "",
        }

        for prop in properties:

            if not isinstance(prop, dict):

                continue

            prop_name = str(prop.get("name") or "").strip().lower()
            prop_value = str(prop.get("value") or "").strip()

            if not prop_value:

                continue

            if any(marker in prop_name for marker in ("розм", "размер", "size")):

                variant["size"] = prop_value

                if prop_value not in sizes:

                    sizes.append(prop_value)

            if any(marker in prop_name for marker in ("колір", "цвет", "color", "colour")):

                variant["color"] = prop_value

                if prop_value not in colors:

                    colors.append(prop_value)

        variants.append(variant)

    return sizes, colors, variants

def normalize_keycrm_product(product: dict, index: int) -> dict:

    offers = product.get("offers") or []
    sizes, colors, variants = keycrm_offer_options(offers)

    palette = (
        ("#c74f67", "#68142c"),
        ("#d4c4b5", "#867161"),
        ("#9c7567", "#402b27"),
        ("#59514f", "#171313"),
        ("#d9a9af", "#8c4e5b"),
        ("#777675", "#292827"),
    )
    color_a, color_b = palette[index % len(palette)]

    return {
        "id": str(product.get("id") or product.get("uuid") or f"keycrm-{index + 1}"),
        "name": str(product.get("name") or product.get("title") or "Товар").strip(),
        "meta": " · ".join(sizes[:6]),
        "price": keycrm_product_price(product),
        "category": keycrm_product_category(product),
        "gender": keycrm_product_gender(product),
        "subcategory": keycrm_product_subcategory(
            str(product.get("name") or product.get("title") or "")
        ),
        "tag": "",
        "tags": [],
        "image": keycrm_product_image(product),
        "sizes": sizes,
        "colors": colors,
        "variants": variants,
        "sales_count": keycrm_number(product.get("sales_count")),
        "featured": False,
        "a": color_a,
        "b": color_b,
        "rotate": f"{(index % 7) - 3}deg",
    }

@app.route("/api/products", methods=["GET"])

def products_api():

    global PRODUCT_CATALOG_CACHE

    if (
        PRODUCT_CATALOG_CACHE["payload"] is not None
        and PRODUCT_CATALOG_CACHE["expires_at"] > time.time()
    ):
        return (
            PRODUCT_CATALOG_CACHE["payload"],
            200,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    try:

        raw_products = []
        page = 1

        while True:

            payload = keycrm_request(
                "products",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            batch = payload.get("data", []) if isinstance(payload, dict) else []
            raw_products.extend(batch)

            current_page = int(payload.get("current_page") or page)
            last_page = int(payload.get("last_page") or current_page)

            if not batch or current_page >= last_page:

                break

            page += 1

        raw_categories = []
        page = 1

        while True:

            categories_payload = keycrm_request(
                "products/categories",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            categories_batch = (
                categories_payload.get("data", [])
                if isinstance(categories_payload, dict)
                else []
            )
            raw_categories.extend(categories_batch)

            current_page = int(categories_payload.get("current_page") or page)
            last_page = int(categories_payload.get("last_page") or current_page)

            if not categories_batch or current_page >= last_page:

                break

            page += 1

        categories_by_id = {
            str(category.get("id")): category
            for category in raw_categories
            if category.get("id") is not None
        }

        def category_path(category_id):

            names = []
            seen = set()
            current_id = str(category_id or "")

            while current_id and current_id not in seen:

                seen.add(current_id)
                category = categories_by_id.get(current_id)

                if not category:

                    break

                name = str(category.get("name") or "").strip()

                if name:

                    names.append(name)

                current_id = str(category.get("parent_id") or "")

            return " / ".join(reversed(names))

        for product in raw_products:

            product["category_name"] = category_path(product.get("category_id"))

        raw_offers = []
        page = 1

        while True:

            offers_payload = keycrm_request(
                "offers",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            offers_batch = (
                offers_payload.get("data", [])
                if isinstance(offers_payload, dict)
                else []
            )
            raw_offers.extend(offers_batch)

            current_page = int(offers_payload.get("current_page") or page)
            last_page = int(offers_payload.get("last_page") or current_page)

            if not offers_batch or current_page >= last_page:

                break

            page += 1

        offers_by_product = {}

        for offer in raw_offers:

            product_id = str(offer.get("product_id") or "")
            offers_by_product.setdefault(product_id, []).append(offer)

        for product in raw_products:

            product["offers"] = offers_by_product.get(
                str(product.get("id") or product.get("uuid") or ""),
                [],
            )

        sales_by_product = {}
        page = 1

        while page <= 1:

            orders_payload = keycrm_request(
                "order",
                {
                    "limit": 50,
                    "page": page,
                    "sort": "-id",
                    "include": "products.offer",
                },
            )
            orders_batch = (
                orders_payload.get("data", [])
                if isinstance(orders_payload, dict)
                else []
            )

            for order in orders_batch:

                if order.get("payment_status") not in ("paid", "overpaid"):

                    continue

                for order_product in order.get("products") or []:

                    offer = order_product.get("offer") or {}
                    product_id = str(offer.get("product_id") or "")

                    if not product_id:

                        continue

                    sales_by_product[product_id] = (
                        sales_by_product.get(product_id, 0)
                        + keycrm_number(order_product.get("quantity") or 1)
                    )

            current_page = int(orders_payload.get("current_page") or page)
            last_page = int(orders_payload.get("last_page") or current_page)

            if not orders_batch or current_page >= last_page:

                break

            page += 1

        for product in raw_products:

            product["sales_count"] = sales_by_product.get(
                str(product.get("id") or product.get("uuid") or ""),
                0,
            )

        products = [
            normalize_keycrm_product(product, index)
            for index, product in enumerate(raw_products)
        ]
        products = [
            product
            for product in products
            if product["name"] and product["price"] > 0
        ]

        newest_products = sorted(
            products,
            key=lambda product: keycrm_number(product.get("id")),
            reverse=True,
        )[:8]

        for product in newest_products:

            product["tags"].append("New")

        for gender in ("women", "men"):

            gender_products = [
                product for product in products if product["gender"] == gender
            ]
            ranked = sorted(
                gender_products,
                key=lambda product: (
                    product["sales_count"],
                    keycrm_number(product.get("id")),
                ),
                reverse=True,
            )

            for product in ranked[:8]:

                product["featured"] = True
                product["tags"].append("Bestseller")

        for product in products:

            product["tags"] = list(dict.fromkeys(product["tags"]))
            product["tag"] = product["tags"][0] if product["tags"] else ""

        payload = {
            "source": "keycrm",
            "count": len(products),
            "products": products,
        }
        PRODUCT_CATALOG_CACHE = {
            "expires_at": time.time() + 300,
            "payload": payload,
        }

        return (
            payload,
            200,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    except Exception as error:

        print(f"KeyCRM products failed: {error}")
        return (
            {"error": "Products are temporarily unavailable"},
            502,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

def keycrm_post(path: str, payload: dict):

    if not KEYCRM_API_KEY:

        raise RuntimeError("KEYCRM_API_KEY is missing")

    response = requests.post(
        f"{KEYCRM_API_URL}/{path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {KEYCRM_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    try:

        result = response.json()

    except Exception:

        result = {}

    if response.status_code >= 400:

        raise RuntimeError(
            result.get("message")
            or json.dumps(result, ensure_ascii=False)
            or "KeyCRM request failed"
        )

    return result

def keycrm_put(path: str, payload: dict):

    if not KEYCRM_API_KEY:

        raise RuntimeError("KEYCRM_API_KEY is missing")

    response = requests.put(
        f"{KEYCRM_API_URL}/{path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {KEYCRM_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    try:

        result = response.json()

    except Exception:

        result = {}

    if response.status_code >= 400:

        raise RuntimeError(
            result.get("message")
            or json.dumps(result, ensure_ascii=False)
            or "KeyCRM request failed"
        )

    return result

def keycrm_status_id(kind: str):

    env_name = {
        "awaiting_prepayment": "KEYCRM_AWAITING_PREPAYMENT_STATUS_ID",
        "paid": "KEYCRM_PAID_STATUS_ID",
        "new": "KEYCRM_NEW_STATUS_ID",
    }.get(kind, "KEYCRM_NEW_STATUS_ID")
    configured = os.getenv(env_name, "").strip()

    if configured.isdigit():

        return int(configured)

    payload = keycrm_request("order/status", {"limit": 50, "page": 1})
    statuses = payload.get("data", []) if isinstance(payload, dict) else []

    for status in statuses:

        name = str(status.get("name") or "").strip().casefold()

        if kind == "awaiting_prepayment":

            waiting_for_payment_names = {
                "очікування оплати",
                "очікує оплату",
                "очікуємо оплату",
                "ожидание оплаты",
                "ожидает оплату",
                "ожидаем оплату",
                "чекає оплату",
            }
            is_match = (
                name in waiting_for_payment_names
                or (
                    any(marker in name for marker in ("оплат", "передоплат"))
                    and any(marker in name for marker in ("очіку", "ожида", "чека"))
                )
            )

        elif kind == "paid":

            is_match = name in {
                "оплачено",
                "оплачений",
                "оплачен",
                "сплачено",
                "сплачений",
                "paid",
            }

        else:

            is_match = name in {"новий", "новый", "new"}

        if is_match and str(status.get("id") or "").isdigit():

            return int(status["id"])

    return None

def ensure_store_invoice_columns():

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                ALTER TABLE invoices
                    ADD COLUMN IF NOT EXISTS keycrm_order_id BIGINT,
                    ADD COLUMN IF NOT EXISTS store_customer JSONB,
                    ADD COLUMN IF NOT EXISTS delivery_data JSONB,
                    ADD COLUMN IF NOT EXISTS store_order_total NUMERIC(12, 2),
                    ADD COLUMN IF NOT EXISTS store_payment_type TEXT,
                    ADD COLUMN IF NOT EXISTS store_order_items JSONB,
                    ADD COLUMN IF NOT EXISTS delivery_checkbox_receipt_id UUID,
                    ADD COLUMN IF NOT EXISTS delivery_checkbox_status TEXT,
                    ADD COLUMN IF NOT EXISTS delivery_checkbox_error TEXT,
                    ADD COLUMN IF NOT EXISTS delivery_fiscalized_at TIMESTAMPTZ
                """
            )

def store_source_id() -> int:

    configured = os.getenv("KEYCRM_SOURCE_ID", "").strip()

    if configured.isdigit():

        return int(configured)

    payload = keycrm_request("order/source", {"limit": 50, "page": 1})
    sources = payload.get("data", []) if isinstance(payload, dict) else []

    if not sources:

        raise RuntimeError("У KeyCRM немає джерела замовлень")

    for source in sources:

        name = str(source.get("name") or "").strip().casefold()

        if name in {"сайт", "website", "flawless website"}:

            return int(source["id"])

    return int(sources[0]["id"])

def store_prepare_items(requested_items: list) -> tuple[list, Decimal, Decimal]:

    if not isinstance(requested_items, list) or not requested_items:

        raise ValueError("Кошик порожній")

    if len(requested_items) > 30:

        raise ValueError("Забагато товарів у замовленні")

    product_ids = []

    for item in requested_items:

        product_id = str(item.get("id") or "").strip()

        if not product_id.isdigit():

            raise ValueError("Некоректний товар у кошику")

        product_ids.append(product_id)

    ids_csv = ",".join(sorted(set(product_ids)))
    products_payload = keycrm_request(
        "products",
        {
            "limit": 50,
            "page": 1,
            "filter[product_id]": ids_csv,
        },
    )
    offers_payload = keycrm_request(
        "offers",
        {
            "limit": 50,
            "page": 1,
            "include": "product",
            "filter[product_id]": ids_csv,
        },
    )
    crm_products = {
        str(product.get("id")): product
        for product in products_payload.get("data", [])
    }
    offers_by_product = {}

    for offer in offers_payload.get("data", []):

        offers_by_product.setdefault(
            str(offer.get("product_id") or ""),
            [],
        ).append(offer)

    prepared = []

    for requested, product_id in zip(requested_items, product_ids):

        product = crm_products.get(product_id)
        offers = offers_by_product.get(product_id, [])

        if not product or not offers:

            raise ValueError("Один із товарів більше недоступний")

        requested_size = str(requested.get("size") or "").strip()
        requested_color = str(requested.get("color") or "").strip()
        selected_offer = None

        for offer in offers:

            properties = {
                str(prop.get("name") or "").lower(): str(prop.get("value") or "")
                for prop in offer.get("properties") or []
                if isinstance(prop, dict)
            }
            offer_size = next(
                (
                    value for name, value in properties.items()
                    if any(marker in name for marker in ("розм", "размер", "size"))
                ),
                "",
            )
            offer_color = next(
                (
                    value for name, value in properties.items()
                    if any(marker in name for marker in ("колір", "цвет", "color"))
                ),
                "",
            )

            if (
                (not requested_size or requested_size == offer_size)
                and (not requested_color or requested_color == offer_color)
            ):

                selected_offer = offer
                break

        if not selected_offer:

            raise ValueError("Обраний розмір або колір вже недоступний")

        price = Decimal(str(selected_offer.get("price") or product.get("price") or 0))

        if price <= 0:

            raise ValueError("Для товару не вказана ціна")

        properties = []

        if requested_color:

            properties.append({"name": "Колір", "value": requested_color})

        if requested_size:

            properties.append({"name": "Розмір", "value": requested_size})

        prepared.append(
            {
                "product_id": product_id,
                "offer_id": str(selected_offer.get("id") or ""),
                "sku": str(selected_offer.get("sku") or ""),
                "name": str(product.get("name") or "Товар").strip(),
                "picture": (
                    str(selected_offer.get("thumbnail_url") or "").strip()
                    or keycrm_product_image(product)
                ),
                "fiscal_name": str(product.get("name") or "Одяг").strip(),
                "original_price": price,
                "price": price,
                "discount_percent": Decimal("0"),
                "properties": properties,
            }
        )

    if len(prepared) == 2:

        cheaper_index = min(
            range(2),
            key=lambda index: prepared[index]["original_price"],
        )
        prepared[cheaper_index]["discount_percent"] = Decimal("10")

    elif len(prepared) >= 3:

        for item in prepared:

            item["discount_percent"] = Decimal("10")

    subtotal = Decimal("0")
    total = Decimal("0")

    for item in prepared:

        original_price = item["original_price"]
        multiplier = Decimal("1") - item["discount_percent"] / Decimal("100")
        item["price"] = (original_price * multiplier).quantize(Decimal("0.01"))
        subtotal += original_price
        total += item["price"]

    return prepared, subtotal, total

def store_checkout_reference(
    customer: dict,
    delivery: dict,
    requested_items: list,
    payment_type: str,
) -> str:

    normalized_items = sorted(
        [
            {
                "id": str(item.get("id") or ""),
                "size": str(item.get("size") or "").strip(),
                "color": str(item.get("color") or "").strip(),
            }
            for item in requested_items or []
        ],
        key=lambda item: (item["id"], item["size"], item["color"]),
    )
    fingerprint = json.dumps(
        {
            "phone": customer["phone"],
            "delivery": delivery,
            "items": normalized_items,
            "payment_type": payment_type,
            "window": int(time.time() // 1800),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]

    return f"flawless_site_{digest}"

def store_existing_checkout(order_id: str):

    ensure_store_invoice_columns()

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT keycrm_order_id, store_order_total,
                       store_payment_type, amount, href
                FROM invoices
                WHERE order_id = %s
                  AND created_by_name = 'Flawless website'
                LIMIT 1
                """,
                (order_id,),
            )

            return cursor.fetchone()

def store_create_keycrm_order(
    source_uuid: str,
    customer: dict,
    delivery: dict,
    items: list,
    subtotal: Decimal,
    total: Decimal,
    payment_type: str,
    payment_amount: Decimal,
):

    payment_label = (
        "Передоплата 150 грн"
        if payment_type == "prepay"
        else "Повна оплата LiqPay"
    )

    comment_lines = [
        "Замовлення з сайту Flawless",
        f"Сума до знижки: {subtotal:.2f} UAH",
        f"Знижка: {(subtotal - total):.2f} UAH",
        f"До сплати: {total:.2f} UAH",
        f"Спосіб оплати: {payment_label}",
        f"Сума LiqPay: {payment_amount:.2f} UAH",
    ]

    if payment_type == "prepay":

        balance_due = max(total - payment_amount, Decimal("0.00"))
        comment_lines.append(
            f"Залишок до оплати при отриманні: {balance_due:.2f} UAH"
        )

    if delivery.get("comment"):

        comment_lines.append(f"Коментар: {delivery['comment']}")

    awaiting_status_id = keycrm_status_id("awaiting_prepayment")

    if not awaiting_status_id:

        comment_lines.append("Статус оплати: очікуємо передоплату")

    payload = {
        "source_id": store_source_id(),
        "source_uuid": source_uuid,
        "buyer": {
            "full_name": customer["name"],
            "phone": f"+{customer['phone']}",
            "email": customer.get("email") or None,
        },
        "manager_comment": "\n".join(comment_lines),
        "shipping": {
            "shipping_service": "Нова Пошта",
            "shipping_address_country": "Ukraine",
            "shipping_address_city": delivery["city"],
            "shipping_receive_point": delivery["warehouse"],
            "recipient_full_name": customer["name"],
            "recipient_phone": f"+{customer['phone']}",
        },
        "products": [
            {
                "sku": item["sku"],
                "name": item["name"],
                "price": float(item["original_price"]),
                "discount_percent": float(item["discount_percent"]),
                "quantity": 1,
                "properties": item["properties"],
                **(
                    {"picture": item["picture"]}
                    if item.get("picture")
                    else {}
                ),
            }
            for item in items
        ],
    }

    if awaiting_status_id:

        payload["status_id"] = awaiting_status_id

    return keycrm_post("order", payload)

def store_save_details(
    order_id,
    keycrm_order_id,
    customer,
    delivery,
    total,
    payment_type,
    items,
):

    ensure_store_invoice_columns()

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET keycrm_order_id = %s,
                    store_customer = %s::jsonb,
                    delivery_data = %s::jsonb,
                    store_order_total = %s,
                    store_payment_type = %s,
                    store_order_items = %s::jsonb,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (
                    keycrm_order_id,
                    json.dumps(customer, ensure_ascii=False),
                    json.dumps(delivery, ensure_ascii=False),
                    total,
                    payment_type,
                    json.dumps(items, ensure_ascii=False, default=str),
                    order_id,
                ),
            )

def store_mark_keycrm_paid(keycrm_order_id, amount):

    if not keycrm_order_id:

        return

    payment = keycrm_post(
        f"order/{keycrm_order_id}/payment",
        {
            "payment_method": "LiqPay",
            "amount": float(amount),
            "status": "paid",
            "description": "Оплата на сайті Flawless",
        },
    )

    new_status_id = keycrm_status_id("new")

    if not new_status_id:

        raise RuntimeError("У KeyCRM не знайдено статус «Новий»")

    keycrm_put(
        f"order/{keycrm_order_id}",
        {"status_id": new_status_id},
    )

    return payment

def store_checkout_response(payload, status=200):

    return (
        payload,
        status,
        {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Cache-Control": "no-store",
        },
    )

def notify_store_order_created(
    keycrm_order_id,
    customer,
    delivery,
    items,
    total,
    payment_type,
    payment_amount,
):

    payment_label = (
        "Передоплата 150 грн"
        if payment_type == "prepay"
        else "Повна оплата"
    )
    product_names = ", ".join(item["name"] for item in items)
    message = (
        "🛍 <b>Нове замовлення з сайту</b>\n"
        f"Замовлення CRM: <b>#{html.escape(str(keycrm_order_id))}</b>\n"
        f"Клієнт: {html.escape(customer['name'])}\n"
        f"Телефон: <code>+{html.escape(customer['phone'])}</code>\n"
        f"Товар: {html.escape(product_names)}\n"
        f"Сума замовлення: <b>{total:.2f} UAH</b>\n"
        f"До оплати зараз: <b>{payment_amount:.2f} UAH</b>\n"
        f"Оплата: {html.escape(payment_label)}\n"
        f"Доставка: {html.escape(delivery['city'])}, "
        f"{html.escape(delivery['warehouse'])}\n"
        "Статус: <b>очікуємо оплату</b>"
    )

    for user_id in ALLOWED_USER_IDS:

        try:

            bot.send_message(user_id, message)

        except Exception as error:

            print(f"Store order Telegram notification failed: {error}")

def store_checkout():

    if request.method == "OPTIONS":

        return store_checkout_response({}, 204)

    try:

        payload = request.get_json(silent=True) or {}
        customer = payload.get("customer") or {}
        delivery = payload.get("delivery") or {}
        name = str(customer.get("name") or "").strip()
        phone = clean_phone(str(customer.get("phone") or ""))
        email = str(customer.get("email") or "").strip()
        city = str(delivery.get("city") or "").strip()
        warehouse = str(delivery.get("warehouse") or "").strip()
        comment = str(delivery.get("comment") or "").strip()
        payment_type = str(payload.get("paymentType") or "full").strip()

        if payment_type not in {"full", "prepay"}:

            payment_type = "full"

        if len(name) < 2:

            raise ValueError("Вкажіть ім'я та прізвище")

        if len(phone) != 12 or not phone.startswith("380"):

            raise ValueError("Вкажіть український номер телефону")

        if not city or not warehouse:

            raise ValueError("Вкажіть місто та відділення Нової пошти")

        customer = {"name": name, "phone": phone, "email": email}
        delivery = {
            "city": city,
            "warehouse": warehouse,
            "comment": comment,
        }
        requested_items = payload.get("items") or []
        order_id = store_checkout_reference(
            customer,
            delivery,
            requested_items,
            payment_type,
        )
        existing_checkout = store_existing_checkout(order_id)

        if existing_checkout:

            (
                existing_keycrm_order_id,
                existing_total,
                existing_payment_type,
                existing_amount,
                existing_href,
            ) = existing_checkout

            if existing_keycrm_order_id and existing_href:

                return store_checkout_response(
                    {
                        "order_id": order_id,
                        "keycrm_order_id": existing_keycrm_order_id,
                        "total": float(existing_total or existing_amount),
                        "payment_type": existing_payment_type or payment_type,
                        "payment_amount": float(existing_amount),
                        "payment_url": existing_href,
                        "reused": True,
                    }
                )

        items, subtotal, total = store_prepare_items(requested_items)
        payment_amount = (
            min(total, Decimal("150.00"))
            if payment_type == "prepay"
            else total
        ).quantize(Decimal("0.01"))
        crm_order = store_create_keycrm_order(
            order_id,
            customer,
            delivery,
            items,
            subtotal,
            total,
            payment_type,
            payment_amount,
        )
        keycrm_order_id = crm_order.get("id")

        if not keycrm_order_id:

            raise RuntimeError("KeyCRM не повернула номер замовлення")

        description = ", ".join(item["name"] for item in items)[:255]
        order_id, invoice_result = create_invoice(
            amount=f"{payment_amount:.2f}",
            description=description,
            order_id=order_id,
        )
        href = (
            invoice_result.get("href")
            or invoice_result.get("url")
            or invoice_result.get("checkout_url")
        )

        if not href:

            raise RuntimeError("LiqPay не повернув посилання на оплату")

        short_code = make_short_code()
        if payment_type == "prepay":

            invoice_items = [
                {
                    "id": "prepayment",
                    "offer_id": "",
                    "name": f"Передоплата за замовлення: {description}",
                    "fiscal_name": f"Передоплата за замовлення: {description}",
                    "price": float(payment_amount),
                    "original_price": float(payment_amount),
                    "discount_percent": 0,
                    "sku": "",
                    "properties": [
                        {"name": "Повна сума замовлення", "value": f"{total:.2f} UAH"},
                        {"name": "Спосіб оплати", "value": "Передоплата 150 грн"},
                    ],
                }
            ]

        else:

            invoice_items = [
                {
                    "id": item["product_id"],
                    "offer_id": item["offer_id"],
                    "name": item["name"],
                    "fiscal_name": item["fiscal_name"],
                    "price": float(item["price"]),
                    "original_price": float(item["original_price"]),
                    "discount_percent": float(item["discount_percent"]),
                    "sku": item["sku"],
                    "properties": item["properties"],
                }
                for item in items
            ]
        save_invoice(
            order_id=order_id,
            invoice_id=invoice_result.get("invoice_id"),
            phone=phone,
            amount=f"{payment_amount:.2f}",
            description=description,
            href=href,
            short_code=short_code,
            items=invoice_items,
            created_by=0,
            created_by_name="Flawless website",
        )
        store_save_details(
            order_id,
            int(keycrm_order_id),
            customer,
            delivery,
            total,
            payment_type,
            items,
        )
        notify_store_order_created(
            keycrm_order_id,
            customer,
            delivery,
            items,
            total,
            payment_type,
            payment_amount,
        )

        return store_checkout_response(
            {
                "order_id": order_id,
                "keycrm_order_id": keycrm_order_id,
                "subtotal": float(subtotal),
                "discount": float(subtotal - total),
                "total": float(total),
                "payment_type": payment_type,
                "payment_amount": float(payment_amount),
                "payment_url": href,
            }
        )

    except ValueError as error:

        return store_checkout_response({"error": str(error)}, 400)

    except Exception as error:

        print(f"Store checkout failed: {error}")
        return store_checkout_response(
            {"error": "Не вдалося оформити замовлення. Спробуйте ще раз."},
            502,
        )

@app.route(
    "/api/store/checkout",
    methods=["POST", "OPTIONS"],
)
def store_checkout_route():

    return store_checkout()

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
    liqpay_payment_id = str(
        callback_data.get("payment_id")
        or callback_data.get("transaction_id")
        or callback_data.get("liqpay_order_id")
        or ""
    ) or None
    if not order_id:

        return "Missing order_id", 400

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                WITH previous AS (
                    SELECT status
                    FROM invoices
                    WHERE order_id = %s
                    FOR UPDATE
                )
                UPDATE invoices
                SET status = %s,
                    liqpay_payment_id = COALESCE(%s, liqpay_payment_id),
                    updated_at = NOW()
                WHERE order_id = %s
                RETURNING amount, currency, phone, description, items,
                          keycrm_order_id, created_by_name,
                          (SELECT status FROM previous) AS previous_status
                """,
                (order_id, status, liqpay_payment_id, order_id),
            )

            updated_invoice = cursor.fetchone()

    if status in {"reversed", "failure", "error"} and updated_invoice:

        amount, currency, phone, description, items, keycrm_order_id, created_by_name, previous_status = updated_invoice
        product_names = format_product_names(items, description)
        reason = (
            callback_data.get("err_description")
            or callback_data.get("err_code")
            or callback_data.get("result")
            or callback_data.get("description")
            or "LiqPay не передав причину"
        )

        for user_id in (
            [] if created_by_name == "Flawless website" else ALLOWED_USER_IDS
        ):

            try:

                bot.send_message(
                    user_id,
                    "↩️ <b>LiqPay повернув/відхилив платіж</b>\n"
                    f"Статус: <code>{html.escape(status)}</code>\n"
                    f"Товар: {html.escape(product_names)}\n"
                    f"Сума: <b>{amount} {html.escape(currency)}</b>\n"
                    f"ID оплати LiqPay: <code>{html.escape(liqpay_payment_id or '')}</code>\n"
                    f"Причина: <code>{html.escape(str(reason))}</code>",
                )

            except Exception:

                pass

    if status == "success" and updated_invoice:

        amount, currency, phone, description, items, keycrm_order_id, created_by_name, previous_status = updated_invoice

        if previous_status == "success":

            return "ok", 200
        phone_for_display = display_phone(phone)
        product_names = format_product_names(items, description)
        payment_id_line = (
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id)}</code>\n"
            if liqpay_payment_id
            else ""
        )

        if keycrm_order_id:

            try:

                store_mark_keycrm_paid(keycrm_order_id, amount)

            except Exception as error:

                print(f"KeyCRM payment sync failed: {error}")

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

                copy_phone_markup = None

                if phone_for_display:

                    copy_phone_markup = telebot.types.InlineKeyboardMarkup()
                    copy_phone_markup.add(
                        telebot.types.InlineKeyboardButton(
                            text=f"📋 Копировать {phone_for_display}",
                            copy_text=telebot.types.CopyTextButton(
                                text=phone_for_display,
                            ),
                        )
                    )

                notification_title = (
                    "✅ <b>Замовлення з сайту оплачено</b>"
                    if created_by_name == "Flawless website"
                    else "✅ <b>Инвойс оплачен</b>"
                )
                crm_order_line = (
                    f"Замовлення CRM: <b>#{keycrm_order_id}</b>\n"
                    if keycrm_order_id
                    else ""
                )

                bot.send_message(
                    user_id,
                    f"{notification_title}\n"
                    f"{crm_order_line}"
                    f"{phone_message_line(phone)}"
                    f"Товар: {html.escape(product_names)}\n"
                    f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
                    f"{payment_id_line}"
                    f"{checkbox_message}",
                    reply_markup=copy_phone_markup,
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

if "store_checkout_route" not in app.view_functions:

    app.add_url_rule(
        "/api/store/checkout",
        endpoint="store_checkout_live",
        view_func=store_checkout,
        methods=["POST", "OPTIONS"],
    )

if "store_checkout_route" not in app.view_functions:

    app.add_url_rule(
        "/api/store/checkout",
        endpoint="store_checkout_live",
        view_func=store_checkout,
        methods=["POST", "OPTIONS"],
    )

if "store_checkout_route" not in app.view_functions:

    app.add_url_rule(
        "/api/store/checkout",
        endpoint="store_checkout_live",
        view_func=store_checkout,
        methods=["POST", "OPTIONS"],
    )

init_db()
ensure_store_invoice_columns()

threading.Thread(
    target=checkbox_retry_worker,
    name="checkbox-retry",
    daemon=True,
).start()

# The webhook is configured once, after the final live app is selected below.

if __name__ == "__main__" and False:

    print(f"Flawless LiqPay bot запущен на порту {PORT}")

    app.run(host="0.0.0.0", port=PORT)

# Keep the fully configured application above as the single live instance.
# A legacy duplicate below is still parsed for shared helper definitions, but
# it must not replace the bot that already has all invoice/refund handlers.
LIVE_BOT = bot
LIVE_APP = app
import os

import json

import time

import threading

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

KEYCRM_API_KEY = os.getenv("KEYCRM_API_KEY", "")

KEYCRM_API_URL = "https://openapi.keycrm.app/v1"
PRODUCT_CATALOG_CACHE = {"expires_at": 0, "payload": None}

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
                    ADD COLUMN IF NOT EXISTS fiscalized_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS liqpay_payment_id TEXT,
                    ADD COLUMN IF NOT EXISTS refund_status TEXT,
                    ADD COLUMN IF NOT EXISTS refund_amount NUMERIC(12, 2),
                    ADD COLUMN IF NOT EXISTS refund_checkbox_receipt_id UUID,
                    ADD COLUMN IF NOT EXISTS refund_error TEXT,
                    ADD COLUMN IF NOT EXISTS refund_requested_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ
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

def phone_message_line(phone: str) -> str:

    phone_for_display = display_phone(phone)

    if not phone_for_display:

        return ""

    return f"Телефон: <code>{html.escape(phone_for_display)}</code>\n"

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

def extract_liqpay_callback_phone(callback_data: dict) -> str:

    if not isinstance(callback_data, dict):

        return ""

    phone_keys = {
        "phone",
        "sender_phone",
        "customer_phone",
        "payer_phone",
        "client_phone",
        "card_phone",
    }

    def walk(value):

        if isinstance(value, dict):

            for key, nested_value in value.items():

                if str(key).lower() in phone_keys:

                    phone = clean_phone(str(nested_value or ""))

                    if phone:

                        return phone

                phone = walk(nested_value)

                if phone:

                    return phone

        if isinstance(value, list):

            for item in value:

                phone = walk(item)

                if phone:

                    return phone

        return ""

    return walk(callback_data)

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

def liqpay_checkout_url(params: dict) -> str:

    json_string = json.dumps(params, ensure_ascii=False, separators=(",", ":"))

    data_b64 = base64.b64encode(json_string.encode("utf-8")).decode("utf-8")

    signature = make_signature(data_b64)

    return "https://www.liqpay.ua/api/3/checkout?" + urllib.parse.urlencode({
        "data": data_b64,
        "signature": signature,
    })

def create_invoice(amount: str, description: str, phone: str = "") -> tuple[str, dict]:

    order_id = f"flawless_{int(time.time())}_{secrets.token_hex(4)}"

    params = {

        "version": 3,

        "public_key": LIQPAY_PUBLIC_KEY,

        "action": "pay",

        "amount": amount,

        "currency": CURRENCY,

        "description": description,

        "order_id": order_id,

        "language": "uk",

        "server_url": f"{WEBHOOK_URL}/liqpay/callback",

        "paytypes": "card,apay,gpay",

    }

    return order_id, {
        "status": "checkout_url",
        "href": liqpay_checkout_url(params),
    }

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

def checkbox_shift_is_open() -> bool:

    if not CHECKBOX_LICENSE_KEY or not CHECKBOX_PIN_CODE:

        return False

    token = checkbox_signin()
    response = requests.get(
        f"{CHECKBOX_API_URL}/cashier/shift",
        headers=checkbox_headers(token),
        timeout=20,
    )

    if response.status_code == 404:

        return False

    try:

        result = response.json()

    except Exception:

        response.raise_for_status()
        return False

    if response.status_code >= 400:

        return False

    shift = result.get("shift") if isinstance(result, dict) else None
    shift_status = (
        shift.get("status")
        if isinstance(shift, dict)
        else result.get("status") if isinstance(result, dict) else None
    )

    return str(shift_status).upper() == "OPENED"

def checkbox_good_code(name: str) -> str:

    return hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()[:16]

def fiscalize_checkbox_receipt(order_id: str, items: list, amount) -> str:

    receipt_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"flawless-checkbox:{order_id}")
    )

    goods = []

    for item in items:

        fiscal_name = item.get("fiscal_name") or item["name"]
        price_cents = int(
            (Decimal(str(item["price"])) * 100).quantize(Decimal("1"))
        )

        goods.append(
            {
                "good": {
                    "code": checkbox_good_code(fiscal_name),
                    "name": fiscal_name[:255],
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

def fiscalize_checkbox_return(order_id: str, items: list, amount) -> str:

    receipt_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"flawless-checkbox-return:{order_id}")
    )
    goods = []

    for item in items:

        fiscal_name = item.get("fiscal_name") or item["name"]
        price_cents = int(
            (Decimal(str(item["price"])) * 100).quantize(Decimal("1"))
        )
        goods.append(
            {
                "good": {
                    "code": checkbox_good_code(fiscal_name),
                    "name": fiscal_name[:255],
                    "price": price_cents,
                    "tax": [CHECKBOX_TAX_CODE],
                },
                "quantity": 1000,
                "is_return": True,
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
                    "label": "Повернення на картку",
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
            or "Checkbox return receipt creation failed"
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

def get_pending_checkbox_invoices(limit: int = 50):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id
                FROM invoices
                WHERE status = 'success'
                  AND checkbox_receipt_id IS NULL
                  AND checkbox_status = 'error'
                ORDER BY updated_at
                LIMIT %s
                """,
                (limit,),
            )

            return [row[0] for row in cursor.fetchall()]

def get_pending_checkbox_returns(limit: int = 50):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id, items, refund_amount
                FROM invoices
                WHERE refund_status IN ('receipt_pending', 'receipt_error')
                  AND refund_checkbox_receipt_id IS NULL
                ORDER BY refund_requested_at
                LIMIT %s
                """,
                (limit,),
            )

            return cursor.fetchall()

def mark_refund_receipt(
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
                SET refund_status = %s,
                    refund_checkbox_receipt_id =
                        COALESCE(%s, refund_checkbox_receipt_id),
                    refund_error = %s,
                    refunded_at = CASE
                        WHEN %s = 'completed' THEN NOW()
                        ELSE refunded_at
                    END,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (status, receipt_id, error, status, order_id),
            )

def retry_pending_checkbox_receipts():

    if not checkbox_shift_is_open():

        return

    for order_id in get_pending_checkbox_invoices():

        invoice_to_fiscalize = claim_invoice_for_fiscalization(order_id)

        if not invoice_to_fiscalize:

            continue

        items, invoice_amount = invoice_to_fiscalize

        if not items:

            mark_checkbox_receipt(
                order_id,
                "error",
                error="Invoice has no structured items",
            )
            continue

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

        except Exception as error:

            mark_checkbox_receipt(
                order_id,
                "error",
                error=str(error)[:500],
            )

    for order_id, items, refund_amount in get_pending_checkbox_returns():

        try:

            receipt_id = fiscalize_checkbox_return(
                order_id,
                items,
                refund_amount,
            )
            mark_refund_receipt(
                order_id,
                "completed",
                receipt_id=receipt_id,
            )

        except Exception as error:

            mark_refund_receipt(
                order_id,
                "receipt_error",
                error=str(error)[:500],
            )

def checkbox_retry_worker():

    while True:

        try:

            retry_pending_checkbox_receipts()

        except Exception as error:

            print(f"Checkbox retry failed: {error}")

        time.sleep(60)

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
                       status, short_code, created_by_name, created_at,
                       liqpay_payment_id
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

def refund_liqpay_payment(order_id: str, amount) -> dict:

    return liqpay_request(
        {
            "version": 3,
            "public_key": LIQPAY_PUBLIC_KEY,
            "action": "refund",
            "order_id": order_id,
            "amount": str(amount),
        }
    )

def get_paid_invoices_by_phone(phone: str, limit: int = 10):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id, amount, currency, description, created_at,
                       checkbox_receipt_id, refund_status, status,
                       liqpay_payment_id
                FROM invoices
                WHERE phone = %s
                  AND status IN ('success', 'reversed')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (phone, limit),
            )

            return cursor.fetchall()

def get_paid_invoices_by_phone_for_refund(order_id: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT amount, currency, description
                FROM invoices
                WHERE order_id = %s
                  AND status = 'success'
                  AND checkbox_receipt_id IS NOT NULL
                  AND refund_status IS NULL
                """,
                (order_id,),
            )

            return cursor.fetchone()

def claim_invoice_for_refund(order_id: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET refund_status = 'processing',
                    refund_amount = amount,
                    refund_error = NULL,
                    refund_requested_at = NOW(),
                    updated_at = NOW()
                WHERE order_id = %s
                  AND status = 'success'
                  AND checkbox_receipt_id IS NOT NULL
                  AND refund_status IS NULL
                RETURNING phone, amount, currency, description, items
                """,
                (order_id,),
            )

            return cursor.fetchone()

def mark_refund_failed(order_id: str, error: str):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET refund_status = 'failed',
                    refund_error = %s,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (error[:500], order_id),
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

    markup.add(telebot.types.KeyboardButton("Возврат"))

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

def amount_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    markup.row(
        telebot.types.KeyboardButton("150"),
        telebot.types.KeyboardButton("590"),
    )

    for left_amount, right_amount in (
        ("990", "891"),
        ("890", "801"),
        ("650", "585"),
        ("690", "621"),
        ("790", "711"),
        ("1590", "1431"),
    ):

        markup.row(
            telebot.types.KeyboardButton(left_amount),
            telebot.types.KeyboardButton(right_amount),
        )

    return markup

def product_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    for product_name in (
        "Штани шовк на резиночці",
        "Комбінезон",
        "Боді з мереживом літо",
        "Футболка бавовна",
        "Сукня з комірцем",
        "Комплект піджак брюки та жилет",
        "Боді принтоване",
    ):

        markup.add(telebot.types.KeyboardButton(product_name))

    return markup

def jumpsuit_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    for product_name in (
        "Комбінезон - сукня трикотаж",
        "Комбінезон кльош майкою",
        "Комбінезон короткий рукав трикотаж",
        "Комбінезон біфлекс",
        "Комбінезон з вирізом",
    ):

        markup.add(telebot.types.KeyboardButton(product_name))

    markup.add(telebot.types.KeyboardButton("⬅️ До списку товарів"))

    return markup

def ask_item_price(chat_id: int, item_number: int):

    bot.send_message(
        chat_id,
        f"Выберите сумму товара №{item_number} или введите другую вручную:",
        reply_markup=amount_menu(),
    )

def ask_item_name(chat_id: int):

    bot.send_message(
        chat_id,
        "Выберите товар из списка или введите другое название вручную:",
        reply_markup=product_menu(),
    )

def add_item_and_show_actions(
    chat_id: int,
    data: dict,
    product_name: str,
    fiscal_name: str = None,
):

    item = {
        "name": product_name,
        "price": data.pop("pending_item_price"),
    }

    if fiscal_name:

        item["fiscal_name"] = fiscal_name

    data["items"].append(item)
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

@bot.message_handler(commands=["refund"])

def refund_command(message):

    ask_refund_phone(message)

@bot.message_handler(func=lambda message: message.text == "Возврат")

def refund_button(message):

    ask_refund_phone(message)

def ask_refund_phone(message):

    if not require_access(message):

        return

    user_steps[message.chat.id] = {"step": "refund_phone"}
    bot.send_message(
        message.chat.id,
        "Введите номер телефона клиента для поиска оплаченных инвойсов:\n"
        "<code>0939325197</code>\n\n"
        "Для выхода напишите: <code>отмена</code>",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )

def show_refund_search_results(chat_id: int, phone: str):

    invoices = get_paid_invoices_by_phone(phone)

    if not invoices:

        bot.send_message(
            chat_id,
            "Оплаченные инвойсы по этому номеру не найдены.",
            reply_markup=main_menu(),
        )
        return

    bot.send_message(
        chat_id,
        "Найдены оплаты. Выберите нужную:",
        reply_markup=main_menu(),
    )

    for (
        order_id,
        amount,
        currency,
        description,
        created_at,
        checkbox_receipt_id,
        refund_status,
        payment_status,
        liqpay_payment_id,
    ) in invoices:

        markup = telebot.types.InlineKeyboardMarkup()

        if (
            payment_status == "success"
            and checkbox_receipt_id
            and refund_status is None
        ):

            markup.add(
                telebot.types.InlineKeyboardButton(
                    text="↩️ Оформить возврат",
                    callback_data=f"refund:{order_id}",
                )
            )

        refund_label = {
            "processing": "⏳ Возврат обрабатывается",
            "receipt_pending": "⏳ Деньги возвращены, создаётся чек",
            "receipt_error": "⚠️ Деньги возвращены, чек ожидает смену",
            "completed": "✅ Возврат завершён",
            "failed": "❌ Ошибка возврата",
        }.get(refund_status)

        text = (
            f"<b>{created_at.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
            f"Товар: {html.escape(description)}\n"
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id or '—')}</code>\n"
            f"Чек Checkbox: {'найден' if checkbox_receipt_id else 'не найден'}"
        )

        if refund_label:

            text += f"\n{refund_label}"

        bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("refund:"))

def ask_refund_confirmation(call):

    if not is_allowed(call.from_user.id):

        bot.answer_callback_query(call.id, "У вас нет доступа.", show_alert=True)
        return

    order_id = call.data.split(":", 1)[1]
    invoice = get_paid_invoices_by_phone_for_refund(order_id)

    if not invoice:

        bot.answer_callback_query(
            call.id,
            "Эта оплата недоступна для возврата.",
            show_alert=True,
        )
        return

    amount, currency, description = invoice
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(
            text="Да, вернуть деньги",
            callback_data=f"confirm_refund:{order_id}",
        ),
        telebot.types.InlineKeyboardButton(
            text="Нет",
            callback_data="keep_invoice",
        ),
    )
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "Подтвердите полный возврат:\n"
        f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
        f"Товар: {html.escape(description)}\n\n"
        "Деньги будут возвращены через LiqPay, "
        "а бот создаст чек возврата Checkbox.",
        reply_markup=markup,
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_refund:")
)

def confirm_refund_payment(call):

    if not is_allowed(call.from_user.id):

        bot.answer_callback_query(call.id, "У вас нет доступа.", show_alert=True)
        return

    order_id = call.data.split(":", 1)[1]
    invoice = claim_invoice_for_refund(order_id)

    if not invoice:

        bot.answer_callback_query(
            call.id,
            "Возврат уже запускался или оплата недоступна.",
            show_alert=True,
        )
        return

    phone, amount, currency, description, items = invoice
    bot.answer_callback_query(call.id, "Запускаю возврат")
    bot.edit_message_text(
        "⏳ <b>Оформляю возврат…</b>\n"
        f"Сумма: <b>{amount} {html.escape(currency)}</b>",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )

    try:

        result = refund_liqpay_payment(order_id, amount)

        if result.get("result") != "ok":

            raise RuntimeError(
                result.get("err_description")
                or result.get("status")
                or str(result)
            )

    except Exception as error:

        mark_refund_failed(order_id, str(error))
        bot.send_message(
            call.message.chat.id,
            "❌ LiqPay не принял возврат.\n"
            f"Ошибка: <code>{html.escape(str(error))}</code>",
            reply_markup=main_menu(),
        )
        return

    mark_refund_receipt(order_id, "receipt_pending")
    checkbox_message = "🧾 Чек возврата Checkbox создан."

    try:

        receipt_id = fiscalize_checkbox_return(order_id, items, amount)
        mark_refund_receipt(
            order_id,
            "completed",
            receipt_id=receipt_id,
        )

    except Exception as error:

        mark_refund_receipt(
            order_id,
            "receipt_error",
            error=str(error)[:500],
        )
        checkbox_message = (
            "⚠️ Деньги отправлены на возврат.\n"
            "Чек Checkbox будет создан автоматически после открытия смены."
        )

    wait_message = (
        "\nВозврат будет выполнен за счёт будущих оплат."
        if str(result.get("wait_amount")).lower() == "true"
        else ""
    )
    bot.send_message(
        call.message.chat.id,
        "✅ <b>Возврат принят LiqPay</b>\n"
        f"{phone_message_line(phone)}"
        f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
        f"{checkbox_message}"
        f"{wait_message}",
        reply_markup=main_menu(),
    )

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
        liqpay_payment_id,
    ) in invoices:

        phone_for_display = display_phone(phone)
        payment_link = make_short_link(short_code) if short_code else None

        item = (
            f"<b>{created_at.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"{status_label(status)}\n"
            f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
            f"{phone_message_line(phone)}"
            f"Описание: {html.escape(description)}\n"
            f"Создал: {html.escape(created_by_name)}\n"
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id or '—')}</code>\n"
            f"ID: <code>{html.escape(order_id)}</code>"
        )

        if payment_link:

            item += f"\n{html.escape(payment_link)}"

        copy_markup = telebot.types.InlineKeyboardMarkup()

        if phone_for_display:

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

    user_steps[message.chat.id] = {
        "step": "item_price",
        "phone": "",
        "items": [],
    }

    bot.send_message(
        message.chat.id,
        "Номер телефона клиента сейчас не спрашиваем.\n"
        "Создаём инвойс без привязки к телефону.",
    )

    ask_item_price(message.chat.id, 1)

@bot.message_handler(func=lambda message: message.chat.id in user_steps)

def handle_invoice_steps(message):

    chat_id = message.chat.id

    text = (message.text or "").strip()

    if text.lower() in ["/cancel", "отмена", "скасувати", "cancel"]:

        user_steps.pop(chat_id, None)

        bot.send_message(chat_id, "Ок, действие отменено.", reply_markup=main_menu())

        return

    data = user_steps.get(chat_id, {})

    step = data.get("step")

    if step == "refund_phone":

        phone = extract_phone(text)

        if not phone:

            bot.send_message(
                chat_id,
                "Не получилось найти один номер телефона. "
                "Отправьте его отдельно, например: "
                "<code>380671234567</code>",
            )
            return

        user_steps.pop(chat_id, None)
        show_refund_search_results(chat_id, phone)
        return

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

        data["step"] = "item_price"

        user_steps[chat_id] = data

        ask_item_price(chat_id, 1)

        return

    if step == "item_price":

        price_text = text.replace(" ", "").replace(",", ".")

        try:

            price = Decimal(price_text)

            if price <= 0:

                raise InvalidOperation

        except (InvalidOperation, ValueError):

            bot.send_message(
                chat_id,
                "Сумма должна быть числом. Выберите кнопку или введите сумму вручную.",
                reply_markup=amount_menu(),
            )

            return

        price = price.quantize(Decimal("0.01"))

        price_display = (
            str(int(price))
            if price == price.to_integral_value()
            else f"{price:.2f}"
        )

        data["pending_item_price"] = price_display

        if price_display == "150":

            add_item_and_show_actions(
                chat_id,
                data,
                product_name="Одяг",
                fiscal_name="Шкарпетки",
            )

            return

        data["step"] = "item_name"

        user_steps[chat_id] = data

        ask_item_name(chat_id)

        return

    if step == "item_name":

        if not text:

            bot.send_message(chat_id, "Введите название товара.")

            return

        if text == "Комбінезон":

            bot.send_message(
                chat_id,
                "Выберите вариант комбинезона:",
                reply_markup=jumpsuit_menu(),
            )

            return

        if text == "⬅️ До списку товарів":

            ask_item_name(chat_id)

            return

        add_item_and_show_actions(chat_id, data, product_name=text)

        return

    if step == "item_action":

        if text == "➕ Добавить ещё товар":

            data["step"] = "item_price"

            user_steps[chat_id] = data

            ask_item_price(chat_id, len(data["items"]) + 1)

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

        msg = (

            "✅ <b>Инвойс создан</b>\n\n"

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

        created_invoice_markup = telebot.types.InlineKeyboardMarkup()

        if invoice_saved:

            created_invoice_markup.add(
                telebot.types.InlineKeyboardButton(
                    text="❌ Скасувати інвойс",
                    callback_data=f"cancel:{order_id}",
                )
            )

        bot.send_message(
            chat_id,
            msg,
            reply_markup=(
                created_invoice_markup
                if invoice_saved
                else main_menu()
            ),
        )

        if href:

            client_message = (
                "Ваше замовлення сформоване 🌸 "
                f"Швидка оплата за посиланням : {short_link}\n"
                "Або можемо надати реквізити iban"
            )

            bot.send_message(
                chat_id,
                client_message,
                disable_web_page_preview=True,
                reply_markup=main_menu(),
            )

        elif invoice_saved:

            bot.send_message(
                chat_id,
                "Инвойс можно отменить кнопкой выше или через раздел «История».",
                reply_markup=main_menu(),
            )

@bot.message_handler(func=lambda message: True)

def fallback(message):

    bot.send_message(

        message.chat.id,

        "Я умею создавать инвойсы LiqPay.\nНажми <b>Создать инвойс</b> или отправь /invoice.",

        reply_markup=main_menu()

    )

def keycrm_request(path: str, params=None):

    if not KEYCRM_API_KEY:

        raise RuntimeError("KEYCRM_API_KEY is missing")

    response = requests.get(
        f"{KEYCRM_API_URL}/{path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {KEYCRM_API_KEY}",
        },
        params=params or {},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()

def keycrm_product_image(product: dict) -> str:

    for field in (
        "thumbnail_url",
        "image",
        "image_url",
        "picture",
        "picture_url",
        "thumbnail",
    ):

        value = product.get(field)

        if isinstance(value, str) and value.startswith("http"):

            return value

        if isinstance(value, dict):

            url = value.get("url") or value.get("src")

            if isinstance(url, str) and url.startswith("http"):

                return url

    for field in ("attachments_data", "images", "pictures", "photos"):

        for image in product.get(field) or []:

            if isinstance(image, str) and image.startswith("http"):

                return image

            if isinstance(image, dict):

                url = image.get("url") or image.get("src") or image.get("thumbnail")

                if isinstance(url, str) and url.startswith("http"):

                    return url

    return ""

def keycrm_number(value) -> float:

    try:

        return float(str(value or "").replace(" ", "").replace(",", "."))

    except (TypeError, ValueError):

        return 0

def keycrm_product_price(product: dict) -> float:

    for field in ("price", "price_min", "min_price", "sale_price"):

        value = keycrm_number(product.get(field))

        if value > 0:

            return value

    prices = []

    for offer in product.get("offers") or []:

        for field in ("price", "sale_price", "price_min"):

            value = keycrm_number(offer.get(field))

            if value > 0:

                prices.append(value)
                break

    return min(prices) if prices else 0

def keycrm_product_category(product: dict) -> str:

    category = product.get("category") or {}
    raw_name = (
        category.get("name")
        if isinstance(category, dict)
        else category
    ) or product.get("category_name") or ""
    name = str(raw_name).lower()

    if "сук" in name or "dress" in name:

        return "dresses"

    if "костюм" in name or "комплект" in name or "set" in name:

        return "sets"

    if "топ" in name or "бод" in name or "футбол" in name:

        return "tops"

    return "all"

def keycrm_product_subcategory(name: str) -> str:

    normalized = name.lower()
    groups = (
        (("комбінез", "комбинез"), "Комбінезони"),
        (("боді", "боди"), "Боді"),
        (("сукн", "плать"), "Сукні"),
        (("костюм", "комплект"), "Костюми та комплекти"),
        (("футбол",), "Футболки"),
        (("сороч", "рубаш"), "Сорочки"),
        (("штан", "брюк", "палац"), "Штани"),
        (("спідниц", "юбк"), "Спідниці"),
        (("топ",), "Топи"),
        (("піджак", "жакет", "блейзер"), "Піджаки та жилети"),
    )

    for fragments, label in groups:

        if any(fragment in normalized for fragment in fragments):

            return label

    return "Інше"

def keycrm_product_gender(product: dict) -> str:

    category = product.get("category") or {}
    category_name = (
        category.get("name")
        if isinstance(category, dict)
        else category
    ) or product.get("category_name") or ""
    searchable = f"{category_name} {product.get('name') or ''}".lower()

    if any(marker in searchable for marker in ("чолов", "муж", "для нього", "men")):

        return "men"

    return "women"

def keycrm_offer_options(offers: list) -> tuple[list, list, list]:

    sizes = []
    colors = []
    variants = []

    for offer in offers:

        properties = offer.get("properties") or []
        variant = {
            "id": str(offer.get("id") or ""),
            "sku": str(offer.get("sku") or ""),
            "price": keycrm_number(offer.get("price")),
            "quantity": keycrm_number(offer.get("quantity")),
            "image": str(offer.get("thumbnail_url") or ""),
            "size": "",
            "color": "",
        }

        for prop in properties:

            if not isinstance(prop, dict):

                continue

            prop_name = str(prop.get("name") or "").strip().lower()
            prop_value = str(prop.get("value") or "").strip()

            if not prop_value:

                continue

            if any(marker in prop_name for marker in ("розм", "размер", "size")):

                variant["size"] = prop_value

                if prop_value not in sizes:

                    sizes.append(prop_value)

            if any(marker in prop_name for marker in ("колір", "цвет", "color", "colour")):

                variant["color"] = prop_value

                if prop_value not in colors:

                    colors.append(prop_value)

        variants.append(variant)

    return sizes, colors, variants

def normalize_keycrm_product(product: dict, index: int) -> dict:

    offers = product.get("offers") or []
    sizes, colors, variants = keycrm_offer_options(offers)

    palette = (
        ("#c74f67", "#68142c"),
        ("#d4c4b5", "#867161"),
        ("#9c7567", "#402b27"),
        ("#59514f", "#171313"),
        ("#d9a9af", "#8c4e5b"),
        ("#777675", "#292827"),
    )
    color_a, color_b = palette[index % len(palette)]

    return {
        "id": str(product.get("id") or product.get("uuid") or f"keycrm-{index + 1}"),
        "name": str(product.get("name") or product.get("title") or "Товар").strip(),
        "meta": " · ".join(sizes[:6]),
        "price": keycrm_product_price(product),
        "category": keycrm_product_category(product),
        "gender": keycrm_product_gender(product),
        "subcategory": keycrm_product_subcategory(
            str(product.get("name") or product.get("title") or "")
        ),
        "tag": "",
        "tags": [],
        "image": keycrm_product_image(product),
        "sizes": sizes,
        "colors": colors,
        "variants": variants,
        "sales_count": keycrm_number(product.get("sales_count")),
        "featured": False,
        "a": color_a,
        "b": color_b,
        "rotate": f"{(index % 7) - 3}deg",
    }

@app.route("/api/products", methods=["GET"])

def products_api():

    global PRODUCT_CATALOG_CACHE

    if (
        PRODUCT_CATALOG_CACHE["payload"] is not None
        and PRODUCT_CATALOG_CACHE["expires_at"] > time.time()
    ):
        return (
            PRODUCT_CATALOG_CACHE["payload"],
            200,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    try:

        raw_products = []
        page = 1

        while True:

            payload = keycrm_request(
                "products",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            batch = payload.get("data", []) if isinstance(payload, dict) else []
            raw_products.extend(batch)

            current_page = int(payload.get("current_page") or page)
            last_page = int(payload.get("last_page") or current_page)

            if not batch or current_page >= last_page:

                break

            page += 1

        raw_categories = []
        page = 1

        while True:

            categories_payload = keycrm_request(
                "products/categories",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            categories_batch = (
                categories_payload.get("data", [])
                if isinstance(categories_payload, dict)
                else []
            )
            raw_categories.extend(categories_batch)

            current_page = int(categories_payload.get("current_page") or page)
            last_page = int(categories_payload.get("last_page") or current_page)

            if not categories_batch or current_page >= last_page:

                break

            page += 1

        categories_by_id = {
            str(category.get("id")): category
            for category in raw_categories
            if category.get("id") is not None
        }

        def category_path(category_id):

            names = []
            seen = set()
            current_id = str(category_id or "")

            while current_id and current_id not in seen:

                seen.add(current_id)
                category = categories_by_id.get(current_id)

                if not category:

                    break

                name = str(category.get("name") or "").strip()

                if name:

                    names.append(name)

                current_id = str(category.get("parent_id") or "")

            return " / ".join(reversed(names))

        for product in raw_products:

            product["category_name"] = category_path(product.get("category_id"))

        raw_offers = []
        page = 1

        while True:

            offers_payload = keycrm_request(
                "offers",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            offers_batch = (
                offers_payload.get("data", [])
                if isinstance(offers_payload, dict)
                else []
            )
            raw_offers.extend(offers_batch)

            current_page = int(offers_payload.get("current_page") or page)
            last_page = int(offers_payload.get("last_page") or current_page)

            if not offers_batch or current_page >= last_page:

                break

            page += 1

        offers_by_product = {}

        for offer in raw_offers:

            product_id = str(offer.get("product_id") or "")
            offers_by_product.setdefault(product_id, []).append(offer)

        for product in raw_products:

            product["offers"] = offers_by_product.get(
                str(product.get("id") or product.get("uuid") or ""),
                [],
            )

        sales_by_product = {}
        page = 1

        while page <= 1:

            orders_payload = keycrm_request(
                "order",
                {
                    "limit": 50,
                    "page": page,
                    "sort": "-id",
                    "include": "products.offer",
                },
            )
            orders_batch = (
                orders_payload.get("data", [])
                if isinstance(orders_payload, dict)
                else []
            )

            for order in orders_batch:

                if order.get("payment_status") not in ("paid", "overpaid"):

                    continue

                for order_product in order.get("products") or []:

                    offer = order_product.get("offer") or {}
                    product_id = str(offer.get("product_id") or "")

                    if not product_id:

                        continue

                    sales_by_product[product_id] = (
                        sales_by_product.get(product_id, 0)
                        + keycrm_number(order_product.get("quantity") or 1)
                    )

            current_page = int(orders_payload.get("current_page") or page)
            last_page = int(orders_payload.get("last_page") or current_page)

            if not orders_batch or current_page >= last_page:

                break

            page += 1

        for product in raw_products:

            product["sales_count"] = sales_by_product.get(
                str(product.get("id") or product.get("uuid") or ""),
                0,
            )

        products = [
            normalize_keycrm_product(product, index)
            for index, product in enumerate(raw_products)
        ]
        products = [
            product
            for product in products
            if product["name"] and product["price"] > 0
        ]

        newest_products = sorted(
            products,
            key=lambda product: keycrm_number(product.get("id")),
            reverse=True,
        )[:8]

        for product in newest_products:

            product["tags"].append("New")

        for gender in ("women", "men"):

            gender_products = [
                product for product in products if product["gender"] == gender
            ]
            ranked = sorted(
                gender_products,
                key=lambda product: (
                    product["sales_count"],
                    keycrm_number(product.get("id")),
                ),
                reverse=True,
            )

            for product in ranked[:8]:

                product["featured"] = True
                product["tags"].append("Bestseller")

        for product in products:

            product["tags"] = list(dict.fromkeys(product["tags"]))
            product["tag"] = product["tags"][0] if product["tags"] else ""

        payload = {
            "source": "keycrm",
            "count": len(products),
            "products": products,
        }
        PRODUCT_CATALOG_CACHE = {
            "expires_at": time.time() + 300,
            "payload": payload,
        }

        return (
            payload,
            200,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    except Exception as error:

        print(f"KeyCRM products failed: {error}")
        return (
            {"error": "Products are temporarily unavailable"},
            502,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
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
    liqpay_payment_id = str(
        callback_data.get("payment_id")
        or callback_data.get("transaction_id")
        or callback_data.get("liqpay_order_id")
        or ""
    ) or None
    if not order_id:

        return "Missing order_id", 400

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                WITH previous AS (
                    SELECT status
                    FROM invoices
                    WHERE order_id = %s
                    FOR UPDATE
                )
                UPDATE invoices
                SET status = %s,
                    liqpay_payment_id = COALESCE(%s, liqpay_payment_id),
                    updated_at = NOW()
                WHERE order_id = %s
                RETURNING amount, currency, phone, description, items,
                          keycrm_order_id, created_by_name,
                          (SELECT status FROM previous) AS previous_status
                """,
                (order_id, status, liqpay_payment_id, order_id),
            )

            updated_invoice = cursor.fetchone()

    if status in {"reversed", "failure", "error"} and updated_invoice:

        amount, currency, phone, description, items, keycrm_order_id, created_by_name, previous_status = updated_invoice
        product_names = format_product_names(items, description)
        reason = (
            callback_data.get("err_description")
            or callback_data.get("err_code")
            or callback_data.get("result")
            or callback_data.get("description")
            or "LiqPay не передав причину"
        )

        for user_id in (
            [] if created_by_name == "Flawless website" else ALLOWED_USER_IDS
        ):

            try:

                bot.send_message(
                    user_id,
                    "↩️ <b>LiqPay повернув/відхилив платіж</b>\n"
                    f"Статус: <code>{html.escape(status)}</code>\n"
                    f"Товар: {html.escape(product_names)}\n"
                    f"Сума: <b>{amount} {html.escape(currency)}</b>\n"
                    f"ID оплати LiqPay: <code>{html.escape(liqpay_payment_id or '')}</code>\n"
                    f"Причина: <code>{html.escape(str(reason))}</code>",
                )

            except Exception:

                pass

    if status == "success" and updated_invoice:

        amount, currency, phone, description, items, keycrm_order_id, created_by_name, previous_status = updated_invoice

        if previous_status == "success":

            return "ok", 200
        phone_for_display = display_phone(phone)
        product_names = format_product_names(items, description)
        payment_id_line = (
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id)}</code>\n"
            if liqpay_payment_id
            else ""
        )

        if keycrm_order_id:

            try:

                store_mark_keycrm_paid(keycrm_order_id, amount)

            except Exception as error:

                print(f"KeyCRM payment sync failed: {error}")

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

                copy_phone_markup = None

                if phone_for_display:

                    copy_phone_markup = telebot.types.InlineKeyboardMarkup()
                    copy_phone_markup.add(
                        telebot.types.InlineKeyboardButton(
                            text=f"📋 Копировать {phone_for_display}",
                            copy_text=telebot.types.CopyTextButton(
                                text=phone_for_display,
                            ),
                        )
                    )

                notification_title = (
                    "✅ <b>Замовлення з сайту оплачено</b>"
                    if created_by_name == "Flawless website"
                    else "✅ <b>Инвойс оплачен</b>"
                )
                crm_order_line = (
                    f"Замовлення CRM: <b>#{keycrm_order_id}</b>\n"
                    if keycrm_order_id
                    else ""
                )

                bot.send_message(
                    user_id,
                    f"{notification_title}\n"
                    f"{crm_order_line}"
                    f"{phone_message_line(phone)}"
                    f"Товар: {html.escape(product_names)}\n"
                    f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
                    f"{payment_id_line}"
                    f"{checkbox_message}",
                    reply_markup=copy_phone_markup,
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

if "store_checkout_live" not in app.view_functions:

    app.add_url_rule(
        "/api/store/checkout",
        endpoint="store_checkout_live",
        view_func=store_checkout,
        methods=["POST", "OPTIONS"],
    )


def claim_store_invoice_for_refund(keycrm_order_id: int):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET refund_status = 'processing',
                    refund_amount = amount,
                    refund_error = NULL,
                    refund_requested_at = NOW(),
                    updated_at = NOW()
                WHERE keycrm_order_id = %s
                  AND created_by_name = 'Flawless website'
                  AND status = 'success'
                  AND refund_status IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements(COALESCE(items, '[]'::jsonb)) AS item
                      WHERE item->>'id' = 'prepayment'
                  )
                RETURNING order_id, amount, items, checkbox_receipt_id
                """,
                (keycrm_order_id,),
            )

            return cursor.fetchone()


def keycrm_cancelled_status_ids() -> set[int]:

    configured = {
        int(value.strip())
        for value in os.getenv("KEYCRM_CANCELLED_STATUS_IDS", "").split(",")
        if value.strip().isdigit()
    }

    if configured:

        return configured

    payload = keycrm_request("order/status", {"limit": 50, "page": 1})
    statuses = payload.get("data", []) if isinstance(payload, dict) else []
    cancelled = set()

    for status in statuses:

        name = str(status.get("name") or "").strip().casefold()
        status_id = str(status.get("id") or "").strip()

        if (
            status_id.isdigit()
            and any(
                marker in name
                for marker in ("скасован", "отмен", "cancel")
            )
        ):

            cancelled.add(int(status_id))

    return cancelled


def keycrm_delivered_status_ids() -> set[int]:

    configured = {
        int(value.strip())
        for value in os.getenv("KEYCRM_DELIVERED_STATUS_IDS", "").split(",")
        if value.strip().isdigit()
    }

    if configured:

        return configured

    payload = keycrm_request("order/status", {"limit": 50, "page": 1})
    statuses = payload.get("data", []) if isinstance(payload, dict) else []
    delivered = set()
    exact_names = {
        "отримано",
        "отриманий",
        "отримана",
        "забрано",
        "забраний",
        "виконано",
        "виконаний",
        "delivered",
        "completed",
    }

    for status in statuses:

        name = str(status.get("name") or "").strip().casefold()
        status_id = str(status.get("id") or "").strip()

        if status_id.isdigit() and (
            name in exact_names
            or any(
                marker in name
                for marker in (
                    "отримано клієнтом",
                    "отриман покупателем",
                    "успішно доставлено",
                    "успешно доставлен",
                )
            )
        ):

            delivered.add(int(status_id))

    return delivered


def store_invoice_total_from_items(items, fallback) -> Decimal:

    for item in items or []:

        for prop in item.get("properties") or []:

            if str(prop.get("name") or "").strip() != "Повна сума замовлення":

                continue

            value = str(prop.get("value") or "").replace("UAH", "").strip()

            try:

                return Decimal(value).quantize(Decimal("0.01"))

            except (InvalidOperation, ValueError):

                pass

    return Decimal(str(fallback or 0)).quantize(Decimal("0.01"))


def store_invoice_product_names(store_items, receipt_items, description) -> str:

    names = [
        str(item.get("name") or "").strip()
        for item in store_items or []
        if str(item.get("name") or "").strip()
    ]

    if not names:

        names = [
            str(item.get("name") or "")
            .replace("Передоплата за замовлення:", "", 1)
            .strip()
            for item in receipt_items or []
            if str(item.get("name") or "").strip()
        ]

    return ", ".join(names) or str(description or "Товар").strip()


def claim_store_delivery_fiscalization(keycrm_order_id: int):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET delivery_checkbox_status = 'processing',
                    delivery_checkbox_error = NULL,
                    updated_at = NOW()
                WHERE keycrm_order_id = %s
                  AND created_by_name = 'Flawless website'
                  AND status = 'success'
                  AND (
                      store_payment_type = 'prepay'
                      OR EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements(COALESCE(items, '[]'::jsonb)) AS item
                          WHERE item->>'id' = 'prepayment'
                      )
                  )
                  AND delivery_checkbox_receipt_id IS NULL
                  AND COALESCE(delivery_checkbox_status, 'new')
                      NOT IN ('processing', 'created')
                RETURNING order_id, amount, store_order_total,
                          store_order_items, items, description
                """,
                (keycrm_order_id,),
            )

            return cursor.fetchone()


def mark_store_delivery_fiscalization(
    order_id,
    status,
    receipt_id=None,
    error=None,
):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                UPDATE invoices
                SET delivery_checkbox_status = %s,
                    delivery_checkbox_receipt_id =
                        COALESCE(%s, delivery_checkbox_receipt_id),
                    delivery_checkbox_error = %s,
                    delivery_fiscalized_at = CASE
                        WHEN %s = 'created' THEN NOW()
                        ELSE delivery_fiscalized_at
                    END,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (status, receipt_id, error, status, order_id),
            )


def fiscalize_delivered_store_order(keycrm_order_id: int) -> str:

    invoice = claim_store_delivery_fiscalization(keycrm_order_id)

    if not invoice:

        return "ignored"

    order_id, prepaid_amount, stored_total, store_items, receipt_items, description = invoice
    total = (
        Decimal(str(stored_total)).quantize(Decimal("0.01"))
        if stored_total is not None
        else store_invoice_total_from_items(receipt_items, prepaid_amount)
    )
    balance = max(
        total - Decimal(str(prepaid_amount)),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))

    if balance <= 0:

        mark_store_delivery_fiscalization(order_id, "created")
        return "no_balance"

    product_names = store_invoice_product_names(
        store_items,
        receipt_items,
        description,
    )
    final_name = f"Післяплата за {product_names}"[:255]
    fiscal_items = [
        {
            "name": final_name,
            "fiscal_name": final_name,
            "price": float(balance),
        }
    ]

    try:

        receipt_id = fiscalize_checkbox_receipt(
            f"{order_id}:delivery",
            fiscal_items,
            balance,
        )
        mark_store_delivery_fiscalization(
            order_id,
            "created",
            receipt_id=receipt_id,
        )

    except Exception as error:

        mark_store_delivery_fiscalization(
            order_id,
            "error",
            error=str(error)[:500],
        )
        raise

    for user_id in ALLOWED_USER_IDS:

        try:

            bot.send_message(
                user_id,
                "📦 <b>Замовлення з сайту отримано</b>\n"
                f"Замовлення CRM: <b>#{keycrm_order_id}</b>\n"
                f"Товар: {html.escape(product_names)}\n"
                f"Фіскалізовано залишок: <b>{balance:.2f} UAH</b>",
            )

        except Exception:

            pass

    return "fiscalized"


def refund_cancelled_store_order(keycrm_order_id: int) -> str:

    invoice = claim_store_invoice_for_refund(keycrm_order_id)

    if not invoice:

        return "ignored"

    order_id, amount, items, checkbox_receipt_id = invoice

    try:

        result = refund_liqpay_payment(order_id, amount)

        if result.get("result") != "ok":

            raise RuntimeError(
                result.get("err_description")
                or result.get("status")
                or str(result)
            )

    except Exception as error:

        mark_refund_failed(order_id, str(error))
        raise

    if not checkbox_receipt_id:

        mark_refund_receipt(
            order_id,
            "completed",
            error="Original Checkbox receipt was not found",
        )
        return "refunded_without_receipt"

    mark_refund_receipt(order_id, "receipt_pending")

    try:

        receipt_id = fiscalize_checkbox_return(order_id, items, amount)
        mark_refund_receipt(order_id, "completed", receipt_id=receipt_id)

    except Exception as error:

        # The existing retry worker will create the return receipt after a
        # temporary Checkbox error or after the cashier shift is reopened.
        mark_refund_receipt(
            order_id,
            "receipt_error",
            error=str(error)[:500],
        )

    return "refunded"


@app.route("/api/keycrm/order-status", methods=["POST"])
def keycrm_order_status_webhook():

    configured_secret = os.getenv("KEYCRM_WEBHOOK_SECRET", "").strip()
    supplied_secret = request.args.get("secret", "")

    if not configured_secret:

        return {"ok": False, "error": "Webhook is not configured"}, 503

    if not hmac.compare_digest(configured_secret, supplied_secret):

        return {"ok": False, "error": "Unauthorized"}, 401

    payload = request.get_json(silent=True) or {}

    if payload.get("event") != "order.change_order_status":

        return {"ok": True, "result": "ignored"}, 200

    context = payload.get("context") or {}
    order_id = str(context.get("id") or "").strip()
    status_id = str(context.get("status_id") or "").strip()

    if not order_id.isdigit() or not status_id.isdigit():

        return {"ok": False, "error": "Invalid KeyCRM payload"}, 400

    numeric_status_id = int(status_id)

    try:

        if numeric_status_id in keycrm_cancelled_status_ids():

            result = refund_cancelled_store_order(int(order_id))

        elif numeric_status_id in keycrm_delivered_status_ids():

            result = fiscalize_delivered_store_order(int(order_id))

        else:

            return {"ok": True, "result": "ignored"}, 200

    except Exception as error:

        print(
            "KeyCRM store status automation failed:",
            int(order_id),
            str(error),
        )
        return {"ok": False, "error": "Refund failed"}, 500

    return {"ok": True, "result": result}, 200


def pending_store_delivery_fiscalizations(limit: int = 50) -> list[int]:

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT keycrm_order_id
                FROM invoices
                WHERE created_by_name = 'Flawless website'
                  AND delivery_checkbox_receipt_id IS NULL
                  AND delivery_checkbox_status = 'error'
                  AND keycrm_order_id IS NOT NULL
                ORDER BY updated_at
                LIMIT %s
                """,
                (limit,),
            )

            return [int(row[0]) for row in cursor.fetchall()]


def store_delivery_fiscalization_retry_worker():

    while True:

        try:

            if checkbox_shift_is_open():

                for keycrm_order_id in pending_store_delivery_fiscalizations():

                    try:

                        fiscalize_delivered_store_order(keycrm_order_id)

                    except Exception as error:

                        print(
                            "Store delivery fiscalization retry failed:",
                            keycrm_order_id,
                            str(error),
                        )

        except Exception as error:

            print(f"Store delivery retry worker failed: {error}")

        time.sleep(60)

init_db()
ensure_store_invoice_columns()

threading.Thread(
    target=checkbox_retry_worker,
    name="checkbox-retry",
    daemon=True,
).start()

threading.Thread(
    target=store_delivery_fiscalization_retry_worker,
    name="store-delivery-fiscalization-retry",
    daemon=True,
).start()

# The webhook is configured once, after the final live app is selected below.

# This second application contains the complete refund and website workflows.
LIVE_BOT = bot
LIVE_APP = app

if __name__ == "__main__" and False:

    print(f"Flawless LiqPay bot запущен на порту {PORT}")

    app.run(host="0.0.0.0", port=PORT)
import os

import json

import time

import threading

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

KEYCRM_API_KEY = os.getenv("KEYCRM_API_KEY", "")

KEYCRM_API_URL = "https://openapi.keycrm.app/v1"
PRODUCT_CATALOG_CACHE = {"expires_at": 0, "payload": None}

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
                    ADD COLUMN IF NOT EXISTS fiscalized_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS liqpay_payment_id TEXT
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

def phone_message_line(phone: str) -> str:

    phone_for_display = display_phone(phone)

    if not phone_for_display:

        return ""

    return f"Телефон: <code>{html.escape(phone_for_display)}</code>\n"

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

def extract_liqpay_callback_phone(callback_data: dict) -> str:

    if not isinstance(callback_data, dict):

        return ""

    phone_keys = {
        "phone",
        "sender_phone",
        "customer_phone",
        "payer_phone",
        "client_phone",
        "card_phone",
    }

    def walk(value):

        if isinstance(value, dict):

            for key, nested_value in value.items():

                if str(key).lower() in phone_keys:

                    phone = clean_phone(str(nested_value or ""))

                    if phone:

                        return phone

                phone = walk(nested_value)

                if phone:

                    return phone

        if isinstance(value, list):

            for item in value:

                phone = walk(item)

                if phone:

                    return phone

        return ""

    return walk(callback_data)

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

def liqpay_checkout_url(params: dict) -> str:

    json_string = json.dumps(params, ensure_ascii=False, separators=(",", ":"))

    data_b64 = base64.b64encode(json_string.encode("utf-8")).decode("utf-8")

    signature = make_signature(data_b64)

    return "https://www.liqpay.ua/api/3/checkout?" + urllib.parse.urlencode({
        "data": data_b64,
        "signature": signature,
    })

def create_invoice(amount: str, description: str, phone: str = "") -> tuple[str, dict]:

    order_id = f"flawless_{int(time.time())}_{secrets.token_hex(4)}"

    params = {

        "version": 3,

        "public_key": LIQPAY_PUBLIC_KEY,

        "action": "pay",

        "amount": amount,

        "currency": CURRENCY,

        "description": description,

        "order_id": order_id,

        "language": "uk",

        "server_url": f"{WEBHOOK_URL}/liqpay/callback",

        "paytypes": "card,apay,gpay",

    }

    return order_id, {
        "status": "checkout_url",
        "href": liqpay_checkout_url(params),
    }

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

def checkbox_shift_is_open() -> bool:

    if not CHECKBOX_LICENSE_KEY or not CHECKBOX_PIN_CODE:

        return False

    token = checkbox_signin()
    response = requests.get(
        f"{CHECKBOX_API_URL}/cashier/shift",
        headers=checkbox_headers(token),
        timeout=20,
    )

    if response.status_code == 404:

        return False

    try:

        result = response.json()

    except Exception:

        response.raise_for_status()
        return False

    if response.status_code >= 400:

        return False

    shift = result.get("shift") if isinstance(result, dict) else None
    shift_status = (
        shift.get("status")
        if isinstance(shift, dict)
        else result.get("status") if isinstance(result, dict) else None
    )

    return str(shift_status).upper() == "OPENED"

def checkbox_good_code(name: str) -> str:

    return hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()[:16]

def fiscalize_checkbox_receipt(order_id: str, items: list, amount) -> str:

    receipt_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"flawless-checkbox:{order_id}")
    )

    goods = []

    for item in items:

        fiscal_name = item.get("fiscal_name") or item["name"]
        price_cents = int(
            (Decimal(str(item["price"])) * 100).quantize(Decimal("1"))
        )

        goods.append(
            {
                "good": {
                    "code": checkbox_good_code(fiscal_name),
                    "name": fiscal_name[:255],
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

def get_pending_checkbox_invoices(limit: int = 50):

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT order_id
                FROM invoices
                WHERE status = 'success'
                  AND checkbox_receipt_id IS NULL
                  AND checkbox_status = 'error'
                ORDER BY updated_at
                LIMIT %s
                """,
                (limit,),
            )

            return [row[0] for row in cursor.fetchall()]

def retry_pending_checkbox_receipts():

    if not checkbox_shift_is_open():

        return

    for order_id in get_pending_checkbox_invoices():

        invoice_to_fiscalize = claim_invoice_for_fiscalization(order_id)

        if not invoice_to_fiscalize:

            continue

        items, invoice_amount = invoice_to_fiscalize

        if not items:

            mark_checkbox_receipt(
                order_id,
                "error",
                error="Invoice has no structured items",
            )
            continue

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

        except Exception as error:

            mark_checkbox_receipt(
                order_id,
                "error",
                error=str(error)[:500],
            )

def checkbox_retry_worker():

    while True:

        try:

            retry_pending_checkbox_receipts()

        except Exception as error:

            print(f"Checkbox retry failed: {error}")

        time.sleep(60)

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
                       status, short_code, created_by_name, created_at,
                       liqpay_payment_id
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

    markup.add(telebot.types.KeyboardButton("Возврат"))

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

def amount_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    markup.row(
        telebot.types.KeyboardButton("150"),
        telebot.types.KeyboardButton("590"),
    )

    for left_amount, right_amount in (
        ("990", "891"),
        ("890", "801"),
        ("650", "585"),
        ("690", "621"),
        ("790", "711"),
        ("1590", "1431"),
    ):

        markup.row(
            telebot.types.KeyboardButton(left_amount),
            telebot.types.KeyboardButton(right_amount),
        )

    return markup

def product_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    for product_name in (
        "Штани шовк на резиночці",
        "Комбінезон",
        "Боді з мереживом літо",
        "Футболка бавовна",
        "Сукня з комірцем",
        "Комплект піджак брюки та жилет",
        "Боді принтоване",
    ):

        markup.add(telebot.types.KeyboardButton(product_name))

    return markup

def jumpsuit_menu():

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    for product_name in (
        "Комбінезон - сукня трикотаж",
        "Комбінезон кльош майкою",
        "Комбінезон короткий рукав трикотаж",
        "Комбінезон біфлекс",
        "Комбінезон з вирізом",
    ):

        markup.add(telebot.types.KeyboardButton(product_name))

    markup.add(telebot.types.KeyboardButton("⬅️ До списку товарів"))

    return markup

def ask_item_price(chat_id: int, item_number: int):

    bot.send_message(
        chat_id,
        f"Выберите сумму товара №{item_number} или введите другую вручную:",
        reply_markup=amount_menu(),
    )

def ask_item_name(chat_id: int):

    bot.send_message(
        chat_id,
        "Выберите товар из списка или введите другое название вручную:",
        reply_markup=product_menu(),
    )

def add_item_and_show_actions(
    chat_id: int,
    data: dict,
    product_name: str,
    fiscal_name: str = None,
):

    item = {
        "name": product_name,
        "price": data.pop("pending_item_price"),
    }

    if fiscal_name:

        item["fiscal_name"] = fiscal_name

    data["items"].append(item)
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

@bot.message_handler(commands=["refund"])

def final_refund_command(message):

    ask_refund_phone(message)

@bot.message_handler(
    func=lambda message: any(
        marker in str(message.text or "").strip().casefold()
        for marker in ("возврат", "повернен")
    )
)

def final_refund_button(message):

    ask_refund_phone(message)

@bot.callback_query_handler(func=lambda call: call.data.startswith("refund:"))

def final_refund_confirmation(call):

    ask_refund_confirmation(call)

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_refund:")
)

def final_confirm_refund_payment(call):

    confirm_refund_payment(call)

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
        liqpay_payment_id,
    ) in invoices:

        phone_for_display = display_phone(phone)
        payment_link = make_short_link(short_code) if short_code else None

        item = (
            f"<b>{created_at.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"{status_label(status)}\n"
            f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
            f"{phone_message_line(phone)}"
            f"Описание: {html.escape(description)}\n"
            f"Создал: {html.escape(created_by_name)}\n"
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id or '—')}</code>\n"
            f"ID: <code>{html.escape(order_id)}</code>"
        )

        if payment_link:

            item += f"\n{html.escape(payment_link)}"

        copy_markup = telebot.types.InlineKeyboardMarkup()

        if phone_for_display:

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

    user_steps[message.chat.id] = {
        "step": "item_price",
        "phone": "",
        "items": [],
    }

    bot.send_message(
        message.chat.id,
        "Номер телефона клиента сейчас не спрашиваем.\n"
        "Создаём инвойс без привязки к телефону.",
    )

    ask_item_price(message.chat.id, 1)

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

        data["step"] = "item_price"

        user_steps[chat_id] = data

        ask_item_price(chat_id, 1)

        return

    if step == "item_price":

        price_text = text.replace(" ", "").replace(",", ".")

        try:

            price = Decimal(price_text)

            if price <= 0:

                raise InvalidOperation

        except (InvalidOperation, ValueError):

            bot.send_message(
                chat_id,
                "Сумма должна быть числом. Выберите кнопку или введите сумму вручную.",
                reply_markup=amount_menu(),
            )

            return

        price = price.quantize(Decimal("0.01"))

        price_display = (
            str(int(price))
            if price == price.to_integral_value()
            else f"{price:.2f}"
        )

        data["pending_item_price"] = price_display

        if price_display == "150":

            add_item_and_show_actions(
                chat_id,
                data,
                product_name="Одяг",
                fiscal_name="Шкарпетки",
            )

            return

        data["step"] = "item_name"

        user_steps[chat_id] = data

        ask_item_name(chat_id)

        return

    if step == "item_name":

        if not text:

            bot.send_message(chat_id, "Введите название товара.")

            return

        if text == "Комбінезон":

            bot.send_message(
                chat_id,
                "Выберите вариант комбинезона:",
                reply_markup=jumpsuit_menu(),
            )

            return

        if text == "⬅️ До списку товарів":

            ask_item_name(chat_id)

            return

        add_item_and_show_actions(chat_id, data, product_name=text)

        return

    if step == "item_action":

        if text == "➕ Добавить ещё товар":

            data["step"] = "item_price"

            user_steps[chat_id] = data

            ask_item_price(chat_id, len(data["items"]) + 1)

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

        msg = (

            "✅ <b>Инвойс создан</b>\n\n"

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

        created_invoice_markup = telebot.types.InlineKeyboardMarkup()

        if invoice_saved:

            created_invoice_markup.add(
                telebot.types.InlineKeyboardButton(
                    text="❌ Скасувати інвойс",
                    callback_data=f"cancel:{order_id}",
                )
            )

        bot.send_message(
            chat_id,
            msg,
            reply_markup=(
                created_invoice_markup
                if invoice_saved
                else main_menu()
            ),
        )

        if href:

            client_message = (
                "Ваше замовлення сформоване 🌸 "
                f"Швидка оплата за посиланням : {short_link}\n"
                "Або можемо надати реквізити iban"
            )

            bot.send_message(
                chat_id,
                client_message,
                disable_web_page_preview=True,
                reply_markup=main_menu(),
            )

        elif invoice_saved:

            bot.send_message(
                chat_id,
                "Инвойс можно отменить кнопкой выше или через раздел «История».",
                reply_markup=main_menu(),
            )

@bot.message_handler(func=lambda message: True)

def fallback(message):

    bot.send_message(

        message.chat.id,

        "Я умею создавать инвойсы LiqPay.\nНажми <b>Создать инвойс</b> или отправь /invoice.",

        reply_markup=main_menu()

    )

def keycrm_request(path: str, params=None):

    if not KEYCRM_API_KEY:

        raise RuntimeError("KEYCRM_API_KEY is missing")

    response = requests.get(
        f"{KEYCRM_API_URL}/{path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {KEYCRM_API_KEY}",
        },
        params=params or {},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()

def keycrm_product_image(product: dict) -> str:

    for field in (
        "thumbnail_url",
        "image",
        "image_url",
        "picture",
        "picture_url",
        "thumbnail",
    ):

        value = product.get(field)

        if isinstance(value, str) and value.startswith("http"):

            return value

        if isinstance(value, dict):

            url = value.get("url") or value.get("src")

            if isinstance(url, str) and url.startswith("http"):

                return url

    for field in ("attachments_data", "images", "pictures", "photos"):

        for image in product.get(field) or []:

            if isinstance(image, str) and image.startswith("http"):

                return image

            if isinstance(image, dict):

                url = image.get("url") or image.get("src") or image.get("thumbnail")

                if isinstance(url, str) and url.startswith("http"):

                    return url

    return ""

def keycrm_number(value) -> float:

    try:

        return float(str(value or "").replace(" ", "").replace(",", "."))

    except (TypeError, ValueError):

        return 0

def keycrm_product_price(product: dict) -> float:

    for field in ("price", "price_min", "min_price", "sale_price"):

        value = keycrm_number(product.get(field))

        if value > 0:

            return value

    prices = []

    for offer in product.get("offers") or []:

        for field in ("price", "sale_price", "price_min"):

            value = keycrm_number(offer.get(field))

            if value > 0:

                prices.append(value)
                break

    return min(prices) if prices else 0

def keycrm_product_category(product: dict) -> str:

    category = product.get("category") or {}
    raw_name = (
        category.get("name")
        if isinstance(category, dict)
        else category
    ) or product.get("category_name") or ""
    name = str(raw_name).lower()

    if "сук" in name or "dress" in name:

        return "dresses"

    if "костюм" in name or "комплект" in name or "set" in name:

        return "sets"

    if "топ" in name or "бод" in name or "футбол" in name:

        return "tops"

    return "all"

def keycrm_product_subcategory(name: str) -> str:

    normalized = name.lower()
    groups = (
        (("комбінез", "комбинез"), "Комбінезони"),
        (("боді", "боди"), "Боді"),
        (("сукн", "плать"), "Сукні"),
        (("костюм", "комплект"), "Костюми та комплекти"),
        (("футбол",), "Футболки"),
        (("сороч", "рубаш"), "Сорочки"),
        (("штан", "брюк", "палац"), "Штани"),
        (("спідниц", "юбк"), "Спідниці"),
        (("топ",), "Топи"),
        (("піджак", "жакет", "блейзер"), "Піджаки та жилети"),
    )

    for fragments, label in groups:

        if any(fragment in normalized for fragment in fragments):

            return label

    return "Інше"

def keycrm_product_gender(product: dict) -> str:

    category = product.get("category") or {}
    category_name = (
        category.get("name")
        if isinstance(category, dict)
        else category
    ) or product.get("category_name") or ""
    searchable = f"{category_name} {product.get('name') or ''}".lower()

    if any(marker in searchable for marker in ("чолов", "муж", "для нього", "men")):

        return "men"

    return "women"

def keycrm_offer_options(offers: list) -> tuple[list, list, list]:

    sizes = []
    colors = []
    variants = []

    for offer in offers:

        properties = offer.get("properties") or []
        variant = {
            "id": str(offer.get("id") or ""),
            "sku": str(offer.get("sku") or ""),
            "price": keycrm_number(offer.get("price")),
            "quantity": keycrm_number(offer.get("quantity")),
            "image": str(offer.get("thumbnail_url") or ""),
            "size": "",
            "color": "",
        }

        for prop in properties:

            if not isinstance(prop, dict):

                continue

            prop_name = str(prop.get("name") or "").strip().lower()
            prop_value = str(prop.get("value") or "").strip()

            if not prop_value:

                continue

            if any(marker in prop_name for marker in ("розм", "размер", "size")):

                variant["size"] = prop_value

                if prop_value not in sizes:

                    sizes.append(prop_value)

            if any(marker in prop_name for marker in ("колір", "цвет", "color", "colour")):

                variant["color"] = prop_value

                if prop_value not in colors:

                    colors.append(prop_value)

        variants.append(variant)

    return sizes, colors, variants

def normalize_keycrm_product(product: dict, index: int) -> dict:

    offers = product.get("offers") or []
    sizes, colors, variants = keycrm_offer_options(offers)

    palette = (
        ("#c74f67", "#68142c"),
        ("#d4c4b5", "#867161"),
        ("#9c7567", "#402b27"),
        ("#59514f", "#171313"),
        ("#d9a9af", "#8c4e5b"),
        ("#777675", "#292827"),
    )
    color_a, color_b = palette[index % len(palette)]

    return {
        "id": str(product.get("id") or product.get("uuid") or f"keycrm-{index + 1}"),
        "name": str(product.get("name") or product.get("title") or "Товар").strip(),
        "meta": " · ".join(sizes[:6]),
        "price": keycrm_product_price(product),
        "category": keycrm_product_category(product),
        "gender": keycrm_product_gender(product),
        "subcategory": keycrm_product_subcategory(
            str(product.get("name") or product.get("title") or "")
        ),
        "tag": "",
        "tags": [],
        "image": keycrm_product_image(product),
        "sizes": sizes,
        "colors": colors,
        "variants": variants,
        "sales_count": keycrm_number(product.get("sales_count")),
        "featured": False,
        "a": color_a,
        "b": color_b,
        "rotate": f"{(index % 7) - 3}deg",
    }

@app.route("/api/products", methods=["GET"])

def products_api():

    global PRODUCT_CATALOG_CACHE

    if (
        PRODUCT_CATALOG_CACHE["payload"] is not None
        and PRODUCT_CATALOG_CACHE["expires_at"] > time.time()
    ):
        return (
            PRODUCT_CATALOG_CACHE["payload"],
            200,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    try:

        raw_products = []
        page = 1

        while True:

            payload = keycrm_request(
                "products",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            batch = payload.get("data", []) if isinstance(payload, dict) else []
            raw_products.extend(batch)

            current_page = int(payload.get("current_page") or page)
            last_page = int(payload.get("last_page") or current_page)

            if not batch or current_page >= last_page:

                break

            page += 1

        raw_categories = []
        page = 1

        while True:

            categories_payload = keycrm_request(
                "products/categories",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            categories_batch = (
                categories_payload.get("data", [])
                if isinstance(categories_payload, dict)
                else []
            )
            raw_categories.extend(categories_batch)

            current_page = int(categories_payload.get("current_page") or page)
            last_page = int(categories_payload.get("last_page") or current_page)

            if not categories_batch or current_page >= last_page:

                break

            page += 1

        categories_by_id = {
            str(category.get("id")): category
            for category in raw_categories
            if category.get("id") is not None
        }

        def category_path(category_id):

            names = []
            seen = set()
            current_id = str(category_id or "")

            while current_id and current_id not in seen:

                seen.add(current_id)
                category = categories_by_id.get(current_id)

                if not category:

                    break

                name = str(category.get("name") or "").strip()

                if name:

                    names.append(name)

                current_id = str(category.get("parent_id") or "")

            return " / ".join(reversed(names))

        for product in raw_products:

            product["category_name"] = category_path(product.get("category_id"))

        raw_offers = []
        page = 1

        while True:

            offers_payload = keycrm_request(
                "offers",
                {
                    "limit": 50,
                    "page": page,
                },
            )
            offers_batch = (
                offers_payload.get("data", [])
                if isinstance(offers_payload, dict)
                else []
            )
            raw_offers.extend(offers_batch)

            current_page = int(offers_payload.get("current_page") or page)
            last_page = int(offers_payload.get("last_page") or current_page)

            if not offers_batch or current_page >= last_page:

                break

            page += 1

        offers_by_product = {}

        for offer in raw_offers:

            product_id = str(offer.get("product_id") or "")
            offers_by_product.setdefault(product_id, []).append(offer)

        for product in raw_products:

            product["offers"] = offers_by_product.get(
                str(product.get("id") or product.get("uuid") or ""),
                [],
            )

        sales_by_product = {}
        page = 1

        while page <= 1:

            orders_payload = keycrm_request(
                "order",
                {
                    "limit": 50,
                    "page": page,
                    "sort": "-id",
                    "include": "products.offer",
                },
            )
            orders_batch = (
                orders_payload.get("data", [])
                if isinstance(orders_payload, dict)
                else []
            )

            for order in orders_batch:

                if order.get("payment_status") not in ("paid", "overpaid"):

                    continue

                for order_product in order.get("products") or []:

                    offer = order_product.get("offer") or {}
                    product_id = str(offer.get("product_id") or "")

                    if not product_id:

                        continue

                    sales_by_product[product_id] = (
                        sales_by_product.get(product_id, 0)
                        + keycrm_number(order_product.get("quantity") or 1)
                    )

            current_page = int(orders_payload.get("current_page") or page)
            last_page = int(orders_payload.get("last_page") or current_page)

            if not orders_batch or current_page >= last_page:

                break

            page += 1

        for product in raw_products:

            product["sales_count"] = sales_by_product.get(
                str(product.get("id") or product.get("uuid") or ""),
                0,
            )

        products = [
            normalize_keycrm_product(product, index)
            for index, product in enumerate(raw_products)
        ]
        products = [
            product
            for product in products
            if product["name"] and product["price"] > 0
        ]

        newest_products = sorted(
            products,
            key=lambda product: keycrm_number(product.get("id")),
            reverse=True,
        )[:8]

        for product in newest_products:

            product["tags"].append("New")

        for gender in ("women", "men"):

            gender_products = [
                product for product in products if product["gender"] == gender
            ]
            ranked = sorted(
                gender_products,
                key=lambda product: (
                    product["sales_count"],
                    keycrm_number(product.get("id")),
                ),
                reverse=True,
            )

            for product in ranked[:8]:

                product["featured"] = True
                product["tags"].append("Bestseller")

        for product in products:

            product["tags"] = list(dict.fromkeys(product["tags"]))
            product["tag"] = product["tags"][0] if product["tags"] else ""

        payload = {
            "source": "keycrm",
            "count": len(products),
            "products": products,
        }
        PRODUCT_CATALOG_CACHE = {
            "expires_at": time.time() + 300,
            "payload": payload,
        }

        return (
            payload,
            200,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    except Exception as error:

        print(f"KeyCRM products failed: {error}")
        return (
            {"error": "Products are temporarily unavailable"},
            502,
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
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
    liqpay_payment_id = str(
        callback_data.get("payment_id")
        or callback_data.get("transaction_id")
        or callback_data.get("liqpay_order_id")
        or ""
    ) or None
    if not order_id:

        return "Missing order_id", 400

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                """
                WITH previous AS (
                    SELECT status
                    FROM invoices
                    WHERE order_id = %s
                    FOR UPDATE
                )
                UPDATE invoices
                SET status = %s,
                    liqpay_payment_id = COALESCE(%s, liqpay_payment_id),
                    updated_at = NOW()
                WHERE order_id = %s
                RETURNING amount, currency, phone, description, items,
                          keycrm_order_id, created_by_name,
                          (SELECT status FROM previous) AS previous_status
                """,
                (order_id, status, liqpay_payment_id, order_id),
            )

            updated_invoice = cursor.fetchone()

    if status in {"reversed", "failure", "error"} and updated_invoice:

        amount, currency, phone, description, items, keycrm_order_id, created_by_name, previous_status = updated_invoice
        product_names = format_product_names(items, description)
        reason = (
            callback_data.get("err_description")
            or callback_data.get("err_code")
            or callback_data.get("result")
            or callback_data.get("description")
            or "LiqPay не передав причину"
        )

        for user_id in (
            [] if created_by_name == "Flawless website" else ALLOWED_USER_IDS
        ):

            try:

                bot.send_message(
                    user_id,
                    "↩️ <b>LiqPay повернув/відхилив платіж</b>\n"
                    f"Статус: <code>{html.escape(status)}</code>\n"
                    f"Товар: {html.escape(product_names)}\n"
                    f"Сума: <b>{amount} {html.escape(currency)}</b>\n"
                    f"ID оплати LiqPay: <code>{html.escape(liqpay_payment_id or '')}</code>\n"
                    f"Причина: <code>{html.escape(str(reason))}</code>",
                )

            except Exception:

                pass

    if status == "success" and updated_invoice:

        amount, currency, phone, description, items, keycrm_order_id, created_by_name, previous_status = updated_invoice

        if previous_status == "success":

            return "ok", 200
        phone_for_display = display_phone(phone)
        product_names = format_product_names(items, description)
        payment_id_line = (
            f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id)}</code>\n"
            if liqpay_payment_id
            else ""
        )

        if keycrm_order_id:

            try:

                store_mark_keycrm_paid(keycrm_order_id, amount)

            except Exception as error:

                print(f"KeyCRM payment sync failed: {error}")

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

                copy_phone_markup = None

                if phone_for_display:

                    copy_phone_markup = telebot.types.InlineKeyboardMarkup()
                    copy_phone_markup.add(
                        telebot.types.InlineKeyboardButton(
                            text=f"📋 Копировать {phone_for_display}",
                            copy_text=telebot.types.CopyTextButton(
                                text=phone_for_display,
                            ),
                        )
                    )

                notification_title = (
                    "✅ <b>Замовлення з сайту оплачено</b>"
                    if created_by_name == "Flawless website"
                    else "✅ <b>Инвойс оплачен</b>"
                )
                crm_order_line = (
                    f"Замовлення CRM: <b>#{keycrm_order_id}</b>\n"
                    if keycrm_order_id
                    else ""
                )

                bot.send_message(
                    user_id,
                    f"{notification_title}\n"
                    f"{crm_order_line}"
                    f"{phone_message_line(phone)}"
                    f"Товар: {html.escape(product_names)}\n"
                    f"Сумма: <b>{amount} {html.escape(currency)}</b>\n"
                    f"{payment_id_line}"
                    f"{checkbox_message}",
                    reply_markup=copy_phone_markup,
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

if "store_checkout_live" not in app.view_functions:

    app.add_url_rule(
        "/api/store/checkout",
        endpoint="store_checkout_live",
        view_func=store_checkout,
        methods=["POST", "OPTIONS"],
    )

bot = LIVE_BOT
app = LIVE_APP
setup_webhook()

if "keycrm_order_status_webhook_live" not in app.view_functions:

    app.add_url_rule(
        "/api/keycrm/order-status",
        endpoint="keycrm_order_status_webhook_live",
        view_func=keycrm_order_status_webhook,
        methods=["POST"],
    )

if __name__ == "__main__":

    print(f"Flawless LiqPay bot запущен на порту {PORT}")

    app.run(host="0.0.0.0", port=PORT)
