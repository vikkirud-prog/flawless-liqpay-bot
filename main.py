import os
import time
import json
import base64
import hashlib
import logging
from urllib.parse import quote_plus

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LIQPAY_PUBLIC_KEY = os.getenv("LIQPAY_PUBLIC_KEY")
LIQPAY_PRIVATE_KEY = os.getenv("LIQPAY_PRIVATE_KEY")

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


def make_liqpay_link(amount: str, description: str) -> str:
    clean_amount = str(amount).replace(",", ".").strip()
    order_id = f"flawless_{int(time.time())}"

    payload = {
        "version": 3,
        "public_key": LIQPAY_PUBLIC_KEY,
        "action": "pay",
        "amount": clean_amount,
        "currency": "UAH",
        "description": description.strip(),
        "order_id": order_id,
        "language": "uk",
    }

    data = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")

    signature = base64.b64encode(
        hashlib.sha1(
            (LIQPAY_PRIVATE_KEY + data + LIQPAY_PRIVATE_KEY).encode("utf-8")
        ).digest()
    ).decode("utf-8")

    return f"https://www.liqpay.ua/api/checkout?data={quote_plus(data)}&signature={quote_plus(signature)}"


def main_keyboard():
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
        reply_markup=main_keyboard()
    )


@dp.callback_query(lambda c: c.data == "create_invoice")
async def create_invoice(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(InvoiceState.waiting_amount)
    await callback.message.answer("Введите сумму в грн. Например: 1890")
    await callback.answer()


@dp.message(InvoiceState.waiting_amount)
async def get_amount(message: types.Message, state: FSMContext):
    amount_text = message.text.replace(",", ".").strip()

    try:
        amount = float(amount_text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Сумма должна быть числом. Например: 1890")
        return

    await state.update_data(amount=amount_text)
    await state.set_state(InvoiceState.waiting_description)
    await message.answer("Теперь введи описание заказа. Например: Комбинезон черный S")


@dp.message(InvoiceState.waiting_description)
async def get_description(message: types.Message, state: FSMContext):
    description = message.text.strip()
    if len(description) < 2:
        await message.answer("Описание слишком короткое. Напиши, что продаем.")
        return

    data = await state.get_data()
    amount = data["amount"]

    link = make_liqpay_link(amount, description)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {amount} грн", url=link)],
            [InlineKeyboardButton(text="💳 Создать еще счет", callback_data="create_invoice")]
        ]
    )

    await message.answer(
        f"✅ Счет готов\n\n"
        f"Сумма: {amount} грн\n"
        f"Описание: {description}\n\n"
        f"Ссылка для клиента:\n{link}",
        reply_markup=keyboard
    )
    await state.clear()


async def health(request):
    return web.Response(text="Bot is running")


async def on_startup(app):
    logging.info("Starting Telegram polling...")
    app["polling_task"] = asyncio.create_task(dp.start_polling(bot))


async def on_cleanup(app):
    logging.info("Stopping bot...")
    await bot.session.close()


import asyncio

app = web.Application()
app.router.add_get("/", health)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)