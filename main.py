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
import time
import signal
import sys
import random

from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
from threading import Thread

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

# --- ИМПОРТЫ ИЗ ВАШИХ ФАЙЛОВ ---
from config import (
    BOT_TOKEN,
    DATABASE_URL,
    OWNER_ID_1,
    OWNER_ID_2,
    NOWPAYMENTS_API_KEY,
    NOWPAYMENTS_IPN_SECRET,
    PAYMENT_CURRENCY,
    CARD_NUMBER,
)
from products_config import SUBSCRIPTIONS, DIGITAL_PRODUCTS, DIGITAL_PRODUCT_MAP
from pay_rules import (
    parse_pay_command,
    get_full_product_info,
    generate_pay_command_from_selection,
    generate_pay_command_from_digital_product,
)

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Отключаем логи httpx, они слишком многословные
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
bot_running = False
bot_lock = threading.Lock()

OWNER_IDS = [id for id in [OWNER_ID_1, OWNER_ID_2] if id is not None]

NOWPAYMENTS_API_URL = "https://api.nowpayments.io/v1"

# --- СИМУЛЯЦИЯ БАЗЫ ДАННЫХ В ПАМЯТИ (временно, лучше использовать настоящую БД) ---
users_db = {}
active_conversations = {}  # {client_id: {assigned_owner, user_info, type, order_details}}
owner_client_map = {}       # {owner_id: client_id}

# --- ПИНГОВАЛКА ---
PING_INTERVAL = 60 * 5  # 5 минут
WEBHOOK_URL = os.environ.get('RENDER_EXTERNAL_URL') or "http://localhost:10000" # URL вашего Render сервиса
ping_running = False
ping_thread = None

def ping_loop():
    """Цикл пингования для предотвращения засыпания сервиса на Render."""
    global ping_running
    ping_url = f"{WEBHOOK_URL}/health" # Пингуем наш собственный health-check endpoint
    logger.info(f"🔁 Пинговалка запущена. Интервал: {PING_INTERVAL}с. Цель: {ping_url}")
    
    while ping_running:
        try:
            # Добавляем немного случайности в User-Agent, чтобы запросы выглядели по-разному
            user_agent = random.choice([
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "python-requests/2.31.0",
                "Render-Keepalive-Bot/1.0",
                "TelegramBot-Pinger/2.0"
            ])
            headers = {'User-Agent': user_agent}
            
            response = requests.get(ping_url, timeout=10, headers=headers)
            if response.status_code == 200:
                logger.debug(f"✅ Ping успешен: {response.status_code}")
            else:
                logger.warning(f"⚠️ Ping вернул статус: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка ping (requests): {e}")
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка ping: {e}")
        
        # Ждем до следующего пинга
        time.sleep(PING_INTERVAL)

def start_ping_service():
    """Запускает пинговалку в отдельном потоке."""
    global ping_running, ping_thread
    if not ping_running:
        ping_running = True
        ping_thread = threading.Thread(target=ping_loop, daemon=True)
        ping_thread.start()
        logger.info("🔁 Сервис пингования запущен.")

def stop_ping_service():
    """Останавливает пинговалку."""
    global ping_running
    ping_running = False
    logger.info("⏹️ Сервис пингования остановлен.")

# --- HTTP СЕРВЕР ДЛЯ HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    """Обработчик HTTP-запросов для проверки состояния."""
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = json.dumps({
                'status': 'ok', 
                'bot': 'running', 
                'timestamp': datetime.now().isoformat()
            }).encode('utf-8')
            self.wfile.write(response)
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Telegram Bot SecureShop is running. Use /health for status.")
        else:
            self.send_response(404)
            self.end_headers()

def start_http_server(port):
    """Запускает HTTP-сервер для health check."""
    try:
        httpd = socketserver.TCPServer(("", port), HealthCheckHandler)
        logger.info(f"🌐 HTTP сервер запущен на порту {port}")
        httpd.serve_forever()
    except OSError as e:
        if e.errno == 48: # Address already in use
            logger.warning(f"🌐 Порт {port} занят, HTTP сервер не запущен.")
        else:
            logger.error(f"❌ Ошибка запуска HTTP сервера: {e}")
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка HTTP сервера: {e}")

# --- ФУНКЦИИ РАБОТЫ С БАЗОЙ ДАННЫХ ---
def ensure_user_exists(user):
    """
    Проверяет и создает/обновляет запись пользователя в БД.
    В текущей реализации использует временное хранилище в памяти.
    """
    try:
        # В реальной БД здесь был бы SQL-запрос
        if user.id not in users_db:
            logger.info(f"👤 Добавление нового пользователя: {user.id}")
        users_db[user.id] = {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'language_code': user.language_code,
            'is_bot': user.is_bot,
            'created_at': datetime.now(),
            'updated_at': datetime.now(),
        }
        # logger.info(f"Пользователь {user.id} добавлен/обновлен в БД")
    except Exception as e:
        logger.error(f"Ошибка при добавлении/обновлении пользователя {user.id}: {e}")

