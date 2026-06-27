# Flawless LiqPay Invoice Phone Bot

Telegram-бот для создания LiqPay-инвойсов по номеру телефона клиента.

## Как работает

1. Менеджер нажимает **Создать инвойс**.
2. Вводит номер телефона клиента.
3. Вводит сумму.
4. Вводит описание заказа.
5. Бот создает инвойс через LiqPay API action `invoice_send`.

## Переменные окружения в Render

```text
BOT_TOKEN=токен_бота_из_BotFather
LIQPAY_PUBLIC_KEY=публичный_ключ_LiqPay
LIQPAY_PRIVATE_KEY=приватный_ключ_LiqPay
CURRENCY=UAH
ALLOWED_USER_IDS=твой_telegram_id
```

`ALLOWED_USER_IDS` можно оставить пустым, но безопаснее указать свой Telegram ID.

Чтобы узнать ID, нажми в боте **Мой Telegram ID** или отправь `/id`.
