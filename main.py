# main.py
import os
import logging
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

bot_running = False
bot_lock = threading.Lock()
OWNER_IDS = [id for id in [OWNER_ID_1, OWNER_ID_2] if id is not None]
MANAGER_ID = int(os.environ.get('SECURE_SUPPORT_ID', 0))
NOWPAYMENTS_API_URL = "https://api.nowpayments.io/v1"

# Доступные криптовалюты и сети для оплаты
AVAILABLE_CURRENCIES = {
    "USDT (Solana)": "usdtsol",
    "USDT (TRC20)": "usdttrc20",
    "ETH": "eth",
    "USDT (Arbitrum)": "usdtarb",
    "USDT (Polygon)": "usdtmatic",   
    "USDT (TON)": "usdtton",        
    "AVAX (C-Chain)": "avax",
    "APTOS (APT)": "apt"
}

def init_db():
    """Инициализирует базу данных."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGINT PRIMARY KEY,
                        username VARCHAR(255),
                        first_name VARCHAR(255),
                        last_name VARCHAR(255),
                        language_code VARCHAR(10),
                        is_bot BOOLEAN,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_stats (
                        id SERIAL PRIMARY KEY,
                        total_orders INTEGER DEFAULT 0,
                        total_questions INTEGER DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS active_questions (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        order_id VARCHAR(255),
                        items TEXT,
                        total_uah INTEGER,
                        status VARCHAR(50) DEFAULT 'created',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("SELECT COUNT(*) FROM bot_stats")
                if cur.fetchone()[0] == 0:
                    cur.execute("INSERT INTO bot_stats (total_orders, total_questions) VALUES (0, 0)")
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка инициализации базы данных: {e}")

def get_stats():
    """Получает статистику из БД."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT total_orders, total_questions FROM bot_stats ORDER BY id DESC LIMIT 1")
                return cur.fetchone()
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        return {'total_orders': 0, 'total_questions': 0}

def increment_orders():
    """Увеличивает счетчик заказов."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bot_stats SET total_orders = total_orders + 1, updated_at = NOW()")
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка увеличения счетчика заказов: {e}")

def increment_questions():
    """Увеличивает счетчик вопросов."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bot_stats SET total_questions = total_questions + 1, updated_at = NOW()")
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка увеличения счетчика вопросов: {e}")

def save_user(user):
    """Сохраняет или обновляет информацию о пользователе в БД."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, first_name, last_name, language_code, is_bot, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        language_code = EXCLUDED.language_code,
                        updated_at = NOW()
                """, (user.id, user.username, user.first_name, user.last_name, user.language_code, user.is_bot))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя {user.id}: {e}")

def get_total_users_count():
    """Получает общее количество пользователей."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Ошибка получения количества пользователей: {e}")
        return 0

def get_all_users():
    """Получает всех пользователей."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM users")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Ошибка получения пользователей: {e}")
        return []

def save_question(user_id, message):
    """Сохраняет вопрос в БД."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO active_questions (user_id, message, created_at)
                    VALUES (%s, %s, NOW())
                """, (user_id, message))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения вопроса от {user_id}: {e}")

def get_active_questions_count():
    """Получает количество активных вопросов."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM active_questions")
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Ошибка получения количества активных вопросов: {e}")
        return 0

def save_order(user_id, order_id, items, total_uah):
    """Сохраняет заказ в БД."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO orders (user_id, order_id, items, total_uah, status, created_at)
                    VALUES (%s, %s, %s, %s, 'created', NOW())
                """, (user_id, order_id, items, total_uah))
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения заказа {order_id}: {e}")

def get_orders_count():
    """Получает количество заказов."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM orders")
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Ошибка получения количества заказов: {e}")
        return 0

PING_INTERVAL = 60 * 5
WEBHOOK_URL = os.environ.get('RENDER_EXTERNAL_URL') or "http://localhost:10000"
ping_running = False
ping_thread = None

def ping_loop():
    """Цикл пингования для предотвращения засыпания сервиса."""
    global ping_running
    ping_url = f"{WEBHOOK_URL}/health"
    logger.info(f"🔁 Пинговалка запущена. Интервал: {PING_INTERVAL}с. Цель: {ping_url}")
    while ping_running:
        try:
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
        time.sleep(PING_INTERVAL)

