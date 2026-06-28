import os

import json

import time

import base64

import hashlib

import html

import secrets

import requests

import telebot

from flask import Flask, request, redirect

BOT_TOKEN = os.getenv("BOT_TOKEN")

LIQPAY_PUBLIC_KEY = os.getenv("LIQPAY_PUBLIC_KEY")

LIQPAY_PRIVATE_KEY = os.getenv("LIQPAY_PRIVATE_KEY")

CURRENCY = os.getenv("CURRENCY", "UAH")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

PORT = int(os.getenv("PORT", "10000"))

PAY_DOMAIN = "https://pay.flawless-design.com.ua"

if not BOT_TOKEN:

    raise RuntimeError("BOT_TOKEN is missing")

if not LIQPAY_PUBLIC_KEY or not LIQPAY_PRIVATE_KEY:

    raise RuntimeError("LIQPAY_PUBLIC_KEY or LIQPAY_PRIVATE_KEY is missing")

if not WEBHOOK_URL:

    raise RuntimeError("WEBHOOK_URL is missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

app = Flask(__name__)

user_steps = {}

short_links = {}

def clean_phone(phone: str) -> str:

    phone = phone.strip()

    for symbol in [" ", "-", "(", ")", "."]:

        phone = phone.replace(symbol, "")

    if phone.startswith("+"):

        phone = phone[1:]

    if phone.startswith("0") and len(phone) == 10:

        return "38" + phone

    if phone.startswith("380") and len(phone) == 12:

        return phone

    return phone

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

def create_invoice(phone: str, amount: str, description: str) -> dict:

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

    }

    return liqpay_request(params)

def make_short_link(original_url: str) -> str:

    code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]

    short_links[code] = original_url

    return f"{PAY_DOMAIN}/{code}"

def main_menu():

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add(telebot.types.KeyboardButton("Создать инвойс"))

    return markup

@bot.message_handler(commands=["start"])

def start(message):

    bot.send_message(

        message.chat.id,

        "Привет! Я бот Flawless для создания LiqPay-инвойсов.\n\n"

        "Нажми <b>Создать инвойс</b> или отправь /invoice.",

        reply_markup=main_menu()

    )

@bot.message_handler(commands=["invoice"])

def invoice_command(message):

    ask_phone(message)

@bot.message_handler(func=lambda message: message.text == "Создать инвойс")

def invoice_button(message):

    ask_phone(message)

def ask_phone(message):

    user_steps[message.chat.id] = {"step": "phone"}

    bot.send_message(

        message.chat.id,

        "Введите номер телефона клиента:\n"

        "<code>0939325197</code>\n\n"

        "Можно с плюсом или 380 — бот сам исправит.\n"

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

        phone = clean_phone(text)

        if not phone.isdigit() or len(phone) < 10 or len(phone) > 15:

            bot.send_message(chat_id, "Похоже, номер введен неправильно. Пример: <code>380671234567</code>")

            return

        data["phone"] = phone

        data["step"] = "amount"

        user_steps[chat_id] = data

        bot.send_message(chat_id, "Теперь введите сумму в грн, например:\n<code>2490</code>")

        return

    if step == "amount":

        amount = text.replace(",", ".")

        try:

            amount_float = float(amount)

            if amount_float <= 0:

                raise ValueError

        except ValueError:

            bot.send_message(chat_id, "Сумма должна быть числом. Например: <code>2490</code>")

            return

        amount = str(int(amount_float)) if amount_float.is_integer() else f"{amount_float:.2f}"

        data["amount"] = amount

        data["step"] = "description"

        user_steps[chat_id] = data

        bot.send_message(

            chat_id,

            "Введите описание платежа, например:\n"

            "<code>Комбинезон Flawless, размер M, черный</code>\n\n"

            "Или напишите <code>-</code>, если описание не нужно."

        )

        return

    if step == "description":

        description = text if text != "-" else "Оплата заказа Flawless"

        phone = data["phone"]

        amount = data["amount"]

        bot.send_message(chat_id, "Создаю инвойс LiqPay…")

        try:

            result = create_invoice(phone=phone, amount=amount, description=description)

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

        invoice_id = result.get("invoice_id") or result.get("order_id") or result.get("id")

        display_phone = "0" + phone[2:] if phone.startswith("380") else phone

        msg = (

            "✅ <b>Инвойс создан</b>\n\n"

            f"Телефон: <code>{html.escape(display_phone)}</code>\n"

            f"Сумма: <b>{html.escape(amount)} {html.escape(CURRENCY)}</b>\n"

            f"Описание: {html.escape(description)}\n"

            f"Статус: <code>{status}</code>\n"

        )

        if invoice_id:

            msg += f"ID: <code>{html.escape(str(invoice_id))}</code>\n"

        if href:

            short_link = make_short_link(str(href))

            msg += f"\n🔗 Ссылка на оплату:\n{html.escape(short_link)}"

        else:

            msg += "\nLiqPay отправил счет клиенту по телефону, но ссылку в ответе не вернул."

        bot.send_message(chat_id, msg, reply_markup=main_menu())

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

    liqpay_url = short_links.get(code)

    if not liqpay_url:
        return "Ссылка не найдена или уже недействительна", 404

    user_agent = request.headers.get("User-Agent", "").lower()

    if (
        "instagram" in user_agent
        or "facebookexternalhit" in user_agent
        or "whatsapp" in user_agent
        or "telegrambot" in user_agent
    ):
        return """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title></title>
<meta name="robots" content="noindex,nofollow">
</head>
<body></body>
</html>
"""
    return redirect(liqpay_url, code=302)
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

setup_webhook()

if __name__ == "__main__":

    print(f"Flawless LiqPay bot запущен на порту {PORT}")

    app.run(host="0.0.0.0", port=PORT)
