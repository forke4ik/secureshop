# main.py (обновленный с интеграцией /payout - выбор оплаты пользователем)
import logging
import os
import asyncio
import threading
import time
import json
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg
from psycopg.rows import dict_row
import requests # Добавлено для интеграции NOWPayments

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальные переменные состояния
bot_running = False
bot_lock = threading.Lock()

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
OWNER_ID_1 = 7106925462 # @HiGki2pYYY
OWNER_ID_2 = 6279578957 # @oc33t
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'
DATABASE_URL = os.getenv('DATABASE_URL')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840)) # 14 минут

# - Добавленные/обновлённые переменные окружения для оплаты -
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY')
EXCHANGE_RATE_UAH_TO_USD = float(os.getenv('EXCHANGE_RATE_UAH_TO_USD', 41.26))

# Доступные криптовалюты для NOWPayments (из оплата.txt)
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
# - Конец добавленных переменных -

# Путь к файлу с данными
STATS_FILE = "bot_stats.json"

# Оптимизация: буферизация запросов к БД
BUFFER_FLUSH_INTERVAL = 300 # 5 минут
BUFFER_MAX_SIZE = 50
message_buffer = []
active_conv_buffer = []

# Оптимизация: кэш пользователей и истории
user_cache = set()
history_cache = {}

# Глобальные переменные для статистики и активных диалогов
bot_statistics = {
    'total_users': 0,
    'total_orders': 0,
    'total_questions': 0,
    'last_reset': datetime.now().isoformat()
}
active_conversations = {}
owner_client_map = {}

# Глобальные переменные для приложения
telegram_app = None
flask_app = Flask(__name__)
CORS(flask_app) # Разрешаем CORS для всех доменов

# - Добавленные/обновлённые вспомогательные функции -
def get_uah_amount_from_order_text(order_text: str) -> float:
    """Извлекает сумму в UAH из текста заказа."""
    match = re.search(r'💳 Всього: (\d+(?:\.\d+)?) UAH', order_text) # Улучшен regex для дробных чисел
    if match:
        return float(match.group(1))
    return 0.0

def convert_uah_to_usd(uah_amount: float) -> float:
    """Конвертирует сумму из UAH в USD по фиксированному курсу."""
    if uah_amount <= 0:
        return 0.0
    return round(uah_amount / EXCHANGE_RATE_UAH_TO_USD, 2)

def load_stats():
    """Загружает статистику из файла"""
    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            data['last_reset'] = datetime.fromisoformat(data['last_reset']) # Преобразуем строку обратно в datetime
            logger.info("📊 Статистика загружена из файла")
            return data
    except FileNotFoundError:
        logger.info("🆕 Файл статистики не найден, создается новый")
        return bot_statistics
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки статистики: {e}")
        return bot_statistics

def save_stats():
    try:
        # Сохраняем копию, чтобы не модифицировать исходный объект
        stats_to_save = bot_statistics.copy()
        stats_to_save['last_reset'] = stats_to_save['last_reset'].isoformat() # Преобразуем datetime в строку
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats_to_save, f, ensure_ascii=False, indent=4)
        logger.info("💾 Статистика сохранена в файл")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения статистики: {e}")

def auto_save_loop():
    """Автосохранение статистики каждые 10 минут"""
    while True:
        time.sleep(600) # 10 минут
        save_stats()
        logger.info("✅ Статистика автосохранена")

# Инициализация статистики
bot_statistics = load_stats()

