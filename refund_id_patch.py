from pathlib import Path


path = Path(__file__).with_name("main.py")
text = path.read_text(encoding="utf-8")

if "def extract_payment_identifier" not in text:
    text = text.replace(
        "def extract_liqpay_callback_phone(callback_data: dict) -> str:\n",
        """def extract_payment_identifier(text: str):

    value = (text or "").strip()

    if not value:

        return None

    cleaned = value.replace(" ", "")

    if re.fullmatch(r"\\d{6,30}", cleaned):

        return cleaned

    if re.fullmatch(r"[A-Za-z0-9_-]{6,80}", cleaned):

        return cleaned

    return None

def extract_liqpay_callback_phone(callback_data: dict) -> str:
""",
    )

if "def get_paid_invoice_by_payment_identifier" not in text:
    text = text.replace(
        "def get_paid_invoices_by_phone_for_refund(order_id: str):\n",
        """def get_paid_invoice_by_payment_identifier(identifier: str):

    search_value = str(identifier).strip()

    with get_db() as connection:

        with connection.cursor() as cursor:

            cursor.execute(
                \"\"\"
                SELECT order_id, phone, amount, currency, description,
                       created_at, checkbox_receipt_id, refund_status,
                       status, liqpay_payment_id
                FROM invoices
                WHERE liqpay_payment_id = %s
                   OR invoice_id = %s
                   OR order_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                \"\"\",
                (search_value, search_value, search_value),
            )

            return cursor.fetchone()

def get_paid_invoices_by_phone_for_refund(order_id: str):
""",
    )

text = text.replace(
    """    user_steps[message.chat.id] = {"step": "refund_phone"}
    bot.send_message(
        message.chat.id,
        "Введите номер телефона клиента для поиска оплаченных инвойсов:\\n"
        "<code>0939325197</code>\\n\\n"
        "Для выхода напишите: <code>отмена</code>",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )
""",
    """    user_steps[message.chat.id] = {"step": "refund_lookup"}
    bot.send_message(
        message.chat.id,
        "Введите номер телефона клиента или ID платежа LiqPay:\\n"
        "<code>0939325197</code>\\n\\n"
        "<code>2883180949</code>\\n\\n"
        "Для выхода напишите: <code>отмена</code>",
        reply_markup=telebot.types.ReplyKeyboardRemove(),
    )
""",
)

if "def show_refund_payment_id_result" not in text:
    text = text.replace(
        "@bot.callback_query_handler(func=lambda call: call.data.startswith(\"refund:\"))\n",
        """def show_refund_payment_id_result(chat_id: int, identifier: str):

    invoice = get_paid_invoice_by_payment_identifier(identifier)

    if not invoice:

        bot.send_message(
            chat_id,
            "Оплата с таким ID не найдена в базе бота.\\n\\n"
            "Если это старый платеж, попробуйте найти его по телефону клиента. "
            "Новые оплаты бот сохраняет с ID LiqPay автоматически.",
            reply_markup=main_menu(),
        )
        return

    (
        order_id,
        phone,
        amount,
        currency,
        description,
        created_at,
        checkbox_receipt_id,
        refund_status,
        payment_status,
        liqpay_payment_id,
    ) = invoice

    markup = telebot.types.InlineKeyboardMarkup()

    if payment_status == "success" and refund_status is None:

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
        "Найдена оплата по ID LiqPay:\\n"
        f"<b>{created_at.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')}</b>\\n"
        f"Телефон: <code>{html.escape(display_phone(phone))}</code>\\n"
        f"Сумма: <b>{amount} {html.escape(currency)}</b>\\n"
        f"Товар: {html.escape(description)}\\n"
        f"ID оплаты LiqPay: <code>{html.escape(liqpay_payment_id or identifier)}</code>\\n"
        f"Order ID: <code>{html.escape(str(order_id))}</code>\\n"
        f"Чек Checkbox: {'найден' if checkbox_receipt_id else 'не найден'}"
    )

    if refund_label:

        text += f"\\n{refund_label}"

    if payment_status != "success":

        text += f"\\nСтатус оплаты: {status_label(payment_status)}"

    bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("refund:"))
""",
    )

text = text.replace(
    """    if step == "refund_phone":

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
""",
    """    if step in ("refund_lookup", "refund_phone"):

        phone = extract_phone(text)
        payment_identifier = extract_payment_identifier(text)

        if phone:

            user_steps.pop(chat_id, None)
            show_refund_search_results(chat_id, phone)
            return

        if payment_identifier:

            user_steps.pop(chat_id, None)
            show_refund_payment_id_result(chat_id, payment_identifier)
            return

        bot.send_message(
            chat_id,
            "Не поняла. Отправьте телефон клиента или ID платежа LiqPay.\\n"
            "Например: <code>0939325197</code> или <code>2883180949</code>",
        )
        return
""",
)

path.write_text(text, encoding="utf-8")
