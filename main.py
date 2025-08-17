# main.py (обновленный)
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
import requests # Добавлено для интеграции NOWPayments

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальные переменные состояния
bot_running = False
bot_lock = threading.Lock()

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
OWNER_ID_1 = 7106925462 # @HiGki2pYYY
OWNER_ID_2 = 6279578957 # @oc33t
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://your-app-url.onrender.com')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840)) # 14 минут
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://user:password@host:port/dbname')

# --- Добавленные/обновленные переменные окружения ---
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY') # "FTD5K08-DE94C4F-M9RB0XS-XSGBA26"
EXCHANGE_RATE_UAH_TO_USD = float(os.getenv('EXCHANGE_RATE_UAH_TO_USD', 41.26))
# --- Конец добавленных переменных ---

# Путь к файлу с данными
STATS_FILE = "bot_stats.json"

# Оптимизация: буферизация запросов к БД
BUFFER_FLUSH_INTERVAL = 300 # 5 минут
BUFFER_MAX_SIZE = 50
message_buffer = []
active_conv_buffer = []
user_cache = set()

# Оптимизация: кэш для истории сообщений
history_cache = {}

# --- Добавленные/обновлённые словари и списки ---
# Список доступных валют для NOWPayments (проверим коды!)
AVAILABLE_CURRENCIES = {
    "USDT (Solana)": "usdtsol",
    "USDT (TRC20)": "usdttrc20",
    "ETH": "eth",
    "USDT (Arbitrum)": "usdtarb",
    "USDT (Polygon)": "usdtmatic", # правильный код
    "USDT (TON)": "usdtton", # правильный код
    "AVAX (C-Chain)": "avax",
    "APTOS (APT)": "apt"
}
# --- Конец добавленных словарей ---