def start_ping_service():
    """Запускает пинговалку."""
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

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Обработчик HTTP запросов для health check."""
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
    """Запускает HTTP сервер для health check."""
    try:
        httpd = socketserver.TCPServer(("", port), HealthCheckHandler)
        logger.info(f"🌐 HTTP сервер запущен на порту {port}")
        httpd.serve_forever()
    except OSError as e:
        if e.errno == 48:
            logger.warning(f"🌐 Порт {port} занят, HTTP сервер не запущен.")
        else:
            logger.error(f"❌ Ошибка запуска HTTP сервера: {e}")
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка HTTP сервера: {e}")

users_db = {}

def ensure_user_exists(user):
    """Добавляет или обновляет информацию о пользователе."""
    try:
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
        save_user(user)
    except Exception as e:
        logger.error(f"Ошибка при добавлении/обновлении пользователя {user.id}: {e}")

def create_nowpayments_invoice(price_amount, order_id, product_name, pay_currency="usdtsol", pay_amount=None):
    """
    Создает инвойс в NOWPayments.
    
    Args:
        price_amount (int): Цена в гривнах (UAH).
        order_id (str): Уникальный ID заказа.
        product_name (str): Название продукта.
        pay_currency (str): Код криптовалюты для оплаты (например, 'usdttrc20').
        pay_amount (float, optional): Фиксированная сумма для оплаты в криптовалюте (например, 1.3). Если None, NOWPayments рассчитает сумму самостоятельно.
        
    Returns:
        dict: Данные инвойса от NOWPayments или словарь с ошибкой.
    """
    logger.info(
        f"🧾 Создание инвойса NOWPayments: цена {price_amount} {PAYMENT_CURRENCY}, заказ {order_id}, валюта оплаты {pay_currency}"
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
    
    # Убедимся, что price_amount является числом
    try:
        price_amount = int(price_amount)
    except (ValueError, TypeError) as e:
        logger.error(f"❌ Ошибка преобразования суммы цены в число: {e}")
        return {"error": "Некорректная сумма цены"}

    payload = {
        "price_amount": price_amount,
        "price_currency": PAYMENT_CURRENCY, # UAH
        "pay_currency": pay_currency,       # Криптовалюта, например, 'usdttrc20'
        "order_id": order_id,
        "order_description": f"Оплата за {product_name}",
        "ipn_callback_url": f"{WEBHOOK_URL}/ipn",
        "success_url": "https://t.me/SecureShopBot",
        "cancel_url": "https://t.me/SecureShopBot",
    }
    
    # Добавляем фиксированную сумму в криптовалюте, если она указана
    if pay_amount is not None:
        try:
            payload["pay_amount"] = float(pay_amount)
        except (ValueError, TypeError) as e:
            logger.error(f"❌ Ошибка преобразования суммы оплаты в число: {e}")
            # Не добавляем pay_amount, пусть NOWPayments сам рассчитает
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        logger.info(f"🧾 Статус ответа NOWPayments: {response.status_code}")
        if response.status_code in [200, 201]:
            data = response.json()
            logger.debug(f"🧾 Данные инвойса от NOWPayments: {data}")
            return data
        else:
            error_text = response.text
            logger.error(
                f"🧾 Ошибка NOWPayments при создании инвойса: {response.status_code} - {error_text}"
            )
            return {"error": f"Ошибка API: {response.status_code}"}
    except requests.exceptions.RequestException as e:
        logger.error(f"🧾 Ошибка сети при создании инвойса NOWPayments: {e}")
        return {"error": f"Ошибка сети: {e}"}
    except Exception as e:
        logger.error(f"🧾 Исключение при создании инвойса NOWPayments: {e}")
        return {"error": f"Исключение: {e}"}
        
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /start."""
    logger.info(f"🚀 Вызов /start пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)
    is_owner = user.id in OWNER_IDS
    
    if is_owner:
        keyboard = [
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
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
    """Обрабатывает команду /help."""
    logger.info(f"📖 Вызов /help пользователем {update.effective_user.id}")
    help_text = (
        "👋 Доброго дня! Я бот магазину SecureShop.\n"
        "🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій. "
        "Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання "
        "по нашому сервісу можна задавати цілодобово.\n"
        "📌 Список доступних команд:\n"
        "/start - Головне меню\n"
        "/help - Ця довідка\n"
        "/order - Зробити замовлення\n"
        "/question - Поставити запитання\n"
        "/channel - Наш головний канал\n"
        "Також ви можете відправити команду `/pay` з сайту для оформлення замовлення."
    )
    await update.message.reply_text(help_text)

async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /channel."""
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
        "- ℹ️ Важливі оновлення сервісу\n"
        "Приєднуйтесь, щоб бути в курсі всіх новин! 👇"
    )
    await update.message.reply_text(message_text, reply_markup=reply_markup)

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /order."""
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
    """Обрабатывает команду /question."""
    logger.info(f"❓ Вызов /question пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)
    context.user_data["conversation_type"] = "question"
    await update.message.reply_text(
        "📝 Напишіть ваше запитання. Я передам його менеджеру магазину."
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /stats."""
    logger.info(f"📈 Вызов /stats пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if owner_id not in OWNER_IDS:
        return

    try:
        stats = get_stats()
        total_users_db = get_total_users_count()
        active_questions_db = get_active_questions_count()
        orders_db = get_orders_count()
        
        stats_message = (
            f"📊 Статистика бота:\n"
            f"👤 Усього користувачів (БД): {total_users_db}\n"
            f"🛒 Усього замовлень (БД): {stats['total_orders']}\n"
            f"❓ Усього запитаннь (БД): {stats['total_questions']}\n"
            f"👥 Активних запитаннь (БД): {active_questions_db}\n"
            f"📦 Усього записаних замовлень (БД): {orders_db}"
        )
        await update.message.reply_text(stats_message)
    except Exception as e:
        logger.error(f"Ошибка получения статистики из БД: {e}")
        await update.message.reply_text("❌ Помилка при отриманні статистики з бази даних.")

async def export_users_json(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экспортирует список пользователей в JSON файл (для разработчиков)."""
    logger.info(f"📁 Вызов /json пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if owner_id not in OWNER_IDS:
        await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
        return

    try:
        users_data_db = get_all_users()
        if not users_data_db:
             await update.message.reply_text("ℹ️ В базі даних немає користувачів для експорту.")
             return

        import io
        users_data = []
        for user in users_data_db:
            users_data.append({
                'id': user['id'],
                'username': user['username'],
                'first_name': user['first_name'],
                'last_name': user['last_name'],
                'language_code': user['language_code'],
                'is_bot': user['is_bot'],
                'created_at': user['created_at'].isoformat() if user['created_at'] else None,
                'updated_at': user['updated_at'].isoformat() if user['updated_at'] else None
            })

        json_data = json.dumps(users_data, ensure_ascii=False, indent=2).encode('utf-8')
        file = io.BytesIO(json_data)
        file.seek(0)
        file.name = 'users_export.json'
        await update.message.reply_document(document=file, caption="📊 Експорт усіх користувачів у JSON")
    except Exception as e:
        logger.error(f"Ошибка экспорта пользователей в JSON: {e}")
        await update.message.reply_text("❌ Помилка при експорті користувачів у JSON.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки."""
    query = update.callback_query
    await query.answer()
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
        context.user_data["conversation_type"] = "question"
        await query.message.edit_text(
            "📝 Напишіть ваше запитання. Я передам його менеджеру магазину."
        )
    elif query.data == "help":
        help_text = (
            "👋 Доброго дня! Я бот магазину SecureShop.\n"
            "🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій. "
            "Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання "
            "по нашому сервісу можна задавати цілодобово.\n"
            "📌 Список доступних команд:\n"
            "/start - Головне меню\n"
            "/help - Ця довідка\n"
            "/order - Зробити замовлення\n"
            "/question - Поставити запитання\n"
            "/channel - Наш головний канал\n"
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
            "- ℹ️ Важливі оновлення сервісу\n"
            "Приєднуйтесь, щоб бути в курсі всіх новин! 👇"
        )
        await query.message.edit_text(message_text, reply_markup=reply_markup)
    elif query.data == "back_to_main":
        is_owner = user.id in OWNER_IDS
        if is_owner:
            keyboard = [
                [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
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
                for option in plan_data.get('options', []):
                    callback_data = f"add_{service_key}_{plan_key}_{option['period'].replace(' ', '_')}_{option['price']}"
                    keyboard.append([InlineKeyboardButton(f"{option['period']} - {option['price']} UAH", callback_data=callback_data)])
                keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f'service_{service_key}')])
                await query.message.edit_text(
                    f"🛒 {service['name']} {plan_data['name']}\nОберіть період:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
    elif query.data.startswith('add_'):
        parts = query.data.split('_')
        if len(parts) < 5 or not parts[-1].isdigit():
             logger.error(f"❌ Неверный формат callback_data 'add_': {query.data}")
             await query.message.edit_text("❌ Помилка обробки вибору періоду.")
             return
        service_key = parts[1]
        plan_key = parts[2]
        price_str = parts[-1] 
        period_parts = parts[3:-1] 
        period_key = "_".join(period_parts)
        period = period_key.replace('_', ' ')
        try:
            price = int(price_str)
            service = SUBSCRIPTIONS.get(service_key)
            if service and plan_key in service['plans']:
                service_abbr = service_key[:3].capitalize()
                plan_abbr = plan_key.upper()
                period_abbr = period.replace('місяць', 'м').replace('місяців', 'м')
                order_id = 'O' + str(user_id)[-4:] + str(price)[-2:]
                command = f"/pay {order_id} {service_abbr}-{plan_abbr}-{period_abbr}-{price}"
                context.user_data['pending_order'] = {
                    'order_id': order_id,
                    'service': service['name'],
                    'plan': service['plans'][plan_key]['name'],
                    'period': period,
                    'price': price,
                    'command': command,
                    'type': 'subscription'
                }
                message = (
                    f"🛍️ Ваше замовлення готове!\n"
                    f"Скопіюйте цю команду та відправте її боту для підтвердження:\n"
                    f"<code>{command}</code>\n"
                    f"Або оберіть спосіб оплати нижче."
                )
                keyboard = [
                    [InlineKeyboardButton("💳 Оплатити карткою", callback_data=f"pay_card_{price}")],
                    [InlineKeyboardButton("₿ Оплатити криптовалютою", callback_data=f"select_crypto_{price}")],
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
            price_str = query.data.split('_')[2]
            price = int(price_str)
            pending_order = context.user_data.get('pending_order')
            if not pending_order:
                await query.message.edit_text("❌ Помилка: інформація про замовлення відсутня.")
                return
            formatted_card_number = f"`{CARD_NUMBER}`"
            message = (
                f"💳 Оплата карткою:\n"
                f"Сума: {price} UAH\n"
                f"Номер картки: {formatted_card_number}\n"
                f"(Натисніть на номер, щоб скопіювати)\n"
                f"Призначення платежу: Оплата за {pending_order['service']} {pending_order['plan']} ({pending_order['period']})\n"
                f"Після оплати натисніть кнопку нижче."
            )
            keyboard = [
                [InlineKeyboardButton("✅ Оплачено", callback_data='paid_card')],
                [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment')]
            ]
            await query.message.edit_text(
                message, 
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки оплаты картой: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати карткою.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке оплаты картой: {e}")
            await query.message.edit_text("❌ Неожиданная помилка обробки оплати карткою.")
    
    # --- Выбор криптовалюты для оплаты ---
    elif query.data.startswith('select_crypto_'):
        try:
            price_str = query.data.split('_')[2]
            price = int(price_str)
            
            # Создаем клавиатуру с выбором криптовалют
            keyboard = []
            for name, code in AVAILABLE_CURRENCIES.items():
                callback_data = f"pay_crypto_{price}_{code}"
                keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])
            
            keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment')])
            
            await query.message.edit_text(
                f"Оберіть криптовалюту для оплати {price} UAH:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки выбора криптовалюты: {e}")
            await query.message.edit_text("❌ Помилка обробки вибору криптовалюти.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке выбора криптовалюты: {e}")
            await query.message.edit_text("❌ Неожиданная помилка обробки вибору криптовалюти.")
    
    # --- Обработка оплаты КРИПТОВАЛЮТОЙ с конкретной сетью ---
    elif query.data.startswith('pay_crypto_'):
        try:
            logger.info(f"Начало обработки {query.data} для пользователя {user_id}")
            parts = query.data.split('_')
            logger.info(f"Разобранные части callback_ {parts}")
            if len(parts) < 4 or not parts[2].isdigit():
                raise ValueError("Некорректный формат callback_data для оплаты криптовалютой")
            
            price = int(parts[2])
            logger.info(f"Извлеченная цена: {price}")
            pay_currency_code = parts[3]
            logger.info(f"Извлеченный код валюты: {pay_currency_code}")
            
            pending_order = context.user_data.get('pending_order')
            logger.info(f"Полученный pending_order: {pending_order}")
            if not pending_order:
                logger.warning(f"Информация о заказе отсутствует для пользователя {user_id}")
                await query.message.edit_text("❌ Помилка: інформація про замовлення відсутня.")
                return
                
            # Получаем название криптовалюты для отображения
            currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == pay_currency_code), pay_currency_code)
            logger.info(f"Название валюты для отображения: {currency_name}")
            
            # Создаем инвойс в NOWPayments с указанной криптовалютой
            logger.info("Вызов create_nowpayments_invoice...")
            invoice_data = create_nowpayments_invoice(
                price_amount=price, 
                order_id=pending_order['order_id'], 
                product_name=f"{pending_order['service']} {pending_order['plan']} ({pending_order['period']})",
                pay_currency=pay_currency_code
            )
            logger.info(f"Результат создания инвойса: {invoice_data}")
            
            # Проверяем, успешно ли создан инвойс и содержит ли он URL
            if invoice_data and isinstance(invoice_data, dict) and 'invoice_url' in invoice_data:
                pay_url = invoice_data['invoice_url']
                logger.info(f"Извлеченный pay_url: {pay_url}")
                message = (
                    f"₿ Оплата криптовалютою:\n"
                    f"Сума: {price} UAH\n"
                    f"Валюта: {currency_name}\n"
                    f"Натисніть кнопку нижче для переходу до оплати.\n"
                    f"Після оплати натисніть кнопку \"✅ Оплачено\"."
                )
                keyboard = [
                    [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                    [InlineKeyboardButton("✅ Оплачено", callback_data='paid_crypto')],
                    [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment')]
                ]
                await query.message.edit_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                # Обрабатываем случай, если инвойс не создан или не содержит URL
                error_msg = invoice_data.get('error', 'Невідома помилка') if isinstance(invoice_data, dict) else str(invoice_data) if invoice_data else 'Невідома помилка'
                logger.error(f"Помилка отримання посилання оплати від NOWPayments: {invoice_data}")
                await query.message.edit_text(f"❌ Помилка створення інвойсу для оплати криптовалютою: {error_msg}")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки оплаты криптовалютой (Value/Index) для пользователя {user_id}: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати криптовалютою.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке оплаты криптовалютой для пользователя {user_id}: {e}", exc_info=True)
            await query.message.edit_text("❌ Неожиданная помилка обробки оплати криптовалютою.")

    # --- Обработка нажатия кнопки "ОПЛАЧЕНО" (карта или крипта) ---
    elif query.data in ['paid_card', 'paid_crypto']:
        pending_order = context.user_data.get('pending_order')
        if pending_order:# Формируем сообщение для владельцев
            order_summary = (
                f"🛍️ НОВЕ ЗАМОВЛЕННЯ #{pending_order['order_id']}\n"
                f"👤 Клієнт: @{user.username or user.first_name} (ID: {user_id})\n"
                f"📦 Деталі замовлення:\n"
                f"▫️ Сервіс: {pending_order['service']}\n"
                f"▫️ План: {pending_order['plan']}\n"
                f"▫️ Період: {pending_order['period']}\n"
                f"▫️ Сума: {pending_order['price']} UAH\n"
                f"💳 ЗАГАЛЬНА СУМА: {pending_order['price']} UAH\n"
                f"Команда для підтвердження: <code>{pending_order['command']}</code>"
            )
            # Отправляем уведомление всем владельцам
            success = False
            for owner_id in OWNER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=owner_id,
                        text=order_summary,
                        parse_mode='HTML'
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
                f"Сума: {pending_order['price']} UAH\n"
                f"Ви можете зробити нове замовлення через /start."
            )
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
            if product_data.get('category') == 'bzn':
                keyboard.append([InlineKeyboardButton(f"{product_data['name']} - {product_data['price']} UAH", callback_data=product_callback)])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="digital_discord_decor")])
        await query.message.edit_text("🎨 Discord Украшення (Без Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "discord_decor_zn":
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            if product_data.get('category') == 'zn':
                keyboard.append([InlineKeyboardButton(f"{product_data['name']} - {product_data['price']} UAH", callback_data=product_callback)])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="digital_discord_decor")])
        await query.message.edit_text("✨ Discord Украшення (З Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "digital_psn_cards":
        keyboard = []
        for product_callback, product_id in DIGITAL_PRODUCT_MAP.items():
            product_data = DIGITAL_PRODUCTS[product_id]
            if product_data.get('category') == 'psn':
                keyboard.append([InlineKeyboardButton(f"{product_data['name']} - {product_data['price']} UAH", callback_data=product_callback)])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="order_digital")])
        await query.message.edit_text("🎮 PSN Gift Cards:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data.startswith('digital_'):
        product_id = DIGITAL_PRODUCT_MAP.get(query.data)
        if product_id:
            product_data = DIGITAL_PRODUCTS[product_id]
            order_id = 'D' + str(user_id)[-4:] + str(product_data['price'])[-2:]
            service_abbr = "Dis" if "Discord" in product_data['name'] else "Dig"
            plan_abbr = "Dec" if "Украшення" in product_data['name'] else "Prod"
            price = product_data['price']
            command = f"/pay {order_id} {service_abbr}-{plan_abbr}-1шт-{price}"
            context.user_data['pending_order'] = {
                'order_id': order_id,
                'service': "Цифровий товар",
                'plan': product_data['name'],
                'period': "1 шт",
                'price': price,
                'command': command,
                'type': 'digital'
            }
            message = (
                f"🛍️ Ваше замовлення готове!\n"
                f"Скопіюйте цю команду та відправте її боту для підтвердження:\n"
                f"<code>{command}</code>\n"
                f"Або оберіть спосіб оплати нижче."
            )
            keyboard = [
                [InlineKeyboardButton("💳 Оплатити карткою", callback_data=f"pay_card_{price}")],
                [InlineKeyboardButton("₿ Оплатити криптовалютою", callback_data=f"select_crypto_{price}")],
                [InlineKeyboardButton("📋 Головне меню", callback_data='back_to_main')]
            ]
            await query.message.edit_text(message, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.message.edit_text("❌ Помилка: цифровий товар не знайдено.")

    # --- Обработка оплаты КАРТОЙ из команды /pay ---
    elif query.data.startswith('pay_card_from_command_'):
        try:
            price_str = query.data.split('_')[-1]
            price = int(price_str)
            formatted_card_number = f"`{CARD_NUMBER}`"
            message = (
                f"💳 Оплата карткою:\n"
                f"Сума: {price} UAH\n"
                f"Номер картки: {formatted_card_number}\n"
                f"(Натисніть на номер, щоб скопіювати)\n"
                f"Призначення платежу: Оплата замовлення #{context.user_data.get('pending_order_from_command', {}).get('order_id', 'N/A')}\n"
                f"Після оплати натисніть кнопку нижче."
            )
            keyboard = [
                [InlineKeyboardButton("✅ Оплачено", callback_data='paid_after_command')],
                [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment_command')]
            ]
            await query.message.edit_text(
                message,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except (ValueError, IndexError) as e:
            logger.error(f"Помилка обробки оплати карткою з /pay: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати карткою.")
        except Exception as e:
            logger.error(f"Неочікувана помилка при обробці оплати карткою з /pay: {e}")
            await query.message.edit_text("❌ Неочікувана помилка обробки оплати карткою.")

    # --- Выбор криптовалюты для оплаты из команды /pay ---
    elif query.data.startswith('select_crypto_from_command_'):
        try:
            price_str = query.data.split('_')[-1]
            price = int(price_str)
            
            # Создаем клавиатуру с выбором криптовалют
            keyboard = []
            for name, code in AVAILABLE_CURRENCIES.items():
                callback_data = f"pay_crypto_from_command_{price}_{code}"
                keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])
            
            keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment_command')])
            
            await query.message.edit_text(
                f"Оберіть криптовалюту для оплати {price} UAH:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки выбора криптовалюты из /pay: {e}")
            await query.message.edit_text("❌ Помилка обробки вибору криптовалюти.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке выбора криптовалюты из /pay: {e}")
            await query.message.edit_text("❌ Неожиданная помилка обробки вибору криптовалюти.")

    # --- Обработка оплаты КРИПТОВАЛЮТОЙ с конкретной сетью из команды /pay ---
    elif query.data.startswith('pay_crypto_from_command_'):
        try:
            logger.info(f"Начало обработки {query.data} для пользователя {user_id}")
            parts = query.data.split('_')
            logger.info(f"Разобранные части callback_ {parts}")
            if len(parts) < 5 or not parts[4].isdigit():
                raise ValueError("Некорректный формат callback_data для оплаты криптовалютой из /pay")
            
            price = int(parts[4])
            logger.info(f"Извлеченная цена: {price}")
            pay_currency_code = parts[5]
            logger.info(f"Извлеченный код валюты: {pay_currency_code}")
            
            pending_order_data = context.user_data.get('pending_order_from_command')
            logger.info(f"Полученный pending_order_from_command: {pending_order_data}")
            if not pending_order_:
                logger.warning(f"Информация о заказе из /pay отсутствует для пользователя {user_id}")
                await query.message.edit_text("❌ Помилка: інформація про замовлення відсутня.")
                return
                
            # Получаем название криптовалюты для отображения
            currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == pay_currency_code), pay_currency_code)
            logger.info(f"Название валюты для отображения: {currency_name}")
            
            # Создаем инвойс в NOWPayments с указанной криптовалютой
            logger.info("Вызов create_nowpayments_invoice...")
            invoice_data = create_nowpayments_invoice(
                price_amount=price, 
                order_id=pending_order_data['order_id'], 
                product_name="Замовлення через /pay",
                pay_currency=pay_currency_code
            )
            logger.info(f"Результат создания инвойса: {invoice_data}")
            
            # Проверяем, успешно ли создан инвойс и содержит ли он URL
            if invoice_data and isinstance(invoice_data, dict) and 'invoice_url' in invoice_:
                pay_url = invoice_data['invoice_url']
                logger.info(f"Извлеченный pay_url: {pay_url}")
                message = (
                    f"₿ Оплата криптовалютою:\n"
                    f"Сума: {price} UAH\n"
                    f"Валюта: {currency_name}\n"
                    f"Натисніть кнопку нижче для переходу до оплати.\n"
                    f"Після оплати натисніть кнопку \"✅ Оплачено\"."
                )
                keyboard = [
                    [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                    [InlineKeyboardButton("✅ Оплачено", callback_data='paid_after_command')],
                    [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment_command')]
                ]
                await query.message.edit_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                # Обрабатываем случай, если инвойс не создан или не содержит URL
                error_msg = invoice_data.get('error', 'Невідома помилка') if isinstance(invoice_data, dict) else str(invoice_data) if invoice_data else 'Невідома помилка'
                logger.error(f"Помилка отримання посилання оплати від NOWPayments: {invoice_data}")
                await query.message.edit_text(f"❌ Помилка створення інвойсу для оплати криптовалютою: {error_msg}")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки оплаты криптовалютой из /pay (Value/Index) для пользователя {user_id}: {e}")
            await query.message.edit_text("❌ Помилка обробки оплати криптовалютою.")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при обработке оплаты криптовалютой из /pay для пользователя {user_id}: {e}", exc_info=True)
            await query.message.edit_text("❌ Неожиданная помилка обробки оплати криптовалютою.")

    # --- Обработка натискання кнопки "ОПЛАЧЕНО" після команди /pay ---
    elif query.data == 'paid_after_command':
        # Отримуємо інформацію про замовлення з user_data
        pending_order_data = context.user_data.get('pending_order_from_command')
        if pending_order_:
            order_id = pending_order_data['order_id']
            total_uah = pending_order_data['total_uah']
            order_text = pending_order_data['order_text']
            
            # Сохраняем заказ в БД
            try:
                save_order(user_id, order_id, order_text, total_uah)
                increment_orders() # Увеличиваем счетчик заказов
            except Exception as e:
                logger.error(f"Ошибка сохранения заказа из /pay: {e}")

            # Формуємо повідомлення для власників
            order_summary = (
                f"🛍️ НОВЕ ЗАМОВЛЕННЯ #{order_id}\n"
                f"👤 Клієнт: @{user.username or user.first_name} (ID: {user_id})\n"
                f"{order_text}\n"
                f"Команда для підтвердження: <code>/pay {order_id} {pending_order_data['items_str']}</code>"
            )

            # Відправляємо повідомлення всім власникам
            success = False
            for owner_id in OWNER_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=owner_id,
                        text=order_summary,
                        parse_mode='HTML'
                    )
                    success = True
                except Exception as e:
                    logger.error(f"Не вдалося відправити повідомлення власнику {owner_id}: {e}")

            if success:
                await query.message.edit_text("✅ Дякуємо за оплату! Ми зв'яжемося з вами найближчим часом для підтвердження замовлення.")
            else:
                await query.message.edit_text("✅ Дякуємо за оплату! Виникла помилка при відправці сповіщення, але оплата прийнята.")

            # Очищуємо дані замовлення з user_data
            context.user_data.pop('pending_order_from_command', None)
        else:
            await query.message.edit_text("ℹ️ Інформація про оплату вже оброблена або відсутня.")

    # --- Обработка натискання кнопки "СКАСУВАТИ" оплату з команды /pay---
    elif query.data == 'cancel_payment_command':
        pending_order_data = context.user_data.get('pending_order_from_command')
        if pending_order_:
            await query.message.edit_text(
                f"❌ Оплата скасована.\n"
                f"Номер замовлення: #{pending_order_data['order_id']}\n"
                f"Сума: {pending_order_data['total_uah']} UAH\n"
                f"Ви можете зробити нове замовлення через /start або повторно відправити команду /pay."
            )
            # Очищуємо дані замовлення
            context.user_data.pop('pending_order_from_command', None)
        else:
            await query.message.edit_text("❌ Оплата вже скасована або відсутня.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает текстовые сообщения."""
    logger.info(f"📨 Получено текстовое сообщение от пользователя {update.effective_user.id}")
    user = update.effective_user
    user_id = user.id
    message_text = update.message.text
    ensure_user_exists(user)
    
    # --- Обработка данных для подписки после оплаты ---
    awaiting_data = context.user_data.get('awaiting_subscription_data', False)
    if awaiting_:
        subscription_details = context.user_data.get('subscription_order_details', {})
        if subscription_details:
            # Формируем сообщение с данными для владельцев
            data_message = (
                f"🔐 Дані для замовлення (Підписка) #{subscription_details['order_id']} від @{user.username or user.first_name} (ID: {user_id}):\n"
                f"📦 Сервіс: {subscription_details['service']}\n"
                f"▫️ План: {subscription_details['plan']}\n"
                f"▫️ Період: {subscription_details['period']}\n"
                f"▫️ Сума: {subscription_details['price']} UAH\n"
                f"🔑 Логін/Пароль:\n{message_text}"
            )
            # Отправляем владельцам
            success = False
            for owner_id in OWNER_IDS:
                try:
                    await context.bot.send_message(chat_id=owner_id, text=data_message)
                    success = True
                except Exception as e:
                    logger.error(f"Не удалось отправить данные владельцу {owner_id}: {e}")
            
            if success:
                await update.message.reply_text("✅ Дякуємо! Дані отримано. Наш менеджер зв'яжеться з вами найближчим часом.")
            else:
                await update.message.reply_text("❌ Виникла помилка при відправці даних. Спробуйте ще раз пізніше або зв'яжіться з підтримкою.")
            
            # Очищаем флаги
            context.user_data.pop('awaiting_subscription_data', None)
            context.user_data.pop('subscription_order_details', None)
            return # Завершаем обработку

    # --- Обработка вопросов ---
    conversation_type = context.user_data.get('conversation_type')
    if conversation_type == 'question':
        # Сохраняем вопрос в БД
        try:
            save_question(user_id, message_text)
            increment_questions() # Увеличиваем счетчик вопросов
        except Exception as e:
            logger.error(f"Ошибка сохранения вопроса в БД: {e}")
        
        forward_message = (
            f"❓ Нове запитання від клієнта:\n"
            f"👤 Клієнт: {user.first_name}\n"
            f"📱 Username: @{user.username if user.username else 'не вказано'}\n"
            f"🆔 ID: {user.id}\n"
            f"💬 Повідомлення:\n{message_text}"
        )
        # Отправляем вопрос менеджеру
        try:
            await context.bot.send_message(chat_id=MANAGER_ID, text=forward_message)
            await update.message.reply_text("✅ Ваше запитання надіслано. Очікуйте відповіді від менеджера.")
        except Exception as e:
            logger.error(f"Не удалось отправить вопрос менеджеру {MANAGER_ID}: {e}")
            await update.message.reply_text("❌ На жаль, не вдалося надіслати ваше запитання. Спробуйте пізніше.")
        
        # --- ВАЖНО: Завершаем режим вопроса после первого сообщения ---
        context.user_data.pop('conversation_type', None)
        # Не возвращаемся в start автоматически, пользователь должен сам вызвать /start
        return

    # --- Обработка команды /pay ---
    if message_text.startswith('/pay'):
        await pay_command(update, context)
        return

    # --- Если ничего не подошло, предлагаем меню ---
    # Это будет срабатывать для любого текстового сообщения, если не активен awaiting_subscription_data или conversation_type
    await start(update, context)

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /pay."""
    logger.info(f"💰 Вызов команды /pay пользователем {update.effective_user.id}")
    user = update.effective_user
    ensure_user_exists(user)
    if not context.args:
        await update.message.reply_text("❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ...")
        return
    order_id = context.args[0]
    items_str = " ".join(context.args[1:])
    pattern = r'(\w{2,4})-(\w{2,4})-([\w\s$]+?)-(\d+)'
    items = re.findall(pattern, items_str)
    if not items:
        await update.message.reply_text("❌ Не вдалося розпізнати товари у замовленні. Перевірте формат.")
        return
    order_text = f"🛍️ Нове замовлення #{order_id} від @{user.username or user.first_name} (ID: {user.id})\n"
    total_uah = 0
    order_details = []
    for service_abbr, plan_abbr, period, price_str in items:
        price = int(price_str)
        total_uah += price
        order_details.append(f"▫️ {service_abbr}-{plan_abbr}-{period} - {price} UAH")
    order_text += "\n".join(order_details)
    order_text += f"\n💳 Всього: {total_uah} UAH"

    context.user_data['pending_order_from_command'] = {
        'order_id': order_id,
        'items_str': items_str,
        'total_uah': total_uah,
        'order_text': order_text
    }

    # Отправляем уведомление владельцам (можно убрать, если не нужно дублировать)
    success = False
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(chat_id=owner_id, text=order_text)
            success = True
        except Exception as e:
            logger.error(f"Не удалось отправить заказ владельцу {owner_id}: {e}")

    # Создаем инвойс в NOWPayments
    invoice_data = create_nowpayments_invoice(total_uah, order_id, "Замовлення через /pay")
    
    # Формируем сообщение и клавиатуру
    if invoice_data and isinstance(invoice_data, dict) and 'invoice_url' in invoice_:
        pay_url = invoice_data['invoice_url']
        payment_message = (
            f"✅ Дякуємо за замовлення #{order_id}!\n"
            f"💳 Сума до сплати: {total_uah} UAH\n"
            f"Оберіть спосіб оплати:"
        )
        
        # Клавиатура с кнопками оплаты и "Оплачено"
        keyboard = [
            [InlineKeyboardButton("₿ Оплатити криптовалютою", url=pay_url)],
            [InlineKeyboardButton("💳 Оплатити карткою", callback_data=f"pay_card_from_command_{total_uah}")],
            [InlineKeyboardButton("💱 Вибрати криптовалюту", callback_data=f"select_crypto_from_command_{total_uah}")],
            [InlineKeyboardButton("✅ Оплачено", callback_data='paid_after_command')],
            [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment_command')]
        ]
        
        await update.message.reply_text(
            payment_message, 
            parse_mode='Markdown', 
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        error_msg = invoice_data.get('error', 'Невідома помилка') if isinstance(invoice_data, dict) else str(invoice_data) if invoice_data else 'Невідома помилка'
        await update.message.reply_text(
            f"✅ Дякуємо за замовлення #{order_id}!\n"
            f"💳 Сума до сплати: {total_uah} UAH\n"
            f"⚠️ Помилка створення посилання для оплати: {error_msg}\n"
            f"Ми зв'яжемося з вами найближчим часом для підтвердження."
        )
        # Даже если инвойс не создан, показываем кнопки оплаты (кроме крипты) и "Оплачено"
        keyboard = [
            [InlineKeyboardButton("💳 Оплатити карткою", callback_data=f"pay_card_from_command_{total_uah}")],
            [InlineKeyboardButton("💱 Вибрати криптовалюту", callback_data=f"select_crypto_from_command_{total_uah}")],
            [InlineKeyboardButton("✅ Оплачено", callback_data='paid_after_command')],
            [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment_command')]
        ]
        await update.message.reply_text(
            "Оберіть спосіб оплати:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

def main() -> None:
    """Главная функция запуска бота."""
    logger.info("🚀 Инициализация приложения бота...")
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.critical("🔑 BOT_TOKEN не установлен или имеет значение по умолчанию!")
        return
    if not DATABASE_URL or DATABASE_URL == "YOUR_DATABASE_URL_HERE":
        logger.critical("💾 DATABASE_URL не установлен или имеет значение по умолчанию!")
        return
    
    # Инициализируем БД
    init_db()
    
    port = int(os.environ.get('PORT', 10000))
    http_thread = Thread(target=start_http_server, args=(port,), daemon=True)
    http_thread.start()
    logger.info(f"🌐 HTTP сервер запущен в потоке на порту {port}")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # --- Регистрация обработчиков команд ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("order", order_command))
    application.add_handler(CommandHandler("question", question_command))
    application.add_handler(CommandHandler("channel", channel_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("json", export_users_json)) # Новая команда для разработчиков
    application.add_handler(CommandHandler("pay", pay_command))
    
    # --- Регистрация обработчиков callback-запросов ---
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # --- Регистрация обработчика текстовых сообщений ---
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # --- Установка команд меню ---
    async def set_commands_menu(application):
        # Команды для обычных пользователей
        user_commands = [
            BotCommand("start", "Головне меню"),
            BotCommand("help", "Допомога та інформація"),
            BotCommand("order", "Зробити замовлення"),
            BotCommand("question", "Поставити запитання"),
            BotCommand("channel", "Наш головний канал"),
        ]
        
        # Команды для владельцев
        owner_commands = user_commands + [
            BotCommand("stats", "Статистика бота"),
            BotCommand("json", "Експорт користувачів у JSON (для розробників)"),
        ]
        
        try:
            # Устанавливаем команды для обычных пользователей
            await application.bot.set_my_commands(user_commands)
            # Устанавливаем команды для владельцев
            for owner_id in OWNER_IDS:
                await application.bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(owner_id))
        except Exception as e:
            logger.error(f"Ошибка установки команд меню: {e}")

    # --- Обработчик сигналов завершения ---
    def signal_handler(signum, frame):
        """Обработчик сигналов завершения (Ctrl+C, SIGTERM)."""
        logger.info("🛑 Принято сигнал завершения. Остановка бота...")
        stop_ping_service() # Останавливаем пинговалку
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # --- Запуск сервисов ---
    start_ping_service() # Запускаем пинговалку
    
    # Устанавливаем команды меню
    application.post_init = set_commands_menu
    
    logger.info("🤖 Бот запущен. Нажмите Ctrl+C для остановки.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()







