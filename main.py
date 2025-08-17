# main.py (обновленный с интеграцией /payout)
import logging
import os
import asyncio
import threading
import time
import json
import re
from datetime import datetime
from telegram import BotCommandScopeDefault
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg
from psycopg.rows import dict_row
import io
from urllib.parse import unquote
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
        self.application.add_handler(CommandHandler("dialog", self.continue_dialog_command))

        # Обработчики callback кнопок
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        # - Добавлено: Обработчик callback кнопок оплаты -
        self.application.add_handler(CallbackQueryHandler(self.payment_callback_handler, pattern='^(pay_|payout_|check_payment_status|manual_payment_confirmed|payout_manual_payment_confirmed|back_to_|payout_cancel)'))
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
        user_commands = [BotCommandScopeDefault()]

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
        pass # Заглушка, замените на реальную логику из вашего файла

    async def payout_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /payout для основателей"""
        user = update.effective_user
        user_id = user.id

        # Проверяем, является ли пользователь основателем
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return

        # Проверяем аргументы команды
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Неправильний формат команди.\n"
                "Використовуйте: `/payout <user_id> <amount_in_uah>`\n"
                "Наприклад: `/payout 123456789 500`",
                parse_mode='Markdown'
            )
            return

        try:
            # --- Исправлено: user_id -> target_user_id ---
            target_user_id = int(context.args[0])
            uah_amount = float(context.args[1])
            # ---
            if uah_amount <= 0:
                raise ValueError("Сума повинна бути більше нуля.")
        except ValueError as e:
            await update.message.reply_text(f"❌ Неправильний формат ID користувача або суми: {e}")
            return

        # Конвертируем сумму в USD
        usd_amount = convert_uah_to_usd(uah_amount)
        if usd_amount <= 0:
            await update.message.reply_text("❌ Сума в USD занадто мала для створення рахунку.")
            return

        # Сохраняем данные в контексте для последующей обработки
        # --- Исправлено: сохраняем target_user_id ---
        context.user_data['payout_target_user_id'] = target_user_id
        # ---
        context.user_data['payout_amount_uah'] = uah_amount
        context.user_data['payout_amount_usd'] = usd_amount

        # Предлагаем выбрать метод оплаты
        keyboard = [
            [InlineKeyboardButton("💳 Оплата карткою", callback_data='payout_card')],
            [InlineKeyboardButton("🪙 Криптовалюта", callback_data='payout_crypto')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
            [InlineKeyboardButton("❓ Запитання", callback_data='question')] # Эта кнопка работает, так как обрабатывается button_handler
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"💳 Створення рахунку на {uah_amount}₴ ({usd_amount}$) для користувача `{target_user_id}`.\n"
            f"Оберіть метод оплати:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def payment_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок, связанных с оплатой"""
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id # ID того, кто нажал кнопку (основатель)
        data = query.data

        # --- Логика для /pay ---
        # ... (оставьте существующую логику pay_... без изменений) ...
        if data.startswith('pay_'):
             # ... (оставьте существующую логику pay_... без изменений) ...
             pass # Заглушка, замените на реальную логику из вашего файла


        # --- Логика для /payout ---
        elif data.startswith('payout_'):
            logger.info(f"Обработка payout callback: {data} от пользователя {user_id}")
            # Проверяем, является ли пользователь основателем
            if user_id not in [OWNER_ID_1, OWNER_ID_2]:
                await query.answer("❌ У вас немає доступу.", show_alert=True)
                logger.warning(f"Несанкционированный доступ к payout для пользователя {user_id}")
                return

            # Проверяем, есть ли данные в контексте
            # --- Исправлено: получаем target_user_id ---
            target_user_id = context.user_data.get('payout_target_user_id')
            # ---
            uah_amount = context.user_data.get('payout_amount_uah')
            usd_amount = context.user_data.get('payout_amount_usd')

            if not target_user_id or not uah_amount or not usd_amount:
                error_msg = "❌ Помилка: інформація про платіж втрачена. Спробуйте ще раз."
                await query.edit_message_text(error_msg)
                logger.error(f"Данные payout потеряны для пользователя {user_id}")
                return

            # - Отмена -
            if data == 'payout_cancel':
                await query.edit_message_text("❌ Створення рахунку скасовано.")
                # Очищаем контекст
                context.user_data.pop('payout_target_user_id', None)
                context.user_data.pop('payout_amount_uah', None)
                context.user_data.pop('payout_amount_usd', None)
                context.user_data.pop('payout_nowpayments_invoice_id', None)
                logger.info(f"Создание payout отменено пользователем {user_id}")
                return

            # - Оплата карткой -
            elif data == 'payout_card':
                # Создаем временную ссылку или просто сообщаем пользователю
                keyboard = [
                    [InlineKeyboardButton("✅ Оплачено", callback_data='payout_manual_payment_confirmed')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"💳 Оплата {uah_amount}₴ ({usd_amount}$) карткою.\n"
                    f"(Тут будуть реквізити для оплати)\n"
                    f"Після оплати натисніть кнопку '✅ Оплачено'.",
                    reply_markup=reply_markup
                )
                logger.info(f"Показаны реквизиты для карты payout пользователю {user_id}")

            # - Оплата криптовалютою -
            elif data == 'payout_crypto':
                # Отображаем список доступных криптовалют
                keyboard = []
                for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                    keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'payout_crypto_{currency_code}')])
                keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')])
                keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"🪙 Оберіть криптовалюту для створення рахунку на {uah_amount}₴ ({usd_amount}$):",
                    reply_markup=reply_markup
                )
                logger.info(f"Показан выбор криптовалюты для payout пользователю {user_id}")

            # - Выбор конкретной криптовалюты -
            elif data.startswith('payout_crypto_'):
                pay_currency = data.split('_')[2]  # e.g., 'usdttrc20'
                # Находим название валюты
                currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == pay_currency), pay_currency)
                try:
                    # Создаем счет в NOWPayments
                    headers = {
                        'Authorization': f'Bearer {NOWPAYMENTS_API_KEY}', # Используем Bearer токен
                        'Content-Type': 'application/json'
                    }
                    payload = {
                        "price_amount": usd_amount,
                        "price_currency": "usd",
                        "pay_currency": pay_currency,
                        "ipn_callback_url": f"{WEBHOOK_URL}/nowpayments_ipn",  # URL для уведомлений (если используется webhook)
                        "order_id": f"payout_{user_id}_{target_user_id}_{int(time.time())}",  # Уникальный ID
                        "order_description": f"Виставлення рахунку основателем {user_id} для користувача {target_user_id}"
                    }
                    logger.info(f"Создание инвойса NOWPayments для payout: {payload}")
                    response = requests.post("https://api.nowpayments.io/v1/invoice", json=payload, headers=headers)
                    logger.info(f"Ответ NOWPayments: {response.status_code}")
                    response.raise_for_status()
                    invoice = response.json()
                    pay_url = invoice.get("invoice_url", "Помилка отримання посилання")
                    invoice_id = invoice.get("invoice_id", "Невідомий ID рахунку")

                    # Сохраняем ID инвойса
                    context.user_data['payout_nowpayments_invoice_id'] = invoice_id

                    keyboard = [
                        [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                        [InlineKeyboardButton("🔄 Перевірити статус", callback_data='payout_check_payment_status')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    # --- Исправлено: Отправляем ссылку ЦЕЛЕВОМУ пользователю ---
                    try:
                        await context.bot.send_message(
                            chat_id=target_user_id, # <-- Отправляем target_user_id
                            text=f"🪙 Вам виставлено рахунок на {uah_amount}₴ ({usd_amount}$) в {currency_name}:\n"
                                 f"{pay_url}\n"
                                 f"ID рахунку: `{invoice_id}`\n"
                                 f"Будь ласка, здійсніть оплату.",
                            parse_mode='Markdown'
                        )
                        confirmation_msg = f"✅ Рахунок створено та надіслано користувачу `{target_user_id}`.\nПосилання: {pay_url}"
                        await query.edit_message_text(confirmation_msg, parse_mode='Markdown')
                        logger.info(f"Инвойс {invoice_id} создан и отправлен пользователю {target_user_id} по запросу основателя {user_id}")
                    except Exception as e:
                        error_send_msg = f"❌ Рахунок створено, але не вдалося надіслати користувачу {target_user_id}: {e}\nПосилання: {pay_url}"
                        await query.edit_message_text(error_send_msg)
                        logger.error(f"Ошибка отправки инвойса {invoice_id} пользователю {target_user_id}: {e}")
                    # ---
                except requests.exceptions.RequestException as e:
                    error_msg = f"❌ Помилка з'єднання з сервісом оплати: {e}"
                    await query.edit_message_text(error_msg)
                    logger.error(f"Ошибка сети NOWPayments для payout: {e}")
                except Exception as e:
                    error_msg = f"❌ Помилка створення посилання для оплати: {e}"
                    await query.edit_message_text(error_msg)
                    logger.error(f"Ошибка создания инвойса NOWPayments для payout: {e}")

            # - Ручне підтвердження оплати -
            elif data == 'payout_manual_payment_confirmed':
                # Здесь можно добавить логику проверки оплаты вручную или запись в БД
                await query.edit_message_text(
                    "✅ Оплата карткою підтверджена вручну.\n"
                    "Інформуйте користувача про подальші дії."
                )
                # Очищаем контекст после завершения
                context.user_data.pop('payout_target_user_id', None)
                context.user_data.pop('payout_amount_uah', None)
                context.user_data.pop('payout_amount_usd', None)
                context.user_data.pop('payout_nowpayments_invoice_id', None)
                logger.info(f"Ручное подтверждение оплаты payout для пользователя {user_id}")

            # - Перевірка статуса оплати -
            elif data == 'payout_check_payment_status':
                invoice_id = context.user_data.get('payout_nowpayments_invoice_id')
                if not invoice_id:
                    await query.edit_message_text("❌ Не знайдено ID рахунку для перевірки.")
                    logger.warning(f"ID инвойса не найден для проверки статуса payout пользователем {user_id}")
                    return
                try:
                    headers = {
                        'Authorization': f'Bearer {NOWPAYMENTS_API_KEY}', # Используем Bearer токен
                        'Content-Type': 'application/json'
                    }
                    response = requests.get(f"https://api.nowpayments.io/v1/invoice/{invoice_id}", headers=headers)
                    response.raise_for_status()
                    status_data = response.json()
                    payment_status = status_data.get('payment_status', 'unknown')

                    if payment_status == 'finished':
                        success_msg = "✅ Оплата успішно пройшла!\nІнформуйте користувача про подальші дії."
                        await query.edit_message_text(success_msg)
                        # Очищаем контекст после успешной оплаты
                        context.user_data.pop('payout_target_user_id', None)
                        context.user_data.pop('payout_amount_uah', None)
                        context.user_data.pop('payout_amount_usd', None)
                        context.user_data.pop('payout_nowpayments_invoice_id', None)
                        logger.info(f"Оплата инвойса {invoice_id} успешно завершена по проверке статуса пользователем {user_id}")
                    elif payment_status in ['waiting', 'confirming', 'confirmed']:
                        keyboard = [
                            [InlineKeyboardButton("🔄 Перевірити ще раз", callback_data='payout_check_payment_status')],
                            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        status_msg = f"⏳ Статус оплати: `{payment_status}`. Будь ласка, зачекайте або перевірте ще раз."
                        await query.edit_message_text(status_msg, parse_mode='Markdown', reply_markup=reply_markup)
                        logger.info(f"Статус оплаты инвойса {invoice_id}: {payment_status} (проверка пользователем {user_id})")
                    else:  # cancelled, expired, partially_paid, etc.
                        keyboard = [
                            [InlineKeyboardButton("💳 Інший метод оплати", callback_data='payout_cancel')], # Просто отмена, можно добавить повторный выбор
                            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        fail_msg = f"❌ Оплата не пройшла або була скасована. Статус: `{payment_status}`."
                        await query.edit_message_text(fail_msg, parse_mode='Markdown', reply_markup=reply_markup)
                        logger.info(f"Оплата инвойса {invoice_id} не удалась или отменена. Статус: {payment_status} (проверка пользователем {user_id})")
                except Exception as e:
                    error_msg = f"❌ Помилка перевірки статусу оплати: {e}"
                    await query.edit_message_text(error_msg)
                    logger.error(f"Ошибка проверки статуса NOWPayments для payout инвойса {invoice_id}: {e}")
                    
        # --- Логика для /payout ---
        elif data.startswith('payout_'):
            # Проверяем, является ли пользователь основателем
            if user_id not in [OWNER_ID_1, OWNER_ID_2]:
                await query.answer("❌ У вас немає доступу.", show_alert=True)
                return

            # Проверяем, есть ли данные в контексте
            target_user_id = context.user_data.get('payout_target_user_id')
            uah_amount = context.user_data.get('payout_amount_uah')
            usd_amount = context.user_data.get('payout_amount_usd')

            if not target_user_id or not uah_amount or not usd_amount:
                await query.edit_message_text("❌ Помилка: інформація про платіж втрачена. Спробуйте ще раз.")
                return

            # - Отмена -
            if data == 'payout_cancel':
                await query.edit_message_text("❌ Створення рахунку скасовано.")
                # Очищаем контекст
                context.user_data.pop('payout_target_user_id', None)
                context.user_data.pop('payout_amount_uah', None)
                context.user_data.pop('payout_amount_usd', None)
                context.user_data.pop('payout_nowpayments_invoice_id', None)
                return

            # - Оплата карткой -
            elif data == 'payout_card':
                # Создаем временную ссылку или просто сообщаем пользователю
                keyboard = [
                    [InlineKeyboardButton("✅ Оплачено", callback_data='payout_manual_payment_confirmed')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"💳 Оплата {uah_amount}₴ ({usd_amount}$) карткою.\n"
                    f"(Тут будуть реквізити для оплати)\n"
                    f"Після оплати натисніть кнопку '✅ Оплачено'.",
                    reply_markup=reply_markup
                )
                # Можно установить флаг ожидания подтверждения, если нужно

            # - Оплата криптовалютою -
            elif data == 'payout_crypto':
                # Отображаем список доступных криптовалют
                keyboard = []
                for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                    keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'payout_crypto_{currency_code}')])
                keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')])
                keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"🪙 Оберіть криптовалюту для створення рахунку на {uah_amount}₴ ({usd_amount}$):",
                    reply_markup=reply_markup
                )

            # - Выбор конкретной криптовалюты -
            elif data.startswith('payout_crypto_'):
                pay_currency = data.split('_')[2]  # e.g., 'usdttrc20'
                # Находим название валюты
                currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == pay_currency), pay_currency)
                try:
                    # Создаем счет в NOWPayments
                    headers = {
                        'Authorization': f'Bearer {NOWPAYMENTS_API_KEY}', # Используем Bearer токен
                        'Content-Type': 'application/json'
                    }
                    payload = {
                        "price_amount": usd_amount,
                        "price_currency": "usd",
                        "pay_currency": pay_currency,
                        "ipn_callback_url": f"{WEBHOOK_URL}/nowpayments_ipn",  # URL для уведомлений (если используется webhook)
                        "order_id": f"payout_{user_id}_{target_user_id}_{int(time.time())}",  # Уникальный ID
                        "order_description": f"Виставлення рахунку основателем {user_id} для користувача {target_user_id}"
                    }
                    logger.info(f"Создание инвойса NOWPayments: {payload}")
                    response = requests.post("https://api.nowpayments.io/v1/invoice", json=payload, headers=headers)
                    logger.info(f"Ответ NOWPayments: {response.status_code}, {response.text}")
                    response.raise_for_status()
                    invoice = response.json()
                    pay_url = invoice.get("invoice_url", "Помилка отримання посилання")
                    invoice_id = invoice.get("invoice_id", "Невідомий ID рахунку")

                    # Сохраняем ID инвойса
                    context.user_data['payout_nowpayments_invoice_id'] = invoice_id

                    keyboard = [
                        [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                        [InlineKeyboardButton("🔄 Перевірити статус", callback_data='payout_check_payment_status')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    # Отправляем ссылку пользователю
                    try:
                        await context.bot.send_message(
                            chat_id=target_user_id,
                            text=f"🪙 Вам виставлено рахунок на {uah_amount}₴ ({usd_amount}$) в {currency_name}:\n"
                                 f"{pay_url}\n"
                                 f"ID рахунку: `{invoice_id}`\n"
                                 f"Будь ласка, здійсніть оплату.",
                            parse_mode='Markdown'
                        )
                        await query.edit_message_text(
                            f"✅ Рахунок створено та надіслано користувачу `{target_user_id}`.\n"
                            f"Посилання: {pay_url}",
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Помилка надсилання рахунку користувачу {target_user_id}: {e}")
                        await query.edit_message_text(
                            f"❌ Рахунок створено, але не вдалося надіслати користувачу: {e}\n"
                            f"Посилання: {pay_url}"
                        )
                except requests.exceptions.RequestException as e:
                    logger.error(f"Помилка мережі NOWPayments для payout: {e}")
                    await query.edit_message_text(f"❌ Помилка з'єднання з сервісом оплати: {e}")
                except Exception as e:
                    logger.error(f"Помилка створення інвойсу NOWPayments для payout: {e}")
                    await query.edit_message_text(f"❌ Помилка створення посилання для оплати: {e}")

            # - Ручне підтвердження оплати -
            elif data == 'payout_manual_payment_confirmed':
                # Здесь можно добавить логику проверки оплаты вручную или запись в БД
                await query.edit_message_text(
                    "✅ Оплата карткою підтверджена вручну.\n"
                    "Інформуйте користувача про подальші дії."
                )
                # Очищаем контекст после завершения
                context.user_data.pop('payout_target_user_id', None)
                context.user_data.pop('payout_amount_uah', None)
                context.user_data.pop('payout_amount_usd', None)
                context.user_data.pop('payout_nowpayments_invoice_id', None)

            # - Перевірка статуса оплати -
            elif data == 'payout_check_payment_status':
                invoice_id = context.user_data.get('payout_nowpayments_invoice_id')
                if not invoice_id:
                    await query.edit_message_text("❌ Не знайдено ID рахунку для перевірки.")
                    return
                try:
                    headers = {
                        'Authorization': f'Bearer {NOWPAYMENTS_API_KEY}', # Используем Bearer токен
                        'Content-Type': 'application/json'
                    }
                    response = requests.get(f"https://api.nowpayments.io/v1/invoice/{invoice_id}", headers=headers)
                    response.raise_for_status()
                    status_data = response.json()
                    payment_status = status_data.get('payment_status', 'unknown')

                    if payment_status == 'finished':
                        await query.edit_message_text(
                            "✅ Оплата успішно пройшла!\n"
                            "Інформуйте користувача про подальші дії."
                        )
                        # Очищаем контекст после успешной оплаты
                        context.user_data.pop('payout_target_user_id', None)
                        context.user_data.pop('payout_amount_uah', None)
                        context.user_data.pop('payout_amount_usd', None)
                        context.user_data.pop('payout_nowpayments_invoice_id', None)
                    elif payment_status in ['waiting', 'confirming', 'confirmed']:
                        keyboard = [
                            [InlineKeyboardButton("🔄 Перевірити ще раз", callback_data='payout_check_payment_status')],
                            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await query.edit_message_text(
                            f"⏳ Статус оплати: `{payment_status}`. Будь ласка, зачекайте або перевірте ще раз.",
                            parse_mode='Markdown',
                            reply_markup=reply_markup
                        )
                    else:  # cancelled, expired, partially_paid, etc.
                        keyboard = [
                            [InlineKeyboardButton("💳 Інший метод оплати", callback_data='payout_cancel')], # Просто отмена, можно добавить повторный выбор
                            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await query.edit_message_text(
                            f"❌ Оплата не пройшла або була скасована. Статус: `{payment_status}`.",
                            parse_mode='Markdown',
                            reply_markup=reply_markup
                        )
                except Exception as e:
                    logger.error(f"Помилка перевірки статусу NOWPayments для payout: {e}")
                    await query.edit_message_text(f"❌ Помилка перевірки статусу оплати: {e}")

    async def request_account_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрашивает у пользователя логин и пароль от аккаунта"""
        query = update.callback_query
        user_id = query.from_user.id if update.callback_query else update.effective_user.id
        order_details = context.user_data.get('order_details_for_payment', '')

        # Определяем тип заказа (можно уточнить логику)
        is_digital = 'digital_order' in active_conversations.get(user_id, {}).get('type', '')
        item_type = "акаунту Discord" if is_digital else "акаунту"

        # Сообщаем пользователю и переходим в состояние ожидания данных
        message_text = (
            f"✅ Оплата пройшла успішно!\n"
            f"Будь ласка, надішліть мені логін та пароль від {item_type}.\n"
            f"Наприклад: `login:password` або `login password`"
        )
        if update.callback_query:
            await query.edit_message_text(message_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(message_text, parse_mode='Markdown')
        context.user_data['awaiting_account_data'] = True
        # Сохраняем детали заказа для передачи основателям позже
        context.user_data['account_details_order'] = order_details

    async def handle_account_data_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает сообщение с логином и паролем"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text.strip()

        # Проверяем, ожидаем ли мы данные аккаунта
        if not context.user_data.get('awaiting_account_data'):
            return

        # Проверяем формат
        if ':' in message_text:
            parts = message_text.split(':', 1)
        elif ' ' in message_text:
            parts = message_text.split(' ', 1)
        else:
            await update.message.reply_text("❌ Неправильний формат. Використовуйте `login:password` або `login password`.")
            return

        login = parts[0]
        password = parts[1]
        order_details = context.user_data.get('account_details_order', 'Невідомі деталі замовлення')

        account_info_message = (
            f"🔐 Нові дані акаунту від користувача {user.first_name} (@{user.username or 'не вказано'}) (ID: {user_id}):\n"
            f"Логін: `{login}`\n"
            f"Пароль: `{password}`\n"
            f"Деталі замовлення: {order_details}"
        )

        # Отправляем данные основателям
        success_count = 0
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(chat_id=owner_id, text=account_info_message, parse_mode='Markdown')
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

    # - Конец добавленных методов для оплаты -

    async def continue_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /dialog для основателей"""
        # Логика продолжения диалога
        pass # Заглушка, реализация не предоставлена

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
                [InlineKeyboardButton("🎮 Discord Прикраси", callback_data='category_discord_decor')], # Обновлено
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=reply_markup) # Обновлено

        # --- Меню Підписок ---
        # Меню ChatGPT
        elif query.data == 'category_chatgpt':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_1')],
                [InlineKeyboardButton("12 місяців - 6500 UAH", callback_data='chatgpt_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("💬 ChatGPT Plus:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Discord
        elif query.data == 'category_discord':
            keyboard = [
                [InlineKeyboardButton("Discord Nitro Basic 1м - 260 UAH", callback_data='discord_basic_1')],
                [InlineKeyboardButton("Discord Nitro Basic 12м - 2600 UAH", callback_data='discord_basic_12')],
                [InlineKeyboardButton("Discord Nitro Full 1м - 390 UAH", callback_data='discord_full_1')],
                [InlineKeyboardButton("Discord Nitro Full 12м - 3900 UAH", callback_data='discord_full_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎮 Discord Nitro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Duolingo
        elif query.data == 'category_duolingo':
            keyboard = [
                [InlineKeyboardButton("Duolingo Max 1м - 520 UAH", callback_data='duolingo_1')],
                [InlineKeyboardButton("Duolingo Max 12м - 5200 UAH", callback_data='duolingo_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎓 Duolingo Max:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Picsart
        elif query.data == 'category_picsart':
            keyboard = [
                [InlineKeyboardButton("Picsart AI Plus", callback_data='picsart_plus')],
                [InlineKeyboardButton("Picsart AI Pro", callback_data='picsart_pro')],
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
                [InlineKeyboardButton("1 місяць - 520 UAH", callback_data='picsart_pro_1')],
                [InlineKeyboardButton("12 місяців - 5200 UAH", callback_data='picsart_pro_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text("🎨 Picsart AI Pro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Canva
        elif query.data == 'category_canva':
            keyboard = [
                [InlineKeyboardButton("Canva Pro Individual", callback_data='canva_ind')],
                [InlineKeyboardButton("Canva Pro Family", callback_data='canva_fam')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📊 Оберіть варіант Canva Pro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Canva Individual
        elif query.data == 'canva_ind':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 520 UAH", callback_data='canva_ind_1')],
                [InlineKeyboardButton("12 місяців - 5200 UAH", callback_data='canva_ind_12')],
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
                [InlineKeyboardButton("Netflix Mobile 1м - 260 UAH", callback_data='netflix_mob_1')],
                [InlineKeyboardButton("Netflix Mobile 12м - 2600 UAH", callback_data='netflix_mob_12')],
                [InlineKeyboardButton("Netflix Basic 1м - 390 UAH", callback_data='netflix_bas_1')],
                [InlineKeyboardButton("Netflix Basic 12м - 3900 UAH", callback_data='netflix_bas_12')],
                [InlineKeyboardButton("Netflix Standard 1м - 520 UAH", callback_data='netflix_std_1')],
                [InlineKeyboardButton("Netflix Standard 12м - 5200 UAH", callback_data='netflix_std_12')],
                [InlineKeyboardButton("Netflix Premium 1м - 650 UAH", callback_data='netflix_pre_1')],
                [InlineKeyboardButton("Netflix Premium 12м - 6500 UAH", callback_data='netflix_pre_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📺 Оберіть підписку Netflix:", reply_markup=InlineKeyboardMarkup(keyboard))

        # --- Меню Цифрових товарів ---
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
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Оберіть прикрасу Discord (Без Nitro):", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # Подменю Discord Прикраси З Nitro
        elif query.data == 'discord_decor_with_nitro':
            keyboard = [
                [InlineKeyboardButton("5$ - 145 UAH", callback_data='discord_decor_zn_5')],
                [InlineKeyboardButton("7$ - 205 UAH", callback_data='discord_decor_zn_7')],
                [InlineKeyboardButton("8.5$ - 250 UAH", callback_data='discord_decor_zn_8_5')],
                [InlineKeyboardButton("9$ - 265 UAH", callback_data='discord_decor_zn_9')],
                [InlineKeyboardButton("14$ - 410 UAH", callback_data='discord_decor_zn_14')],
                [InlineKeyboardButton("22$ - 650 UAH", callback_data='discord_decor_zn_22')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Оберіть прикрасу Discord (З Nitro):", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # --- Обработка выбора конкретного товара ---
        # Пример для одного товара, остальные по аналогии
        elif query.data.startswith('chatgpt_') or \
             query.data.startswith('discord_') or \
             query.data.startswith('duolingo_') or \
             query.data.startswith('picsart_') or \
             query.data.startswith('canva_') or \
             query.data.startswith('netflix_') or \
             query.data.startswith('discord_decor_'):

            # Определяем тип заказа
            conversation_type = 'subscription_order' if not query.data.startswith('discord_decor_') else 'digital_order'

            # Извлекаем информацию из callback_data
            # Это упрощенный пример, можно сделать более сложный парсер
            item_map = {
                # ChatGPT
                'chatgpt_1': ("ChatGPT Plus", "1 місяць", 650),
                'chatgpt_12': ("ChatGPT Plus", "12 місяців", 6500),
                # Discord
                'discord_basic_1': ("Discord Nitro Basic", "1 місяць", 260),
                'discord_basic_12': ("Discord Nitro Basic", "12 місяців", 2600),
                'discord_full_1': ("Discord Nitro Full", "1 місяць", 390),
                'discord_full_12': ("Discord Nitro Full", "12 місяців", 3900),
                # Duolingo
                'duolingo_1': ("Duolingo Max", "1 місяць", 520),
                'duolingo_12': ("Duolingo Max", "12 місяців", 5200),
                # Picsart Plus
                'picsart_plus_1': ("Picsart AI Plus", "1 місяць", 400),
                'picsart_plus_12': ("Picsart AI Plus", "12 місяців", 4000),
                # Picsart Pro
                'picsart_pro_1': ("Picsart AI Pro", "1 місяць", 520),
                'picsart_pro_12': ("Picsart AI Pro", "12 місяців", 5200),
                # Canva Individual
                'canva_ind_1': ("Canva Pro Individual", "1 місяць", 520),
                'canva_ind_12': ("Canva Pro Individual", "12 місяців", 5200),
                # Canva Family
                'canva_fam_1': ("Canva Pro Family", "1 місяць", 650),
                'canva_fam_12': ("Canva Pro Family", "12 місяців", 6500),
                # Netflix
                'netflix_mob_1': ("Netflix Mobile", "1 місяць", 260),
                'netflix_mob_12': ("Netflix Mobile", "12 місяців", 2600),
                'netflix_bas_1': ("Netflix Basic", "1 місяць", 390),
                'netflix_bas_12': ("Netflix Basic", "12 місяців", 3900),
                'netflix_std_1': ("Netflix Standard", "1 місяць", 520),
                'netflix_std_12': ("Netflix Standard", "12 місяців", 5200),
                'netflix_pre_1': ("Netflix Premium", "1 місяць", 650),
                'netflix_pre_12': ("Netflix Premium", "12 місяців", 6500),
                # Discord Прикраси Без Nitro
                'discord_decor_bzn_6': ("Discord Прикраса (Без Nitro)", "6$", 180),
                'discord_decor_bzn_8': ("Discord Прикраса (Без Nitro)", "8$", 240),
                'discord_decor_bzn_9': ("Discord Прикраса (Без Nitro)", "9$", 265),
                'discord_decor_bzn_12': ("Discord Прикраса (Без Nitro)", "12$", 355),
                # Discord Прикраси З Nitro
                'discord_decor_zn_5': ("Discord Прикраса (З Nitro)", "5$", 145),
                'discord_decor_zn_7': ("Discord Прикраса (З Nitro)", "7$", 205),
                'discord_decor_zn_8_5': ("Discord Прикраса (З Nitro)", "8.5$", 250),
                'discord_decor_zn_9': ("Discord Прикраса (З Nitro)", "9$", 265),
                'discord_decor_zn_14': ("Discord Прикраса (З Nitro)", "14$", 410),
                'discord_decor_zn_22': ("Discord Прикраса (З Nitro)", "22$", 650),
            }

            if query.data in item_map:
                service_name, period, price_uah = item_map[query.data]
                order_text = f"📦 Замовлення:\n  • {service_name} ({period}) - {price_uah} UAH\n💳 Всього: {price_uah} UAH"

                # Проверяем активные диалоги
                if user_id in active_conversations:
                     await query.answer("❗ У вас вже є активний діалог.", show_alert=True)
                     return

                # Создаем запись о заказе
                active_conversations[user_id] = {
                    'type': conversation_type,
                    'user_info': user,
                    'assigned_owner': None,
                    'last_message': order_text,
                    'order_details': order_text
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
                await query.edit_message_text(confirmation_text.strip(), reply_markup=reply_markup)
            else:
                await query.edit_message_text("❌ Не вдалося обробити замовлення. Спробуйте ще раз.")

        # --- Обработка оплаты после выбора товара ---
        elif query.data == 'proceed_to_payment':
            if user_id not in active_conversations or 'order_details' not in active_conversations[user_id]:
                await query.edit_message_text("❌ Не знайдено активного замовлення.")
                return

            order_text = active_conversations[user_id]['order_details']

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
            context.user_data['order_details_for_payment'] = order_text

            # Предлагаем выбрать метод оплаты
            keyboard = [
                [InlineKeyboardButton("💳 Оплата карткою", callback_data='pay_card')],
                [InlineKeyboardButton("🪙 Криптовалюта", callback_data='pay_crypto')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                [InlineKeyboardButton("❓ Запитання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(f"💳 Оберіть метод оплати для суми {usd_amount}$:", reply_markup=reply_markup)

        # --- Добавлено: Кнопка "Запитання" ---
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

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text.strip()

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Если ожидаем данные аккаунта
        if context.user_data.get('awaiting_account_data'):
            await self.handle_account_data_message(update, context)
            return

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
        user = update.effective_user
        user_id = user.id
        document = update.message.document

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

        # Проверяем тип файла
        if document.mime_type != 'application/json':
            await update.message.reply_text("❌ Підтримуються тільки JSON файли.")
            return

        try:
            # Скачиваем файл
            file = await context.bot.get_file(document.file_id)
            file_bytes = await file.download_as_bytearray()
            # Декодируем JSON
            order_data = json.loads(file_bytes.decode('utf-8'))

            # Пример обработки JSON (адаптируйте под ваш формат)
            # Предположим, JSON имеет структуру: {"order_id": "...", "items": [{"service": "...", "plan": "...", "period": "...", "price_uah": ...}]}
            order_id = order_data.get('order_id', 'N/A')
            items = order_data.get('items', [])

            if not items:
                await update.message.reply_text("❌ JSON файл не містить товарів.")
                return

            order_lines = [f"📦 Замовлення #{order_id} (з файлу):"]
            total_uah = 0
            for item in items:
                service = item.get('service', 'Невідомо')
                plan = item.get('plan', 'Невідомо')
                period = item.get('period', 'Невідомо')
                price_uah = item.get('price_uah', 0)
                total_uah += price_uah
                order_lines.append(f"  • {service} {plan} ({period}) - {price_uah} UAH")

            order_lines.append(f"💳 Всього: {total_uah} UAH")
            order_text = "\n".join(order_lines)

            # Определяем тип заказа (простая логика)
            conversation_type = 'subscription_order' # или 'digital_order' в зависимости от содержимого

            # Создаем запись о заказе
            active_conversations[user_id] = {
                'type': conversation_type,
                'user_info': user,
                'assigned_owner': None,
                'last_message': order_text,
                'order_details': order_text
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
            confirmation_text = f"""✅ Ваше замовлення з файлу прийнято!
{order_text}
Будь ласка, оберіть дію 👇"""
            await update.message.reply_text(confirmation_text.strip(), reply_markup=reply_markup)

        except json.JSONDecodeError:
            await update.message.reply_text("❌ Неправильний формат JSON файлу.")
        except Exception as e:
            logger.error(f"❌ Ошибка обработки JSON файла: {e}")
            await update.message.reply_text(f"❌ Помилка обробки файлу: {e}")

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
        if json_data:
            update = Update.de_json(json_data, telegram_app.bot)
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

# pass # Заглушка, реализация не предоставлена
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
