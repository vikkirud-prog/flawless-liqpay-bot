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

SHORT_LINKS = {}

def is_allowed(message):

    return True

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

    code = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:7]

    SHORT_LINKS[code] = original_url

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

    if text.
