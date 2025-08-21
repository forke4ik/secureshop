# main.py

import os
import logging
import asyncio
import threading
import requests
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import psycopg
from psycopg.rows import dict_row

# Импортируем наши новые модули
from config import (
    BOT_TOKEN,
    DATABASE_URL,
    OWNER_ID_1,
    OWNER_ID_2,
    NOWPAYMENTS_API_KEY,
    NOWPAYMENTS_IPN_SECRET,
    PAYMENT_CURRENCY,  # Будет "UAH"
    CARD_NUMBER,
)
from products_config import SUBSCRIPTIONS, DIGITAL_PRODUCTS, DIGITAL_PRODUCT_MAP
from pay_rules import (
    parse_pay_command,
    get_full_product_info,
    generate_pay_command_from_selection,
    generate_pay_command_from_digital_product,
)

# --- НАСТРОЙКИ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

bot_running = False
bot_lock = threading.Lock()

OWNER_IDS = [id for id in [OWNER_ID_1, OWNER_ID_2] if id is not None]

# NOWPayments API
NOWPAYMENTS_API_URL = "https://api.nowpayments.io/v1"

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ СОСТОЯНИЯ ---
# В реальном приложении это лучше хранить в БД или кэше
users_db = {}  # Хранение информации о пользователях (временно)
active_conversations = {}  # Активные диалоги {client_id: {assigned_owner, user_info, type}}
owner_client_map = {}  # Сопоставление владельца с клиентом {owner_id: client_id}

# --- ФУНКЦИИ РАБОТЫ С БАЗОЙ ДАННЫХ ---
# (Оставлены без изменений, как в оригинале)
# ... (все функции работы с БД из оригинального main.py)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def ensure_user_exists(user):
    """Проверяет и создает/обновляет запись пользователя в БД."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO users (id, username, first_name, last_name, language_code, is_bot, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        language_code = EXCLUDED.language_code,
                        updated_at = CURRENT_TIMESTAMP
                """,
                    (
                        user.id,
                        user.username,
                        user.first_name,
                        user.last_name,
                        user.language_code,
                        user.is_bot,
                        datetime.now(),
                    ),
                )
                conn.commit()
                logger.info(f"Пользователь {user.id} добавлен/обновлен в БД")
    except Exception as e:
        logger.error(f"Ошибка при добавлении/обновлении пользователя {user.id}: {e}")

