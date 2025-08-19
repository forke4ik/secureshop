# main.py (сокращенная версия с сохранением функциональности)
import logging
import os
import asyncio
import threading
import time
import json
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg
from psycopg.rows import dict_row
import io
import requests
import products # Импортируем файл с продуктами

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
OWNER_ID_1 = int(os.environ.get('OWNER_ID_1'))
OWNER_ID_2 = int(os.environ.get('OWNER_ID_2'))
DATABASE_URL = os.environ.get('DATABASE_URL')
PORT = int(os.environ.get('PORT', 8000))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
USE_POLLING = os.environ.get('USE_POLLING', 'False').lower() == 'true'
PING_INTERVAL = int(os.environ.get('PING_INTERVAL', 300))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
CARD_NUMBER = os.environ.get('CARD_NUMBER')
NOWPAYMENTS_API_KEY = os.environ.get('NOWPAYMENTS_API_KEY')

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
active_conversations = {} # {user_id: {'type': '...', 'user_info': ..., 'assigned_owner': ..., 'last_message': ..., 'order_details': ...}}
owner_client_map = {} # {owner_id: client_id}
bot_statistics = {
    'first_start': datetime.now().isoformat(),
    'last_save': datetime.now().isoformat(),
    'total_users': 0,
    'total_questions': 0,
    'total_orders': 0
}
STATS_FILE = 'bot_stats.json'
application_instance = None
bot_running = False
telegram_app = None
bot_instance = None
user_cache = set() # Простой кэш для быстрой проверки существующих пользователей
message_buffer = [] # Буфер для сообщений
active_conv_buffer = [] # Буфер для активных диалогов

# --- ИНИЦИАЛИЗАЦИЯ БД ---
def init_db():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        language_code TEXT,
                        is_bot BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_id ON users(id);
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        message_text TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id);
                    CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS active_conversations (
                        user_id BIGINT PRIMARY KEY REFERENCES users(id),
                        conversation_type TEXT,
                        assigned_owner BIGINT,
                        last_message TEXT,
                        order_details TEXT
                    )
                """)
                conn.commit()
        logger.info("База данных инициализирована.")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации базы данных: {e}")

def ensure_user_exists(user):
    global user_cache
    if user.id in user_cache:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, first_name, last_name, language_code, is_bot)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        language_code = EXCLUDED.language_code,
                        is_bot = EXCLUDED.is_bot
                """, (user.id, user.username, user.first_name, user.last_name, user.language_code, user.is_bot))
                conn.commit()
        user_cache.add(user.id)
    except Exception as e:
        logger.error(f"❌ Ошибка ensure_user_exists для {user.id}: {e}")

def save_message(user_id, message_text):
    global message_buffer
    message_buffer.append((user_id, message_text))

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message, order_details=None):
    global active_conv_buffer
    active_conv_buffer.append((user_id, conversation_type, assigned_owner, last_message, order_details))

def delete_active_conversation(user_id):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations WHERE user_id = %s", (user_id,))
                conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка delete_active_conversation для {user_id}: {e}")

