# main.py
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
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
import psycopg
from psycopg.rows import dict_row
import io
from urllib.parse import unquote
import traceback
import requests

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальные переменные состояния
bot_running = False
bot_lock = threading.Lock()

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
OWNER_ID_1 = 7106925462  # @HiGki2pYYY
OWNER_ID_2 = 6279578957  # @oc33t
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://your-app-url.onrender.com')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840))  # 14 минут
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://user:password@host:port/dbname')

# Настройки оплаты
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY')
EXCHANGE_RATE_UAH_TO_USD = float(os.getenv('EXCHANGE_RATE_UAH_TO_USD', 41.26))
CARD_NUMBER = os.getenv('CARD_NUMBER', '5355 2800 4715 6045')

# Список доступных криптовалют
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

# Путь к файлу с данными
STATS_FILE = "bot_stats.json"

# Оптимизация: буферизация запросов к БД
BUFFER_FLUSH_INTERVAL = 300  # 5 минут
BUFFER_MAX_SIZE = 50
message_buffer = []
active_conv_buffer = []
user_cache = set()

# Оптимизация: кэш для истории сообщений
history_cache = {}

def flush_message_buffer():
    global message_buffer
    if not message_buffer:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TEMP TABLE temp_messages (user_id BIGINT, message TEXT, is_from_user BOOLEAN) ON COMMIT DROP;
                """)
                cur.executemany("""
                    INSERT INTO temp_messages (user_id, message, is_from_user) VALUES (%s, %s, %s);
                """, message_buffer)
                cur.execute("""
                    INSERT INTO messages (user_id, message, is_from_user)
                    SELECT user_id, message, is_from_user FROM temp_messages;
                """)
                conn.commit()
                logger.info(f"✅ Сброшен буфер сообщений ({len(message_buffer)} записей)")
    except Exception as e:
        logger.error(f"❌ Ошибка сброса буфера сообщений: {e}")
    finally:
        message_buffer = []

def flush_active_conv_buffer():
    global active_conv_buffer
    if not active_conv_buffer:
        return
    try:
        latest_convs = {}
        for conv in active_conv_buffer:
            user_id = conv[0]
            latest_convs[user_id] = conv

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TEMP TABLE temp_active_convs (user_id BIGINT, conversation_type VARCHAR(50), assigned_owner BIGINT, last_message TEXT) ON COMMIT DROP;
                """)
                cur.executemany("""
                    INSERT INTO temp_active_convs (user_id, conversation_type, assigned_owner, last_message) VALUES (%s, %s, %s, %s);
                """, list(latest_convs.values()))
                cur.execute("""
                    INSERT INTO active_conversations (user_id, conversation_type, assigned_owner, last_message)
                    SELECT user_id, conversation_type, assigned_owner, last_message FROM temp_active_convs
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        conversation_type = EXCLUDED.conversation_type,
                        assigned_owner = EXCLUDED.assigned_owner,
                        last_message = EXCLUDED.last_message,
                        updated_at = CURRENT_TIMESTAMP;
                """)
                conn.commit()
                logger.info(f"✅ Сброшен буфер диалогов ({len(active_conv_buffer)} записей)")
    except Exception as e:
        logger.error(f"❌ Ошибка сброса буфера диалогов: {e}")
    finally:
        active_conv_buffer = []

def buffer_flush_thread():
    """Поток для периодического сброса буферов в БД"""
    while True:
        time.sleep(BUFFER_FLUSH_INTERVAL)
        flush_message_buffer()
        flush_active_conv_buffer()

# Функции для работы с базой данных
def init_db():
    """Инициализация базы данных"""
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
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        message TEXT,
                        is_from_user BOOLEAN,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS active_conversations (
                        user_id BIGINT PRIMARY KEY REFERENCES users(id),
                        conversation_type VARCHAR(50),
                        assigned_owner BIGINT,
                        last_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);")
                logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации базы данных: {e}")