# Функции для работы с БД (заглушки, замените на реальные)
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
                        updated_at = CURRENT_TIMESTAMP;
                """, (user.id, user.username, user.first_name, user.last_name, user.language_code, user.is_bot))
        user_cache.add(user.id)
        logger.debug(f"👤 Пользователь {user.id} добавлен/обновлён в БД")
    except Exception as e:
        logger.error(f"❌ Ошибка ensure_user_exists для {user.id}: {e}")

def save_message(user_id, message_text, is_from_user):
    """Сохраняет сообщение в буфер"""
    message_buffer.append((user_id, message_text, is_from_user))
    if len(message_buffer) >= BUFFER_MAX_SIZE:
        flush_message_buffer()

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
        logger.info(f"📨 Буфер сообщений ({len(message_buffer)}) записан в БД")
        message_buffer = []
    except Exception as e:
        logger.error(f"❌ Ошибка записи буфера сообщений: {e}")

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message):
    """Сохраняет активный диалог в буфер"""
    active_conv_buffer.append((user_id, conversation_type, assigned_owner, last_message))
    # Простая проверка размера буфера, можно улучшить
    if len(active_conv_buffer) >= BUFFER_MAX_SIZE:
        flush_active_conv_buffer()

def flush_active_conv_buffer():
    global active_conv_buffer
    if not active_conv_buffer:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Группируем по user_id, оставляя последнюю запись
                latest_convs = {}
                for item in active_conv_buffer:
                    user_id = item[0]
                    latest_convs[user_id] = item # Перезаписываем более старыми данными

                # Создаем временную таблицу
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
        logger.info(f"🔄 Буфер активных диалогов ({len(active_conv_buffer)}) записан в БД")
        active_conv_buffer = []
    except Exception as e:
        logger.error(f"❌ Ошибка записи буфера активных диалогов: {e}")

def delete_active_conversation(user_id):
    """Удаляет активный диалог из БД"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations WHERE user_id = %s", (user_id,))
                conn.commit()
        logger.debug(f"🗑️ Диалог пользователя {user_id} удалён из БД")
    except Exception as e:
        logger.error(f"❌ Ошибка удаления диалога {user_id}: {e}")

def get_conversation_history(client_id):
    """Получает историю сообщений клиента"""
    if client_id in history_cache:
        logger.debug(f"📖 История для {client_id} получена из кэша")
        return history_cache[client_id]
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT message, is_from_user, created_at
                    FROM messages
                    WHERE user_id = %s
                    ORDER BY created_at ASC
                """, (client_id,))
                history = cur.fetchall()
        history_cache[client_id] = history # Кэшируем
        logger.debug(f"📖 История для {client_id} загружена из БД")
        return history
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории для {client_id}: {e}")
        return []

def get_active_conversations():
    """Получает все активные диалоги из БД"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM active_conversations")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Ошибка получения активных диалогов: {e}")
        return []

def get_users():
    """Получает список всех пользователей"""
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
                conn.commit()
        logger.info("🗑️ Все активные диалоги очищены из БД")
    except Exception as e:
        logger.error(f"❌ Ошибка очистки активных диалогов: {e}")
# - Конец вспомогательных функций -

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.initialized = False
        self.loop = None

    def setup_handlers(self):
        # Обработчики команд
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("order", self.order))
        self.application.add_handler(CommandHandler("question", self.question))
        self.application.add_handler(CommandHandler("channel", self.channel))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("stop", self.stop))
        self.application.add_handler(CommandHandler("pay", self.pay_command)) # Добавлено
        self.application.add_handler(CommandHandler("payout", self.payout_command)) # Добавлено для основателей
        self.application.add_handler(CommandHandler("stats", self.stats_command)) # Исправлено
        self.application.add_handler(CommandHandler("dialog", self.continue_dialog_command))

        # Обработчики callback кнопок
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        # - Добавлено: Обработчик callback кнопок оплаты -
        # Упрощенный паттерн для отладки и захвата всех pay_ и payout_ callback'ов
        self.application.add_handler(CallbackQueryHandler(self.payment_callback_handler))
        # - Конец добавленных обработчиков -

        # Обработчик текстовых сообщений (должен быть последним)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Обработчик документов (для заказов из файлов)
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

    async def set_commands_menu(self):
        """Установка стандартного меню команд"""
        owner_commands = [BotCommandScopeChat(chat_id=OWNER_ID_1), BotCommandScopeChat(chat_id=OWNER_ID_2)]
        # Исправлено: Используем BotCommandScopeDefault для всех пользователей
        user_commands = [BotCommandScopeDefault()] # Для всех пользователей

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
                ('dialog', 'Продовжити діалог (для основателей)'),
                ('payout', 'Створити платіж для користувача (для основателей)') # Добавлена команда
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

    # --- Основные обработчики команд ---

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

    async def order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /order"""
        user = update.effective_user
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user.id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог.\n"
                "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                "якщо хочете почати новий діалог."
            )
            return

        keyboard = [
            [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
            [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("📦 Оберіть тип товару:", reply_markup=reply_markup)

    async def question(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /question"""
        user = update.effective_user
        user_id = user.id
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог.\n"
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
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n"
            "Щоб завершити цей діалог пізніше, використайте команду /stop."
        )

    async def channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /channel"""
        await update.message.reply_text("🔗 Наш канал: https://t.me/+_DqX27kO0a41YzNi")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        message = update.effective_message
        user = update.effective_user
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Определяем, является ли пользователь основателем
        is_owner = user.id in [OWNER_ID_1, OWNER_ID_2]

        help_text = f"""ℹ️ Довідка по боту SecureShop

