import os
import json
import time
import base64
import hashlib
import html
import requests
import telebot
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN")
LIQPAY_PUBLIC_KEY = os.getenv("LIQPAY_PUBLIC_KEY")
LIQPAY_PRIVATE_KEY = os.getenv("LIQPAY_PRIVATE_KEY")
CURRENCY = os.getenv("CURRENCY", "UAH")
ALLOWED_USER_IDS = os.getenv("ALLOWED_USER_IDS", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not LIQPAY_PUBLIC_KEY or not LIQPAY_PRIVATE_KEY:
    raise RuntimeError("LIQPAY_PUBLIC_KEY or LIQPAY_PRIVATE_KEY is missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
user_steps = {}

def is_allowed(message):
    if not ALLOWED_USER_IDS:
        return True
    allowed = [x.strip() for x in ALLOWED_USER_IDS.split(",") if x.strip()]
    return str(message.from_user.id) in allowed

def clean_phone(phone: str) -> str:
    phone = phone.strip()
    for symbol in [" ", "-", "(", ")", "."]:
        phone = phone.replace(symbol, "")
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "38" + phone
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

def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Создать инвойс"))
    markup.add(types.KeyboardButton("Мой Telegram ID"))
    return markup

@bot.message_handler(commands=["start"])
def start(message):
    if not is_allowed(message):
        bot.reply_to(message, "⛔ У тебя нет доступа к этому боту.")
        return

    bot.send_message(
        message.chat.id,
        "Привет! Я бот Flawless для создания LiqPay-инвойсов.\n\n"
        "Нажми <b>Создать инвойс</b> или отправь /invoice.",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["id"])
def show_id_command(message):
    bot.reply_to(message, f"Твой Telegram ID: <code>{message.from_user.id}</code>")

@bot.message_handler(func=lambda message: message.text == "Мой Telegram ID")
def show_id_button(message):
    bot.reply_to(message, f"Твой Telegram ID: <code>{message.from_user.id}</code>")

@bot.message_handler(commands=["invoice"])
def invoice_command(message):
    ask_phone(message)

@bot.message_handler(func=lambda message: message.text == "Создать инвойс")
def invoice_button(message):
    ask_phone(message)

def ask_phone(message):
    if not is_allowed(message):
        bot.reply_to(message, "⛔ У тебя нет доступа к этому боту.")
        return

    user_steps[message.chat.id] = {"step": "phone"}
    bot.send_message(
        message.chat.id,
        "Введите номер телефона клиента в формате:\n"
        "<code>380XXXXXXXXX</code>\n\n"
        "Можно также вставить с плюсом: <code>+380XXXXXXXXX</code>\n"
        "Для отмены напиши: <code>отмена</code>"
    )

@bot.message_handler(func=lambda message: message.chat.id in user_steps)
def handle_invoice_steps(message):
    if not is_allowed(message):
        bot.reply_to(message, "⛔ У тебя нет доступа к этому боту.")
        return

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
            bot.send_message(
                chat_id,
                "Похоже, номер введен неправильно. Попробуй еще раз.\n"
                "Пример: <code>380671234567</code>"
            )
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

        if amount_float.is_integer():
            amount = str(int(amount_float))
        else:
            amount = f"{amount_float:.2f}"

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
        description = text
        if description == "-":
            description = "Оплата заказа Flawless"

        phone = data["phone"]
        amount = data["amount"]

        bot.send_message(chat_id, "Создаю инвойс LiqPay…")

        try:
            result = create_invoice(phone=phone, amount=amount, description=description)
        except Exception as e:
            user_steps.pop(chat_id, None)
            safe_error = html.escape(str(e))
            bot.send_message(
                chat_id,
                "❌ Не получилось создать инвойс.\n\n"
                f"Ошибка: <code>{safe_error}</code>\n\n"
                "Проверь ключи LiqPay в Render и доступность API.",
                reply_markup=main_menu()
            )
            return

        user_steps.pop(chat_id, None)

        status = html.escape(str(result.get("status", "unknown")))
        href = result.get("href") or result.get("url") or result.get("checkout_url")
        invoice_id = result.get("invoice_id") or result.get("order_id") or result.get("id")

        safe_description = html.escape(description)
        safe_phone = html.escape(phone)
        safe_amount = html.escape(amount)

        msg = (
            "✅ <b>Инвойс создан</b>\n\n"
            f"Телефон: <code>{safe_phone}</code>\n"
            f"Сумма: <b>{safe_amount} {html.escape(CURRENCY)}</b>\n"
            f"Описание: {safe_description}\n"
            f"Статус: <code>{status}</code>\n"
        )

        if invoice_id:
            msg += f"ID: <code>{html.escape(str(invoice_id))}</code>\n"

        if href:
            msg += f"\nСсылка на оплату:\n{html.escape(str(href))}"
        else:
            msg += "\nLiqPay мог отправить счет клиенту по телефону, но ссылку в ответе не вернул."

        bot.send_message(chat_id, msg, reply_markup=main_menu())

@bot.message_handler(func=lambda message: True)
def fallback(message):
    if not is_allowed(message):
        bot.reply_to(message, "⛔ У тебя нет доступа к этому боту.")
        return

    bot.send_message(
        message.chat.id,
        "Я умею создавать инвойсы LiqPay.\nНажми <b>Создать инвойс</b> или отправь /invoice.",
        reply_markup=main_menu()
    )

if __name__ == "__main__":
    print("Flawless LiqPay bot started")
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