# --- ФУНКЦИИ NOWPAYMENTS ---
def create_nowpayments_invoice(amount_uah, order_id, product_name):
    """
    Создает инвойс в NOWPayments.
    """
    logger.info(
        f"Создание инвойса NOWPayments: сумма {amount_uah} {PAYMENT_CURRENCY}, заказ {order_id}"
    )

    if not NOWPAYMENTS_API_KEY:
        logger.error("NOWPayments API ключ не установлен!")
        return {"error": "API ключ не настроен"}

    url = f"{NOWPAYMENTS_API_URL}/invoice"

    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "price_amount": amount_uah,
        "price_currency": PAYMENT_CURRENCY,  # Используем UAH
        "order_id": order_id,
        "order_description": f"Оплата за {product_name}",
        "ipn_callback_url": f"https://your-render-app-url.onrender.com/ipn", # Замените на ваш URL
        "success_url": "https://t.me/SecureShopBot", # URL успеха
        "cancel_url": "https://t.me/SecureShopBot",   # URL отмены
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"Статус ответа NOWPayments: {response.status_code}")
        logger.debug(f"Тело ответа NOWPayments: {response.text}")

        if response.status_code in [200, 201]:
            return response.json()
        else:
            logger.error(
                f"Ошибка NOWPayments при создании инвойса: {response.status_code} - {response.text}"
            )
            return {"error": f"Ошибка API: {response.status_code}"}
    except Exception as e:
        logger.error(f"Исключение при создании инвойса NOWPayments: {e}")
        return {"error": f"Исключение: {e}"}

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветственное сообщение."""
    logger.info(f"Вызов /start пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)
    is_owner = user.id in OWNER_IDS

    if is_owner:
        keyboard = [
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("👥 Активные чаты", callback_data="chats")],
            [InlineKeyboardButton("🛍️ Заказы", callback_data="orders")],
            [InlineKeyboardButton("❓ Вопросы", callback_data="questions")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        greeting = f"👋 Привіт, {user.first_name}!\nВи є власником цього бота."
        await update.message.reply_text(greeting, reply_markup=reply_markup)
    else:
        keyboard = [
            [InlineKeyboardButton("🛒 Замовити", callback_data="order")],
            [InlineKeyboardButton("❓ Задати питання", callback_data="question")],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data="help")],
            [InlineKeyboardButton("📢 Канал", callback_data="channel")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        greeting = f"👋 Привіт, {user.first_name}!\nЛаскаво просимо до SecureShop!"
        await update.message.reply_text(greeting, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет справку."""
    logger.info(f"Вызов /help пользователем {update.effective_user.id}")
    help_text = (
        "👋 Доброго дня! Я бот магазину SecureShop.\n\n"
        "🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій. "
        "Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання "
        "по нашому сервісу можна задавати цілодобово.\n\n"
        "📌 Список доступних команд:\n"
        "/start - Головне меню\n"
        "/help - Ця довідка\n"
        "/order - Зробити замовлення\n"
        "/question - Поставити запитання\n"
        "/channel - Наш головний канал\n"
        "/stop - Завершити поточний діалог\n\n"
        "Також ви можете відправити команду `/pay` з сайту для оформлення замовлення."
    )
    await update.message.reply_text(help_text)