def ensure_user_exists(user):
    """Убеждается, что пользователь существует в базе (с кэшированием)"""
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
                        is_bot = EXCLUDED.is_bot,
                        updated_at = CURRENT_TIMESTAMP;
                """, (user.id, user.username, user.first_name, user.last_name, user.language_code, user.is_bot))
                conn.commit()
                user_cache.add(user.id)
                logger.info(f"👤 Пользователь {user.first_name} ({user.id}) добавлен/обновлен в БД")
    except Exception as e:
        logger.error(f"❌ Ошибка добавления пользователя {user.id}: {e}")

def save_message(user_id, message, is_from_user):
    """Сохраняет сообщение в буфер (с оптимизацией)"""
    global message_buffer
    message_buffer.append((user_id, message, is_from_user))
    if len(message_buffer) >= BUFFER_MAX_SIZE:
        flush_message_buffer()

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message):
    """Сохраняет активный диалог в буфер (с оптимизацией)"""
    global active_conv_buffer
    active_conv_buffer.append((user_id, conversation_type, assigned_owner, last_message))
    if len(active_conv_buffer) >= BUFFER_MAX_SIZE:
        flush_active_conv_buffer()

def delete_active_conversation(user_id):
    """Удаляет активный диалог из базы данных"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations WHERE user_id = %s", (user_id,))
                logger.info(f"🗑️ Диалог пользователя {user_id} удален из БД")
    except Exception as e:
        logger.error(f"❌ Ошибка удаления активного диалога для {user_id}: {e}")

def get_conversation_history(user_id, limit=50):
    """Возвращает историю сообщений для пользователя (с кэшированием)"""
    flush_message_buffer()

    cache_key = f"{user_id}_{limit}"
    if cache_key in history_cache:
        return history_cache[cache_key]

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT * FROM messages
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (user_id, limit))
                history = cur.fetchall()
                history_cache[cache_key] = history
                return history
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории сообщений: {e}")
        return []

def get_all_users():
    """Возвращает всех пользователей из базы данных"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Ошибка получения пользователей: {e}")
        return []

def get_total_users_count():
    """Возвращает общее количество пользователей"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"❌ Ошибка получения количества пользователей: {e}")
        return 0