# --- ФУНКЦИИ NOWPAYMENTS ---
def create_nowpayments_invoice(amount_uah, order_id, product_name):
    """
    Создает инвойс в NOWPayments.
    """
    logger.info(
        f"🧾 Создание инвойса NOWPayments: сумма {amount_uah} {PAYMENT_CURRENCY}, заказ {order_id}"
    )

    if not NOWPAYMENTS_API_KEY or NOWPAYMENTS_API_KEY in ['YOUR_NOWPAYMENTS_API_KEY_HERE', '']:
        logger.error("🔑 NOWPayments API ключ не установлен или имеет значение по умолчанию!")
        return {"error": "API ключ не настроен"}

    url = f"{NOWPAYMENTS_API_URL}/invoice"

    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "price_amount": amount_uah,
        "price_currency": PAYMENT_CURRENCY, # Гривна
        "order_id": order_id,
        "order_description": f"Оплата за {product_name}",
        # URL для Instant Payment Notification - уведомления от NOWPayments о статусе платежа
        "ipn_callback_url": f"{WEBHOOK_URL}/ipn", 
        "success_url": "https://t.me/SecureShopBot", # URL успеха
        "cancel_url": "https://t.me/SecureShopBot",   # URL отмены
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"🧾 Статус ответа NOWPayments: {response.status_code}")
        # logger.debug(f"Тело ответа NOWPayments: {response.text}")

        if response.status_code in [200, 201]:
            return response.json()
        else:
            logger.error(
                f"🧾 Ошибка NOWPayments при создании инвойса: {response.status_code} - {response.text}"
            )
            return {"error": f"Ошибка API: {response.status_code}"}
    except Exception as e:
        logger.error(f"🧾 Исключение при создании инвойса NOWPayments: {e}")
        return {"error": f"Исключение: {e}"}

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветственное сообщение."""
    logger.info(f"🚀 Вызов /start пользователем {update.effective_user.id}")
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
    logger.info(f"📖 Вызов /help пользователем {update.effective_user.id}")
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
    logger.info(f"📢 Вызов /channel пользователем {update.effective_user.id}")
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
    logger.info(f"📦 Вызов /order пользователем {update.effective_user.id}")
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
    logger.info(f"❓ Вызов /question пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)
    # Сохраняем тип разговора в пользовательских данных
    context.user_data["conversation_type"] = "question"
    await update.message.reply_text(
        "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n"
        "Щоб завершити цей діалог пізніше, використайте команду /stop."
    )

async def stop_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Завершает текущий диалог."""
    logger.info(f"⏹️ Вызов /stop пользователем {update.effective_user.id}")
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
            await context.bot.send_message(chat_id=client_id, text="👤 Магазин завершив діалог.")
        except Exception as e:
            logger.error(f"Не удалось уведомить клиента {client_id}: {e}")
            await update.message.reply_text("Не вдалося сповістити клієнта (можливо, він заблокував бота), але діалог було завершено з вашого боку.")
        else:
            await update.message.reply_text(f"Діалог з клієнтом завершено.")
        return

    # Если это клиент, завершаем его диалог
    if user_id in active_conversations:
        owner_id = active_conversations[user_id].get("assigned_owner")
        if owner_id and owner_id in owner_client_map:
            del owner_client_map[owner_id]
        del active_conversations[user_id]
        try:
            if owner_id:
                await context.bot.send_message(chat_id=owner_id, text=f"Клієнт {user_name} завершив діалог.")
        except Exception as e:
            logger.error(f"Не удалось уведомить владельца {owner_id}: {e}")
        await update.message.reply_text("Ваш діалог із магазином завершено.")
    else:
        await update.message.reply_text("У вас немає активного діалогу.")