async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет ссылку на канал."""
    logger.info(f"Вызов /channel пользователем {update.effective_user.id}")
    keyboard = [
        [InlineKeyboardButton("📢 Перейти в SecureShopUA", url="https://t.me/SecureShopUA")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = (
        "📢 Наш головний канал з асортиментом, оновленнями та розіграшами:\n"
        "👉 Тут ви знайдете:\n"
        "- 🆕 Актуальні товари та послуги\n"
        "- 🔥 Спеціальні пропозиції та знижки\n"
        "- 🎁 Розіграші та акції\n"
        "- ℹ️ Важливі оновлення сервісу\n\n"
        "Приєднуйтесь, щоб бути в курсі всіх новин! 👇"
    )
    await update.message.reply_text(message_text, reply_markup=reply_markup)

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Начинает процесс заказа."""
    logger.info(f"Вызов /order пользователем {update.effective_user.id}")
    keyboard = [
        [InlineKeyboardButton("💳 Підписки", callback_data="order_subscriptions")],
        [InlineKeyboardButton("🎮 Цифрові товари", callback_data="order_digital")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")],
    ]
    await update.message.reply_text(
        "📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def question_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Начинает процесс задания вопроса."""
    logger.info(f"Вызов /question пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)
    context.user_data["conversation_type"] = "question"
    await update.message.reply_text(
        "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n"
        "Щоб завершити цей діалог пізніше, використайте команду /stop."
    )

async def stop_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Завершает текущий диалог."""
    logger.info(f"Вызов /stop пользователем {update.effective_user.id}")
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    # Если это владелец, завершаем диалог с клиентом
    if user_id in OWNER_IDS and user_id in owner_client_map:
        client_id = owner_client_map[user_id]
        # client_info = active_conversations.get(client_id, {}).get('user_info')
        if client_id in active_conversations:
            del active_conversations[client_id]
        del owner_client_map[user_id]
        try:
            await context.bot.send_message(
                chat_id=client_id, text="👤 Магазин завершив діалог."
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить клиента {client_id}: {e}")
            await update.message.reply_text(
                "Не вдалося сповістити клієнта (можливо, він заблокував бота), але діалог було завершено з вашого боку."
            )
        else:
            await update.message.reply_text(
                f"Діалог з клієнтом завершено."
            )
        return

    # Если это клиент, завершаем его диалог
    if user_id in active_conversations:
        owner_id = active_conversations[user_id].get("assigned_owner")
        if owner_id and owner_id in owner_client_map:
            del owner_client_map[owner_id]
        del active_conversations[user_id]
        try:
            if owner_id:
                await context.bot.send_message(
                    chat_id=owner_id, text=f"Клієнт {user_name} завершив діалог."
                )
        except Exception as e:
            logger.error(f"Не удалось уведомить владельца {owner_id}: {e}")
        await update.message.reply_text("Ваш діалог із магазином завершено.")
    else:
        await update.message.reply_text("У вас немає активного діалогу.")

# --- ОБРАБОТЧИКИ CALLBACK-ЗАПРОСОВ ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    ensure_user_exists(user)

    logger.info(f"Получен callback запрос '{query.data}' от пользователя {user_id}")

    # --- Навигация верхнего уровня ---
    if query.data == "order":
        keyboard = [
            [InlineKeyboardButton("💳 Підписки", callback_data="order_subscriptions")],
            [InlineKeyboardButton("🎮 Цифрові товари", callback_data="order_digital")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")],
        ]
        await query.message.edit_text(
            "📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "question":
        context.user_data["conversation_type"] = "question"
        await query.message.edit_text(
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n"
            "Щоб завершити цей діалог пізніше, використайте команду /stop."
        )

    elif query.data == "help":
        help_text = (
            "👋 Доброго дня! Я бот магазину SecureShop.\n\n"
            "🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій. "
            "Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання "
            "по нашому сервісу можна задавати цілодобово.\n\n"
            "📌 Список доступних команд:\n"
            "/start - Головне меню\n"
            "/help - Ця довідка\n"
            "/order - Зробити замовлення\n"
            "/question - Поставити запитання\n"
            "/channel - Наш головний канал\n"
            "/stop - Завершити поточний діалог\n\n"
            "Також ви можете відправити команду `/pay` з сайту для оформлення замовлення."
        )
        await query.message.edit_text(help_text)

    elif query.data == "channel":
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Перейти в SecureShopUA", url="https://t.me/SecureShopUA"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = (
            "📢 Наш головний канал з асортиментом, оновленнями та розіграшами:\n"
            "👉 Тут ви знайдете:\n"
            "- 🆕 Актуальні товари та послуги\n"
            "- 🔥 Спеціальні пропозиції та знижки\n"
            "- 🎁 Розіграші та акції\n"
            "- ℹ️ Важливі оновлення сервісу\n\n"
            "Приєднуйтесь, щоб бути в курсі всіх новин! 👇"
        )
        await query.message.edit_text(message_text, reply_markup=reply_markup)

    elif query.data == "back_to_main":
        is_owner = user.id in OWNER_IDS
        if is_owner:
            keyboard = [
                [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
                [InlineKeyboardButton("👥 Активные чаты", callback_data="chats")],
                [InlineKeyboardButton("🛍️ Заказы", callback_data="orders")],
                [InlineKeyboardButton("❓ Вопросы", callback_data="questions")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            greeting = f"👋 Привіт, {user.first_name}!\nВи є власником цього бота."
            await query.message.edit_text(greeting, reply_markup=reply_markup)
        else:
            keyboard = [
                [InlineKeyboardButton("🛒 Замовити", callback_data="order")],
                [InlineKeyboardButton("❓ Задати питання", callback_data="question")],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data="help")],
                [InlineKeyboardButton("📢 Канал", callback_data="channel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            greeting = f"👋 Привіт, {user.first_name}!\nЛаскаво просимо до SecureShop!"
            await query.message.edit_text(greeting, reply_markup=reply_markup)

    # --- Обработка подписок ---
    elif query.data == "order_subscriptions":
        keyboard = []
        for service_key, service_data in SUBSCRIPTIONS.items():
            keyboard.append(
                [
                    InlineKeyboardButton(
                        service_data["name"], callback_data=f"service_{service_key}"
                    )
                ]
            )
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="order")])
        await query.message.edit_text(
            "💳 Оберіть підписку:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("service_"):
        service_key = query.data.split("_")[1]
        service = SUBSCRIPTIONS.get(service_key)
        if service:
            keyboard = []
            for plan_key, plan_data in service["plans"].items():
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            plan_data["name"],
                            callback_data=f"plan_{service_key}_{plan_key}",
                        )
                    ]
                )
            keyboard.append(
                [InlineKeyboardButton("⬅️ Назад", callback_data="order_subscriptions")]
            )
            await query.message.edit_text(
                f"📋 Оберіть план для {service['name']}:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    elif query.data.startswith("plan_"):
        parts = query.data.split("_")
        if len(parts) == 3:
            service_key, plan_key = parts[1], parts[2]
            service = SUBSCRIPTIONS.get(service_key)
            if service and plan_key in service["plans"]:
                plan_data = service["plans"][plan_key]
                keyboard = []
                for option in plan_data.get("options", []):
                    callback_data = f"add_{service_key}_{plan_key}_{option['period'].replace(' ', '_')}_{option['price']}"
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                f"{option['period']} - {option['price']} UAH",
                                callback_data=callback_data,
                            )
                        ]
                    )
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад", callback_data=f"service_{service_key}"
                        )
                    ]
                )
                await query.message.edit_text(
                    f"🛒 {service['name']} {plan_data['name']}\nОберіть період:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )

    elif query.data.startswith("add_"):
        try:
            parts = query.data.split("_")
            logger.debug(f"Разбор callback_data 'add_': parts={parts}")

            if len(parts) < 5 or not parts[-1].isdigit():
                raise ValueError("Некорректный формат callback_data для add")

            service_key = parts[1]
            plan_key = parts[2]
            price_str = parts[-1]
            period_parts = parts[3:-1]
            period_key = "_".join(period_parts)
            period = period_key.replace("_", " ")

            price = int(price_str)
            service = SUBSCRIPTIONS.get(service_key)

            if service and plan_key in service["plans"]:
                # Генерируем команду /pay
                command, order_id = generate_pay_command_from_selection(
                    user_id, service_key, plan_key, period, price
                )

                # Сохраняем информацию о заказе
                context.user_data["pending_order"] = {
                    "order_id": order_id,
                    "service": service["name"],
                    "plan": service["plans"][plan_key]["name"],
                    "period": period,
                    "price": price,
                    "command": command,
                }

                # Создаем сообщение с деталями заказа
                order_text = (
                    f"🛍️ Нове замовлення #{order_id}\n"
                    f"Сервіс: {service['name']}\n"
                    f"План: {service['plans'][plan_key]['name']}\n"
                    f"Період: {period}\n"
                    f"Сума: {price} UAH\n\n"
                )

                message = (
                    f"{order_text}"
                    f"Оберіть спосіб оплати:"
                )

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "💳 Оплатити карткою", callback_data=f"pay_card_{price}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "₿ Оплатити криптовалютою",
                            callback_data=f"pay_crypto_{price}",
                        )
                    ],
                    [InlineKeyboardButton("📋 Головне меню", callback_data="back_to_main")],
                ]

                await query.message.edit_text(
                    message, reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await query.message.edit_text("❌ Помилка: сервіс або план не знайдено.")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки add_ callback: {e}")
            await query.message.edit_text("❌ Помилка обробки вибору періоду.")

    # --- Обработка цифровых товаров ---
    elif query.data == "order_digital":
        keyboard = [
            [InlineKeyboardButton("🎮 Discord Украшення", callback_data="digital_discord_decor")],
            # Добавляем PSN Gift Cards как отдельный пункт
            [InlineKeyboardButton("🎮 PSN Gift Cards", callback_data="digital_psn_cards")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="order")],
        ]
        await query.message.edit_text(
            "🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # --- Обработка Discord Украшення с вкладками ---
    elif query.data == "digital_discord_decor":
        # Показываем вкладки "Без Nitro" и "З Nitro"
        keyboard = [
            [
                InlineKeyboardButton(
                    "🎨 Украшення Без Nitro", callback_data="discord_decor_bzn"
                )
            ],
            [
                InlineKeyboardButton(
                    "✨ Украшення З Nitro", callback_data="discord_decor_zn"
                )
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="order_digital")],
        ]
        await query.message.edit_text(
            "🎮 Оберіть тип Discord Украшення:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif query.data == "discord_decor_bzn":
        # Показываем товары "Без Nitro"
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            # Фильтруем только товары категории 'bzn'
            if product_data.get("category") == "bzn":
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{product_data['name']} - {product_data['price']} UAH",
                            callback_data=product_callback,
                        )
                    ]
                )
        keyboard.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="digital_discord_decor")]
        )
        await query.message.edit_text(
            "🎨 Discord Украшення (Без Nitro):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif query.data == "discord_decor_zn":
        # Показываем товары "З Nitro"
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            # Фильтруем только товары категории 'zn'
            if product_data.get("category") == "zn":
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{product_data['name']} - {product_data['price']} UAH",
                            callback_data=product_callback,
                        )
                    ]
                )
        keyboard.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="digital_discord_decor")]
        )
        await query.message.edit_text(
            "✨ Discord Украшення (З Nitro):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # --- Обработка PSN Gift Cards ---
    elif query.data == "digital_psn_cards":
        # Показываем все PSN Gift Cards
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            # Фильтруем только товары категории 'psn'
            if product_data.get("category") == "psn":
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{product_data['name']} - {product_data['price']} UAH",
                            callback_data=product_callback,
                        )
                    ]
                )
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="order_digital")])
        await query.message.edit_text(
            "🎮 PSN Gift Cards:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # --- Обработка выбора конкретного цифрового товара ---
    elif query.data.startswith("digital_"):
        product_id = DIGITAL_PRODUCT_MAP.get(query.data)
        if product_id:
            product_data = DIGITAL_PRODUCTS[product_id]
            command, order_id = generate_pay_command_from_digital_product(
                user_id, product_id, product_data
            )

            context.user_data["pending_order"] = {
                "order_id": order_id,
                "service": "Цифровий товар",
                "plan": product_data["name"],
                "period": "1 шт",
                "price": product_data["price"],
                "command": command,
            }

            # Создаем сообщение с деталями заказа
            order_text = (
                f"🛍️ Нове замовлення #{order_id}\n"
                f"Товар: {product_data['name']}\n"
                f"Сума: {product_data['price']} UAH\n\n"
            )

            message = (
                f"{order_text}"
                f"Оберіть спосіб оплати:"
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "💳 Оплатити карткою", callback_data=f"pay_card_{product_data['price']}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "₿ Оплатити криптовалютою",
                        callback_data=f"pay_crypto_{product_data['price']}",
                    )
                ],
                [InlineKeyboardButton("📋 Головне меню", callback_data="back_to_main")],
            ]

            await query.message.edit_text(
                message, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.message.edit_text("❌ Помилка: цифровий товар не знайдено.")

    # --- Обработка оплаты ---
    elif query.data.startswith("pay_card_"):
        try:
            price = int(query.data.split("_")[2])
            pending_order = context.user_data.get("pending_order")

            if not pending_order:
                await query.message.edit_text("❌ Помилка: інформація про замовлення відсутня.")
                return

            message = (
                f"💳 Оплата карткою:\n"
                f"Сума: {price} UAH\n"
                f"Номер картки: {CARD_NUMBER}\n"
                f"Призначення платежу: Оплата за {pending_order['service']} {pending_order['plan']} ({pending_order['period']})\n\n"
                f"Після оплати натисніть кнопку нижче."
            )

            keyboard = [
                [InlineKeyboardButton("✅ Оплачено", callback_data="paid_card")],
                [InlineKeyboardButton("❌ Скасувати", callback_data="cancel_payment")],
            ]

            await query.message.edit_text(
                message, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Ошибка обработки оплаты картой: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати карткою.")

    elif query.data.startswith("pay_crypto_"):
        try:
            price = int(query.data.split("_")[2])
            pending_order = context.user_data.get("pending_order")

            if not pending_order:
                await query.message.edit_text("❌ Помилка: інформація про замовлення відсутня.")
                return

            # Создаем инвойс в NOWPayments
            invoice_data = create_nowpayments_invoice(
                price,
                pending_order["order_id"],
                f"{pending_order['service']} {pending_order['plan']} ({pending_order['period']})",
            )

            if invoice_data and "invoice_url" in invoice_data:
                pay_url = invoice_data["invoice_url"]
                message = (
                    f"₿ Оплата криптовалютою:\n"
                    f"Сума: {price} UAH\n"
                    f"Натисніть кнопку нижче для переходу до оплати.\n\n"
                    f"Після оплати натисніть кнопку \"✅ Оплачено\"."
                )

                keyboard = [
                    [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                    [InlineKeyboardButton("✅ Оплачено", callback_data="paid_crypto")],
                    [InlineKeyboardButton("❌ Скасувати", callback_data="cancel_payment")],
                ]

                await query.message.edit_text(
                    message, reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                error_msg = invoice_data.get("error", "Невідома помилка")
                await query.message.edit_text(
                    f"❌ Помилка створення інвойсу для оплати криптовалютою: {error_msg}"
                )
        except Exception as e:
            logger.error(f"Ошибка обработки оплаты криптовалютой: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати криптовалютою.")

    elif query.data in ["paid_card", "paid_crypto"]:
        pending_order = context.user_data.get("pending_order")
        if pending_order:
            # Отправляем уведомление владельцам с деталями заказа
            order_summary = (
                f"🛍️ НОВЕ ЗАМОВЛЕННЯ #{pending_order['order_id']}\n\n"
                f"👤 Клієнт: @{user.username or user.first_name} (ID: {user_id})\n\n"
                f"📦 Деталі замовлення:\n"
            )
            
            if pending_order['service'] == "Цифровий товар":
                order_summary += (
                    f"▫️ Товар: {pending_order['plan']}\n"
                    f"▫️ Кількість: 1 шт\n"
                    f"▫️ Сума: {pending_order['price']} UAH\n"
                )
            else:
                order_summary += (
                    f"▫️ Сервіс: {pending_order['service']}\n"
                    f"▫️ План: {pending_order['plan']}\n"
                    f"▫️ Період: {pending_order['period']}\n"
                    f"▫️ Сума: {pending_order['price']} UAH\n"
                )
            
            order_summary += (
                f"\n💳 ЗАГАЛЬНА СУМА: {pending_order['price']} UAH\n\n"
                f"Команда для підтвердження: <code>{pending_order['command']}</code>\n\n"
                f"Натисніть '✅ Взяти', щоб обробити це замовлення."
            )

            keyboard = [
                [InlineKeyboardButton("✅ Взяти", callback_data=f"take_order_{user_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            success = False
            for owner_id in OWNER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=owner_id, text=order_summary, parse_mode="HTML", reply_markup=reply_markup
                    )
                    success = True
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление владельцу {owner_id}: {e}")

            if success:
                await query.message.edit_text(
                    "✅ Дякуємо за оплату! Ми зв'яжемося з вами найближчим часом для підтвердження замовлення."
                )
            else:
                await query.message.edit_text(
                    "✅ Дякуємо за оплату! Виникла помилка при відправці сповіщення, але оплата прийнята."
                )

            # Очищаем данные заказа
            context.user_data.pop("pending_order", None)
        else:
            await query.message.edit_text("ℹ️ Інформація про оплату вже оброблена або відсутня.")

    elif query.data == "cancel_payment":
        pending_order = context.user_data.get("pending_order")
        if pending_order:
            await query.message.edit_text(
                f"❌ Оплата скасована.\n"
                f"Сервіс: {pending_order['service']}\n"
                f"План: {pending_order['plan']}\n"
                f"Період: {pending_order['period']}\n"
                f"Сума: {pending_order['price']} UAH\n\n"
                f"Ви можете зробити нове замовлення через /start."
            )
            # Очищаем данные заказа
            context.user_data.pop("pending_order", None)
        else:
            await query.message.edit_text("❌ Оплата вже скасована або відсутня.")

    # --- Обработка взятия заказа владельцем ---
    elif query.data.startswith("take_order_"):
        client_id = int(query.data.split("_")[-1])
        owner_id = user_id

        # Проверяем, не взят ли уже заказ другим владельцем
        if client_id in active_conversations and active_conversations[client_id].get("assigned_owner"):
            await query.answer("❌ Це замовлення вже обробляється іншим представником магазину.", show_alert=True)
            return

        # Начинаем диалог
        client_info = users_db.get(client_id, {})
        active_conversations[client_id] = {
            "assigned_owner": owner_id,
            "user_info": client_info,
            "type": "order", # или "subscription_order", "digital_order" если нужно точнее
            "order_details": context.user_data.get("pending_order", {})
        }
        owner_client_map[owner_id] = client_id

        # Уведомляем клиента
        try:
            await context.bot.send_message(
                chat_id=client_id,
                text="✅ Ваше замовлення прийнято! Представник магазину зв'яжеться з вами найближчим часом.\n\n"
                     "Для завершення діалогу використовуйте /stop."
            )
            # Уведомляем владельца
            await query.message.edit_text(
                f"✅ Ви взяли замовлення від клієнта {client_info.get('first_name', 'Невідомий')} (ID: {client_id}).\n\n"
                "Тепер ви можете надсилати повідомлення цьому клієнту. Для завершення діалогу використовуйте /stop."
            )
        except Exception as e:
            logger.error(f"Ошибка при начале диалога: {e}")
            await query.message.edit_text("❌ Помилка при початку діалогу.")

    # --- Команды владельца (оставлены без изменений, как в оригинале) ---
    # ... (stats, chats, orders, questions, history, dialog, clear и т.д.)

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает текстовые сообщения."""
    logger.info(f"Получено текстовое сообщение от пользователя {update.effective_user.id}")
    user = update.effective_user
    user_id = user.id
    message_text = update.message.text
    ensure_user_exists(user)

    # Проверка, является ли пользователь владельцем
    is_owner = user_id in OWNER_IDS

    # Если пользователь - владелец, обрабатываем его сообщения отдельно
    if is_owner:
        # Пересылка сообщений клиенту в рамках активного диалога
        if user_id in owner_client_map:
            client_id = owner_client_map[user_id]
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text=f"📩 Відповідь від магазину:\n{message_text}",
                )
                # Можно не подтверждать отправку владельцу, или отправить краткое подтверждение
                # await update.message.reply_text("✅ Повідомлення надіслано клієнту.")
            except Exception as e:
                logger.error(
                    f"Ошибка при пересылке сообщения от владельца {user_id} клиенту {client_id}: {e}"
                )
                await update.message.reply_text(
                    "❌ Помилка при надсиланні повідомлення клієнту. Можливо, клієнт заблокував бота."
                )
        else:
            await update.message.reply_text(
                "ℹ️ Ви не ведете діалог з жодним клієнтом. Очікуйте нове повідомлення або скористайтесь командою /dialog."
            )
        return

    # Если это команда /pay
    if message_text.startswith("/pay"):
        await pay_command(update, context)
        return

    # Обработка диалога с пользователем (вопросы, заказы)
    conversation_type = context.user_data.get("conversation_type")

    if conversation_type == "question":
        # Пересылаем вопрос владельцам
        forward_message = (
            f"❓ Нове запитання від клієнта:\n"
            f"👤 Клієнт: {user.first_name}\n"
            f"📱 Username: @{user.username if user.username else 'не вказано'}\n"
            f"🆔 ID: {user.id}\n"
            f"💬 Повідомлення:\n{message_text}"
        )
        keyboard = [
            [InlineKeyboardButton("✅ Відповісти", callback_data=f"take_question_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        success = False
        for owner_id in OWNER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=owner_id, text=forward_message, reply_markup=reply_markup
                )
                success = True
            except Exception as e:
                logger.error(f"Не удалось отправить вопрос владельцу {owner_id}: {e}")

        if success:
            await update.message.reply_text("✅ Ваше запитання надіслано. Очікуйте відповіді.")
        else:
            await update.message.reply_text(
                "❌ На жаль, не вдалося надіслати ваше запитання. Спробуйте пізніше."
            )
        return

    # Если ничего не подошло, предлагаем меню
    await start(update, context)

# --- ОБРАБОТЧИК КОМАНДЫ /PAY ---
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /pay."""
    logger.info(f"Вызов /pay пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)

    # Парсим аргументы команды
    order_id, result = parse_pay_command(context.args)
    if order_id is None:
        # result содержит сообщение об ошибке
        await update.message.reply_text(result)
        return

    parsed_items = result  # result теперь это список распарсенных элементов

    # Преобразуем распарсенные элементы в полную информацию
    order_details = []
    total_uah = 0

    for parsed_item in parsed_items:
        full_info = get_full_product_info(parsed_item)
        if full_info:
            total_uah += full_info["price"]
            if full_info['type'] == 'digital':
                order_details.append(
                    f"▫️ {full_info['service_name']} {full_info['plan_name']} - {full_info['price']} UAH"
                )
            else:
                order_details.append(
                    f"▫️ {full_info['service_name']} {full_info['plan_name']} ({full_info['period']}) - {full_info['price']} UAH"
                )
        else:
            # Если не удалось получить информацию, добавляем "сырой" элемент
            order_details.append(
                f"▫️ ???? ({parsed_item['service_abbr']}-{parsed_item['plan_abbr']}-{parsed_item['period']}) - {parsed_item['price']} UAH"
            )
            total_uah += parsed_item["price"]

    # Формируем текст заказа
    order_text = f"🛍️ Нове замовлення #{order_id} від @{user.username or user.first_name} (ID: {user.id})\n\n"
    order_text += "\n".join(order_details)
    order_text += f"\n\n💳 Всього: {total_uah} UAH"

    # Отправляем уведомление владельцам с кнопкой "Взять"
    keyboard = [
        [InlineKeyboardButton("✅ Взяти", callback_data=f"take_order_{user.id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    success = False
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(chat_id=owner_id, text=order_text, reply_markup=reply_markup)
            success = True
        except Exception as e:
            logger.error(f"Не удалось отправить заказ владельцу {owner_id}: {e}")

    if success:
        await update.message.reply_text(
            f"✅ Дякуємо за замовлення #{order_id}!\n"
            f"💳 Сума до сплати: {total_uah} UAH\n\n"
            f"Ми зв'яжемося з вами найближчим часом для підтвердження."
        )
    else:
        await update.message.reply_text(
            "❌ На жаль, не вдалося обробити ваше замовлення. Спробуйте пізніше."
        )

# --- ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---
def main() -> None:
    """Запускает бота."""
    logger.info("Инициализация приложения бота...")
    
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN не установлен!")
        return

    if not DATABASE_URL:
        logger.critical("DATABASE_URL не установлен!")
        return

    # Создаем Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("order", order_command))
    application.add_handler(CommandHandler("question", question_command))
    application.add_handler(CommandHandler("channel", channel_command))
    application.add_handler(CommandHandler("stop", stop_conversation))
    application.add_handler(CommandHandler("pay", pay_command))
    # ... (добавьте остальные команды владельца: stats, chats, orders, questions, history, dialog, clear)

    # Регистрируем обработчик callback-запросов
    application.add_handler(CallbackQueryHandler(button_handler))

    # Регистрируем обработчик текстовых сообщений (должен быть последним)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запускаем бота
    logger.info("Бот запущен. Ожидание обновлений...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
