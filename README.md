# Flawless LiqPay Webhook Bot

Telegram-бот для создания LiqPay-инвойсов. Версия для Render Web Service через Webhook.

## Render settings

Build Command:

```text
pip install -r requirements.txt
```

Start Command:

```text
gunicorn main:app --bind 0.0.0.0:$PORT
```

Environment Variables:

```text
BOT_TOKEN=токен Telegram-бота
LIQPAY_PUBLIC_KEY=публичный ключ LiqPay
LIQPAY_PRIVATE_KEY=приватный ключ LiqPay
CURRENCY=UAH
ALLOWED_USER_IDS=твой Telegram ID
WEBHOOK_URL=https://flawless-liqpay-bot.onrender.com
```

`WEBHOOK_URL` должен быть адресом твоего Web Service в Render без слеша в конце.