📌 Список доступних команд:
/start - Головне меню
/order - Зробити замовлення
/question - Поставити запитання
/channel - Наш канал з асортиментом, оновленнями та розіграшами
/stop - Завершити поточний діалог
/help - Ця довідка"""

        if is_owner:
            help_text += """
            
🔐 Команди основателя:
/stats - Статистика бота
/history - Історія повідомлень клієнта
/chats - Активні чати
/clear - Очистити активні чати
/dialog - Продовжити діалог з клієнтом
/payout - Створити платіж для користувача"""

        help_text += "\n\n💬 Якщо у вас виникли питання, не соромтеся звертатися!"

        await message.reply_text(help_text.strip())

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop"""
        user = update.effective_user
        user_id = user.id
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id not in active_conversations:
            await update.message.reply_text("❌ У вас немає активного діалогу.")
            return

        # Получаем тип диалога
        conversation_type = active_conversations[user_id]['type']

        # Удаляем диалог из активных (в памяти)
        del active_conversations[user_id]
        # Удаляем диалог из БД
        delete_active_conversation(user_id)

        # Формируем текст подтверждения
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

    # - Добавленные методы для оплаты -
    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /pay для заказов с сайта или ручного ввода"""
        # ... (оставьте существующую логику /pay_command без изменений) ...
        await update.message.reply_text("❌ Логика /pay не реализована в этом примере.")

    async def payout_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /payout для основателей - выбор оплаты пользователем"""
        logger.info("📥 /payout команда получена")
        user = update.effective_user
        owner_id = user.id

        # Проверяем, является ли пользователь основателем
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            logger.info(f"🚫 /payout: Доступ запрещен для пользователя {owner_id}")
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return

        logger.info(f"✅ /payout: Доступ разрешен для основателя {owner_id}")

        # Проверяем аргументы команды
        if len(context.args) < 2:
            logger.info(f"⚠️ /payout: Неверный формат от пользователя {owner_id}")
            await update.message.reply_text(
                "❌ Неправильний формат команди.\n"
                "Використовуйте: `/payout <user_id> <amount_in_uah>`\n"
                "Наприклад: `/payout 123456789 500`",
                parse_mode='Markdown'
            )
            return

        try:
            target_user_id = int(context.args[0])
            uah_amount = float(context.args[1])
            if uah_amount <= 0:
                raise ValueError("Сума повинна бути більше нуля.")
            logger.info(f"✅ /payout: Аргументы распознаны. target_user_id={target_user_id}, uah_amount={uah_amount}")
        except ValueError as e:
            logger.error(f"❌ /payout: Ошибка парсинга аргументов от {owner_id}: {e}")
            await update.message.reply_text(f"❌ Неправильний формат ID користувача або суми: {e}")
            return

        # Конвертируем сумму в USD
        usd_amount = convert_uah_to_usd(uah_amount)
        if usd_amount <= 0:
            logger.info(f"⚠️ /payout: Сумма USD слишком мала для {owner_id}")
            await update.message.reply_text("❌ Сума в USD занадто мала для створення рахунку.")
            return

        # --- НОВАЯ ЛОГИКА: Отправляем запрос на выбор оплаты целевому пользователю ---
        # Сохраняем данные в контексте ОСНОВАТЕЛЯ для последующего использования
        context.user_data['payout_pending'] = {
            'target_user_id': target_user_id,
            'uah_amount': uah_amount,
            'usd_amount': usd_amount
        }
        logger.info(f"💾 /payout: Данные сохранены в context.user_data основателя {owner_id}")

        # Предлагаем целевому пользователю выбрать метод оплаты
        keyboard = [
            [InlineKeyboardButton("💳 Оплата карткою", callback_data=f'payout_user_card_{target_user_id}_{int(uah_amount*100)}')], # Кодируем сумму в копейках/центах
            [InlineKeyboardButton("🪙 Криптовалюта", callback_data=f'payout_user_crypto_{target_user_id}_{int(uah_amount*100)}')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_user_cancel')],
            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg_text = (
            f"💳 Вам виставлено рахунок на {uah_amount}₴ ({usd_amount}$).\n"
            f"Оберіть зручний спосіб оплати:"
        )
        logger.info(f"📤 /payout: Отправка меню выбора оплаты пользователю {target_user_id}")
        
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=msg_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            await update.message.reply_text(f"✅ Запит на оплату відправлено користувачу `{target_user_id}`.", parse_mode='Markdown')
            logger.info(f"✅ Сообщение с выбором оплаты отправлено пользователю {target_user_id} по запросу основателя {owner_id}")
        except Exception as e:
            error_msg = f"❌ Не вдалося надіслати запит користувачу {target_user_id}: {e}"
            await update.message.reply_text(error_msg)
            logger.error(error_msg)

    async def payment_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок, связанных с оплатой"""
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id # ID того, кто нажал кнопку
        data = query.data

        logger.info(f"📥 Payment Callback received: data='{data}', from user_id={user_id}")

        # --- Логика для /pay (для обычных пользователей) ---
        if data.startswith('pay_'):
            logger.info(f"➡️ Передача callback 'pay_' в существующую логику для пользователя {user_id}")
            # --- ВСТАВЬТЕ СЮДА ВЕСЬ ВАШ СУЩЕСТВУЮЩИЙ КОД ДЛЯ ОБРАБОТКИ pay_ callback'ов ---
            await query.edit_message_text("💳 /pay логика (не реализована в этом примере)")
            return # ВАЖНО: return после обработки pay_

        # --- Логика для /payout (выбор и обработка оплаты пользователем) ---
        elif data.startswith('payout_user_'):
            logger.info(f"🎯 Обработка payout_user callback: '{data}' от пользователя {user_id}")
            
            parts = data.split('_')
            if len(parts) < 3:
                await query.answer("❌ Невірний формат запиту.", show_alert=True)
                logger.warning(f"⚠️ Неверный формат callback '{data}' от пользователя {user_id}")
                return

            action = parts[2]

            # --- Обработка выбора способа оплаты пользователем ---
            if action in ['card', 'crypto']:
                if len(parts) < 5:
                     await query.answer("❌ Невірний формат запиту.", show_alert=True)
                     logger.warning(f"⚠️ Неверный формат callback '{data}' для выбора оплаты от пользователя {user_id}")
                     return
                
                try:
                    target_user_id = int(parts[3])
                    uah_amount_cents = int(parts[4]) # Получаем сумму в копейках
                    uah_amount = uah_amount_cents / 100.0
                    usd_amount = convert_uah_to_usd(uah_amount)
                except (ValueError, IndexError) as e:
                     await query.answer("❌ Помилка обробки даних.", show_alert=True)
                     logger.error(f"❌ Ошибка парсинга данных из callback '{data}' для пользователя {user_id}: {e}")
                     return

                # Проверка, что пользователь, нажавший кнопку, является целевым
                if user_id != target_user_id:
                     await query.answer("❌ Цей рахунок не для вас.", show_alert=True)
                     logger.warning(f"🚫 Пользователь {user_id} пытался оплатить счет для {target_user_id}")
                     return

                if action == 'card':
                    # --- Оплата карткой ---
                    card_number = "XXXX-XXXX-XXXX-XXXX" # Замените на реальный номер
                    keyboard = [
                        [InlineKeyboardButton("✅ Оплачено", callback_data=f'payout_user_card_paid_{target_user_id}_{uah_amount_cents}')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='payout_user_cancel')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"💳 Оплата {uah_amount}₴ ({usd_amount}$) карткою.\n"
                        f"Реквізити: `{card_number}`\n"
                        f"Після оплати натисніть кнопку '✅ Оплачено'.",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    logger.info(f"💳 Показаны реквизиты для карты пользователю {user_id}")

                elif action == 'crypto':
                    # --- Оплата криптовалютою ---
                    keyboard = []
                    for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                        # Кодируем данные в callback
                        callback_data = f'payout_user_crypto_select_{target_user_id}_{uah_amount_cents}_{currency_code}'
                        keyboard.append([InlineKeyboardButton(currency_name, callback_data=callback_data)])
                    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='payout_user_cancel')])
                    keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"🪙 Оберіть криптовалюту для оплати {uah_amount}₴ ({usd_amount}$):",
                        reply_markup=reply_markup
                    )
                    logger.info(f"🪙 Показан выбор криптовалюты пользователю {user_id}")

            # --- Обработка подтверждения оплаты картой пользователем ---
            elif action == 'card' and len(parts) > 3 and parts[3] == 'paid':
                 if len(parts) < 5:
                      await query.answer("❌ Невірний формат запиту.", show_alert=True)
                      logger.warning(f"⚠️ Неверный формат callback '{data}' для подтверждения оплаты картой от пользователя {user_id}")
                      return
                 try:
                      target_user_id = int(parts[4])
                      uah_amount_cents = int(parts[5])
                      uah_amount = uah_amount_cents / 100.0
                      usd_amount = convert_uah_to_usd(uah_amount)
                 except (ValueError, IndexError) as e:
                      await query.answer("❌ Помилка обробки даних.", show_alert=True)
                      logger.error(f"❌ Ошибка парсинга данных из callback '{data}' для подтверждения оплаты картой {user_id}: {e}")
                      return

                 if user_id != target_user_id:
                      await query.answer("❌ Цей рахунок не для вас.", show_alert=True)
                      logger.warning(f"🚫 Пользователь {user_id} пытался подтвердить оплату счета для {target_user_id}")
                      return

                 await query.edit_message_text("✅ Оплата карткою підтверджена. Дякуємо!")
                 # --- Уведомляем ОСНОВАТЕЛЕЙ ---
                 owner_msg = f"✅ Користувач {user_id} повідомив про оплату рахунку {uah_amount}₴ ({usd_amount}$) карткою!"
                 for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                      try:
                           await context.bot.send_message(chat_id=owner_id, text=owner_msg)
                      except Exception as e:
                           logger.error(f"Ошибка уведомления основателя {owner_id} об оплате картой: {e}")
                 logger.info(f"✅ Пользователь {user_id} подтвердил оплату картой {uah_amount}₴")

            # --- Обработка выбора конкретной криптовалюты пользователем ---
            elif action == 'crypto' and len(parts) > 3 and parts[3] == 'select':
                 if len(parts) < 7:
                      await query.answer("❌ Невірний формат запиту.", show_alert=True)
                      logger.warning(f"⚠️ Неверный формат callback '{data}' для выбора крипты от пользователя {user_id}")
                      return
                 try:
                      target_user_id = int(parts[4])
                      uah_amount_cents = int(parts[5])
                      uah_amount = uah_amount_cents / 100.0
                      usd_amount = convert_uah_to_usd(uah_amount)
                      pay_currency = parts[6]
                      currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == pay_currency), pay_currency)
                 except (ValueError, IndexError) as e:
                      await query.answer("❌ Помилка обробки даних.", show_alert=True)
                      logger.error(f"❌ Ошибка парсинга данных из callback '{data}' для выбора крипты {user_id}: {e}")
                      return

                 if user_id != target_user_id:
                      await query.answer("❌ Цей рахунок не для вас.", show_alert=True)
                      logger.warning(f"🚫 Пользователь {user_id} пытался выбрать крипту для счета {target_user_id}")
                      return

                 # --- Создаем инвойс NOWPayments ---
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
                          "order_id": f"payout_user_{target_user_id}_{int(time.time())}",
                          "order_description": f"Оплата користувачем {target_user_id}"
                      }
                      logger.info(f"Создание инвойса NOWPayments для пользователя {target_user_id}: {payload}")
                      response = requests.post("https://api.nowpayments.io/v1/invoice", json=payload, headers=headers)
                      logger.info(f"Ответ NOWPayments для пользователя {target_user_id}: {response.status_code}")
                      response.raise_for_status()
                      invoice = response.json()
                      pay_url = invoice.get("invoice_url", "Помилка отримання посилання")
                      invoice_id = invoice.get("invoice_id", "Невідомий ID рахунку")

                      keyboard = [
                          [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                          [InlineKeyboardButton("🔄 Перевірити статус", callback_data=f'payout_user_crypto_check_{target_user_id}_{invoice_id}')],
                          [InlineKeyboardButton("⬅️ Назад", callback_data='payout_user_cancel')],
                          [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                      ]
                      reply_markup = InlineKeyboardMarkup(keyboard)
                      await query.edit_message_text(
                          f"🪙 Посилання для оплати {uah_amount}₴ ({usd_amount}$) в {currency_name}:\n"
                          f"{pay_url}\n"
                          f"ID рахунку: `{invoice_id}`\n"
                          f"Будь ласка, здійсніть оплату та перевірте статус.",
                          parse_mode='Markdown',
                          reply_markup=reply_markup
                      )
                      logger.info(f"🪙 Инвойс {invoice_id} создан и отправлен пользователю {target_user_id}")

                      # --- Уведомляем ОСНОВАТЕЛЕЙ ---
                      owner_msg = f"🪙 Користувач {user_id} обрав оплату {uah_amount}₴ ({usd_amount}$) в {currency_name}. Інвойс: {invoice_id}"
                      for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                           try:
                                await context.bot.send_message(chat_id=owner_id, text=owner_msg)
                           except Exception as e:
                                logger.error(f"Ошибка уведомления основателя {owner_id} о выборе крипты: {e}")

                 except requests.exceptions.RequestException as e:
                      error_msg = f"❌ Помилка з'єднання з сервісом оплати: {e}"
                      await query.edit_message_text(error_msg)
                      logger.error(f"Ошибка сети NOWPayments для пользователя {target_user_id}: {e}")
                 except Exception as e:
                      error_msg = f"❌ Помилка створення посилання для оплати: {e}"
                      await query.edit_message_text(error_msg)
                      logger.error(f"Ошибка создания инвойса NOWPayments для пользователя {target_user_id}: {e}")

            # --- Обработка проверки статуса крипты пользователем ---
            elif action == 'crypto' and len(parts) > 3 and parts[3] == 'check':
                 if len(parts) < 5:
                      await query.answer("❌ Невірний формат запиту.", show_alert=True)
                      logger.warning(f"⚠️ Неверный формат callback '{data}' для проверки статуса крипты от пользователя {user_id}")
                      return
                 try:
                      target_user_id = int(parts[4])
                      invoice_id = parts[5]
                 except (ValueError, IndexError) as e:
                      await query.answer("❌ Помилка обробки даних.", show_alert=True)
                      logger.error(f"❌ Ошибка парсинга данных из callback '{data}' для проверки статуса крипты {user_id}: {e}")
                      return

                 if user_id != target_user_id:
                      await query.answer("❌ Цей рахунок не для вас.", show_alert=True)
                      logger.warning(f"🚫 Пользователь {user_id} пытался проверить статус счета {target_user_id}")
                      return

                 # --- Проверяем статус инвойса NOWPayments ---
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
                           await query.edit_message_text("✅ Оплата криптовалютою успішно пройшла! Дякуємо.")
                           # --- Уведомляем ОСНОВАТЕЛЕЙ ---
                           owner_msg = f"✅ Оплата криптовалютою рахунку {invoice_id} користувачем {user_id} успішно завершена!"
                           for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                                try:
                                     await context.bot.send_message(chat_id=owner_id, text=owner_msg)
                                except Exception as e:
                                     logger.error(f"Ошибка уведомления основателя {owner_id} об успешной оплате криптой: {e}")
                           logger.info(f"✅ Оплата инвойса {invoice_id} успешно завершена пользователем {user_id}")

                      elif payment_status in ['waiting', 'confirming', 'confirmed']:
                           # Обновляем кнопки для пользователя
                           # (В реальном приложении можно получить pay_url из контекста или БД)
                           keyboard = [
                               [InlineKeyboardButton("🔄 Перевірити ще раз", callback_data=f'payout_user_crypto_check_{target_user_id}_{invoice_id}')],
                               [InlineKeyboardButton("⬅️ Назад", callback_data='payout_user_cancel')],
                               [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                           ]
                           reply_markup = InlineKeyboardMarkup(keyboard)
                           status_msg = f"⏳ Статус оплати: `{payment_status}`. Будь ласка, зачекайте або перевірте ще раз."
                           await query.edit_message_text(status_msg, parse_mode='Markdown', reply_markup=reply_markup)
                           logger.info(f"⏳ Статус оплаты инвойса {invoice_id}: {payment_status} (проверка пользователем {user_id})")

                      else: # cancelled, expired, etc.
                           fail_msg = f"❌ Оплата не пройшла або була скасована. Статус: `{payment_status}`."
                           await query.edit_message_text(fail_msg, parse_mode='Markdown')
                           # --- Уведомляем ОСНОВАТЕЛЕЙ ---
                           owner_msg = f"❌ Оплата криптовалютою рахунку {invoice_id} користувачем {user_id} не вдалася. Статус: {payment_status}"
                           for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                                try:
                                     await context.bot.send_message(chat_id=owner_id, text=owner_msg)
                                except Exception as e:
                                     logger.error(f"Ошибка уведомления основателя {owner_id} о неудачной оплате криптой: {e}")
                           logger.info(f"❌ Оплата инвойса {invoice_id} не удалась или отменена. Статус: {payment_status} (проверка пользователем {user_id})")

                 except Exception as e:
                      error_msg = f"❌ Помилка перевірки статусу оплати: {e}"
                      await query.edit_message_text(error_msg)
                      logger.error(f"Ошибка проверки статуса NOWPayments для инвойса {invoice_id} пользователя {user_id}: {e}")

            # --- Обработка отмены пользователем ---
            elif action == 'cancel':
                 await query.edit_message_text("❌ Оплата скасована.")
                 logger.info(f"❌ Пользователь {user_id} отменил оплату.")

            else:
                 logger.warning(f"⚠️ Неизвестный payout_user action: {action} в callback '{data}' от пользователя {user_id}")
                 await query.answer("❌ Невідома дія.", show_alert=True)

    # - Конец добавленных методов для оплаты -

    # --- Исправленный обработчик /stats ---
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статистику бота"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            logger.info(f"🚫 /stats: Доступ запрещен для пользователя {user_id}")
            # Можно ничего не отвечать или отправить сообщение об ошибке
            # await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return

        logger.info(f"📊 /stats: Запрошена статистика основателем {user_id}")
        total_users = get_total_users_count()
        stats_text = f"""📊 Статистика бота:
👥 Всього користувачів: {total_users}
🛒 Всього замовлень: {bot_statistics['total_orders']}
❓ Всього запитань: {bot_statistics['total_questions']}
⏰ Останнє скидання: {bot_statistics['last_reset']}"""
        await update.message.reply_text(stats_text)

    # --- Конец исправленного обработчика /stats ---

    async def continue_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /dialog для основателей"""
        # Логика продолжения диалога
        pass # Заглушка, реализация не предоставлена

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки (основное меню, заказы и т.д.)"""
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # --- Основная логика кнопок (категории, товары, вопрос и т.д.) ---
        # Главное меню
        if query.data == 'order':
            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("📦 Оберіть тип товару:", reply_markup=reply_markup)

        # Кнопка "Назад" в главное меню
        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Головне меню:", reply_markup=reply_markup)

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
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("💳 Оберіть підписку:", reply_markup=reply_markup)

        # Меню Цифрових товарів
        elif query.data == 'order_digital':
            keyboard = [
                [InlineKeyboardButton("🎮 Discord Прикраси", callback_data='category_discord_decor')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=reply_markup)

        # --- Добавлено: Кнопка "Запитання" из меню ---
        elif query.data == 'question':
            # Проверяем активные диалоги
            if user_id in active_conversations:
                await query.answer(
                    "❗ У вас вже є активний діалог.\n"
                    "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop.",
                    show_alert=True
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

            await query.edit_message_text(
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n"
                "Щоб завершити цей діалог пізніше, використайте команду /stop."
            )
        # --- Конец добавленного ---

        # ... (остальная логика button_handler для категорий товаров и т.д. остается как в вашем оригинальном main.txt) ...
        else:
             # Заглушка для необработанных callback'ов, чтобы не было ошибок
             logger.info(f"ℹ️ Необработанный callback: {query.data}")


    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text.strip()

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id not in active_conversations:
            # Если нет активного диалога, можно предложить начать новый или проигнорировать
            # В данном случае просто игнорируем
            return

        conversation = active_conversations[user_id]
        conversation_type = conversation['type']
        assigned_owner = conversation.get('assigned_owner')

        # Сохраняем последнее сообщение
        conversation['last_message'] = message_text
        save_active_conversation(user_id, conversation_type, assigned_owner, message_text)
        save_message(user_id, message_text, True) # True означает, что сообщение от пользователя

        # Логика обработки сообщений в зависимости от типа диалога
        if conversation_type == 'order':
            # Логика обработки заказа
            # Пересылаем сообщение основателю(ям)
            # В реальной реализации нужно определить, какому основателю отправлять
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                try:
                    # Отправляем сообщение основателю
                    await context.bot.send_message(
                        chat_id=owner_id,
                        text=f"📦 Нове замовлення від користувача {user.first_name} (@{user.username or 'не вказано'}) (ID: {user_id}):\n{message_text}"
                    )
                    # Назначаем этого основателя, если диалог еще не назначен
                    if not assigned_owner:
                        conversation['assigned_owner'] = owner_id
                        owner_client_map[owner_id] = user_id
                        save_active_conversation(user_id, conversation_type, owner_id, message_text)
                        # Отправляем уведомление основателю о назначении
                        await context.bot.send_message(
                            chat_id=owner_id,
                            text=f"📌 Вам призначено діалог з користувачем {user.first_name} (ID: {user_id})."
                        )
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения основателю {owner_id}: {e}")

        elif conversation_type == 'question':
            # Логика обработки вопроса
            # Пересылаем сообщение основателю(ям)
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                try:
                    # Отправляем сообщение основателю
                    await context.bot.send_message(
                        chat_id=owner_id,
                        text=f"❓ Нове запитання від користувача {user.first_name} (@{user.username or 'не вказано'}) (ID: {user_id}):\n{message_text}"
                    )
                    # Назначаем этого основателя, если диалог еще не назначен
                    if not assigned_owner:
                        conversation['assigned_owner'] = owner_id
                        owner_client_map[owner_id] = user_id
                        save_active_conversation(user_id, conversation_type, owner_id, message_text)
                        # Отправляем уведомление основателю о назначении
                        await context.bot.send_message(
                            chat_id=owner_id,
                            text=f"📌 Вам призначено діалог з користувачем {user.first_name} (ID: {user_id})."
                        )
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения основателю {owner_id}: {e}")

        elif conversation_type == 'manual':
            # Логика обработки ручного диалога (например, после команды /dialog)
            if assigned_owner:
                try:
                    # Отправляем сообщение назначеному основателю
                    await context.bot.send_message(
                        chat_id=assigned_owner,
                        text=f"💬 Повідомлення від користувача {user.first_name} (ID: {user_id}):\n{message_text}"
                    )
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения основателю {assigned_owner}: {e}")
            else:
                # Если диалог не назначен, уведомляем пользователя
                await update.message.reply_text("❌ Ваш діалог ще не призначено основателю. Зачекайте.")

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик документов (для заказов из файлов)"""
        # ... (оставьте существующую логику /handle_document без изменений) ...
        await update.message.reply_text("❌ Логика обработки документов не реализована в этом примере.")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(msg="Exception while handling an update:", exc_info=context.error)

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
            max_connections=100,
            allowed_updates=["message", "callback_query", "chat_member"]
        )
        logger.info(f"🌐 Webhook установлен на {WEBHOOK_URL}/{BOT_TOKEN}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка настройки webhook: {e}")
        return False

async def start_bot():
    """Асинхронный запуск бота"""
    global bot_running
    try:
        # Инициализация БД
        init_db()
        # Инициализация приложения
        await bot_instance.initialize()
        # Запуск в зависимости от режима
        if USE_POLLING:
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

# --- Flask handlers ---
@flask_app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'bot_running': bot_running}), 200

@flask_app.route('/stats', methods=['GET'])
def get_stats():
    return jsonify({'stats': bot_statistics}), 200

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
            update = Update.de_json(json_data, bot_instance.application.bot)
            # asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), telegram_app.loop)
            # Используем loop.call_soon_threadsafe для лучшей совместимости
            bot_instance.loop.call_soon_threadsafe(
                asyncio.create_task,
                bot_instance.application.process_update(update)
            )
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"❌ Ошибка обработки webhook: {e}")
        return jsonify({'error': str(e)}), 500

# --- Конец Flask handlers ---

def main():
    # Задержка для Render.com, чтобы избежать конфликтов
    if os.environ.get('RENDER'):
        logger.info("⏳ Ожидаем 10 секунд для предотвращения конфликтов...")
        time.sleep(10)

    # Запускаем автосохранение в отдельном потоке
    auto_save_thread = threading.Thread(target=auto_save_loop, daemon=True)
    auto_save_thread.start()

    # Инициализация БД
    init_db()

    # Запуск Flask сервера в отдельном потоке
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
