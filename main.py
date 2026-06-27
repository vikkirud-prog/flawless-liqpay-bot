import os
import time
import json
import base64
import hashlib
import logging
from urllib.parse import quote_plus

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LIQPAY_PUBLIC_KEY = os.getenv("LIQPAY_PUBLIC_KEY")
LIQPAY_PRIVATE_KEY = os.getenv("LIQPAY_PRIVATE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например: https://flawless-liqpay-bot.onrender.com
WEBHOOK_PATH = "/webhook"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
if not LIQPAY_PUBLIC_KEY:
    raise RuntimeError("LIQPAY_PUBLIC_KEY is missing")
if not LIQPAY_PRIVATE_KEY:
    raise RuntimeError("LIQPAY_PRIVATE_KEY is missing")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class InvoiceState(StatesGroup):
    waiting_amount = State()
    waiting_description = State()


def normalize_amount(amount_text: str) -> str:
    amount_text = amount_text.replace(",", ".").strip()
    value = float(amount_text)
    if value <= 0:
        raise ValueError("Amount must be positive")
    # LiqPay принимает сумму как число. Оставляем 2 знака, если есть копейки.
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"


def make_liqpay_link(amount: str, description: str) -> str:
    order_id = f"flawless_{int(time.time())}"

    payload = {
        "version": 3,
        "public_key": LIQPAY_PUBLIC_KEY,
        "action": "pay",
        "amount": amount,
        "currency": "UAH",
        "description": description.strip(),
        "order_id": order_id,
        "language": "uk",
    }

    data = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8")

    signature = base64.b64encode(
        hashlib.sha1(
            (LIQPAY_PRIVATE_KEY + data + LIQPAY_PRIVATE_KEY).encode("utf-8")
        ).digest()
    ).decode("utf-8")

    return f"https://www.liqpay.ua/api/checkout?data={quote_plus(data)}&signature={quote_plus(signature)}"


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Создать счет", callback_data="create_invoice")]
        ]
    )


@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer(
        "Привет! Я бот для быстрых счетов LiqPay 💳\n\n"
        "Нажми кнопку ниже, введи сумму и описание заказа.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("cancel"))
async def cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, создание счета отменено.", reply_markup=main_keyboard())


@dp.callback_query(lambda c: c.data == "create_invoice")
async def create_invoice(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(InvoiceState.waiting_amount)
    await callback.message.answer("Введите сумму в грн. Например: 1890")
    await callback.answer()


@dp.message(InvoiceState.waiting_amount)
async def get_amount(message: types.Message, state: FSMContext):
    try:
        amount = normalize_amount(message.text or "")
    except Exception:
        await message.answer("Сумма должна быть числом. Например: 1890 или 1890.50")
        return

    await state.update_data(amount=amount)
    await state.set_state(InvoiceState.waiting_description)
    await message.answer("Теперь введи описание заказа. Например: Комбинезон черный S")


@dp.message(InvoiceState.waiting_description)
async def get_description(message: types.Message, state: FSMContext):
    description = (message.text or "").strip()

    if len(description) < 2:
        await message.answer("Описание слишком короткое. Напиши, что продаем.")
        return

    data = await state.get_data()
    amount = data["amount"]

    try:
        link = make_liqpay_link(amount, description)
    except Exception:
        logging.exception("Could not create LiqPay link")
        await message.answer("Не получилось создать ссылку. Проверь LiqPay ключи в Render.")
        await state.clear()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {amount} грн", url=link)],
            [InlineKeyboardButton(text="💳 Создать еще счет", callback_data="create_invoice")],
        ]
    )

    await message.answer(
        f"✅ Счет готов\n\n"
        f"Сумма: {amount} грн\n"
        f"Описание: {description}\n\n"
        f"Ссылка для клиента:\n{link}",
        reply_markup=keyboard,
    )
    await state.clear()


@dp.message()
async def fallback(message: types.Message):
    await message.answer(
        "Я пока умею создавать счета LiqPay.\n\n"
        "Нажми кнопку ниже или отправь /start.",
        reply_markup=main_keyboard(),
    )


async def health(request: web.Request):
    return web.Response(text="Flawless LiqPay bot is running ✅")


async def on_startup(bot: Bot):
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="cancel", description="Отменить создание счета"),
        ]
    )

    if WEBHOOK_URL:
        webhook_url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(webhook_url)
        logging.info("Webhook set to %s", webhook_url)
    else:
        logging.warning("WEBHOOK_URL is not set. Bot will not receive Telegram updates on Render.")


async def on_shutdown(bot: Bot):
    await bot.session.close()


def create_app() -> web.Application:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/", health)

    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    web.run_app(create_app(), host="0.0.0.0", port=port)