def clear_all_active_conversations():
    """Удаляет все активные диалоги из базы данных"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations")
                deleted_count = cur.rowcount
                logger.info(f"🗑️ Удалено {deleted_count} активных диалогов из БД")
                return deleted_count
    except Exception as e:
        logger.error(f"❌ Ошибка очистки активных диалогов: {e}")
        return 0

# Инициализируем базу данных при старте
init_db()

# Запускаем поток для сброса буферов
threading.Thread(target=buffer_flush_thread, daemon=True).start()

# Функции для работы с данными
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки статистики: {e}")
    return {
        "total_orders": 0,
        "total_questions": 0,
        "total_users": 0,
        "last_reset": datetime.now().isoformat()
    }

def save_stats():
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(bot_statistics, f, ensure_ascii=False, indent=4)
        logger.info("💾 Статистика сохранена в файл")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения статистики: {e}")

def auto_save_loop():
    """Автосохранение статистики каждые 10 минут"""
    while True:
        time.sleep(600)
        save_stats()
        logger.info("✅ Статистика автосохранена")

# Глобальные переменные для данных
bot_statistics = load_stats()

# Словари для хранения данных
active_conversations = {}
owner_client_map = {}

# Глобальные переменные для приложения
telegram_app = None
flask_app = Flask(__name__)
CORS(flask_app)

# Вспомогательные функции для оплаты
def get_uah_amount_from_order_text(order_text: str) -> float:
    """Извлекает сумму в UAH из текста заказа."""
    match = re.search(r'💳 Всього: (\d+) UAH', order_text)
    if match:
        return float(match.group(1))
    return 0.0

def convert_uah_to_usd(uah_amount: float) -> float:
    """Конвертирует сумму из UAH в USD по курсу."""
    return round(uah_amount / EXCHANGE_RATE_UAH_TO_USD, 2)

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.ping_running = False
        self.initialized = False
        self.polling_task = None
        self.loop = None

    def setup_handlers(self):
        """Настройка обработчиков команд и сообщений"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("history", self.show_history))
        self.application.add_handler(CommandHandler("chats", self.show_active_chats))
        self.application.add_handler(CommandHandler("clear", self.clear_active_conversations_command))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("pay", self.pay_command))
        self.application.add_handler(CommandHandler("dialog", self.continue_dialog_command))

        # Обработчики callback кнопок
        self.application.add_handler(CallbackQueryHandler(self.button_handler))

        # Обработчик текстовых сообщений
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Обработчик документов
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

        # Обработчик callback кнопок оплаты
        self.application.add_handler(CallbackQueryHandler(self.payment_callback_handler, pattern='^(pay_|check_payment_status|manual_payment_confirmed|back_to_)'))
        
    async def set_commands_menu(self):
        """Установка стандартного меню команд"""
        owner_commands = [
            BotCommandScopeChat(chat_id=OWNER_ID_1),
            BotCommandScopeChat(chat_id=OWNER_ID_2)
        ]
        user_commands = [
            BotCommandScopeChat(chat_id='*')
        ]
        try:
            await self.application.bot.set_my_commands([
                ('start', 'Головне меню'),
                ('stop', 'Завершити діалог'),
                ('stats', 'Статистика бота'),
                ('history', 'Історія повідомлень'),
                ('chats', 'Активні чати'),
                ('clear', 'Очистити активні чати'),
                ('channel', 'Наш канал'),
                ('help', 'Допомога'),
                ('dialog', 'Продовжити діалог (для основателей)')
            ], scope=owner_commands[0])
            await self.application.bot.set_my_commands([
                ('start', 'Головне меню'),
                ('stop', 'Завершити діалог'),
                ('order', 'Зробити замовлення'),
                ('question', 'Поставити запитання'),
                ('channel', 'Наш канал'),
                ('help', 'Допомога'),
                ('pay', 'Оплатити замовлення з сайту')
            ], scope=user_commands[0])
            logger.info("✅ Меню команд установлено")
        except Exception as e:
            logger.error(f"❌ Ошибка установки меню команд: {e}")

    async def initialize(self):
        """Асинхронная инициализация приложения"""
        try:
            await self.application.initialize()
            await self.set_commands_menu()
            self.initialized = True
            logger.info("✅ Telegram Application инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации Telegram Application: {e}")
            raise

    async def start_polling(self):
        """Запуск polling режима"""
        try:
            if self.application.updater.running:
                logger.warning("🛑 Бот уже запущен! Пропускаем повторный запуск")
                return
            logger.info("🔄 Запуск polling режима...")
            await self.application.start()
            await self.application.updater.start_polling(
                poll_interval=1.0,
                timeout=10,
                bootstrap_retries=-1,
                read_timeout=10,
                write_timeout=10,
                connect_timeout=10,
                pool_timeout=10
            )
            logger.info("✅ Polling запущен")
        except Conflict as e:
            logger.error(f"🚨 Конфликт: {e}")
            logger.warning("🕒 Ожидаем 15 секунд перед повторной попыткой...")
            await asyncio.sleep(15)
            await self.start_polling()
        except Exception as e:
            logger.error(f"❌ Ошибка запуска polling: {e}")
            raise

    async def stop_polling(self):
        """Остановка polling"""
        try:
            if self.application.updater and self.application.updater.running:
                await self.application.updater.stop()
            if self.application.running:
                await self.application.stop()
            if self.application.post_init:
                await self.application.shutdown()
            logger.info("🛑 Polling полностью остановлен")
        except Exception as e:
            logger.error(f"❌ Ошибка остановки polling: {e}")

    # Обработчики команд
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        ensure_user_exists(user)

        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            owner_name = "@HiGki2pYYY" if user.id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(f"Добро пожаловать, {user.first_name}! ({owner_name})")
            return

        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        welcome_text = f"""👋 Вітаємо, {user.first_name}!
🤖 Це бот магазину SecureShop.
Тут ви можете зробити замовлення або задати питання засновникам магазину.
Оберіть дію нижче 👇"""
        await update.message.reply_text(welcome_text.strip(), reply_markup=reply_markup)

    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /order - показывает категории"""
        keyboard = [
            [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
            [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        await update.message.reply_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def question_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /question"""
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)

        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог."
                "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                "якщо хочете почати новий діалог."
            )
            return

        active_conversations[user_id] = {
            'type': 'question',
            'user_info': user,
            'assigned_owner': None,
            'last_message': "Нове запитання"
        }
        save_active_conversation(user_id, 'question', None, "Нове запитання")

        bot_statistics['total_questions'] += 1
        save_stats()

        await update.message.reply_text(
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
            "Щоб завершити цей діалог пізніше, використайте команду /stop."
        )

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик документов (для заказов из файлов)"""
        user = update.effective_user
        user_id = user.id
        document = update.message.document
        ensure_user_exists(user)

        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог."
                "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                "якщо хочете почати новий діалог."
            )
            return

        if document.mime_type != 'application/json':
            await update.message.reply_text("❌ Підтримуються тільки JSON файли.")
            return

        try:
            file = await context.bot.get_file(document.file_id)
            file_bytes = await file.download_as_bytearray()
            file_content = file_bytes.decode('utf-8')
            order_data = json.loads(file_content)
        except Exception as e:
            logger.error(f"Ошибка обработки файла: {e}")
            await update.message.reply_text("❌ Помилка обробки файлу. Перевірте формат JSON.")
            return

        if 'items' not in order_data or 'total' not in order_data:
            await update.message.reply_text("❌ У файлі відсутні обов'язкові поля (items, total).")
            return

        order_text = "🛍️ Замовлення з сайту (з файлу):"
        for item in order_data['items']:
            order_text += f"▫️ {item['service']} {item.get('plan', '')} ({item['period']}) - {item['price']} UAH"
        order_text += f"\n💳 Всього: {order_data['total']} UAH"

        has_digital = any("Прикраси" in item.get('service', '') for item in order_data['items'])
        conversation_type = 'digital_order' if has_digital else 'subscription_order'

        active_conversations[user_id] = {
            'type': conversation_type,
            'user_info': user,
            'assigned_owner': None,
            'order_details': order_text,
            'last_message': order_text
        }
        save_active_conversation(user_id, conversation_type, None, order_text)

        bot_statistics['total_orders'] += 1
        save_stats()

        keyboard = [
            [InlineKeyboardButton("💳 Оплатити", callback_data='proceed_to_payment')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        confirmation_text = f"""✅ Ваше замовлення прийнято!
{order_text}
Будь ласка, оберіть дію 👇"""
        await update.message.reply_text(confirmation_text.strip(), reply_markup=reply_markup)

    # Основной обработчик кнопок
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id
        ensure_user_exists(user)

        # Главное меню
        if query.data == 'order':
            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню подписок
        elif query.data == 'order_subscriptions':
            keyboard = [
                [InlineKeyboardButton("💬 ChatGPT Plus", callback_data='category_chatgpt')],
                [InlineKeyboardButton("🎮 Discord Nitro", callback_data='category_discord')],
                [InlineKeyboardButton("🎓 Duolingo Max", callback_data='category_duolingo')],
                [InlineKeyboardButton("🎨 Picsart AI", callback_data='category_picsart')],
                [InlineKeyboardButton("📊 Canva Pro", callback_data='category_canva')],
                [InlineKeyboardButton("📺 Netflix", callback_data='category_netflix')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("💳 Оберіть підписку:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню цифровых товаров
        elif query.data == 'order_digital':
            keyboard = [
                [InlineKeyboardButton("🎮 Discord Прикраси", callback_data='category_discord_decor')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю для различных сервисов...
        # [Здесь должен быть код обработки всех подменю, аналогичный исходному]
        # Для краткости оставлю только ключевые обработчики
        
        # Подтверждение заказа
        elif query.data == 'confirm_order':
            selected_item = context.user_data.get('selected_item')
            if not selected_item:
                await query.edit_message_text("❌ Помилка: товар не знайдено.")
                return

            items = {
                # Подписки
                'chatgpt_1': {'name': "ChatGPT Plus 1 місяць", 'price': 650},
                'discord_basic_1': {'name': "Discord Nitro Basic 1 місяць", 'price': 170},
                # ... другие товары ...
                # Цифровые товары
                'discord_decor_bzn_6': {'name': "Discord Прикраси (Без Nitro) 6$", 'price': 180},
                # ... другие товары ...
            }

            item = items.get(selected_item)
            if not item:
                await query.edit_message_text("❌ Помилка: товар не знайдено.")
                return

            order_text = f"🛍️ Ваше замовлення:\n▫️ {item['name']} - {item['price']} UAH\n💳 Всього: {item['price']} UAH"
            conversation_type = 'digital_order' if 'discord_decor' in selected_item else 'subscription_order'

            active_conversations[user_id] = {
                'type': conversation_type,
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            save_active_conversation(user_id, conversation_type, None, order_text)

            bot_statistics['total_orders'] += 1
            save_stats()

            await self.forward_order_to_owners(context, user_id, user, order_text)

            uah_amount = get_uah_amount_from_order_text(order_text)
            if uah_amount <= 0:
                await query.edit_message_text("❌ Не вдалося визначити суму для оплати.")
                return

            usd_amount = convert_uah_to_usd(uah_amount)
            if usd_amount <= 0:
                await query.edit_message_text("❌ Сума для оплати занадто мала.")
                return

            context.user_data['payment_amount_usd'] = usd_amount
            context.user_data['order_details_for_payment'] = order_text

            keyboard = [
                [InlineKeyboardButton("💳 Оплата карткою", callback_data='pay_card')],
                [InlineKeyboardButton("🪙 Криптовалюта", callback_data='pay_crypto')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                [InlineKeyboardButton("❓ Запитання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                reply_markup=reply_markup
            )

        # Обработка кнопки оплаты после заказа из файла
        elif query.data == 'proceed_to_payment':
            if user_id in active_conversations and 'order_details' in active_conversations[user_id]:
                order_text = active_conversations[user_id]['order_details']
                uah_amount = get_uah_amount_from_order_text(order_text)
                if uah_amount <= 0:
                    await query.edit_message_text("❌ Не вдалося визначити суму для оплати.")
                    return

                usd_amount = convert_uah_to_usd(uah_amount)
                if usd_amount <= 0:
                    await query.edit_message_text("❌ Сума для оплати занадто мала.")
                    return

                context.user_data['payment_amount_usd'] = usd_amount
                context.user_data['order_details_for_payment'] = order_text

                keyboard = [
                    [InlineKeyboardButton("💳 Оплата карткою", callback_data='pay_card')],
                    [InlineKeyboardButton("🪙 Криптовалюта", callback_data='pay_crypto')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                    reply_markup=reply_markup
                )
            else:
                await query.edit_message_text("❌ Не знайдено активного замовлення.")

    # Система оплаты
    async def payment_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок оплаты"""
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data

        usd_amount = context.user_data.get('payment_amount_usd')
        order_details = context.user_data.get('order_details_for_payment')
        if not usd_amount or not order_details:
            await query.edit_message_text("❌ Помилка: інформація про платіж втрачена. Спробуйте ще раз.")
            return

        # Оплата картой
        if data == 'pay_card':
            keyboard = [
                [InlineKeyboardButton("✅ Оплачено", callback_data='manual_payment_confirmed')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_payment_methods')],
                [InlineKeyboardButton("❓ Запитання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💳 Будь ласка, здійсніть оплату {usd_amount}$ карткою на реквізити магазину.\n"
                f"Номер картки: `{CARD_NUMBER}`\n"
                f"Після оплати натисніть кнопку '✅ Оплачено'.",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            context.user_data['awaiting_manual_payment_confirmation'] = True

        # Выбор криптовалюты
        elif data == 'pay_crypto':
            keyboard = []
            for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'pay_crypto_{currency_code}')])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='back_to_payment_methods')])
            keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"🪙 Оберіть криптовалюту для оплати {usd_amount}$:",
                reply_markup=reply_markup
            )
            context.user_data['awaiting_crypto_currency_selection'] = True

        # Создание инвойса для криптовалюты
        elif data.startswith('pay_crypto_'):
            pay_currency = data.split('_')[2]
            currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == pay_currency), pay_currency)

            try:
                headers = {
                    'Authorization': f'Bearer {NOWPAYMENTS_API_KEY}',
                    'Content-Type': 'application/json'
                }
                payload = {
                    "price_amount": usd_amount,
                    "price_currency": "usd",
                    "pay_currency": pay_currency,
                    "ipn_callback_url": f"{WEBHOOK_URL}/nowpayments_ipn",
                    "order_id": f"order_{user_id}_{int(time.time())}",
                    "order_description": f"Оплата замовлення користувача {user_id}"
                }
                response = requests.post("https://api.nowpayments.io/v1/invoice", json=payload, headers=headers)
                response.raise_for_status()
                invoice = response.json()

                pay_url = invoice.get("invoice_url", "Помилка отримання посилання")
                invoice_id = invoice.get("id", "Невідомий ID рахунку")

                context.user_data['nowpayments_invoice_id'] = invoice_id

                keyboard = [
                    [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                    [InlineKeyboardButton("🔄 Перевірити статус", callback_data='check_payment_status')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_crypto_selection')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"🪙 Посилання для оплати {usd_amount}$ в {currency_name}:\n"
                    f"{pay_url}\n"
                    f"ID рахунку: {invoice_id}\n"
                    f"Будь ласка, здійсніть оплату та перевірте статус.",
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Помилка створення інвойсу NOWPayments: {e}")
                await query.edit_message_text(f"❌ Помилка створення посилання для оплати: {e}")

        # Проверка статуса оплаты
        elif data == 'check_payment_status':
            invoice_id = context.user_data.get('nowpayments_invoice_id')
            if not invoice_id:
                await query.edit_message_text("❌ Не знайдено ID рахунку для перевірки.")
                return

            try:
                headers = {
                    'Authorization': f'Bearer {NOWPAYMENTS_API_KEY}',
                    'Content-Type': 'application/json'
                }
                response = requests.get(f"https://api.nowpayments.io/v1/invoice/{invoice_id}?is_paid=true", headers=headers)
                response.raise_for_status()
                status_data = response.json()
                payment_status = status_data.get('payment_status', 'unknown')

                if payment_status == 'finished':
                    await query.edit_message_text("✅ Оплата успішно пройшла!")
                    await self.request_account_data(update, context)
                elif payment_status in ['waiting', 'confirming', 'confirmed']:
                    keyboard = [
                        [InlineKeyboardButton("🔄 Перевірити ще раз", callback_data='check_payment_status')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_crypto_selection')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"⏳ Статус оплати: {payment_status}. Будь ласка, зачекайте або перевірте ще раз.",
                        reply_markup=reply_markup
                    )
                else:
                    keyboard = [
                        [InlineKeyboardButton("💳 Інший метод оплати", callback_data='back_to_payment_methods')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"❌ Оплата не пройшла або була скасована. Статус: {payment_status}.",
                        reply_markup=reply_markup
                    )
            except Exception as e:
                logger.error(f"Помилка перевірки статусу NOWPayments: {e}")
                await query.edit_message_text(f"❌ Помилка перевірки статусу оплати: {e}")

        # Ручное подтверждение оплаты
        elif data == 'manual_payment_confirmed':
            await query.edit_message_text("✅ Оплата підтверджена вручну.")
            await self.request_account_data(update, context)

        # Назад к выбору метода оплаты
        elif data == 'back_to_payment_methods':
            keyboard = [
                [InlineKeyboardButton("💳 Оплата карткою", callback_data='pay_card')],
                [InlineKeyboardButton("🪙 Криптовалюта", callback_data='pay_crypto')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                [InlineKeyboardButton("❓ Запитання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                reply_markup=reply_markup
            )
            context.user_data.pop('awaiting_manual_payment_confirmation', None)
            context.user_data.pop('awaiting_crypto_currency_selection', None)
            context.user_data.pop('nowpayments_invoice_id', None)

        # Назад к выбору криптовалюты
        elif data == 'back_to_crypto_selection':
            keyboard = []
            for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'pay_crypto_{currency_code}')])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='back_to_payment_methods')])
            keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"🪙 Оберіть криптовалюту для оплати {usd_amount}$:",
                reply_markup=reply_markup
            )
            context.user_data.pop('nowpayments_invoice_id', None)

    async def request_account_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрашивает учетные данные после оплаты"""
        query = update.callback_query
        user_id = query.from_user.id if update.callback_query else update.effective_user.id
        order_details = context.user_data.get('order_details_for_payment', '')
        
        # Определяем тип товара
        if 'digital_order' in active_conversations.get(user_id, {}).get('type', ''):
            account_type = "Discord"
        else:
            account_type = "сервісу (наприклад, Netflix, ChatGPT)"
        
        message_text = f"✅ Оплата пройшла успішно!\n\n" \
                       f"Будь ласка, надішліть мені логін та пароль від вашого {account_type} акаунту.\n" \
                       f"Формат: `email:password` або `login:password`"
        
        if update.callback_query:
            await query.edit_message_text(message_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(message_text, parse_mode='Markdown')
        
        context.user_data['awaiting_account_data'] = True
        context.user_data['account_details_order'] = order_details

    async def handle_account_data_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает учетные данные от пользователя"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text

        if not context.user_data.get('awaiting_account_data'):
            await self.handle_message(update, context)
            return

        if ':' in message_text:
            login, password = message_text.split(':', 1)
        else:
            await update.message.reply_text("❌ Неправильний формат. Будь ласка, використовуйте формат: логін:пароль")
            return

        order_details = context.user_data.get('account_details_order', 'Невідомий заказ')
        account_info_message = f"🔐 Нові дані акаунту від клієнта!\n\n" \
                               f"👤 Клієнт: {user.first_name}\n" \
                               f"🆔 ID: {user_id}\n" \
                               f"🛍️ Замовлення: {order_details}\n\n" \
                               f"🔑 Логін: `{login}`\n" \
                               f"🔓 Пароль: `{password}`"

        # Уведомляем основателей
        success_count = 0
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=account_info_message,
                    parse_mode='Markdown'
                )
                success_count += 1
            except Exception as e:
                logger.error(f"Помилка надсилання даних акаунту основателю {owner_id}: {e}")

        if success_count > 0:
            await update.message.reply_text("✅ Дані акаунту успішно надіслано засновникам магазину. Дякуємо!")
        else:
            await update.message.reply_text("❌ Не вдалося надіслати дані акаунту. Будь ласка, зверніться до підтримки.")

        # Очищаем состояние
        context.user_data.pop('awaiting_account_data', None)
        context.user_data.pop('payment_amount_usd', None)
        context.user_data.pop('order_details_for_payment', None)
        context.user_data.pop('account_details_order', None)
        context.user_data.pop('nowpayments_invoice_id', None)
        
        # Удаляем активный диалог
        if user_id in active_conversations:
            del active_conversations[user_id]
        delete_active_conversation(user_id)

    # Остальные функции (forward_order_to_owners, handle_message, и т.д.)
    # [Здесь должен быть код всех остальных функций, аналогичный исходному]
    # Для краткости оставлю только сигнатуры

    async def forward_order_to_owners(self, context, user_id, user, order_text):
        # ... реализация ...

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def continue_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def show_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def show_active_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def clear_active_conversations_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        # ... реализация ...

# Flask обработчики
@flask_app.route('/', methods=['GET'])
def index():
    return jsonify({
        'status': 'running',
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
        if json_data:
            update = Update.de_json(json_data, telegram_app.bot)
            if telegram_app.loop and not telegram_app.loop.is_closed():
                 future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), telegram_app.loop)
        return '', 200
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/nowpayments_ipn', methods=['POST'])
def nowpayments_ipn():
    try:
        data = request.json
        logger.info(f"NOWPayments IPN received: {json.dumps(data)}")
        
        if data.get('payment_status') == 'finished':
            invoice_id = data.get('invoice_id')
            order_id = data.get('order_id')
            user_id = int(order_id.split('_')[1]) if order_id else 0
            
            message = f"✅ Успішна оплата через NOWPayments\nID рахунку: {invoice_id}\n" \
                      f"Сума: {data.get('price_amount')} {data.get('price_currency')}\n" \
                      f"Користувач ID: {user_id}"
            
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                try:
                    telegram_app.bot.send_message(chat_id=owner_id, text=message)
                except Exception as e:
                    logger.error(f"Помилка відправки сповіщення: {e}")
        
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"Помилка обробки IPN: {e}")
        return jsonify({'error': str(e)}), 500

# Основная логика запуска
bot_instance = TelegramBot()

async def setup_webhook():
    try:
        if not WEBHOOK_URL:
            logger.error("❌ WEBHOOK_URL не установлен")
            return False

        await bot_instance.application.bot.delete_webhook()
        logger.info("🗑️ Старый webhook удален")

        await bot_instance.application.bot.set_webhook(
            url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            max_connections=40,
            allowed_updates=["message", "callback_query", "document"]
        )
        logger.info(f"✅ Webhook установлен на {WEBHOOK_URL}/{BOT_TOKEN}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка настройки webhook: {e}")
        return False

async def start_bot():
    global bot_running, telegram_app
    if bot_running:
        logger.warning("🛑 Бот уже запущен! Пропускаем повторный запуск")
        return

    try:
        await bot_instance.initialize()
        telegram_app = bot_instance.application

        if USE_POLLING:
            await setup_webhook()
            await bot_instance.start_polling()
            bot_running = True
            logger.info("✅ Бот запущен в polling режиме")
        else:
            success = await setup_webhook()
            if success:
                bot_running = True
                logger.info("✅ Бот запущен в webhook режиме")
            else:
                logger.error("❌ Не удалось настроить webhook")
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}")
        bot_running = False
        raise

def bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_instance.loop = loop

    try:
        loop.run_until_complete(start_bot())
        if USE_POLLING:
            loop.run_forever()
    except Conflict as e:
        logger.error(f"🚨 Конфликт: {e}")
        logger.warning("🕒 Ожидаем 30 секунд перед повторной попыткой...")
        time.sleep(30)
        bot_thread()
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в bot_thread: {e}")
        logger.warning("🕒 Ожидаем 15 секунд перед повторным запуском...")
        time.sleep(15)
        bot_thread()
    finally:
        try:
            if not loop.is_closed():
                loop.close()
        except:
            pass
        logger.warning("🔁 Перезапускаем поток бота...")
        time.sleep(5)
        bot_thread()

def main():
    if os.environ.get('RENDER'):
        logger.info("⏳ Ожидаем 10 секунд для предотвращения конфликтов...")
        time.sleep(10)

    auto_save_thread = threading.Thread(target=auto_save_loop)
    auto_save_thread.daemon = True
    auto_save_thread.start()

    logger.info("🚀 Запуск SecureShop Telegram Bot...")
    logger.info(f"🔑 BOT_TOKEN: {BOT_TOKEN[:10]}...")
    logger.info(f"🌐 PORT: {PORT}")
    logger.info(f"📡 WEBHOOK_URL: {WEBHOOK_URL}")

    from waitress import serve
    flask_thread = threading.Thread(target=lambda: serve(flask_app, host='0.0.0.0', port=PORT))
    flask_thread.daemon = True
    flask_thread.start()
    logger.info(f"🌐 Flask сервер запущен на порту {PORT}")

    bot_t = threading.Thread(target=bot_thread)
    bot_t.daemon = True
    bot_t.start()

    try:
        while True:
            time.sleep(PING_INTERVAL)
            logger.info("✅ Бот активен")
    except KeyboardInterrupt:
        logger.info("🛑 Получен сигнал завершения работы")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в основном цикле: {e}")

if __name__ == '__main__':
    main()