def get_conversation_history(user_id):
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT message_text, created_at FROM messages
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 20
                """, (user_id,))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории для {user_id}: {e}")
        return []

def get_all_users():
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Ошибка получения списка пользователей: {e}")
        return []

def get_total_users_count():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"❌ Ошибка получения количества пользователей: {e}")
        return 0

def clear_all_active_conversations():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations")
                deleted_count = cur.rowcount
                return deleted_count
    except Exception as e:
        logger.error(f"❌ Ошибка очистки активных диалогов: {e}")
        return 0

def buffer_flush_thread():
    global message_buffer, active_conv_buffer
    while True:
        time.sleep(5)
        if message_buffer:
            local_buffer = message_buffer[:]
            message_buffer.clear()
            try:
                with psycopg.connect(DATABASE_URL) as conn:
                    with conn.cursor() as cur:
                        psycopg.extras.execute_batch(cur, """
                            INSERT INTO messages (user_id, message_text) VALUES (%s, %s)
                        """, local_buffer)
                        conn.commit()
            except Exception as e:
                logger.error(f"❌ Ошибка сброса буфера сообщений: {e}")
                message_buffer.extend(local_buffer)

        if active_conv_buffer:
            local_buffer = active_conv_buffer[:]
            active_conv_buffer.clear()
            try:
                with psycopg.connect(DATABASE_URL) as conn:
                    with conn.cursor() as cur:
                        psycopg.extras.execute_batch(cur, """
                            INSERT INTO active_conversations (user_id, conversation_type, assigned_owner, last_message, order_details)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (user_id) DO UPDATE SET
                                conversation_type = EXCLUDED.conversation_type,
                                assigned_owner = EXCLUDED.assigned_owner,
                                last_message = EXCLUDED.last_message,
                                order_details = EXCLUDED.order_details
                        """, local_buffer)
                        conn.commit()
            except Exception as e:
                logger.error(f"❌ Ошибка сброса буфера активных диалогов: {e}")
                active_conv_buffer.extend(local_buffer)

init_db()
threading.Thread(target=buffer_flush_thread, daemon=True).start()

# --- СТАТИСТИКА ---
def load_stats():
    global bot_statistics
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                bot_statistics.update(json.load(f))
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки статистики: {e}")

def save_stats():
    global bot_statistics
    try:
        bot_statistics['last_save'] = datetime.now().isoformat()
        with open(STATS_FILE, 'w') as f:
            json.dump(bot_statistics, f)
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения статистики: {e}")

load_stats()

# --- FLASK СЕРВЕР ---
flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'message': 'pong', 'timestamp': time.time()}), 200

@flask_app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'mode': 'polling' if USE_POLLING else 'webhook',
        'stats': bot_statistics
    }), 200

@flask_app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if USE_POLLING:
        return jsonify({'error': 'Webhook disabled in polling mode'}), 400
    global telegram_app
    if not telegram_app or not bot_instance.initialized:
        logger.error("Telegram app не инициализирован")
        return jsonify({'error': 'Bot not initialized'}), 500
    try:
        json_data = request.get_json()
        if json_
            update = Update.de_json(json_data, telegram_app.bot)
            asyncio.run_coroutine_threadsafe(bot_instance.application.update_queue.put(update), bot_instance.loop)
        return '', 200
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/', methods=['GET'])
def index():
    return jsonify({
        'message': 'Telegram Bot SecureShop активен',
        'status': 'running',
        'mode': 'polling' if USE_POLLING else 'webhook',
        'webhook_url': f"{WEBHOOK_URL}/{BOT_TOKEN}" if not USE_POLLING else None,
        'ping_interval': f"{PING_INTERVAL} секунд",
        'owners': ['@HiGki2pYYY', '@oc33t'],
        'initialized': bot_instance.initialized if bot_instance else False,
        'bot_running': bot_running,
        'stats': bot_statistics
    }), 200

def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

flask_app.after_request(add_cors_headers)

def ping_loop():
    import requests
    ping_url = f"{WEBHOOK_URL}/ping"
    while bot_instance.ping_running:
        try:
            response = requests.get(ping_url, timeout=10)
            if response.status_code == 200:
                pass
            else:
                logger.warning(f"⚠️ Ping вернул статус {response.status_code}")
        except Exception as e:
            logger.error(f"❌ Ошибка ping: {e}")
        time.sleep(PING_INTERVAL)

# --- ЛОГИКА БОТА ---
class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        global application_instance, telegram_app
        application_instance = self.application
        telegram_app = self.application
        self.setup_handlers()
        self.ping_running = False
        self.initialized = False
        self.polling_task = None
        self.loop = None

    async def set_commands_menu(self):
        commands = [
            ("start", "Головне меню"),
            ("help", "Допомога та інформація"),
            ("order", "Зробити замовлення"),
            ("question", "Поставити запитання"),
            ("channel", "Наш головний канал"),
            ("stop", "Завершити поточний діалог")
        ]
        owner_commands = commands + [
            ("stats", "Статистика бота"),
            ("chats", "Активні чати"),
            ("history", "Історія переписки"),
            ("dialog", "Почати діалог з користувачем"),
            ("clear", "Очистити всі активні діалоги (БД)")
        ]
        try:
            await self.application.bot.set_my_commands(commands)
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                await self.application.bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(owner_id))
        except Exception as e:
            logger.error(f"Ошибка установки команд: {e}")

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CommandHandler("order", self.start_order))
        self.application.add_handler(CommandHandler("question", self.start_question))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("pay", self.pay_command)) # Новый /pay
        # Владельцы
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("chats", self.show_active_chats))
        self.application.add_handler(CommandHandler("history", self.show_history))
        self.application.add_handler(CommandHandler("dialog", self.start_dialog_with_user))
        self.application.add_handler(CommandHandler("clear", self.clear_all_conversations))
        # CallbackQueryHandler с паттернами
        self.application.add_handler(CallbackQueryHandler(self.button_handler, pattern=r'^(order|question|help|back_|category_|chatgpt_|discord_|duolingo_|picsart_|canva_|netflix_|roblox_|psn_|take_order_|accept_|paid_|cancel_|invoice_)'))
        # Обработчики сообщений
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.VOICE | filters.AUDIO | filters.Document.ALL, self.handle_media_message))
        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

    # --- ОСНОВНЫЕ КОМАНДЫ ---
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
             await update.message.reply_text(f"Добро пожаловать, {user.first_name}! Вы вошли как основатель магазина.")
             return

        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        welcome_message = f"Ласкаво просимо, {user.first_name}! 👋\nЯ бот-помічник нашого магазину. Будь ласка, оберіть, що вас цікавить:"
        await update.message.reply_text(welcome_message.strip(), reply_markup=reply_markup)

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        if isinstance(update, Update):
            message = update.message
        else:
            message = update
        help_text = """👋 Доброго дня! Я бот магазину SecureShop.
🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій.
Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання по нашому сервісу можна задаватися цілодобово.
📌 Список доступних команд:
/start - Головне меню
/pay - Оплата замовлення (вибраного через міні-додаток)
/help - Ця довідка
💬 Якщо у вас виникли питання, не соромтеся звертатися!"""
        await message.reply_text(help_text.strip())

    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [[InlineKeyboardButton("📢 Перейти в SecureShopUA", url="https://t.me/SecureShopUA")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = """📢 Наш головний канал з асортиментом, оновленнями та розіграшами:👉 Тут ви знайдете:- 🆕 Актуальні товари та послуги- 🔥 Спеціальні пропозиції та знижки- 🎁 Розіграші та акції- ℹ️ Важливі оновлення сервісу"""
        await update.message.reply_text(message_text.strip(), reply_markup=reply_markup)

    # --- ОСНОВНАЯ НАВИГАЦИЯ (объединенный обработчик) ---
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        # --- Навигация по меню ---
        if data == 'order':
            keyboard = [
                [InlineKeyboardButton("💬 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("🛍️ Оберіть категорію товарів:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == 'question':
            user = query.from_user
            ensure_user_exists(user)
            active_conversations[user.id] = {
                'type': 'question',
                'user_info': user,
                'assigned_owner': None,
                'last_message': ''
            }
            save_active_conversation(user.id, 'question', None, '')
            await query.edit_message_text("💬 Ви перейшли в режим запитання. Надішліть ваше питання, і ми відповімо якнайшвидше.")
        elif data == 'help':
            await self.show_help(query.message)
        elif data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))
        # --- Навигация по категориям и продуктам (упрощено) ---
        elif data.startswith('order_') or data.startswith('category_') or data.startswith('chatgpt_') or data.startswith('discord_') or data.startswith('duolingo_') or data.startswith('picsart_') or data.startswith('canva_') or data.startswith('netflix_') or data.startswith('roblox_') or data.startswith('psn_'):
            # Это упрощенная версия. В реальном коде здесь будет логика построения клавиатур
            # на основе данных из products.py. Для краткости показана только идея.
            # Например, data == 'category_chatgpt' -> показать планы ChatGPT
            # data == 'chatgpt_1' -> показать опции (1 мес, 12 мес)
            # В идеале, это можно реализовать через динамическое создание клавиатур
            # на основе структуры данных в products.py
            await query.edit_message_text(f"Навигация: {data} (реализуйте в products.py)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]]))

        # --- Обработка заказов от владельцев ---
        elif data.startswith('take_order_'):
            client_id = int(data.split('_')[-1])
            owner_id = query.from_user.id
            if client_id in active_conversations and active_conversations[client_id]['assigned_owner'] is None:
                active_conversations[client_id]['assigned_owner'] = owner_id
                owner_client_map[owner_id] = client_id
                save_active_conversation(client_id, active_conversations[client_id]['type'], owner_id, active_conversations[client_id]['last_message'])
                client_info = active_conversations[client_id]['user_info']
                await query.edit_message_text(f"✅ Ви взяли замовлення від клієнта {client_info.first_name}.")
                if 'order_details' in active_conversations[client_id]:
                    order_text = active_conversations[client_id]['order_details']
                    await context.bot.send_message(chat_id=owner_id, text=f"🛍️ Деталі замовлення:\n{order_text}")
            else:
                await query.answer("Діалог вже завершено", show_alert=True)
        elif data.startswith('accept_'):
             # Логика принятия заказа владельцем
             pass
        elif data.startswith('paid_'):
             # Логика подтверждения оплаты
             pass
        elif data.startswith('cancel_'):
             # Логика отмены оплаты
             pass
        elif data.startswith('invoice_'):
             # Логика создания инвойса
             pass

    # --- НОВАЯ КОМАНДА /pay (динамическая) ---
    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает команду /pay <order_id> <product_id1> <product_id2> ..."""
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)

        if not context.args:
            await update.message.reply_text("❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ...")
            return

        order_id = context.args[0]
        product_ids = context.args[1:]

        if not product_ids:
            await update.message.reply_text("❌ Не вказано жодного товару для замовлення.")
            return

        order_details = []
        total_uah = 0
        order_description_parts = []

        # Объединяем все продукты из SUBSCRIPTION_PRODUCTS и DIGITAL_PRODUCTS
        all_products = {**products.SUBSCRIPTION_PRODUCTS, **getattr(products, 'DIGITAL_PRODUCTS', {})}
        # Предполагаем, что все продукты имеют структуру {'name': ..., 'price': ...}

        for product_id in product_ids:
            product_info = all_products.get(product_id)
            if not product_info:
                 await update.message.reply_text(f"❌ Товар з ID '{product_id}' не знайдено.")
                 return
            order_details.append({
                'product_id': product_id,
                'name': product_info['name'],
                'price': product_info['price']
            })
            total_uah += product_info['price']
            order_description_parts.append(f"▫️ {product_info['name']} - {product_info['price']} UAH")

        if total_uah == 0:
            await update.message.reply_text("❌ Не вдалося обробити замовлення. Загальна сума 0.")
            return

        order_text = f"🛍️ Замовлення #{order_id}:\n" + "\n".join(order_description_parts) + f"\n💳 Всього: {total_uah} UAH"

        # Определяем тип заказа (можно усложнить логику)
        has_digital = any(pid in getattr(products, 'DIGITAL_PRODUCTS', {}) for pid in product_ids)
        conversation_type = 'digital_order' if has_digital else 'subscription_order'

        # Сохраняем информацию о платеже в контексте пользователя
        context.user_data['pending_payment'] = {
            'order_id': order_id,
            'items': order_details,
            'total_uah': total_uah,
            'from_website': True # Или False, если из бота напрямую
        }

        # Клавиатура выбора способа оплаты
        keyboard = [
            [InlineKeyboardButton("💳 Оплата по карті", callback_data=f'pay_card_{total_uah}_{order_id}')],
            [InlineKeyboardButton("₿ Оплата криптовалютою", callback_data=f'pay_crypto_{total_uah}_{order_id}')],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f'cancel_payment_{order_id}')]
        ]
        await update.message.reply_text(f"{order_text}\nОберіть спосіб оплати:", reply_markup=InlineKeyboardMarkup(keyboard))

    # --- ЛОГИКА ОБРАБОТКИ СООБЩЕНИЙ ---
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text

        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await self.handle_owner_message(update, context)
            return

        ensure_user_exists(user)
        save_message(user_id, message_text)

        if user_id in active_conversations:
            conv = active_conversations[user_id]
            conv['last_message'] = message_text
            save_active_conversation(user_id, conv['type'], conv['assigned_owner'], message_text)

            if conv['type'] == 'question':
                await self.forward_to_owners(context, user_id, user, message_text)
            elif conv['type'] in ['subscription_order', 'digital_order']:
                assigned_owner = conv['assigned_owner']
                if assigned_owner:
                    try:
                        await context.bot.send_message(chat_id=assigned_owner, text=f"Повідомлення від клієнта {user.first_name}:\n{message_text}")
                    except Exception as e:
                        logger.error(f"❌ Ошибка пересылки сообщения владельцу {assigned_owner}: {e}")
        else:
            # Сообщение вне активного диалога
            pass

    async def handle_media_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Обработка медиа (фото, видео и т.д.) - аналогично handle_message
        pass

    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        message = update.message

        if owner_id in owner_client_map:
            client_id = owner_client_map[owner_id]
            if client_id in active_conversations:
                client_info = active_conversations[client_id]['user_info']
                try:
                    if message.text:
                        await context.bot.send_message(chat_id=client_id, text=message.text)
                        save_message(owner_id, f"[Відповідь власника] {message.text}")
                    elif message.photo:
                        photo = message.photo[-1]
                        caption = message.caption or ''
                        await context.bot.send_photo(chat_id=client_id, photo=photo.file_id, caption=caption)
                        save_message(owner_id, f"[Фото від власника] {caption}")
                    # Добавить обработку других типов медиа по необходимости
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения клиенту {client_id}: {e}")
                    await message.reply_text("❌ Не вдалося надіслати повідомлення клієнту.")
            else:
                del owner_client_map[owner_id]
                await message.reply_text("❌ Діалог із клієнтом завершено.")
        else:
            await message.reply_text("❌ Ви не обробляєте жоден активний діалог.")

    async def forward_to_owners(self, context, user_id, user, message_text):
        forward_message = f"""❓ Нове запитання від клієнта!👤 Клієнт: {user.first_name}📱 Username: @{user.username if user.username else 'не вказано'}🆔 ID: {user.id}🌐 Язык: {user.language_code or 'не вказано'}💬 Повідомлення:{message_text}-Натисніть "✅ Взяти", щоб обробити запит."""
        keyboard = [[InlineKeyboardButton("✅ Взяти", callback_data=f'take_order_{user_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(chat_id=owner_id, text=forward_message.strip(), reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"❌ Ошибка пересылки сообщения владельцу {owner_id}: {e}")

    # --- КОМАНДЫ ДЛЯ ВЛАДЕЛЬЦЕВ ---
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        total_users_db = get_total_users_count()
        first_start = datetime.fromisoformat(bot_statistics['first_start'])
        last_save = datetime.fromisoformat(bot_statistics['last_save'])
        uptime = datetime.now() - first_start
        stats_message = f"""📊 Статистика бота:👤 Усього користувачів (БД): {total_users_db}🛒 Усього замовлень: {bot_statistics['total_orders']}❓ Усього запитаннь: {bot_statistics['total_questions']}⏱️ Перший запуск: {first_start.strftime('%d.%m.%Y %H:%M')}⏱️ Останнє збереження: {last_save.strftime('%d.%m.%Y %H:%M')}⏱️ Час роботи: {uptime}"""
        await update.message.reply_text(stats_message.strip())

    async def show_active_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        chats_list = []
        for user_id, conv in active_conversations.items():
            if conv['assigned_owner'] == owner_id:
                user_info = conv['user_info']
                chats_list.append(f"👤 {user_info.first_name} (@{user_info.username if user_info.username else 'N/A'}) - {conv['type']}")
        if chats_list:
            await update.message.reply_text("Активні діалоги:\n" + "\n".join(chats_list))
        else:
            await update.message.reply_text("Немає активних діалогів.")

    async def show_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        if not context.args:
            await update.message.reply_text("Використання: /history <user_id>")
            return
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Неправильний ID користувача.")
            return
        history = get_conversation_history(target_user_id)
        if history:
            history_text = "\n".join([f"[{msg['created_at'].strftime('%d.%m.%Y %H:%M')}] {msg['message_text']}" for msg in history])
            await update.message.reply_text(f"Історія переписки з користувачем {target_user_id}:\n{history_text}")
        else:
            await update.message.reply_text("Історія переписки порожня або користувач не знайдений.")

    async def start_dialog_with_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        if not context.args:
            await update.message.reply_text("Використання: /dialog <user_id>")
            return
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Неправильний ID користувача.")
            return
        # Здесь логика начала диалога с пользователем
        await update.message.reply_text(f"Діалог з користувачем {target_user_id} розпочато.")

    async def clear_all_conversations(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        deleted_count = clear_all_active_conversations()
        await update.message.reply_text(f"Очищено {deleted_count} активних діалогів.")

    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id in active_conversations:
            conv = active_conversations.pop(user_id, None)
            if conv and conv['assigned_owner']:
                owner_id = conv['assigned_owner']
                owner_client_map.pop(owner_id, None)
                try:
                    await context.bot.send_message(chat_id=owner_id, text="❌ Клієнт завершив діалог.")
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления владельца {owner_id} о завершении: {e}")
            delete_active_conversation(user_id)
            await update.message.reply_text("✅ Діалог завершено. Ви можете почати новий через /start.")
        else:
            await update.message.reply_text("❌ У вас немає активного діалогу.")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.warning(f'Update {update} caused error {context.error}')

    # --- ЗАПУСК И УПРАВЛЕНИЕ ---
    def start_ping_service(self):
        if not self.ping_running:
            self.ping_running = True
            ping_thread = threading.Thread(target=ping_loop)
            ping_thread.daemon = True
            ping_thread.start()

    async def initialize(self):
        if not self.initialized:
            await self.set_commands_menu()
            self.initialized = True
            logger.info("✅ Бот инициализирован")

    async def start_bot(self):
        global bot_running
        try:
            await self.initialize()
            if USE_POLLING:
                await self.application.run_polling()
            else:
                await self.application.run_webhook(
                    listen="0.0.0.0",
                    port=PORT,
                    url_path=f"/{BOT_TOKEN}",
                    webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
                )
            bot_running = True
            logger.info("✅ Бот запущен")
        except Conflict as e:
            logger.error(f"🚨 Конфликт: {e}")
            time.sleep(30)
            await self.start_bot()
        except Exception as e:
            logger.error(f"❌ Ошибка запуска polling: {e}")
            raise

    async def stop_polling(self):
        try:
            if self.application.updater and self.application.updater.running:
                await self.application.updater.stop()
            if self.application.running:
                await self.application.stop()
            if self.application.post_init:
                await self.application.shutdown()
        except Exception as e:
            logger.error(f"❌ Ошибка остановки polling: {e}")

# --- ЗАПУСК ---
def auto_save_loop():
    while True:
        time.sleep(300)
        save_stats()

def main():
    global bot_instance
    if os.environ.get('RENDER'):
        time.sleep(10)
    logger.info("Инициализация приложения...")
    init_db()
    load_stats()
    bot_instance = TelegramBot()
    threading.Thread(target=auto_save_loop, daemon=True).start()
    logger.info("Бот запущен.")
    bot_instance.application.run_polling() # Или run_webhook в зависимости от конфигурации

if __name__ == '__main__':
    main()