async def dialog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Начинает ручной диалог с клиентом (для владельцев)."""
    logger.info(f"💬 Вызов /dialog пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if owner_id not in OWNER_IDS:
        return # Игнорируем команду от не-владельцев

    if not context.args:
        await update.message.reply_text("ℹ️ Использование: /dialog <user_id>")
        return
    try:
        client_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID. ID должно быть числом.")
        return

    if client_id in active_conversations:
        await update.message.reply_text("❌ Этот клиент уже обслуживается.")
        return

    # Получаем информацию о клиенте из БД (в данном случае из временного хранилища)
    try:
        # В реальной БД здесь был бы SQL-запрос
        client_info = users_db.get(client_id, {})
    except Exception as e:
        logger.error(f"Ошибка получения информации о пользователе {client_id}: {e}")
        await update.message.reply_text("❌ Ошибка при получении информации о пользователе.")
        return

    if not client_info:
        await update.message.reply_text("❌ Пользователь не найден.")
        return

    # Начинаем диалог
    active_conversations[client_id] = {
        "assigned_owner": owner_id,
        "user_info": client_info,
        "type": "manual_dialog",
        "last_message": "Діалог розпочато вручну"
    }
    owner_client_map[owner_id] = client_id

    try:
        await context.bot.send_message(
            chat_id=client_id,
            text="👤 Представник магазину розпочав з вами діалог вручну.\n\n"
                 "Для завершення діалогу використовуйте /stop."
        )
        await update.message.reply_text(
            f"✅ Ви розпочали діалог з клієнтом {client_info.get('first_name', 'Невідомий')} (ID: {client_id}).\n\n"
            "Тепер ви можете надсилати повідомлення цьому клієнту. Для завершення діалогу використовуйте /stop."
        )
    except Exception as e:
        logger.error(f"Ошибка при начале ручного диалога: {e}")
        await update.message.reply_text("❌ Помилка при початку діалогу.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статистику (для владельцев)."""
    logger.info(f"📈 Вызов /stats пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if owner_id not in OWNER_IDS:
        return # Игнорируем команду от не-владельцев

    # В реальной БД здесь были бы SQL-запросы
    # Пока используем данные из памяти
    try:
        total_users = len(users_db)
        completed_orders = len([c for c in active_conversations.values() if c.get('type') == 'order'])
        active_questions = len([c for c in active_conversations.values() if c.get('type') == 'question'])
        active_chats = len(active_conversations)
        
        stats_message = (
            f"📊 Статистика бота:\n"
            f"👤 Усього користувачів: {total_users}\n"
            f"🛒 Усього замовлень: {completed_orders}\n"
            f"❓ Активних запитаннь: {active_questions}\n"
            f"👥 Активних чатів: {active_chats}"
        )
        await update.message.reply_text(stats_message)
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await update.message.reply_text("❌ Помилка при отриманні статистики.")

# --- ОБРАБОТЧИКИ CALLBACK-ЗАПРОСОВ ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки."""
    query = update.callback_query
    await query.answer() # Отвечаем на callback, чтобы кнопка перестала "мигать"
    user = query.from_user
    user_id = user.id
    ensure_user_exists(user)
    
    logger.info(f"🔘 Получен callback запрос: {query.data} от пользователя {user_id}")

    # --- Навигация верхнего уровня ---
    if query.data == "order":
        keyboard = [
            [InlineKeyboardButton("💳 Підписки", callback_data="order_subscriptions")],
            [InlineKeyboardButton("🎮 Цифрові товари", callback_data="order_digital")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
        ]
        await query.message.edit_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "question":
        # Устанавливаем тип разговора для последующей обработки сообщений
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
        keyboard = [[InlineKeyboardButton("📢 Перейти в SecureShopUA", url="https://t.me/SecureShopUA")]]
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
        # Возвращаемся к стартовому меню в зависимости от роли пользователя
        is_owner = user.id in OWNER_IDS
        if is_owner:
            keyboard = [
                [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
                [InlineKeyboardButton("👥 Активные чаты", callback_data="chats")], # Заглушка
                [InlineKeyboardButton("🛍️ Заказы", callback_data="orders")],      # Заглушка
                [InlineKeyboardButton("❓ Вопросы", callback_data="questions")],   # Заглушка
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            greeting = f"👋 Привіт, {user.first_name}!\nВи є власником цього бота."
            await query.message.edit_text(greeting, reply_markup=reply_markup)
        else:
            keyboard = [
                [InlineKeyboardButton("🛒 Замовити", callback_data="order")],
                [InlineKeyboardButton("❓ Задати питання", callback_data="question")],
                [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
                [InlineKeyboardButton("📢 Канал", callback_data="channel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            greeting = f"👋 Привет, {user.first_name}!\nДобро пожаловать в SecureShop!"
            await query.message.edit_text(greeting, reply_markup=reply_markup)
    
    # --- Обработка подписок ---
    elif query.data == "order_subscriptions":
        keyboard = []
        for service_key, service_data in SUBSCRIPTIONS.items():
            keyboard.append([InlineKeyboardButton(service_data['name'], callback_data=f'service_{service_key}')])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='order')])
        await query.message.edit_text("💳 Оберіть підписку:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data.startswith('service_'):
        service_key = query.data.split('_')[1]
        service = SUBSCRIPTIONS.get(service_key)
        if service:
            keyboard = []
            for plan_key, plan_data in service['plans'].items():
                keyboard.append([InlineKeyboardButton(plan_data['name'], callback_data=f'plan_{service_key}_{plan_key}')])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')])
            await query.message.edit_text(f"📋 Оберіть план для {service['name']}:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data.startswith('plan_'):
        parts = query.data.split('_')
        if len(parts) == 3:
            service_key, plan_key = parts[1], parts[2]
            service = SUBSCRIPTIONS.get(service_key)
            if service and plan_key in service['plans']:
                plan_data = service['plans'][plan_key]
                keyboard = []
                # Предполагаем, что у плана есть поле 'options' со списком периодов и цен
                for option in plan_data.get('options', []):
                    # Создаем callback_data в формате add_service_plan_period_price
                    # Заменяем пробелы в периоде на подчеркивания для корректного парсинга
                    callback_data = f"add_{service_key}_{plan_key}_{option['period'].replace(' ', '_')}_{option['price']}"
                    keyboard.append([InlineKeyboardButton(f"{option['period']} - {option['price']} UAH", callback_data=callback_data)])
                keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f'service_{service_key}')])
                await query.message.edit_text(
                    f"🛒 {service['name']} {plan_data['name']}\nОберіть період:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
    elif query.data.startswith('add_'):
        # Обработка выбора периода для подписки
        parts = query.data.split('_')
        # parts = ['add', 'service_key', 'plan_key', 'period_part1', 'period_part2', ..., 'price']
        # Минимум 5 частей: add_service_plan_period_price
        if len(parts) < 5 or not parts[-1].isdigit():
             logger.error(f"❌ Неверный формат callback_data 'add_': {query.data}")
             await query.message.edit_text("❌ Помилка обробки вибору періоду.")
             return

        service_key = parts[1]
        plan_key = parts[2]
        # Цена всегда последняя часть
        price_str = parts[-1] 
        # Период - это всё, что между plan_key и price_str
        period_parts = parts[3:-1] 
        period_key = "_".join(period_parts) # Собираем "1_місяць"
        period = period_key.replace('_', ' ') # Получаем "1 місяць"
        
        try:
            price = int(price_str)
            service = SUBSCRIPTIONS.get(service_key)
            
            if service and plan_key in service['plans']:
                # Генерируем команду /pay для оплаты
                # order_id можно сгенерировать, например, на основе user_id и цены
                service_abbr = service_key[:3].capitalize() # Первые 3 буквы + заглавная
                plan_abbr = plan_key.upper()
                # Упрощенное форматирование периода для команды
                period_abbr = period.replace('місяць', 'м').replace('місяців', 'м')
                order_id = 'O' + str(user_id)[-4:] + str(price)[-2:] # Пример: O12345678901234567890
                command = f"/pay {order_id} {service_abbr}-{plan_abbr}-{period_abbr}-{price}"
                
                # Сохраняем информацию о заказе в user_data пользователя
                context.user_data['pending_order'] = {
                    'order_id': order_id,
                    'service': service['name'],
                    'plan': service['plans'][plan_key]['name'],
                    'period': period,
                    'price': price,
                    'command': command
                }
                
                message = (
                    f"🛍️ Ваше замовлення готове!\n\n"
                    f"Скопіюйте цю команду та відправте її боту для підтвердження:\n"
                    f"<code>{command}</code>\n\n"
                    f"Або оберіть спосіб оплати нижче."
                )
                
                keyboard = [
                    [InlineKeyboardButton("💳 Оплатити карткою", callback_data=f"pay_card_{price}")],
                    [InlineKeyboardButton("₿ Оплатити криптовалютою", callback_data=f"pay_crypto_{price}")],
                    [InlineKeyboardButton("📋 Головне меню", callback_data='back_to_main')]
                ]
                
                await query.message.edit_text(message, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await query.message.edit_text("❌ Помилка: сервіс або план не знайдено.")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки add_ callback: {e}")
            await query.message.edit_text("❌ Помилка обробки вибору періоду.")

    # --- Обработка оплаты КАРТОЙ ---
    elif query.data.startswith('pay_card_'):
        try:
            # Извлекаем цену из callback_data, например, "pay_card_100" -> 100
            price_str = query.data.split('_')[2]
            price = int(price_str)
            
            # Получаем информацию о заказе из user_data
            pending_order = context.user_data.get('pending_order')
            
            if not pending_order:
                await query.message.edit_text("❌ Помилка: інформація про замовлення відсутня.")
                return
                
            # Форматируем номер карты для копирования (используем Markdown)
            # Телеграм автоматически делает кликабельным текст внутри ``
            formatted_card_number = f"`{CARD_NUMBER}`"
            
            message = (
                f"💳 Оплата карткою:\n"
                f"Сума: {price} UAH\n"
                f"Номер картки: {formatted_card_number}\n"
                f"(Натисніть на номер, щоб скопіювати)\n"
                f"Призначення платежу: Оплата за {pending_order['service']} {pending_order['plan']} ({pending_order['period']})\n\n"
                f"Після оплати натисніть кнопку нижче."
            )
            
            keyboard = [
                [InlineKeyboardButton("✅ Оплачено", callback_data='paid_card')],
                [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment')]
            ]
            
            await query.message.edit_text(
                message, 
                parse_mode='Markdown', # Важно для корректного отображения `номер карты`
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки оплаты картой: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати карткою.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке оплаты картой: {e}")
            await query.message.edit_text("❌ Неожиданная помилка обробки оплати карткою.")

    # --- Обработка оплаты КРИПТОВАЛЮТОЙ ---
    elif query.data.startswith('pay_crypto_'):
        try:
            # Извлекаем цену из callback_data, например, "pay_crypto_100" -> 100
            price_str = query.data.split('_')[2]
            price = int(price_str)
            
            # Получаем информацию о заказе из user_data
            pending_order = context.user_data.get('pending_order')
            
            if not pending_order:
                await query.message.edit_text("❌ Помилка: інформація про замовлення відсутня.")
                return
            
            # Создаем инвойс в NOWPayments
            invoice_data = create_nowpayments_invoice(
                price, 
                pending_order['order_id'], 
                f"{pending_order['service']} {pending_order['plan']} ({pending_order['period']})"
            )
            
            if invoice_data and 'invoice_url' in invoice_data:
                pay_url = invoice_data['invoice_url']
                message = (
                    f"₿ Оплата криптовалютою:\n"
                    f"Сума: {price} UAH\n"
                    f"Натисніть кнопку нижче для переходу до оплати.\n\n"
                    f"Після оплати натисніть кнопку \"✅ Оплачено\"."
                )
                
                keyboard = [
                    [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                    [InlineKeyboardButton("✅ Оплачено", callback_data='paid_crypto')],
                    [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment')]
                ]
                
                await query.message.edit_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                # Получаем сообщение об ошибке, если оно есть
                error_msg = invoice_data.get('error', 'Невідома помилка') if invoice_data else 'Невідома помилка'
                await query.message.edit_text(f"❌ Помилка створення інвойсу для оплати криптовалютою: {error_msg}")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки оплаты криптовалютой: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати криптовалютою.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке оплаты криптовалютой: {e}")
            await query.message.edit_text("❌ Неожиданная помилка обробки оплати криптовалютою.")

    # --- Обработка нажатия кнопки "ОПЛАЧЕНО" (карта или крипта) ---
    elif query.data in ['paid_card', 'paid_crypto']:
        # Получаем информацию о заказе из user_data
        pending_order = context.user_data.get('pending_order')
        if pending_order:
            # Формируем сообщение для владельцев
            order_summary = (
                f"🛍️ НОВЕ ЗАМОВЛЕННЯ #{pending_order['order_id']}\n\n"
                f"👤 Клієнт: @{user.username or user.first_name} (ID: {user_id})\n\n"
                f"📦 Деталі замовлення:\n"
                f"▫️ Сервіс: {pending_order['service']}\n"
                f"▫️ План: {pending_order['plan']}\n"
                f"▫️ Період: {pending_order['period']}\n"
                f"▫️ Сума: {pending_order['price']} UAH\n\n"
                f"💳 ЗАГАЛЬНА СУМА: {pending_order['price']} UAH\n\n"
                f"Команда для підтвердження: <code>{pending_order['command']}</code>\n\n"
                f"Натисніть '✅ Взяти', щоб обробити це замовлення."
            )
            
            # Кнопка "Взять" для владельцев
            keyboard = [
                [InlineKeyboardButton("✅ Взяти", callback_data=f"take_order_{user_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Отправляем уведомление всем владельцам
            success = False
            for owner_id in OWNER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=owner_id, 
                        text=order_summary, 
                        parse_mode='HTML', # Для корректного отображения <code>
                        reply_markup=reply_markup
                    )
                    success = True
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление владельцу {owner_id}: {e}")
            
            if success:
                await query.message.edit_text("✅ Дякуємо за оплату! Ми зв'яжемося з вами найближчим часом для підтвердження замовлення.")
            else:
                await query.message.edit_text("✅ Дякуємо за оплату! Виникла помилка при відправці сповіщення, але оплата прийнята.")
            
            # Очищаем данные заказа из user_data
            context.user_data.pop('pending_order', None)
        else:
            await query.message.edit_text("ℹ️ Інформація про оплату вже оброблена або відсутня.")

    # --- Обработка нажатия кнопки "СКАСУВАТИ" оплату ---
    elif query.data == 'cancel_payment':
        pending_order = context.user_data.get('pending_order')
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
            context.user_data.pop('pending_order', None)
        else:
            await query.message.edit_text("❌ Оплата вже скасована або відсутня.")

    # --- Обработка цифровых товаров ---
    elif query.data == "order_digital":
        keyboard = [
            [InlineKeyboardButton("🎮 Discord Украшення", callback_data="digital_discord_decor")],
            [InlineKeyboardButton("🎮 PSN Gift Cards", callback_data="digital_psn_cards")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="order")],
        ]
        await query.message.edit_text("🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    # --- Обработка Discord Украшення с вкладками ---
    elif query.data == "digital_discord_decor":
        keyboard = [
            [InlineKeyboardButton("🎨 Украшення Без Nitro", callback_data="discord_decor_bzn")],
            [InlineKeyboardButton("✨ Украшення З Nitro", callback_data="discord_decor_zn")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="order_digital")],
        ]
        await query.message.edit_text("🎮 Оберіть тип Discord Украшення:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "discord_decor_bzn":
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            # Фильтруем только товары категории 'bzn'
            if product_data.get('category') == 'bzn':
                keyboard.append([InlineKeyboardButton(f"{product_data['name']} - {product_data['price']} UAH", callback_data=product_callback)])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="digital_discord_decor")])
        await query.message.edit_text("🎨 Discord Украшення (Без Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "discord_decor_zn":
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            # Фильтруем только товары категории 'zn'
            if product_data.get('category') == 'zn':
                keyboard.append([InlineKeyboardButton(f"{product_data['name']} - {product_data['price']} UAH", callback_data=product_callback)])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="digital_discord_decor")])
        await query.message.edit_text("✨ Discord Украшення (З Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))
        
    # --- Обработка PSN Gift Cards ---
    elif query.data == "digital_psn_cards":
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            # Фильтруем только товары категории 'psn'
            if product_data.get('category') == 'psn':
                keyboard.append([InlineKeyboardButton(f"{product_data['name']} - {product_data['price']} UAH", callback_data=product_callback)])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="order_digital")])
        await query.message.edit_text("🎮 PSN Gift Cards:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    # --- Обработка выбора конкретного цифрового товара ---
    elif query.data.startswith('digital_'):
        product_id = DIGITAL_PRODUCT_MAP.get(query.data) # Получаем реальный ID продукта
        if product_id:
            product_data = DIGITAL_PRODUCTS[product_id]
            # Генерируем команду /pay для цифрового товара
            order_id = 'D' + str(user_id)[-4:] + str(product_data['price'])[-2:] # Пример: D12345678901234567890
            service_abbr = "Dis" if "Discord" in product_data['name'] else "Dig"
            plan_abbr = "Dec" if "Украшення" in product_data['name'] else "Prod"
            price = product_data['price']
            command = f"/pay {order_id} {service_abbr}-{plan_abbr}-1шт-{price}"
            
            # Сохраняем информацию о заказе в user_data
            context.user_data['pending_order'] = {
                'order_id': order_id,
                'service': "Цифровий товар",
                'plan': product_data['name'],
                'period': "1 шт",
                'price': price,
                'command': command
            }
            
            message = (
                f"🛍️ Ваше замовлення готове!\n\n"
                f"Скопіюйте цю команду та відправте її боту для підтвердження:\n"
                f"<code>{command}</code>\n\n"
                f"Або оберіть спосіб оплати нижче."
            )
            
            keyboard = [
                [InlineKeyboardButton("💳 Оплатити карткою", callback_data=f"pay_card_{price}")],
                [InlineKeyboardButton("₿ Оплатити криптовалютою", callback_data=f"pay_crypto_{price}")],
                [InlineKeyboardButton("📋 Головне меню", callback_data='back_to_main')]
            ]
            
            await query.message.edit_text(message, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.message.edit_text("❌ Помилка: цифровий товар не знайдено.")

    # --- Обработка взятия заказа владельцем ---
    elif query.data.startswith('take_order_'):
        # Извлекаем ID клиента из callback_data, например, "take_order_123456789" -> 123456789
        try:
            client_id = int(query.data.split('_')[-1])
            owner_id = user_id # Владелец, который нажал кнопку
            
            # Проверяем, не взят ли уже заказ другим владельцем
            if client_id in active_conversations and active_conversations[client_id].get('assigned_owner'):
                await query.answer("❌ Це замовлення вже обробляється іншим представником магазину.", show_alert=True)
                return

            # Начинаем диалог
            # Получаем информацию о клиенте (в реальной БД здесь был бы запрос)
            client_info = users_db.get(client_id, {})
            # Сохраняем информацию о диалоге
            active_conversations[client_id] = {
                'assigned_owner': owner_id,
                'user_info': client_info,
                'type': 'order', # или "subscription_order", "digital_order" если нужно точнее
                'order_details': context.user_data.get('pending_order', {}) # Передаем детали, если они еще есть
            }
            owner_client_map[owner_id] = client_id

            # Уведомляем клиента
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text="✅ Ваше замовлення прийнято! Представник магазину зв'яжеться з вами найближчим часом.\n\n"
                         "Для завершення діалогу використовуйте /stop."
                )
                # Уведомляем владельца (редактируем его сообщение)
                await query.message.edit_text(
                    f"✅ Ви взяли замовлення від клієнта {client_info.get('first_name', 'Невідомий')} (ID: {client_id}).\n\n"
                    "Тепер ви можете надсилати повідомлення цьому клієнту. Для завершення діалогу використовуйте /stop."
                )
            except Exception as e:
                logger.error(f"Ошибка при начале диалога: {e}")
                await query.message.edit_text("❌ Помилка при початку діалогу.")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки take_order_: {e}")
            await query.message.edit_text("❌ Помилка обробки запиту.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке take_order_: {e}")
            await query.message.edit_text("❌ Неожиданная помилка.")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает текстовые сообщения."""
    logger.info(f"📨 Получено текстовое сообщение от пользователя {update.effective_user.id}")
    user = update.effective_user
    user_id = user.id
    message_text = update.message.text
    ensure_user_exists(user)

    # Проверка, является ли пользователь владельцем
    is_owner = user_id in OWNER_IDS

    # --- Если пользователь - владелец ---
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
        return # Важно: выходим, если это был владелец

    # --- Если пользователь НЕ владелец ---

    # Если это команда /pay
    if message_text.startswith('/pay'):
        await pay_command(update, context)
        return # Важно: выходим, если это была команда /pay

    # Обработка диалога с пользователем (вопросы, заказы)
    conversation_type = context.user_data.get('conversation_type')

    # --- Обработка вопросов ---
    if conversation_type == 'question':
        # Пересылаем вопрос владельцам
        forward_message = (
            f"❓ Нове запитання від клієнта:\n"
            f"👤 Клієнт: {user.first_name}\n"
            f"📱 Username: @{user.username if user.username else 'не вказано'}\n"
            f"🆔 ID: {user.id}\n"
            f"💬 Повідомлення:\n{message_text}"
        )
        keyboard = [[InlineKeyboardButton("✅ Відповісти", callback_data=f'take_question_{user_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        success = False
        for owner_id in OWNER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=owner_id, 
                    text=forward_message, 
                    reply_markup=reply_markup
                )
                success = True
            except Exception as e:
                logger.error(f"Не удалось отправить вопрос владельцу {owner_id}: {e}")
        
        if success:
            await update.message.reply_text("✅ Ваше запитання надіслано. Очікуйте відповіді.")
        else:
            await update.message.reply_text("❌ На жаль, не вдалося надіслати ваше запитання. Спробуйте пізніше.")
        return # Важно: выходим, если это был вопрос

    # --- Обработка активного диалога клиента с владельцем ---
    # Проверяем, участвует ли пользователь в активном диалоге
    if user_id in active_conversations:
        assigned_owner_id = active_conversations[user_id].get('assigned_owner')
        if assigned_owner_id:
            # Это клиент в активном диалоге с владельцем
            try:
                await context.bot.send_message(
                    chat_id=assigned_owner_id,
                    text=f"📩 Повідомлення від клієнта:\n{message_text}",
                )
                # Опционально: подтверждение клиенту
                # await update.message.reply_text("✅ Повідомлення надіслано представнику магазину.")
            except Exception as e:
                logger.error(f"Ошибка пересылки сообщения от клиента {user_id} владельцу {assigned_owner_id}: {e}")
                await update.message.reply_text("❌ Помилка при надсиланні повідомлення. Спробуйте пізніше.")
            return # Важно: выходим, если это был клиент в активном диалоге

    # --- Если ничего не подошло, предлагаем меню ---
    await start(update, context)

# --- ОБРАБОТЧИК КОМАНДЫ /PAY ---
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /pay."""
    logger.info(f"💰 Вызов команды /pay пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)

    if not context.args:
        await update.message.reply_text("❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ...")
        return

    # Парсим аргументы команды
    order_id = context.args[0]
    items_str = " ".join(context.args[1:])
    
    # Простой парсинг (как в оригинале)
    # Паттерн для парсинга отдельного товара: ServiceAbbr-PlanAbbr-Period-Price
    # Пример: Cha-Bas-1м-100 или DisU-Dec-1шт-220
    pattern = r'(\w{2,4})-(\w{2,4})-([\w\s$]+?)-(\d+)'
    items = re.findall(pattern, items_str)

    if not items:
        await update.message.reply_text("❌ Не вдалося розпізнати товари у замовленні. Перевірте формат.")
        return

    # Формируем текст заказа
    order_text = f"🛍️ Нове замовлення #{order_id} від @{user.username or user.first_name} (ID: {user.id})\n\n"
    total_uah = 0
    order_details = []
    for service_abbr, plan_abbr, period, price_str in items:
        price = int(price_str)
        total_uah += price
        order_details.append(f"▫️ {service_abbr}-{plan_abbr}-{period} - {price} UAH")
    
    order_text += "\n".join(order_details)
    order_text += f"\n\n💳 Всього: {total_uah} UAH"

    success = False
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(chat_id=owner_id, text=order_text)
            success = True
        except Exception as e:
            logger.error(f"Не удалось отправить заказ владельцу {owner_id}: {e}")

    if success:
        # Создаем инвойс в NOWPayments
        invoice_data = create_nowpayments_invoice(total_uah, order_id, "Замовлення через /pay")
        
        if invoice_data and 'invoice_url' in invoice_data:
            pay_url = invoice_data['invoice_url']
            payment_message = (
                f"✅ Дякуємо за замовлення #{order_id}!\n"
                f"💳 Сума до сплати: {total_uah} UAH\n\n"
                f"Натисніть кнопку нижче для переходу до оплати:\n"
                f"🔗 [Оплатити зараз]({pay_url})"
            )
            keyboard = [[InlineKeyboardButton("🔗 Оплатити зараз", url=pay_url)]]
            await update.message.reply_text(payment_message, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            error_msg = invoice_data.get('error', 'Невідома помилка') if invoice_data else 'Невідома помилка'
            await update.message.reply_text(
                f"✅ Дякуємо за замовлення #{order_id}!\n"
                f"💳 Сума до сплати: {total_uah} UAH\n"
                f"⚠️ Помилка створення посилання для оплати: {error_msg}\n\n"
                f"Ми зв'яжемося з вами найближчим часом для підтвердження."
            )
    else:
        await update.message.reply_text("❌ На жаль, не вдалося обробити ваше замовлення. Спробуйте пізніше.")

# --- ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---
def main() -> None:
    """Запускает бота."""
    logger.info("🚀 Инициализация приложения бота...")
    
    # Проверка обязательных настроек
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.critical("🔑 BOT_TOKEN не установлен или имеет значение по умолчанию!")
        return
    if not DATABASE_URL or DATABASE_URL == "YOUR_DATABASE_URL_HERE":
        logger.warning("💾 DATABASE_URL не установлен или имеет значение по умолчанию! Используется временное хранилище.")

    # --- ЗАПУСК HTTP СЕРВЕРА ---
    port = int(os.environ.get('PORT', 10000)) # Render предоставляет PORT
    # Запускаем HTTP сервер в отдельном потоке
    http_thread = Thread(target=start_http_server, args=(port,), daemon=True)
    http_thread.start()
    logger.info(f"🌐 HTTP сервер запущен в потоке на порту {port}")

    # --- СОЗДАНИЕ Application ---
    application = Application.builder().token(BOT_TOKEN).build()

    # --- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("order", order_command))
    application.add_handler(CommandHandler("question", question_command))
    application.add_handler(CommandHandler("channel", channel_command))
    application.add_handler(CommandHandler("stop", stop_conversation))
    application.add_handler(CommandHandler("dialog", dialog_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("pay", pay_command))

    # Регистрируем обработчик callback-запросов
    application.add_handler(CallbackQueryHandler(button_handler))

    # Регистрируем обработчик текстовых сообщений (должен быть последним)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- ОБРАБОТЧИК СИГНАЛОВ ЗАВЕРШЕНИЯ ---
    def signal_handler(signum, frame):
        """Обработчик сигналов завершения (Ctrl+C, SIGTERM)."""
        logger.info("🛑 Принято сигнал завершения. Остановка бота...")
        stop_ping_service() # Останавливаем пинговалку
        # Здесь можно добавить код для корректного закрытия соединений, БД и т.д.
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler) # Ctrl+C

    # --- ЗАПУСК ПИНГОВАЛКИ ---
    start_ping_service()

    # --- ЗАПУСК БОТА ---
    logger.info("🤖 Бот запущен. Нажмите Ctrl+C для остановки.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
