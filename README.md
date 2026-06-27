# Flawless LiqPay Telegram Bot

Простой Telegram-бот для быстрого создания ссылок на оплату LiqPay.

## Что умеет

1. Менеджер нажимает "Создать счет".
2. Вводит сумму.
3. Вводит описание заказа.
4. Бот создает ссылку LiqPay и кнопку оплаты.

## Переменные в Render

Добавить в Environment:

- TELEGRAM_BOT_TOKEN
- LIQPAY_PUBLIC_KEY
- LIQPAY_PRIVATE_KEY
- WEBHOOK_URL

WEBHOOK_URL — это адрес сервиса Render, например:

```text
https://flawless-liqpay-bot.onrender.com
```

## Render настройки

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
python main.py
```

## Важно

Не публикуйте TELEGRAM_BOT_TOKEN и LIQPAY_PRIVATE_KEY.
Если токен попал на скриншот, обновите его через BotFather командой /revoke.