def flush_message_buffer():
    global message_buffer
    if not message_buffer:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Создаем временную таблицу
                cur.execute("""
                    CREATE TEMP TABLE temp_messages (user_id BIGINT, message TEXT, is_from_user BOOLEAN) ON COMMIT DROP;
                """)
                # Заполняем временную таблицу
                cur.executemany("""
                    INSERT INTO temp_messages (user_id, message, is_from_user) VALUES (%s, %s, %s);
                """, message_buffer)
                # Вставляем данные из временной таблицы в основную
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
        # Группируем записи по user_id (берем последнюю версию)
        latest_convs = {}
        for conv in active_conv_buffer:
            user_id = conv[0]
            latest_convs[user_id] = conv # Последняя запись для пользователя

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Создаем временную таблицу БЕЗ первичного ключа
                cur.execute("""
                    CREATE TEMP TABLE temp_active_convs (user_id BIGINT, conversation_type VARCHAR(50), assigned_owner BIGINT, last_message TEXT) ON COMMIT DROP;
                """)
                # Заполняем временную таблицу
                cur.executemany("""
                    INSERT INTO temp_active_convs (user_id, conversation_type, assigned_owner, last_message) VALUES (%s, %s, %s, %s);
                """, list(latest_convs.values()))
                # Обновляем или вставляем данные из временной таблицы в основную
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
                # Таблица пользователей
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
                # Таблица сообщений
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        message TEXT,
                        is_from_user BOOLEAN,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Таблица активных диалогов
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS active_conversations (
                        user_id BIGINT PRIMARY KEY REFERENCES users(id),
                        conversation_type VARCHAR(50), -- 'order', 'question', 'manual', 'subscription_order', 'digital_order'
                        assigned_owner BIGINT,
                        last_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Индексы для оптимизации
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
    # Сначала сбрасываем буфер, чтобы получить актуальные данные
    flush_message_buffer()

    # Используем кэш, если есть
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
        time.sleep(600) # 10 минут
        save_stats()
        logger.info("✅ Статистика автосохранена")

# Глобальные переменные для данных
bot_statistics = load_stats()

# Словари для хранения данных (в памяти, для быстрого доступа)
active_conversations = {} # {user_id: {...}}
owner_client_map = {} # {owner_id: client_id}

# Глобальные переменные для приложения
telegram_app = None
flask_app = Flask(__name__)
CORS(flask_app) # Разрешаем CORS для всех доменов

# --- Добавленные/обновлённые вспомогательные функции ---
def get_uah_amount_from_order_text(order_text: str) -> float:
    """Извлекает сумму в UAH из текста заказа."""
    match = re.search(r'💳 Всього: (\d+) UAH', order_text)
    if match:
        return float(match.group(1))
    return 0.0

def convert_uah_to_usd(uah_amount: float) -> float:
    """Конвертирует сумму из UAH в USD по курсу."""
    return round(uah_amount / EXCHANGE_RATE_UAH_TO_USD, 2)
# --- Конец добавленных вспомогательных функций ---

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
        self.application.add_handler(CommandHandler("pay", self.pay_command)) # Добавлено
        self.application.add_handler(CommandHandler("dialog", self.continue_dialog_command))

        # Обработчики callback кнопок
        self.application.add_handler(CallbackQueryHandler(self.button_handler))

        # Обработчик текстовых сообщений (должен быть последним)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Обработчик документов (для заказов из файлов)
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

        # --- Добавлено: Обработчик callback кнопок оплаты ---
        self.application.add_handler(CallbackQueryHandler(self.payment_callback_handler, pattern='^(pay_|check_payment_status|manual_payment_confirmed|back_to_)'))
        # --- Конец добавленных обработчиков ---

    async def set_commands_menu(self):
        """Установка стандартного меню команд"""
        owner_commands = [
            BotCommandScopeChat(chat_id=OWNER_ID_1),
            BotCommandScopeChat(chat_id=OWNER_ID_2)
        ]
        user_commands = [
            BotCommandScopeChat(chat_id='*') # Для всех остальных
        ]
        try:
            # Команды для основателей
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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Для основателей
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            owner_name = "@HiGki2pYYY" if user.id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(f"Добро пожаловать, {user.first_name}! ({owner_name})")
            return

        # Для обычных пользователей
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
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог."
                "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                "якщо хочете почати новий діалог."
            )
            return

        # Создаем запись о вопросе
        active_conversations[user_id] = {
            'type': 'question',
            'user_info': user,
            'assigned_owner': None,
            'last_message': "Нове запитання"
        }
        # Сохраняем в БД
        save_active_conversation(user_id, 'question', None, "Нове запитання")

        # Обновляем статистику
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

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог."
                "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                "якщо хочете почати новий діалог."
            )
            return

        # Проверяем тип файла
        if document.mime_type != 'application/json':
            await update.message.reply_text("❌ Підтримуються тільки JSON файли.")
            return

        try:
            # Скачиваем файл
            file = await context.bot.get_file(document.file_id)
            file_bytes = await file.download_as_bytearray()
            file_content = file_bytes.decode('utf-8')
            order_data = json.loads(file_content)
        except Exception as e:
            logger.error(f"Ошибка обработки файла: {e}")
            await update.message.reply_text("❌ Помилка обробки файлу. Перевірте формат JSON.")
            return

        # Проверяем обязательные поля
        if 'items' not in order_data or 'total' not in order_data:
            await update.message.reply_text("❌ У файлі відсутні обов'язкові поля (items, total).")
            return

        # Формируем текст заказа
        order_text = "🛍️ Замовлення з сайту (з файлу):"
        for item in order_data['items']:
            order_text += f"▫️ {item['service']} {item.get('plan', '')} ({item['period']}) - {item['price']} UAH"
        order_text += f"\n💳 Всього: {order_data['total']} UAH"

        # Определяем тип заказа (простая логика, можно усложнить)
        has_digital = any("Прикраси" in item.get('service', '') for item in order_data['items']) # Обновлено
        conversation_type = 'digital_order' if has_digital else 'subscription_order'

        # Создаем запись о заказе
        active_conversations[user_id] = {
            'type': conversation_type,
            'user_info': user,
            'assigned_owner': None,
            'order_details': order_text,
            'last_message': order_text
        }
        # Сохраняем в БД
        save_active_conversation(user_id, conversation_type, None, order_text)

        # Обновляем статистику
        bot_statistics['total_orders'] += 1
        save_stats()

        # Отправляем подтверждение пользователю
        keyboard = [
            [InlineKeyboardButton("💳 Оплатити", callback_data='proceed_to_payment')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        confirmation_text = f"""✅ Ваше замовлення прийнято!

{order_text}

Будь ласка, оберіть дію 👇"""
        await update.message.reply_text(confirmation_text.strip(), reply_markup=reply_markup)

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Главное меню
        if query.data == 'order':
            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Кнопка "Назад" в главное меню
        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Підписок
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

        # Меню Цифрових товарів
        elif query.data == 'order_digital':
            keyboard = [
                [InlineKeyboardButton("🎮 Discord Прикраси", callback_data='category_discord_decor')], # Обновлено
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # - Меню Підписок -
        # Меню ChatGPT
        elif query.data == 'category_chatgpt':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_1')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("💬 Оберіть варіант ChatGPT Plus:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Discord
        elif query.data == 'category_discord':
            keyboard = [
                [InlineKeyboardButton("Nitro Basic", callback_data='discord_basic')],
                [InlineKeyboardButton("Nitro Full", callback_data='discord_full')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎮 Оберіть варіант Discord Nitro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Discord Basic
        elif query.data == 'discord_basic':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 170 UAH", callback_data='discord_basic_1')],
                [InlineKeyboardButton("12 місяців - 1700 UAH", callback_data='discord_basic_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text("🎮 Discord Nitro Basic:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Discord Full
        elif query.data == 'discord_full':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 330 UAH", callback_data='discord_full_1')],
                [InlineKeyboardButton("12 місяців - 3300 UAH", callback_data='discord_full_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text("🎮 Discord Nitro Full:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Duolingo
        elif query.data == 'category_duolingo':
            keyboard = [
                [InlineKeyboardButton("Individual", callback_data='duolingo_ind')],
                [InlineKeyboardButton("Family", callback_data='duolingo_fam')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎓 Оберіть варіант Duolingo Max:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Duolingo Individual
        elif query.data == 'duolingo_ind':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 550 UAH", callback_data='duolingo_ind_1')],
                [InlineKeyboardButton("12 місяців - 5500 UAH", callback_data='duolingo_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text("🎓 Duolingo Max (Individual):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Duolingo Family
        elif query.data == 'duolingo_fam':
            keyboard = [
                [InlineKeyboardButton("12 місяців - 750 UAH", callback_data='duolingo_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text("🎓 Duolingo Max (Family):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Picsart
        elif query.data == 'category_picsart':
            keyboard = [
                [InlineKeyboardButton("Plus", callback_data='picsart_plus')],
                [InlineKeyboardButton("Pro", callback_data='picsart_pro')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎨 Оберіть варіант Picsart AI:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Picsart Plus
        elif query.data == 'picsart_plus':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 400 UAH", callback_data='picsart_plus_1')],
                [InlineKeyboardButton("12 місяців - 4000 UAH", callback_data='picsart_plus_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text("🎨 Picsart AI Plus:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Picsart Pro
        elif query.data == 'picsart_pro':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 550 UAH", callback_data='picsart_pro_1')],
                [InlineKeyboardButton("12 місяців - 5500 UAH", callback_data='picsart_pro_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text("🎨 Picsart AI Pro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Canva
        elif query.data == 'category_canva':
            keyboard = [
                [InlineKeyboardButton("Individual", callback_data='canva_ind')],
                [InlineKeyboardButton("Family", callback_data='canva_fam')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📊 Оберіть варіант Canva Pro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Canva Individual
        elif query.data == 'canva_ind':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 500 UAH", callback_data='canva_ind_1')],
                [InlineKeyboardButton("12 місяців - 5000 UAH", callback_data='canva_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_canva')]
            ]
            await query.edit_message_text("📊 Canva Pro (Individual):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Canva Family
        elif query.data == 'canva_fam':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='canva_fam_1')],
                [InlineKeyboardButton("12 місяців - 6500 UAH", callback_data='canva_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_canva')]
            ]
            await query.edit_message_text("📊 Canva Pro (Family):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Netflix
        elif query.data == 'category_netflix':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 400 UAH", callback_data='netflix_1')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📺 Оберіть варіант Netflix:", reply_markup=InlineKeyboardMarkup(keyboard))

        # - Меню Цифрових товарів -
        # Меню Discord Прикраси
        elif query.data == 'category_discord_decor':
            keyboard = [
                [InlineKeyboardButton("Без Nitro", callback_data='discord_decor_without_nitro')],
                [InlineKeyboardButton("З Nitro", callback_data='discord_decor_with_nitro')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')]
            ]
            await query.edit_message_text("🎮 Оберіть тип прикраси Discord:", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # Подменю Discord Прикраси Без Nitro
        elif query.data == 'discord_decor_without_nitro':
            keyboard = [
                [InlineKeyboardButton("6$ - 180 UAH", callback_data='discord_decor_bzn_6')],
                [InlineKeyboardButton("8$ - 240 UAH", callback_data='discord_decor_bzn_8')],
                [InlineKeyboardButton("9$ - 265 UAH", callback_data='discord_decor_bzn_9')],
                [InlineKeyboardButton("12$ - 355 UAH", callback_data='discord_decor_bzn_12')],
                [InlineKeyboardButton("14$ - 410 UAH", callback_data='discord_decor_bzn_14')],
                [InlineKeyboardButton("16$ - 470 UAH", callback_data='discord_decor_bzn_16')],
                [InlineKeyboardButton("18$ - 530 UAH", callback_data='discord_decor_bzn_18')],
                [InlineKeyboardButton("24$ - 705 UAH", callback_data='discord_decor_bzn_24')],
                [InlineKeyboardButton("29$ - 855 UAH", callback_data='discord_decor_bzn_29')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Discord Прикраси (Без Nitro):", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # Подменю Discord Прикраси З Nitro
        elif query.data == 'discord_decor_with_nitro':
            keyboard = [
                [InlineKeyboardButton("5$ - 145 UAH", callback_data='discord_decor_zn_5')],
                [InlineKeyboardButton("7$ - 205 UAH", callback_data='discord_decor_zn_7')],
                [InlineKeyboardButton("8.5$ - 250 UAH", callback_data='discord_decor_zn_8_5')],
                [InlineKeyboardButton("9$ - 265 UAH", callback_data='discord_decor_zn_9')],
                [InlineKeyboardButton("14$ - 410 UAH", callback_data='discord_decor_zn_14')],
                [InlineKeyboardButton("22$ - 650 UAH", callback_data='discord_decor_zn_22')],
                [InlineKeyboardButton("25$ - 740 UAH", callback_data='discord_decor_zn_25')],
                [InlineKeyboardButton("30$ - 885 UAH", callback_data='discord_decor_zn_30')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Discord Прикраси (З Nitro):", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # Обработка выбора товара (Подписки)
        elif query.data in [
            'chatgpt_1',
            'discord_basic_1', 'discord_basic_12',
            'discord_full_1', 'discord_full_12',
            'duolingo_ind_1', 'duolingo_ind_12', 'duolingo_fam_12',
            'picsart_plus_1', 'picsart_plus_12',
            'picsart_pro_1', 'picsart_pro_12',
            'canva_ind_1', 'canva_ind_12',
            'canva_fam_1', 'canva_fam_12',
            'netflix_1'
        ]:
            # Сохраняем выбранный товар в контексте
            context.user_data['selected_item'] = query.data
            # Переходим к подтверждению заказа
            await self.confirm_order(update, context)

        # Обработка выбора товара (Цифровые товары)
        elif query.data in [
            'discord_decor_bzn_6', 'discord_decor_bzn_8', 'discord_decor_bzn_9',
            'discord_decor_bzn_12', 'discord_decor_bzn_14', 'discord_decor_bzn_16',
            'discord_decor_bzn_18', 'discord_decor_bzn_24', 'discord_decor_bzn_29',
            'discord_decor_zn_5', 'discord_decor_zn_7', 'discord_decor_zn_8_5',
            'discord_decor_zn_9', 'discord_decor_zn_14', 'discord_decor_zn_22',
            'discord_decor_zn_25', 'discord_decor_zn_30'
        ]:
            # Сохраняем выбранный товар в контексте
            context.user_data['selected_item'] = query.data
            # Переходим к подтверждению заказа
            await self.confirm_order(update, context)

        # Подтверждение заказа
        elif query.data == 'confirm_order':
            selected_item = context.user_data.get('selected_item')
            if not selected_item:
                await query.edit_message_text("❌ Помилка: товар не знайдено.")
                return

            # Получаем информацию о товаре
            items = {
                # Подписки
                'chatgpt_1': {'name': "ChatGPT Plus 1 місяць", 'price': 650},
                'discord_basic_1': {'name': "Discord Nitro Basic 1 місяць", 'price': 170},
                'discord_basic_12': {'name': "Discord Nitro Basic 12 місяців", 'price': 1700},
                'discord_full_1': {'name': "Discord Nitro Full 1 місяць", 'price': 330},
                'discord_full_12': {'name': "Discord Nitro Full 12 місяців", 'price': 3300},
                'duolingo_ind_1': {'name': "Duolingo Max (Individual) 1 місяць", 'price': 550},
                'duolingo_ind_12': {'name': "Duolingo Max (Individual) 12 місяців", 'price': 5500},
                'duolingo_fam_12': {'name': "Duolingo Max (Family) 12 місяців", 'price': 750},
                'picsart_plus_1': {'name': "Picsart AI Plus 1 місяць", 'price': 400},
                'picsart_plus_12': {'name': "Picsart AI Plus 12 місяців", 'price': 4000},
                'picsart_pro_1': {'name': "Picsart AI Pro 1 місяць", 'price': 550},
                'picsart_pro_12': {'name': "Picsart AI Pro 12 місяців", 'price': 5500},
                'canva_ind_1': {'name': "Canva Pro (Individual) 1 місяць", 'price': 500},
                'canva_ind_12': {'name': "Canva Pro (Individual) 12 місяців", 'price': 5000},
                'canva_fam_1': {'name': "Canva Pro (Family) 1 місяць", 'price': 650},
                'canva_fam_12': {'name': "Canva Pro (Family) 12 місяців", 'price': 6500},
                'netflix_1': {'name': "Netflix 1 місяць", 'price': 400},
                # Цифровые товары (Прикраси)
                'discord_decor_bzn_6': {'name': "Discord Прикраси (Без Nitro) 6$", 'price': 180}, # Обновлено
                'discord_decor_bzn_8': {'name': "Discord Прикраси (Без Nitro) 8$", 'price': 240}, # Обновлено
                'discord_decor_bzn_9': {'name': "Discord Прикраси (Без Nitro) 9$", 'price': 265}, # Обновлено
                'discord_decor_bzn_12': {'name': "Discord Прикраси (Без Nitro) 12$", 'price': 355}, # Обновлено
                'discord_decor_bzn_14': {'name': "Discord Прикраси (Без Nitro) 14$", 'price': 410}, # Обновлено
                'discord_decor_bzn_16': {'name': "Discord Прикраси (Без Nitro) 16$", 'price': 470}, # Обновлено
                'discord_decor_bzn_18': {'name': "Discord Прикраси (Без Nitro) 18$", 'price': 530}, # Обновлено
                'discord_decor_bzn_24': {'name': "Discord Прикраси (Без Nitro) 24$", 'price': 705}, # Обновлено
                'discord_decor_bzn_29': {'name': "Discord Прикраси (Без Nitro) 29$", 'price': 855}, # Обновлено
                'discord_decor_zn_5': {'name': "Discord Прикраси (З Nitro) 5$", 'price': 145}, # Обновлено
                'discord_decor_zn_7': {'name': "Discord Прикраси (З Nitro) 7$", 'price': 205}, # Обновлено
                'discord_decor_zn_8_5': {'name': "Discord Прикраси (З Nitro) 8.5$", 'price': 250}, # Обновлено
                'discord_decor_zn_9': {'name': "Discord Прикраси (З Nitro) 9$", 'price': 265}, # Обновлено
                'discord_decor_zn_14': {'name': "Discord Прикраси (З Nitro) 14$", 'price': 410}, # Обновлено
                'discord_decor_zn_22': {'name': "Discord Прикраси (З Nitro) 22$", 'price': 650}, # Обновлено
                'discord_decor_zn_25': {'name': "Discord Прикраси (З Nitro) 25$", 'price': 740}, # Обновлено
                'discord_decor_zn_30': {'name': "Discord Прикраси (З Nitro) 30$", 'price': 885} # Обновлено
            }

            item = items.get(selected_item)
            if not item:
                await query.edit_message_text("❌ Помилка: товар не знайдено.")
                return

            # Формируем текст заказа
            order_text = f"🛍️ Ваше замовлення:\n▫️ {item['name']} - {item['price']} UAH\n💳 Всього: {item['price']} UAH"

            # Определяем тип заказа
            conversation_type = 'digital_order' if 'discord_decor' in selected_item else 'subscription_order'

            # Создаем запись о заказе
            active_conversations[user_id] = {
                'type': conversation_type,
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            # Сохраняем в БД
            save_active_conversation(user_id, conversation_type, None, order_text)

            # Обновляем статистику
            bot_statistics['total_orders'] += 1
            save_stats()

            # Пересылаем заказ обоим владельцам
            await self.forward_order_to_owners(context, user_id, user, order_text)

            # --- Начало логики оплаты ---
            # Извлекаем сумму в UAH
            uah_amount = get_uah_amount_from_order_text(order_text)
            if uah_amount <= 0:
                 await query.edit_message_text("❌ Не вдалося визначити суму для оплати.")
                 return

            # Конвертируем в USD
            usd_amount = convert_uah_to_usd(uah_amount)
            if usd_amount <= 0:
                await query.edit_message_text("❌ Сума для оплати занадто мала.")
                return

            # Сохраняем сумму в USD в контексте пользователя
            context.user_data['payment_amount_usd'] = usd_amount
            context.user_data['order_details_for_payment'] = order_text # Сохраняем детали заказа

            # Предлагаем выбрать метод оплаты
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
            # --- Конец логики оплаты ---

        # Отмена заказа
        elif query.data == 'cancel_order':
            # Удаляем выбранный товар из контекста
            context.user_data.pop('selected_item', None)
            # Возвращаемся в главное меню
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Обработка кнопки "question"
        elif query.data == 'question':
            # Проверяем активные диалоги
            if user_id in active_conversations:
                await query.answer(
                    "❗ У вас вже є активний діалог."
                    "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                    "якщо хочете почати новий діалог.",
                    show_alert=True
                )
                return

            active_conversations[user_id] = {
                'type': 'question',
                'user_info': user,
                'assigned_owner': None,
                'last_message': "Нове запитання"
            }
            # Сохраняем в БД
            save_active_conversation(user_id, 'question', None, "Нове запитання")

            # Обновляем статистику
            bot_statistics['total_questions'] += 1
            save_stats()

            await query.edit_message_text(
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
                "Щоб завершити цей діалог пізніше, використайте команду /stop."
            )

        # Взятие заказа основателем
        elif query.data.startswith('take_order_'):
            client_id = int(query.data.split('_')[2])
            owner_id = user_id

            if client_id not in active_conversations:
                await query.answer("Діалог вже завершено", show_alert=True)
                return

            # Закрепляем заказ за основателем
            active_conversations[client_id]['assigned_owner'] = owner_id
            owner_client_map[owner_id] = client_id
            # Обновляем в БД
            save_active_conversation(client_id, active_conversations[client_id]['type'], owner_id, active_conversations[client_id]['last_message'])

            # Получаем информацию о клиенте
            client_info = active_conversations[client_id]['user_info']
            owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"

            # Уведомляем клиента
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text=f"✅ Ваш запит прийняв засновник магазину {owner_name}. Очікуйте на відповідь."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления клиента: {e}")

            # Уведомляем основателя
            await query.edit_message_text(f"✅ Ви взяли замовлення клієнта {client_info.first_name} (ID: {client_id}).")
            # Отправляем историю сообщений (если есть)
            history = get_conversation_history(client_id)
            if history:
                history_text = "📜 Історія повідомлень:\n"
                for msg in reversed(history): # От старых к новым
                    sender = "Клієнт" if msg['is_from_user'] else "Бот"
                    history_text += f"[{msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')}] {sender}: {msg['message']}\n"
                # Отправляем историю по частям, если она длинная
                if len(history_text) > 4096:
                    parts = [history_text[i:i+4096] for i in range(0, len(history_text), 4096)]
                    for part in parts:
                        await context.bot.send_message(chat_id=owner_id, text=part)
                else:
                    await context.bot.send_message(chat_id=owner_id, text=history_text)
            # Отправляем последнее сообщение и предлагаем ответить
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"💬 Последнее сообщение от клиента:\n{active_conversations[client_id]['last_message']}\nНапишите ответ:"
            )

        # Передача диалога другому основателю
        elif query.data.startswith('transfer_'):
            client_id = int(query.data.split('_')[1])
            current_owner = user_id
            other_owner = OWNER_ID_2 if current_owner == OWNER_ID_1 else OWNER_ID_1
            other_owner_name = "@oc33t" if other_owner == OWNER_ID_2 else "@HiGki2pYYY"

            if client_id in active_conversations:
                active_conversations[client_id]['assigned_owner'] = other_owner
                owner_client_map[other_owner] = client_id
                # Обновляем в БД
                save_active_conversation(client_id, active_conversations[client_id]['type'], other_owner, active_conversations[client_id]['last_message'])

                # Уведомляем нового владельца
                try:
                    client_info = active_conversations[client_id]['user_info']
                    await context.bot.send_message(
                        chat_id=other_owner,
                        text=f"🔄 Вам передали діалог з клієнтом {client_info.first_name} (ID: {client_id})."
                    )
                except Exception as e:
                    logger.error(f"Ошибка уведомления нового владельца: {e}")

                await query.edit_message_text(f"✅ Діалог передано {other_owner_name}.")
            else:
                await query.edit_message_text("❌ Діалог не знайдено.")

        # Продолжение диалога с клиентом (для основателей)
        elif query.data.startswith('continue_chat_'):
            client_id = int(query.data.split('_')[2])
            owner_id = user_id

            if client_id not in active_conversations:
                 await query.answer("Діалог вже завершено", show_alert=True)
                 return

            # Проверяем, не занят ли диалог другим основателем
            assigned_owner = active_conversations[client_id].get('assigned_owner')
            if assigned_owner and assigned_owner != owner_id:
                owner_name = "@HiGki2pYYY" if assigned_owner == OWNER_ID_1 else "@oc33t"
                await query.answer(f"Діалог вже призначено {owner_name}", show_alert=True)
                return

            # Закрепляем заказ за основателем
            active_conversations[client_id]['assigned_owner'] = owner_id
            owner_client_map[owner_id] = client_id
            # Обновляем в БД
            save_active_conversation(client_id, active_conversations[client_id]['type'], owner_id, active_conversations[client_id]['last_message'])

            # Уведомляем основателя
            await query.edit_message_text(f"✅ Ви продовжили діалог з клієнтом ID: {client_id}.")
            # Отправляем историю сообщений (если есть)
            history = get_conversation_history(client_id)
            if history:
                history_text = "📜 Історія повідомлень:\n"
                for msg in reversed(history): # От старых к новым
                    sender = "Клієнт" if msg['is_from_user'] else "Бот"
                    history_text += f"[{msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')}] {sender}: {msg['message']}\n"
                # Отправляем историю по частям, если она длинная
                if len(history_text) > 4096:
                    parts = [history_text[i:i+4096] for i in range(0, len(history_text), 4096)]
                    for part in parts:
                        await context.bot.send_message(chat_id=owner_id, text=part)
                else:
                    await context.bot.send_message(chat_id=owner_id, text=history_text)
            # Отправляем последнее сообщение и предлагаем ответить
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"💬 Последнее сообщение от клиента:\n{active_conversations[client_id]['last_message']}\nНапишите ответ:"
            )

        # Завершение диалога основателем
        elif query.data.startswith('end_chat_'):
            client_id = int(query.data.split('_')[2])
            owner_id = user_id

            # Удаляем диалог клиента из активных (в памяти)
            if client_id in active_conversations:
                # Сохраняем детали заказа перед удалением
                order_details = active_conversations[client_id].get('order_details', '')
                del active_conversations[client_id]
            # Удаляем связь владелец-клиент
            if owner_id in owner_client_map:
                del owner_client_map[owner_id]
            # Удаляем диалог из БД
            delete_active_conversation(client_id)

            # Уведомляем клиента
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text="✅ Ваш діалог із засновником магазину завершено. Дякуємо за звернення!"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления клиента о завершении: {e}")

            await query.edit_message_text("✅ Діалог із клієнтом завершено.")

        # --- Добавлено: Обработка кнопки "Оплатить" после заказа из файла ---
        elif query.data == 'proceed_to_payment':
            # Проверяем, есть ли активный заказ
            if user_id in active_conversations and 'order_details' in active_conversations[user_id]:
                order_text = active_conversations[user_id]['order_details']
                # --- Начало логики оплаты ---
                # Извлекаем сумму в UAH
                uah_amount = get_uah_amount_from_order_text(order_text)
                if uah_amount <= 0:
                     await query.edit_message_text("❌ Не вдалося визначити суму для оплати.")
                     return

                # Конвертируем в USD
                usd_amount = convert_uah_to_usd(uah_amount)
                if usd_amount <= 0:
                    await query.edit_message_text("❌ Сума для оплати занадто мала.")
                    return

                # Сохраняем сумму в USD в контексте пользователя
                context.user_data['payment_amount_usd'] = usd_amount
                context.user_data['order_details_for_payment'] = order_text # Сохраняем детали заказа

                # Предлагаем выбрать метод оплаты
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
                # --- Конец логики оплаты ---
            else:
                 await query.edit_message_text("❌ Не знайдено активного замовлення.")
        # --- Конец добавленной обработки ---

    # --- Добавленные методы ---
    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /pay для заказов с сайта или ручного ввода"""
        user = update.effective_user
        user_id = user.id
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем аргументы команды
        if not context.args:
            # Если аргументов нет, проверяем, есть ли активный заказ
            if user_id in active_conversations and 'order_details' in active_conversations[user_id]:
                order_text = active_conversations[user_id]['order_details']
            else:
                await update.message.reply_text("❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ... або використовуйте цю команду після оформлення замовлення в боті.")
                return
        else:
            # Первый аргумент - ID заказа
            order_id = context.args[0]
            # Объединяем все аргументы в одну строку
            items_str = " ".join(context.args[1:])
            # Используем реглярное выражение для извлечения товаров
            # Формат: <ServiceAbbr>-<PlanAbbr>-<Period>-<Price>
            # Для Discord Прикраси: DisU-BzN-6$-180
            # Для обычных подписок: Dis-Ful-1м-170
            pattern = r'(\w{2,4})-(\w{2,4})-([\w\s$]+?)-(\d+)'
            items = re.findall(pattern, items_str)
            if not items:
                await update.message.reply_text("❌ Не вдалося розпізнати товари у замовленні. Перевірте формат.")
                return

            # Расшифровка сокращений
            # Сервисы
            service_map = {
                "Cha": "ChatGPT",
                "Dis": "Discord",
                "Duo": "Duolingo",
                "Pic": "PicsArt",
                "Can": "Canva",
                "Net": "Netflix",
                "DisU": "Discord Прикраси" # Обновлено: Украшення -> Прикраси
            }
            # Планы
            plan_map = {
                "Bas": "Basic",
                "Ful": "Full",
                "Ind": "Individual",
                "Fam": "Family",
                "Plu": "Plus",
                "Pro": "Pro",
                "Pre": "Premium",
                "BzN": "Без Nitro", # Для прикрас
                "ZN": "З Nitro" # Для прикрас
            }

            total = 0
            order_details = [] # Для сохранения деталей
            # Определяем тип заказа на основе сервиса
            has_digital = False
            for item in items:
                service_abbr = item[0]
                plan_abbr = item[1]
                period = item[2].strip() # Убираем лишние пробелы
                price = item[3]

                service_name = service_map.get(service_abbr, service_abbr)
                plan_name = plan_map.get(plan_abbr, plan_abbr)
                try:
                    price_num = int(price)
                    total += price_num
                    # Определяем тип заказа на основе сервиса
                    if "Прикраси" in service_name:
                        has_digital = True
                    order_details.append(f"▫️ {service_name} {plan_name} ({period}) - {price_num} UAH")
                except ValueError:
                    await update.message.reply_text(f"❌ Неправильна ціна для товару: {service_abbr}-{plan_abbr}-{period}-{price}")
                    return

            # Формируем текст заказа
            order_text = f"🛍️ Замовлення з сайту (#{order_id}):"
            order_text += "\n" + "\n".join(order_details)
            order_text += f"\n💳 Всього: {total} UAH"
            # Сохраняем заказ
            conversation_type = 'digital_order' if has_digital else 'subscription_order'
            active_conversations[user_id] = {
                'type': conversation_type,
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            # Сохраняем в БД
            save_active_conversation(user_id, conversation_type, None, order_text)
            # Обновляем статистику
            bot_statistics['total_orders'] += 1
            save_stats()

        # --- Начало логики оплаты ---
        # Извлекаем сумму в UAH
        uah_amount = get_uah_amount_from_order_text(order_text)
        if uah_amount <= 0:
             await update.message.reply_text("❌ Не вдалося визначити суму для оплати.")
             return

        # Конвертируем в USD
        usd_amount = convert_uah_to_usd(uah_amount)
        if usd_amount <= 0:
            await update.message.reply_text("❌ Сума для оплати занадто мала.")
            return

        # Сохраняем сумму в USD в контексте пользователя
        context.user_data['payment_amount_usd'] = usd_amount
        context.user_data['order_details_for_payment'] = order_text # Сохраняем детали заказа

        # Предлагаем выбрать метод оплаты
        keyboard = [
            [InlineKeyboardButton("💳 Оплата карткою", callback_data='pay_card')],
            [InlineKeyboardButton("🪙 Криптовалюта", callback_data='pay_crypto')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"💳 Оберіть метод оплати для суми {usd_amount}$:",
            reply_markup=reply_markup
        )
        # --- Конец логики оплаты ---

    async def payment_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок, связанных с оплатой"""
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data

        # Проверяем, есть ли сумма в контексте
        usd_amount = context.user_data.get('payment_amount_usd')
        order_details = context.user_data.get('order_details_for_payment')
        if not usd_amount or not order_details:
            await query.edit_message_text("❌ Помилка: інформація про платіж втрачена. Спробуйте ще раз.")
            return

        # --- Оплата карткой ---
        if data == 'pay_card':
            # Создаем временную ссылку или просто сообщаем пользователю
            # В реальном приложении здесь может быть интеграция с платежной системой
            # или генерация уникального идентификатора заказа для ручной проверки
            # Для примера просто покажем сумму и кнопку "Оплачено"
            keyboard = [
                [InlineKeyboardButton("✅ Оплачено", callback_data='manual_payment_confirmed')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_payment_methods')],
                [InlineKeyboardButton("❓ Запитання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💳 Будь ласка, здійсніть оплату {usd_amount}$ карткою на реквізити магазину.\n"
                f"(Тут будуть реквізити)\n"
                f"Після оплати натисніть кнопку '✅ Оплачено'.",
                reply_markup=reply_markup
            )
            context.user_data['awaiting_manual_payment_confirmation'] = True

        # --- Оплата криптовалютою ---
        elif data == 'pay_crypto':
            # Отображаем список доступных криптовалют
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

        # --- Выбор конкретной криптовалюты ---
        elif data.startswith('pay_crypto_'):
            pay_currency = data.split('_')[2] # e.g., 'usdttrc20'
            # Находим название валюты
            currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == pay_currency), pay_currency)

            try:
                # Создаем счет в NOWPayments
                headers = {
                    'Authorization': f'Bearer {NOWPAYMENTS_API_KEY}',
                    'Content-Type': 'application/json'
                }
                payload = {
                    "price_amount": usd_amount,
                    "price_currency": "usd",
                    "pay_currency": pay_currency,
                    "ipn_callback_url": f"{WEBHOOK_URL}/nowpayments_ipn", # URL для уведомлений
                    "order_id": f"order_{user_id}_{int(time.time())}", # Уникальный ID заказа
                    "order_description": f"Оплата замовлення користувача {user_id}"
                }
                response = requests.post("https://api.nowpayments.io/v1/invoice", json=payload, headers=headers)
                response.raise_for_status()
                invoice = response.json()

                pay_url = invoice.get("invoice_url", "Помилка отримання посилання")
                invoice_id = invoice.get("invoice_id", "Невідомий ID рахунку")

                # Сохраняем ID инвойса для возможной проверки статуса
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

        # --- Проверка статуса оплаты ---
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
                response = requests.get(f"https://api.nowpayments.io/v1/invoice/{invoice_id}", headers=headers)
                response.raise_for_status()
                status_data = response.json()
                payment_status = status_data.get('payment_status', 'unknown')

                if payment_status == 'finished':
                    await query.edit_message_text("✅ Оплата успішно пройшла!")
                    # Переходим к сбору данных аккаунта
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
                else: # cancelled, expired, etc.
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

        # --- Ручне підтвердження оплати ---
        elif data == 'manual_payment_confirmed':
            # Здесь можно добавить логику проверки оплаты вручную или просто перейти к следующему шагу
            await query.edit_message_text("✅ Оплата підтверджена вручну.")
            # Переходим к сбору данных аккаунта
            await self.request_account_data(update, context)

        # --- Назад до вибору методу оплати ---
        elif data == 'back_to_payment_methods':
            # Повторно отображаем выбор метода оплаты
            keyboard = [
                [InlineKeyboardButton("💳 Оплата карткою", callback_data='pay_card')],
                [InlineKeyboardButton("🪙 Криптовалютa", callback_data='pay_crypto')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                [InlineKeyboardButton("❓ Запитання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                reply_markup=reply_markup
            )
            # Очищаем временные состояния
            context.user_data.pop('awaiting_manual_payment_confirmation', None)
            context.user_data.pop('awaiting_crypto_currency_selection', None)
            context.user_data.pop('nowpayments_invoice_id', None)

        # --- Назад до вибору криптовалюти ---
        elif data == 'back_to_crypto_selection':
            # Отображаем список доступных криптовалют
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
        """Запрашивает у пользователя логин и пароль от аккаунта"""
        query = update.callback_query
        user_id = query.from_user.id if update.callback_query else update.effective_user.id
        order_details = context.user_data.get('order_details_for_payment', '')

        # Определяем тип заказа
        is_digital = 'digital_order' in active_conversations.get(user_id, {}).get('type', '')
        item_type = "акаунту Discord" if is_digital else "акаунту"

        # Сообщаем пользователю и переходим в состояние ожидания данных
        message_text = f"✅ Оплата пройшла успішно!\n\n" \
                       f"Будь ласка, надішліть мені логін та пароль від {item_type}.\n" \
                       f"Наприклад: `login:password` або `login password`"
        
        if update.callback_query:
            await query.edit_message_text(message_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(message_text, parse_mode='Markdown')
        
        context.user_data['awaiting_account_data'] = True
        # Сохраняем детали заказа для передачи основателям позже
        context.user_data['account_details_order'] = order_details

    async def handle_account_data_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает сообщение с логином и паролем от пользователя"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text

        # Проверяем, ожидаем ли мы данные аккаунта от этого пользователя
        if not context.user_data.get('awaiting_account_data'):
            # Если нет, просто передаем сообщение как обычно
            await self.handle_message(update, context)
            return

        # Извлекаем логин и пароль (простая логика, можно усложнить)
        # Примеры: "user:pass", "user pass"
        parts = re.split(r'[:\s]+', message_text.strip(), maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ Неправильний формат. Будь ласка, надішліть логін та пароль у форматі `login:password` або `login password`.", parse_mode='Markdown')
            return

        login, password = parts[0], parts[1]

        # Получаем детали заказа
        order_details = context.user_data.get('account_details_order', 'Невідомий заказ')

        # Формируем сообщение для основателей
        account_info_message = f"🔐 Нові дані акаунту від клієнта!\n\n" \
                               f"👤 Клієнт: {user.first_name}\n" \
                               f"🆔 ID: {user_id}\n" \
                               f"🛍️ Замовлення: {order_details}\n\n" \
                               f"🔑 Логін: `{login}`\n" \
                               f"🔓 Пароль: `{password}`"

        # Отправляем основателям
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

        # Сообщаем пользователю
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
        # Удаляем активный диалог, так как процесс завершен
        if user_id in active_conversations:
            del active_conversations[user_id]
        delete_active_conversation(user_id)

    # --- Конец добавленных методов ---

    async def confirm_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подтверждение заказа"""
        query = update.callback_query
        selected_item = context.user_data.get('selected_item')
        if not selected_item:
            await query.edit_message_text("❌ Помилка: товар не знайдено.")
            return

        # Получаем информацию о товаре
        items = {
            # Подписки
            'chatgpt_1': {'name': "ChatGPT Plus 1 місяць", 'price': 650},
            'discord_basic_1': {'name': "Discord Nitro Basic 1 місяць", 'price': 170},
            'discord_basic_12': {'name': "Discord Nitro Basic 12 місяців", 'price': 1700},
            'discord_full_1': {'name': "Discord Nitro Full 1 місяць", 'price': 330},
            'discord_full_12': {'name': "Discord Nitro Full 12 місяців", 'price': 3300},
            'duolingo_ind_1': {'name': "Duolingo Max (Individual) 1 місяць", 'price': 550},
            'duolingo_ind_12': {'name': "Duolingo Max (Individual) 12 місяців", 'price': 5500},
            'duolingo_fam_12': {'name': "Duolingo Max (Family) 12 місяців", 'price': 750},
            'picsart_plus_1': {'name': "Picsart AI Plus 1 місяць", 'price': 400},
            'picsart_plus_12': {'name': "Picsart AI Plus 12 місяців", 'price': 4000},
            'picsart_pro_1': {'name': "Picsart AI Pro 1 місяць", 'price': 550},
            'picsart_pro_12': {'name': "Picsart AI Pro 12 місяців", 'price': 5500},
            'canva_ind_1': {'name': "Canva Pro (Individual) 1 місяць", 'price': 500},
            'canva_ind_12': {'name': "Canva Pro (Individual) 12 місяців", 'price': 5000},
            'canva_fam_1': {'name': "Canva Pro (Family) 1 місяць", 'price': 650},
            'canva_fam_12': {'name': "Canva Pro (Family) 12 місяців", 'price': 6500},
            'netflix_1': {'name': "Netflix 1 місяць", 'price': 400},
            # Цифровые товары (Прикраси)
            'discord_decor_bzn_6': {'name': "Discord Прикраси (Без Nitro) 6$", 'price': 180}, # Обновлено
            'discord_decor_bzn_8': {'name': "Discord Прикраси (Без Nitro) 8$", 'price': 240}, # Обновлено
            'discord_decor_bzn_9': {'name': "Discord Прикраси (Без Nitro) 9$", 'price': 265}, # Обновлено
            'discord_decor_bzn_12': {'name': "Discord Прикраси (Без Nitro) 12$", 'price': 355}, # Обновлено
            'discord_decor_bzn_14': {'name': "Discord Прикраси (Без Nitro) 14$", 'price': 410}, # Обновлено
            'discord_decor_bzn_16': {'name': "Discord Прикраси (Без Nitro) 16$", 'price': 470}, # Обновлено
            'discord_decor_bzn_18': {'name': "Discord Прикраси (Без Nitro) 18$", 'price': 530}, # Обновлено
            'discord_decor_bzn_24': {'name': "Discord Прикраси (Без Nitro) 24$", 'price': 705}, # Обновлено
            'discord_decor_bzn_29': {'name': "Discord Прикраси (Без Nitro) 29$", 'price': 855}, # Обновлено
            'discord_decor_zn_5': {'name': "Discord Прикраси (З Nitro) 5$", 'price': 145}, # Обновлено
            'discord_decor_zn_7': {'name': "Discord Прикраси (З Nitro) 7$", 'price': 205}, # Обновлено
            'discord_decor_zn_8_5': {'name': "Discord Прикраси (З Nitro) 8.5$", 'price': 250}, # Обновлено
            'discord_decor_zn_9': {'name': "Discord Прикраси (З Nitro) 9$", 'price': 265}, # Обновлено
            'discord_decor_zn_14': {'name': "Discord Прикраси (З Nitro) 14$", 'price': 410}, # Обновлено
            'discord_decor_zn_22': {'name': "Discord Прикраси (З Nitro) 22$", 'price': 650}, # Обновлено
            'discord_decor_zn_25': {'name': "Discord Прикраси (З Nitro) 25$", 'price': 740}, # Обновлено
            'discord_decor_zn_30': {'name': "Discord Прикраси (З Nitro) 30$", 'price': 885} # Обновлено
        }

        item = items.get(selected_item)
        if not item:
            await query.edit_message_text("❌ Помилка: товар не знайдено.")
            return

        # Формируем текст заказа
        order_text = f"🛍️ Ваше замовлення:\n▫️ {item['name']} - {item['price']} UAH\n💳 Всього: {item['price']} UAH"

        keyboard = [
            [InlineKeyboardButton("✅ Підтвердити", callback_data='confirm_order')],
            [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_order')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"{order_text}\n\nПідтвердити замовлення?", reply_markup=reply_markup)

    async def forward_order_to_owners(self, context, user_id, user, order_text):
        """Пересылает заказ основателям"""
        type_emoji = "🛒"
        conversation_type = active_conversations[user_id]['type']
        if conversation_type == 'subscription_order':
            type_text = "НОВЕ ЗАМОВЛЕННЯ (Підписка)"
        elif conversation_type == 'digital_order':
            type_text = "НОВЕ ЗАМОВЛЕННЯ (Цифровий товар)"
        else:
            type_text = "НОВЕ ПОВІДОМЛЕННЯ"

        forward_message = f"""{type_emoji} {type_text}!
👤 Клієнт: {user.first_name}
📱 Username: @{user.username if user.username else 'не вказано'}
🆔 ID: {user.id}
🌐 Язык: {user.language_code or 'не вказано'}
💬 Повідомлення:
{order_text}
-
Для відповіді просто напишіть повідомлення в цей чат.
Для завершення діалогу використовуйте /stop."""

        # Добавляем кнопки для основателей
        keyboard = [
            [InlineKeyboardButton("✅ Взяти замовлення", callback_data=f'take_order_{user_id}')],
            [InlineKeyboardButton("🔄 Передати іншому засновнику", callback_data=f'transfer_{user_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Отправляем сообщение обоим основателям
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(chat_id=owner_id, text=forward_message, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Ошибка отправки заказа основателю {owner_id}: {e}")

    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик сообщений от основателей"""
        owner_id = update.effective_user.id
        message_text = update.message.text

        # Проверяем, назначен ли основатель на клиента
        if owner_id not in owner_client_map:
            await update.message.reply_text(
                "❌ У вас немає активного діалогу з клієнтом."
                "Ви можете продовжити діалог з клієнтом через команду /chats або /dialog."
            )
            return

        client_id = owner_client_map[owner_id]

        # Проверяем, существует ли диалог клиента
        if client_id not in active_conversations:
            del owner_client_map[owner_id] # Удаляем устаревшую связь
            await update.message.reply_text("❌ Діалог із клієнтом вже завершено.")
            return

        # Пересылаем сообщение клиенту
        try:
            await context.bot.send_message(chat_id=client_id, text=message_text)
            await update.message.reply_text("✅ Повідомлення надіслано клієнту.")
            # Сохраняем сообщение от владельца
            save_message(client_id, message_text, False)
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения клиенту {client_id}: {e}")
            await update.message.reply_text("❌ Не вдалося надіслати повідомлення клієнту.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # 🔴 КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: сначала проверяем владельца
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await self.handle_owner_message(update, context)
            return

        # --- Добавлено: Проверка состояния ожидания данных аккаунта ---
        # Проверяем, ожидаем ли мы данные аккаунта
        if context.user_data.get('awaiting_account_data'):
            await self.handle_account_data_message(update, context)
            return
        # --- Конец добавления ---

        # Проверяем существование диалога
        if user_id not in active_conversations:
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "ℹ️ У вас немає активного діалогу. Щоб розпочати, виберіть дію:",
                reply_markup=reply_markup
            )
            return

        # Сохраняем последнее сообщение
        active_conversations[user_id]['last_message'] = message_text
        save_active_conversation(user_id, active_conversations[user_id]['type'], active_conversations[user_id].get('assigned_owner'), message_text)

        # Пересылаем сообщение основателю
        assigned_owner = active_conversations[user_id].get('assigned_owner')
        if assigned_owner:
            try:
                # Получаем информацию о пользователе
                client_info = active_conversations[user_id]['user_info']
                forward_text = f"Нове повідомлення від клієнта {client_info.first_name} (@{client_info.username or 'не вказано'}):\n\n{message_text}"
                await context.bot.send_message(chat_id=assigned_owner, text=forward_text)
                await update.message.reply_text("✅ Ваше повідомлення надіслано засновнику магазину.")
            except Exception as e:
                logger.error(f"Ошибка пересылки сообщения основателю {assigned_owner}: {e}")
                await update.message.reply_text("❌ Не вдалося надіслати повідомлення. Спробуйте ще раз.")
        else:
            # Пересылаем сообщение обоим основателям
            forward_text = f"Нове повідомлення від клієнта {user.first_name} (@{user.username or 'не вказано'}):\n\n{message_text}"
            success = False
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                try:
                    await context.bot.send_message(chat_id=owner_id, text=forward_text)
                    success = True
                except Exception as e:
                    logger.error(f"Ошибка пересылки сообщения основателю {owner_id}: {e}")
            if success:
                await update.message.reply_text("✅ Ваше повідомлення передано засновникам магазину. Очікуйте на відповідь найближчим часом.")
            else:
                await update.message.reply_text("❌ Не вдалося надіслати повідомлення. Спробуйте ще раз.")

    async def continue_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /dialog <user_id> для основателей"""
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        if not context.args:
            await update.message.reply_text("ℹ️ Использование: /dialog <user_id>")
            return

        try:
            client_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Неверный формат ID. ID должно быть числом.")
            return

        # Проверяем, есть ли пользователь в БД
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT * FROM users WHERE id = %s", (client_id,))
                    client_info = cur.fetchone()
        except Exception as e:
            logger.error(f"Ошибка получения пользователя: {e}")
            await update.message.reply_text("❌ Ошибка получения информации о пользователе.")
            return

        if not client_info:
            await update.message.reply_text("❌ Пользователь с таким ID не найден.")
            return

        # Проверяем, есть ли активный диалог
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT * FROM active_conversations WHERE user_id = %s", (client_id,))
                    conversation = cur.fetchone()
        except Exception as e:
            logger.error(f"Ошибка получения диалога: {e}")
            await update.message.reply_text("❌ Ошибка получения информации о диалоге.")
            return

        if not conversation:
            await update.message.reply_text("❌ У пользователя нет активного диалога.")
            return

        # Проверяем, не занят ли диалог другим основателем
        assigned_owner = conversation['assigned_owner']
        if assigned_owner and assigned_owner != owner_id:
            owner_name = "@HiGki2pYYY" if assigned_owner == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(f"❌ Диалог уже назначен {owner_name}.")
            return

        # Закрепляем заказ за основателем
        active_conversations[client_id] = {
            'type': conversation['conversation_type'],
            'user_info': client_info,
            'assigned_owner': owner_id,
            'last_message': conversation['last_message']
        }
        owner_client_map[owner_id] = client_id
        # Обновляем в БД
        save_active_conversation(client_id, conversation['conversation_type'], owner_id, conversation['last_message'])

        # Уведомляем основателя
        owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"
        keyboard = [
            [InlineKeyboardButton("🔄 Передати іншому засновнику", callback_data=f'transfer_{client_id}')],
            [InlineKeyboardButton("✅ Завершити діалог", callback_data=f'end_chat_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message_text = f"""✅ Ви продовжили діалог з клієнтом {client_info['first_name']} (ID: {client_id}).
Призначено: {owner_name}

Для відповіді просто напишіть повідомлення в цей чат.
Для завершення діалогу використовуйте кнопку нижче 👇"""
        await update.message.reply_text(message_text.strip(), reply_markup=reply_markup)

        # Отправляем историю сообщений (если есть)
        history = get_conversation_history(client_id)
        if history:
            history_text = "📜 Історія повідомлень:\n"
            for msg in reversed(history): # От старых к новым
                sender = "Клієнт" if msg['is_from_user'] else "Бот"
                history_text += f"[{msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')}] {sender}: {msg['message']}\n"
            # Отправляем историю по частям, если она длинная
            if len(history_text) > 4096:
                parts = [history_text[i:i+4096] for i in range(0, len(history_text), 4096)]
                for part in parts:
                    await context.bot.send_message(chat_id=owner_id, text=part)
            else:
                await context.bot.send_message(chat_id=owner_id, text=history_text)

        # Отправляем последнее сообщение и предлагаем ответить
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"💬 Последнее сообщение от клиента:\n{conversation['last_message']}\nНапишите ответ:"
        )

    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop для завершения диалогов"""
        user = update.effective_user
        user_id = user.id
        user_name = user.first_name

        # Для основателей: завершение диалога с клиентом
        if user_id in [OWNER_ID_1, OWNER_ID_2] and user_id in owner_client_map:
            client_id = owner_client_map[user_id]
            client_info = active_conversations.get(client_id, {}).get('user_info')

            # Удаляем диалог клиента из активных (в памяти)
            if client_id in active_conversations:
                # Сохраняем детали заказа перед удалением
                order_details = active_conversations[client_id].get('order_details', '')
                del active_conversations[client_id]
            # Удаляем связь владелец-клиент
            del owner_client_map[user_id]
            # Удаляем диалог из БД
            delete_active_conversation(client_id)

            # Уведомляем клиента
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text="✅ Ваш діалог із засновником магазину завершено. Дякуємо за звернення!"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления клиента о завершении: {e}")

            await update.message.reply_text("✅ Діалог із клієнтом завершено.")
            return

        # Для обычных пользователей
        if user_id not in active_conversations:
            await update.message.reply_text("ℹ️ У вас немає активного діалогу.")
            return

        # Получаем тип диалога
        conversation_type = active_conversations[user_id]['type']

        # Удаляем диалог из активных (в памяти)
        del active_conversations[user_id]
        # Удаляем диалог из БД
        delete_active_conversation(user_id)

        # Уведомляем основателей (если диалог был назначен)
        assigned_owner = active_conversations.get(user_id, {}).get('assigned_owner')
        if assigned_owner:
            try:
                await context.bot.send_message(
                    chat_id=assigned_owner,
                    text=f"ℹ️ Клієнт {user_name} (ID: {user_id}) завершив діалог."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления основателя о завершении: {e}")

        # Отправляем подтверждающее сообщение пользователю
        if conversation_type == 'order':
            confirmation_text = "✅ Ваше замовлення прийнято! Засновник магазину зв'яжеться з вами найближчим часом."
        elif conversation_type == 'question':
            confirmation_text = "✅ Ваше запитання прийнято! Засновник магазину відповість найближчим часом."
        else:
            confirmation_text = "✅ Діалог завершено."

        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(confirmation_text, reply_markup=reply_markup)

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статистику бота"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        total_users = get_total_users_count()
        stats_text = f"""📊 Статистика бота:

👥 Всього користувачів: {total_users}
🛒 Всього замовлень: {bot_statistics['total_orders']}
❓ Всього запитань: {bot_statistics['total_questions']}
⏰ Останнє скидання: {bot_statistics['last_reset']}"""
        await update.message.reply_text(stats_text)

    async def show_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает историю сообщений пользователя"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        if not context.args:
            await update.message.reply_text("ℹ️ Использование: /history <user_id>")
            return

        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Неверный формат ID. ID должно быть числом.")
            return

        history = get_conversation_history(target_user_id)
        if not history:
            await update.message.reply_text("ℹ️ У пользователя нет истории сообщений.")
            return

        history_text = f"📜 Історія повідомлень користувача (ID: {target_user_id}):\n\n"
        for msg in reversed(history): # От старых к новым
            sender = "Клієнт" if msg['is_from_user'] else "Бот"
            history_text += f"[{msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')}] {sender}: {msg['message']}\n"

        # Отправляем историю по частям, если она длинная
        if len(history_text) > 4096:
            parts = [history_text[i:i+4096] for i in range(0, len(history_text), 4096)]
            for part in parts:
                await context.bot.send_message(chat_id=user_id, text=part)
        else:
            await context.bot.send_message(chat_id=user_id, text=history_text)

    async def show_active_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает активные чаты для основателей с кнопкой продолжить"""
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        try:
            # Сначала сбрасываем буфер активных диалогов
            flush_active_conv_buffer()
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("""
                        SELECT ac.*, u.first_name, u.username
                        FROM active_conversations ac
                        JOIN users u ON ac.user_id = u.id
                        ORDER BY ac.updated_at DESC
                    """)
                    conversations = cur.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения активных диалогов: {e}")
            await update.message.reply_text("❌ Ошибка получения активных диалогов.")
            return

        if not conversations:
            await update.message.reply_text("ℹ️ Немає активних діалогів.")
            return

        response_text = "💬 Активні діалоги:\n\n"
        for conv in conversations:
            assigned = "Ні" if conv['assigned_owner'] is None else ("@HiGki2pYYY" if conv['assigned_owner'] == OWNER_ID_1 else "@oc33t")
            response_text += f"▫️ {conv['first_name']} (@{conv['username'] or 'не вказано'}) (ID: {conv['user_id']}) - {conv['conversation_type']} - Призначено: {assigned}\n"

        await update.message.reply_text(response_text)

    async def clear_active_conversations_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда очистки активных диалогов"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        deleted_count = clear_all_active_conversations()
        # Также очищаем память
        active_conversations.clear()
        owner_client_map.clear()
        await update.message.reply_text(f"✅ Очищено {deleted_count} активних діалогів.")

    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает информацию о канале"""
        channel_text = """📢 Наш канал: @SecureShopChannel

Тут ви можете переглянути:
- Асортимент товарів
- Оновлення магазину
- Розіграші та акції

Приєднуйтесь, щоб бути в курсі всіх новин!"""
        await update.message.reply_text(channel_text)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает справку и информацию о сервисе"""
        # Универсальный метод для обработки команды и кнопки
        if isinstance(update, Update):
            message = update.message
        else:
            message = update # для вызова из кнопки

        help_text = """👋 Доброго дня! Я бот магазину SecureShop.

🤖 Я бот магазину SecureShop.
🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій.
Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання по нашому сервісу можна задавати цілодобово.

📌 Список доступних команд:
/start - Головне меню
/order - Зробити замовлення
/question - Поставити запитання
/channel - Наш канал з асортиментом, оновленнями та розіграшами
/stop - Завершити поточний діалог
/help - Ця довідка

💬 Якщо у вас виникли питання, не соромтеся звертатися!"""
        await message.reply_text(help_text.strip())

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(msg="Exception while handling an update:", exc_info=context.error)

        # Формируем текст ошибки
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)

        # Отправляем сообщение об ошибке владельцу
        error_text = f"🚨 Ошибка в боте:\n{tb_string}"
        if len(error_text) > 4096:
            error_text = error_text[:4000] + "\n... (обрезано)"

        try:
            await context.bot.send_message(chat_id=OWNER_ID_1, text=error_text)
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об ошибке: {e}")

# --- Flask приложение ---
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
            # asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), telegram_app.loop)
            # Используем loop.call_soon_threadsafe для лучшей совместимости
            if telegram_app.loop and not telegram_app.loop.is_closed():
                 future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), telegram_app.loop)
                 # Можно добавить future.add_done_callback для обработки результата/ошибок
        return '', 200
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}")
        return jsonify({'error': str(e)}), 500

# --- IPN обработчик для NOWPayments (если будет использоваться) ---
# @flask_app.route('/nowpayments_ipn', methods=['POST'])
# def nowpayments_ipn():
#     # Здесь будет логика обработки уведомлений от NOWPayments
#     # Например, обновление статуса заказа в БД и уведомление пользователя/основателя
#     # data = request.json
#     # logger.info(f"NOWPayments IPN received: {data}")
#     # return jsonify({'status': 'ok'}), 200
#     pass # Заглушка

# --- Основная логика запуска ---
bot_instance = TelegramBot()

async def setup_webhook():
    """Настройка webhook"""
    try:
        if not WEBHOOK_URL:
            logger.error("❌ WEBHOOK_URL не установлен")
            return False

        # Удаляем существующий webhook
        await bot_instance.application.bot.delete_webhook()
        logger.info("🗑️ Старый webhook удален")

        # Устанавливаем новый webhook
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
    """Асинхронный запуск бота"""
    global bot_running, telegram_app
    if bot_running:
        logger.warning("🛑 Бот уже запущен! Пропускаем повторный запуск")
        return

    try:
        await bot_instance.initialize()
        telegram_app = bot_instance.application

        if USE_POLLING:
            await setup_webhook() # Удаляем webhook, если был
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
    # Задержка для Render.com, чтобы избежать конфликтов
    if os.environ.get('RENDER'):
        logger.info("⏳ Ожидаем 10 секунд для предотвращения конфликтов...")
        time.sleep(10)

    # Запускаем автосохранение
    auto_save_thread = threading.Thread(target=auto_save_loop)
    auto_save_thread.daemon = True
    auto_save_thread.start()

    logger.info("🚀 Запуск SecureShop Telegram Bot...")
    logger.info(f"🔑 BOT_TOKEN: {BOT_TOKEN[:10]}...")
    logger.info(f"🌐 PORT: {PORT}")
    logger.info(f"📡 WEBHOOK_URL: {WEBHOOK_URL}")
    logger.info(f"⏰ PING_INTERVAL: {PING_INTERVAL} секунд")

    # Запускаем Flask приложение в отдельном потоке
    from waitress import serve
    flask_thread = threading.Thread(target=lambda: serve(flask_app, host='0.0.0.0', port=PORT))
    flask_thread.daemon = True
    flask_thread.start()
    logger.info(f"🌐 Flask сервер запущен на порту {PORT}")

    # Запускаем бота в отдельном потоке
    bot_t = threading.Thread(target=bot_thread)
    bot_t.daemon = True
    bot_t.start()

    # Основной цикл для проверки состояния
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
