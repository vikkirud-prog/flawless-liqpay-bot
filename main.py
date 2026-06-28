send_message(chat_id, "Ок, создание инвойса отменено.", reply_markup=main_menu())
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
    liqpay_url = SHORT_LINKS.get(code)

    if not liqpay_url:
        return "Ссылка не найдена или уже недействительна", 404

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


if name == "__main__":
    print(f"Беспрооблемный веб-хук-бот LiqPay запущен на порту {PORT}")
    app.run(host="0.0.0.0", port=PORT)
