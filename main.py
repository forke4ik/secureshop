# main.py (обновленный и исправленный)
import logging
import os
import asyncio
import threading
import time
import json
import re
from datetime import datetime
from typing import Dict, Any, Optional
import requests # Добавлено для интеграции NOWPayments

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict, TelegramError
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg
from psycopg.rows import dict_row

# --- Настройка логирования ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Глобальные переменные состояния ---
bot_running = False
bot_lock = threading.Lock()

# --- Конфигурация из переменных окружения ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
OWNER_ID_1 = int(os.getenv('OWNER_ID_1', 0)) # @HiGki2pYYY
OWNER_ID_2 = int(os.getenv('OWNER_ID_2', 0)) # @oc33t
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', f'https://your-app-name.onrender.com')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840)) # 14 минут
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'
DATABASE_URL = os.getenv('DATABASE_URL')
# --- Добавленные/обновленные переменные окружения ---
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY')
EXCHANGE_RATE_UAH_TO_USD = float(os.getenv('EXCHANGE_RATE_UAH_TO_USD', 41.26)) # Курс UAH к USD

# --- Путь к файлу с данными ---
STATS_FILE = "bot_stats.json"

# --- Оптимизация: буферизация запросов к БД ---
BUFFER_FLUSH_INTERVAL = 300 # 5 минут
message_buffer = []
buffer_lock = threading.Lock()

# --- Кэш пользователей ---
user_cache = set()

# --- Список доступных криптовалют для NOWPayments ---
# Проверьте актуальные коды на https://nowpayments.io
AVAILABLE_CURRENCIES = {
    "USDT (Solana)": "usdtsol",
    "USDT (TRC20)": "usdttrc20",
    "ETH": "eth",
    "USDT (Arbitrum)": "usdtarb",
    "USDT (Polygon)": "usdtmatic",
    "USDT (TON)": "usdtton",
    "AVAX (C-Chain)": "avax",
    "APTOS (APT)": "apt"
    # Добавьте другие валюты по необходимости
}

# --- Вспомогательные функции ---
def convert_uah_to_usd(uah_amount: float) -> float:
    """Конвертирует сумму из UAH в USD по установленному курсу."""
    if EXCHANGE_RATE_UAH_TO_USD <= 0:
        logger.error("Курс обмена UAH к USD не установлен или некорректен.")
        return 0.0
    return round(uah_amount / EXCHANGE_RATE_UAH_TO_USD, 2)

def get_uah_amount_from_order_text(order_text: str) -> float:
    """Извлекает сумму в UAH из текста заказа."""
    match = re.search(r'💳 Всього: (\d+(?:\.\d+)?) UAH', order_text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            logger.error(f"Ошибка преобразования суммы из строки: {match.group(1)}")
    logger.warning("Сумма в UAH не найдена в тексте заказа.")
    return 0.0

# --- Функции работы со статистикой (вне класса) ---
def load_stats() -> Dict[str, Any]:
    """Загружает статистику из файла"""
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                stats = json.load(f)
                # Убедимся, что дата в правильном формате
                if 'last_reset' in stats and isinstance(stats['last_reset'], str):
                    stats['last_reset'] = datetime.fromisoformat(stats['last_reset'])
                logger.info("📊 Статистика загружена из файла")
                return stats
        else:
            logger.info("🆕 Файл статистики не найден, создаём новый")
            return {
                "total_orders": 0,
                "total_questions": 0,
                "total_users": 0,
                "last_reset": datetime.now()
            }
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки статистики: {e}")
        return {
            "total_orders": 0,
            "total_questions": 0,
            "total_users": 0,
            "last_reset": datetime.now()
        }

# --- Класс бота ---
class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.init_db()
        # --- Исправление: загружаем статистику в __init__ ---
        self.bot_statistics = load_stats() # <-- ПРАВИЛЬНО: вызываем функцию, результат присваиваем атрибуту self
        # --- Конец исправления ---
        # Инициализация других атрибутов
        self.active_conversations = {} # {user_id: {...}}
        self.owner_client_map = {} # {owner_id: client_id}

    def setup_handlers(self):
        """Регистрация обработчиков команд и сообщений"""
        # --- Команды ---
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("order", self.order_command))
        self.application.add_handler(CommandHandler("question", self.question_command))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("stop", self.stop_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("history", self.history_command))
        self.application.add_handler(CommandHandler("chats", self.chats_command))
        self.application.add_handler(CommandHandler("clear", self.clear_command))
        self.application.add_handler(CommandHandler("dialog", self.continue_dialog_command))
        self.application.add_handler(CommandHandler("pay", self.pay_command)) # Добавлено

        # --- Callback кнопки ---
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        
        # --- Добавлено: Обработчик callback кнопок оплаты ---
        self.application.add_handler(CallbackQueryHandler(self.payment_callback_handler, pattern='^(pay_|check_payment_status|manual_payment_confirmed|back_to_|proceed_to_payment)'))

        # --- Сообщения и документы ---
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # --- Обработчик ошибок ---
        self.application.add_error_handler(self.error_handler)

    def init_db(self):
        """Инициализация таблиц базы данных"""
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
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_active_convs_type ON active_conversations(conversation_type);")
            logger.info("✅ Таблицы базы данных инициализированы")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")

    # --- Методы работы с БД ---
    def ensure_user_exists(self, user: User):
        """Гарантирует наличие пользователя в таблице users"""
        global user_cache
        if user.id in user_cache:
            return
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (id, username, first_name, last_name)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE
                        SET username = EXCLUDED.username,
                            first_name = EXCLUDED.first_name,
                            last_name = EXCLUDED.last_name
                    """, (user.id, user.username, user.first_name, user.last_name))
            user_cache.add(user.id)
            logger.info(f"👤 Пользователь {user.first_name} ({user.id}) добавлен/обновлён в БД")
        except Exception as e:
            logger.error(f"❌ Ошибка добавления пользователя в БД: {e}")

    def save_message_to_db(self, user_id: int, message: str, is_from_user: bool):
        """Сохраняет сообщение в буфер для последующей записи в БД"""
        global message_buffer
        with buffer_lock:
            message_buffer.append((user_id, message, is_from_user))
            # Принудительная запись каждые BUFFER_FLUSH_INTERVAL секунд или при достижении определенного размера буфера
            # Здесь просто добавляем в буфер, автозапись реализована в другом месте

    def flush_message_buffer(self):
        """Записывает накопленные сообщения из буфера в БД"""
        global message_buffer
        if not message_buffer:
            return
        try:
            with buffer_lock:
                local_buffer = message_buffer.copy()
                message_buffer.clear()
            if local_buffer:
                with psycopg.connect(DATABASE_URL) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            CREATE TEMP TABLE temp_messages (user_id BIGINT, message TEXT, is_from_user BOOLEAN) ON COMMIT DROP;
                        """)
                        cur.executemany("""
                            INSERT INTO temp_messages (user_id, message, is_from_user) VALUES (%s, %s, %s);
                        """, local_buffer)
                        cur.execute("""
                            INSERT INTO messages (user_id, message, is_from_user)
                            SELECT user_id, message, is_from_user FROM temp_messages;
                        """)
                        conn.commit()
                logger.info(f"💾 В БД записано {len(local_buffer)} сообщений из буфера")
        except Exception as e:
            logger.error(f"❌ Ошибка записи сообщений в БД: {e}")

    def save_active_conversation(self, user_id: int, conversation_type: str, assigned_owner: Optional[int], last_message: str):
        """Сохраняет или обновляет активный диалог в БД"""
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO active_conversations (user_id, conversation_type, assigned_owner, last_message)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE
                        SET conversation_type = EXCLUDED.conversation_type,
                            assigned_owner = EXCLUDED.assigned_owner,
                            last_message = EXCLUDED.last_message,
                            updated_at = CURRENT_TIMESTAMP
                    """, (user_id, conversation_type, assigned_owner, last_message))
            logger.info(f"💾 Диалог пользователя {user_id} сохранён/обновлён в БД")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения диалога в БД: {e}")

    def delete_active_conversation(self, user_id: int):
        """Удаляет активный диалог из БД"""
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM active_conversations WHERE user_id = %s", (user_id,))
            logger.info(f"🗑️ Диалог пользователя {user_id} удалён из БД")
        except Exception as e:
            logger.error(f"❌ Ошибка удаления диалога из БД: {e}")

    def save_stats(self):
        """Сохраняет статистику в файл"""
        try:
            # Создаём копию и преобразуем datetime в строку
            stats_to_save = self.bot_statistics.copy()
            if 'last_reset' in stats_to_save:
                stats_to_save['last_reset'] = stats_to_save['last_reset'].isoformat()
            with open(STATS_FILE, 'w', encoding='utf-8') as f:
                json.dump(stats_to_save, f, ensure_ascii=False, indent=4)
            logger.info("💾 Статистика сохранена в файл")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения статистики: {e}")

    # --- Команды ---
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        self.ensure_user_exists(user)
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        await update.message.reply_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /order"""
        user = update.effective_user
        self.ensure_user_exists(user)
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
        self.ensure_user_exists(user)
        # Проверяем активные диалоги
        if user_id in self.active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог."
                "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                "якщо хочете почати новий діалог."
            )
            return
        # Создаем запись о вопросе
        self.active_conversations[user_id] = {
            'type': 'question',
            'user_info': user,
            'assigned_owner': None,
            'last_message': "Нове запитання"
        }
        # Сохраняем в БД
        self.save_active_conversation(user_id, 'question', None, "Нове запитання")
        # Обновляем статистику
        self.bot_statistics['total_questions'] += 1
        self.save_stats()
        await update.message.reply_text(
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
            "Щоб завершити цей діалог пізніше, використайте команду /stop."
        )

    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /channel"""
        channel_text = """📢 Наш канал: @SecureShopChannel
Тут ви можете переглянути:
- Асортимент товарів
- Оновлення магазину
- Розіграші та акції

Приєднуйтесь, щоб бути в курсі всіх новин!"""
        await update.message.reply_text(channel_text)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает справку и информацию о сервисе"""
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
        await update.message.reply_text(help_text.strip())

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop"""
        user = update.effective_user
        user_id = user.id
        self.ensure_user_exists(user)
        # Проверяем активные диалоги
        if user_id not in self.active_conversations:
            await update.message.reply_text("У вас немає активного діалогу для завершення.")
            return
        # Удаляем диалог из активных (в памяти)
        conversation = self.active_conversations.pop(user_id, None)
        # Удаляем диалог из БД
        self.delete_active_conversation(user_id)
        # Удаляем связь владелец-клиент, если она есть
        owner_id = None
        if conversation and conversation.get('assigned_owner'):
            owner_id = conversation['assigned_owner']
        elif user_id in self.owner_client_map:
            owner_id = user_id
        if owner_id and owner_id in self.owner_client_map:
            client_id = self.owner_client_map.pop(owner_id)
            # Удаляем диалог клиента из активных (в памяти), если он еще не удален
            if client_id in self.active_conversations:
                self.active_conversations.pop(client_id, None)
                self.delete_active_conversation(client_id)
        # Отправляем подтверждение пользователю
        await update.message.reply_text("✅ Ваш діалог завершено.")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stats"""
        user = update.effective_user
        if user.id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return
        stats_text = f"""📊 Статистика бота:
Загальна кількість замовлень: {self.bot_statistics['total_orders']}
Загальна кількість запитань: {self.bot_statistics['total_questions']}
Загальна кількість користувачів: {self.bot_statistics['total_users']}
Останнє оновлення: {self.bot_statistics['last_reset'].strftime('%Y-%m-%d %H:%M:%S')}
"""
        await update.message.reply_text(stats_text)

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /history"""
        user = update.effective_user
        if user.id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return
        # Получаем последние 20 сообщений из БД
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT u.first_name, u.username, m.message, m.is_from_user, m.created_at
                        FROM messages m
                        JOIN users u ON m.user_id = u.id
                        ORDER BY m.created_at DESC
                        LIMIT 20
                    """)
                    messages = cur.fetchall()
            if not messages:
                await update.message.reply_text("📭 Історія повідомлень порожня.")
                return
            history_text = "📜 Останні 20 повідомлень:\n\n"
            for msg in reversed(messages): # От старых к новым
                sender = msg['first_name'] or msg['username'] or f"ID:{msg['user_id']}"
                direction = "👤" if msg['is_from_user'] else "🤖"
                history_text += f"{direction} {sender} ({msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')}): {msg['message']}\n---\n"
            await update.message.reply_text(history_text)
        except Exception as e:
            logger.error(f"❌ Ошибка получения истории: {e}")
            await update.message.reply_text("❌ Помилка отримання історії.")

    async def chats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /chats"""
        user = update.effective_user
        if user.id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return
        # Получаем активные диалоги из БД
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM active_conversations")
                    active_conv_rows = cur.fetchall()
            if not active_conv_rows:
                await update.message.reply_text("📭 Немає активних чатів.")
                return
            chats_text = "💬 Активні чати:\n\n"
            for conv in active_conv_rows:
                user_id = conv['user_id']
                conv_type = conv['conversation_type']
                last_msg = conv['last_message'][:50] + "..." if len(conv['last_message']) > 50 else conv['last_message']
                assigned = conv['assigned_owner'] if conv['assigned_owner'] else "Ніхто"
                chats_text += f"ID: {user_id}\nТип: {conv_type}\nОстаннє: {last_msg}\nВласник: {assigned}\n---\n"
            await update.message.reply_text(chats_text)
        except Exception as e:
            logger.error(f"❌ Ошибка получения активных чатов: {e}")
            await update.message.reply_text("❌ Помилка отримання активних чатів.")

    async def clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /clear"""
        user = update.effective_user
        if user.id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return
        # Очищаем активные диалоги в памяти
        self.active_conversations.clear()
        self.owner_client_map.clear()
        # Очищаем активные диалоги в БД
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM active_conversations")
                    conn.commit()
            await update.message.reply_text("✅ Активні чати очищено.")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки активных чатов: {e}")
            await update.message.reply_text("❌ Помилка очищення активних чатів.")

    async def continue_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /dialog для основателей"""
        owner = update.effective_user
        owner_id = owner.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return
        # Проверяем, есть ли уже назначенный клиент
        if owner_id in self.owner_client_map:
            client_id = self.owner_client_map[owner_id]
            # Проверяем, существует ли диалог клиента
            if client_id in self.active_conversations:
                await update.message.reply_text(f"💬 Ви вже спілкуєтесь з клієнтом {client_id}. Продовжуйте діалог.")
                return
            else:
                # Диалог клиента был завершен, очищаем связь
                del self.owner_client_map[owner_id]
        # Ищем неназначенный диалог
        assigned_client_id = None
        for client_id, conv_data in self.active_conversations.items():
            if conv_data.get('assigned_owner') is None:
                assigned_client_id = client_id
                break
        if not assigned_client_id:
            await update.message.reply_text("📭 Немає нових діалогів для призначення.")
            return
        # Назначаем диалог основателю
        self.active_conversations[assigned_client_id]['assigned_owner'] = owner_id
        self.owner_client_map[owner_id] = assigned_client_id
        # Сохраняем изменения в БД
        self.save_active_conversation(assigned_client_id, self.active_conversations[assigned_client_id]['type'], owner_id, self.active_conversations[assigned_client_id]['last_message'])
        # Получаем информацию о клиенте
        client_info = self.active_conversations[assigned_client_id]['user_info']
        # Отправляем основателю последние сообщения
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT message, is_from_user, created_at
                        FROM messages
                        WHERE user_id = %s
                        ORDER BY created_at DESC
                        LIMIT 10
                    """, (assigned_client_id,))
                    recent_msgs = cur.fetchall()
            history_text = f"💬 Історія останніх повідомлень від клієнта {client_info.first_name} (@{client_info.username or 'не вказано'}):\n\n"
            for msg in reversed(recent_msgs): # От старых к новым
                sender = "Клієнт" if msg['is_from_user'] else "Бот"
                history_text += f"{sender} ({msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')}): {msg['message']}\n"
            await update.message.reply_text(history_text)
        except Exception as e:
            logger.error(f"❌ Ошибка получения истории для /dialog: {e}")
            await update.message.reply_text("❌ Помилка отримання історії.")
        # Отправляем основателю последнее сообщение и предлагаем ответить
        last_msg = self.active_conversations[assigned_client_id]['last_message']
        await update.message.reply_text(f"💬 Останнє повідомлення від клієнта:\n{last_msg}\nНапишіть відповідь:")

    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /pay для заказов с сайта или ручного ввода"""
        user = update.effective_user
        user_id = user.id
        self.ensure_user_exists(user)
        # Проверяем аргументы команды
        if not context.args:
            # Если аргументов нет, проверяем, есть ли активный заказ
            if user_id in self.active_conversations and 'order_details' in self.active_conversations[user_id]:
                order_text = self.active_conversations[user_id]['order_details']
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
                # Сохраняем сумму в USD и детали заказа в контексте пользователя
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
                await update.message.reply_text(f"💳 Оберіть метод оплати для суми {usd_amount}$:", reply_markup=reply_markup)
            else:
                await update.message.reply_text("❌ Не знайдено активного замовлення. Спочатку зробіть замовлення командою /order або завантажте файл.")
            return
        else:
            # Первый аргумент - ID заказа
            order_id = context.args[0]
            # Объединяем все аргументы в одну строку
            items_str = " ".join(context.args[1:])
            # Используем регулярное выражение для извлечения товаров
            # Формат: <ServiceAbbr>-<PlanAbbr>-<Period>-<Price>
            # Для Discord Прикраси: DisU-BzN-6$-180
            # Для обычных подписок: Dis-Ful-1м-170
            item_pattern = r'(\w+)-(\w+)-([\w$]+)-(\d+)'
            items = re.findall(item_pattern, items_str)
            if not items:
                await update.message.reply_text("❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ... або використовуйте цю команду після оформлення замовлення в боті.")
                return
            # Словари для маппинга аббревиатур
            service_map = {
                "Dis": "Discord",
                "DisU": "Discord Прикраси",
                "ChG": "ChatGPT",
                "DuI": "Duolingo Individual",
                "DuF": "Duolingo Family",
                "PiP": "Picsart Pro",
                "CaP": "Canva Pro",
                "NeS": "Netflix Standard"
            }
            plan_map = {
                "Bas": "Basic",
                "Ful": "Full",
                "Plu": "Plus",
                "Ind": "Individual",
                "Fam": "Family",
                "Sta": "Standard",
                "Pro": "Pro",
                "Max": "Max",
                "BzN": "Без Nitro",
                "ZN": "З Nitro"
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
            self.active_conversations[user_id] = {
                'type': conversation_type,
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            # Сохраняем в БД
            self.save_active_conversation(user_id, conversation_type, None, order_text)
            # Обновляем статистику
            self.bot_statistics['total_orders'] += 1
            self.save_stats()
            # Конвертируем сумму в USD для оплаты
            uah_amount = float(total)
            usd_amount = convert_uah_to_usd(uah_amount)
            if usd_amount <= 0:
                 await update.message.reply_text("❌ Сума для оплати занадто мала.")
                 return
            # Сохраняем сумму в USD и детали заказа в контексте пользователя
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
            confirmation_text = f"""✅ Ваше замовлення прийнято!
{order_text}
Будь ласка, оберіть метод оплати 👇"""
            await update.message.reply_text(confirmation_text.strip(), reply_markup=reply_markup)

    # --- Callback кнопки ---
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id
        self.ensure_user_exists(user)

        # --- Главное меню ---
        if query.data == 'order':
            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'question':
            # Проверяем активные диалоги
            if user_id in self.active_conversations:
                await query.answer(
                    "❗ У вас вже є активний діалог."
                    "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                    "якщо хочете почати новий діалог.",
                    show_alert=True
                )
                return
            # Создаем запись о вопросе
            self.active_conversations[user_id] = {
                'type': 'question',
                'user_info': user,
                'assigned_owner': None,
                'last_message': "Нове запитання"
            }
            # Сохраняем в БД
            self.save_active_conversation(user_id, 'question', None, "Нове запитання")
            # Обновляем статистику
            self.bot_statistics['total_questions'] += 1
            self.save_stats()
            await query.edit_message_text(
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
                "Щоб завершити цей діалог пізніше, використайте команду /stop."
            )

        elif query.data == 'help':
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
            await query.edit_message_text(help_text.strip())

        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

        # --- Меню Підписок ---
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

        # --- Меню Цифрових товарів ---
        elif query.data == 'order_digital':
            keyboard = [
                [InlineKeyboardButton("🎮 Discord Прикраси", callback_data='category_discord_decor')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard))

        # --- Категории Підписок ---
        elif query.data == 'category_chatgpt':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_1')],
                [InlineKeyboardButton("12 місяців - 6500 UAH", callback_data='chatgpt_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("💬 ChatGPT Plus:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'category_discord':
            keyboard = [
                [InlineKeyboardButton("Discord Nitro Basic 1м - 170 UAH", callback_data='discord_basic_1')],
                [InlineKeyboardButton("Discord Nitro Basic 12м - 1700 UAH", callback_data='discord_basic_12')],
                [InlineKeyboardButton("Discord Nitro Full 1м - 300 UAH", callback_data='discord_full_1')],
                [InlineKeyboardButton("Discord Nitro Full 12м - 3000 UAH", callback_data='discord_full_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎮 Discord Nitro:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'category_duolingo':
            keyboard = [
                [InlineKeyboardButton("Duolingo Individual 1м - 300 UAH", callback_data='duolingo_ind_1')],
                [InlineKeyboardButton("Duolingo Individual 12м - 3000 UAH", callback_data='duolingo_ind_12')],
                [InlineKeyboardButton("Duolingo Family 12м - 2100 UAH", callback_data='duolingo_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎓 Duolingo Max:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'category_picsart':
            keyboard = [
                [InlineKeyboardButton("Picsart Pro 1м - 350 UAH", callback_data='picsart_plus_1')],
                [InlineKeyboardButton("Picsart Pro 12м - 3500 UAH", callback_data='picsart_plus_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎨 Picsart AI:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'category_canva':
            keyboard = [
                [InlineKeyboardButton("Canva Pro 1м - 350 UAH", callback_data='canva_pro_1')],
                [InlineKeyboardButton("Canva Pro 12м - 3500 UAH", callback_data='canva_pro_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📊 Canva Pro:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'category_netflix':
            keyboard = [
                [InlineKeyboardButton("Netflix Standard 1м - 400 UAH", callback_data='netflix_std_1')],
                [InlineKeyboardButton("Netflix Standard 12м - 4000 UAH", callback_data='netflix_std_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📺 Netflix:", reply_markup=InlineKeyboardMarkup(keyboard))

        # --- Категории Цифрових товарів ---
        elif query.data == 'category_discord_decor':
            keyboard = [
                [InlineKeyboardButton("Discord Прикраси (Без Nitro)", callback_data='discord_decor_bzn')],
                [InlineKeyboardButton("Discord Прикраси (З Nitro)", callback_data='discord_decor_zn')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')]
            ]
            await query.edit_message_text("🎮 Discord Прикраси:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'discord_decor_bzn':
            keyboard = [
                [InlineKeyboardButton("5$ - 145 UAH", callback_data='discord_decor_bzn_5')],
                [InlineKeyboardButton("10$ - 295 UAH", callback_data='discord_decor_bzn_10')],
                [InlineKeyboardButton("15$ - 440 UAH", callback_data='discord_decor_bzn_15')],
                [InlineKeyboardButton("20$ - 590 UAH", callback_data='discord_decor_bzn_20')],
                [InlineKeyboardButton("25$ - 740 UAH", callback_data='discord_decor_bzn_25')],
                [InlineKeyboardButton("30$ - 885 UAH", callback_data='discord_decor_bzn_30')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Discord Прикраси (Без Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'discord_decor_zn':
            keyboard = [
                [InlineKeyboardButton("5$ - 295 UAH", callback_data='discord_decor_zn_5')],
                [InlineKeyboardButton("10$ - 590 UAH", callback_data='discord_decor_zn_10')],
                [InlineKeyboardButton("15$ - 885 UAH", callback_data='discord_decor_zn_15')],
                [InlineKeyboardButton("20$ - 1180 UAH", callback_data='discord_decor_zn_20')],
                [InlineKeyboardButton("25$ - 1475 UAH", callback_data='discord_decor_zn_25')],
                [InlineKeyboardButton("30$ - 1770 UAH", callback_data='discord_decor_zn_30')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Discord Прикраси (З Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))

        # --- Обработка выбора товара (Подписки) ---
        elif query.data in ['chatgpt_1', 'discord_basic_1', 'discord_basic_12',
                            'discord_full_1', 'discord_full_12',
                            'duolingo_ind_1', 'duolingo_ind_12', 'duolingo_fam_12',
                            'picsart_plus_1', 'picsart_plus_12',
                            'canva_pro_1', 'canva_pro_12',
                            'netflix_std_1', 'netflix_std_12']:
            # Пример обработки, адаптируйте под свои данные
            item_map = {
                'chatgpt_1': ("ChatGPT Plus", "1 місяць", "650"),
                'chatgpt_12': ("ChatGPT Plus", "12 місяців", "6500"),
                'discord_basic_1': ("Discord Nitro Basic", "1 місяць", "170"),
                'discord_basic_12': ("Discord Nitro Basic", "12 місяців", "1700"),
                'discord_full_1': ("Discord Nitro Full", "1 місяць", "300"),
                'discord_full_12': ("Discord Nitro Full", "12 місяців", "3000"),
                'duolingo_ind_1': ("Duolingo Individual", "1 місяць", "300"),
                'duolingo_ind_12': ("Duolingo Individual", "12 місяців", "3000"),
                'duolingo_fam_12': ("Duolingo Family", "12 місяців", "2100"),
                'picsart_plus_1': ("Picsart Pro", "1 місяць", "350"),
                'picsart_plus_12': ("Picsart Pro", "12 місяців", "3500"),
                'canva_pro_1': ("Canva Pro", "1 місяць", "350"),
                'canva_pro_12': ("Canva Pro", "12 місяців", "3500"),
                'netflix_std_1': ("Netflix Standard", "1 місяць", "400"),
                'netflix_std_12': ("Netflix Standard", "12 місяців", "4000"),
            }
            service, period, price = item_map.get(query.data, ("Невідомий товар", "Невідомий період", "0"))
            order_text = f"🛍️ Ваше замовлення:\n▫️ {service} ({period}) - {price} UAH\n💳 Всього: {price} UAH"
            # Сохраняем заказ
            self.active_conversations[user_id] = {
                'type': 'subscription_order',
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            # Сохраняем в БД
            self.save_active_conversation(user_id, 'subscription_order', None, order_text)
            # Обновляем статистику
            self.bot_statistics['total_orders'] += 1
            self.save_stats()
            # Конвертируем сумму в USD для оплаты
            uah_amount = float(price)
            usd_amount = convert_uah_to_usd(uah_amount)
            if usd_amount <= 0:
                 await query.edit_message_text("❌ Сума для оплати занадто мала.")
                 return
            # Сохраняем сумму в USD и детали заказа в контексте пользователя
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
            confirmation_text = f"""✅ Ваше замовлення прийнято!
{order_text}
Будь ласка, оберіть метод оплати 👇"""
            await query.edit_message_text(confirmation_text.strip(), reply_markup=reply_markup)

        # --- Обработка выбора товара (Цифровые товары) ---
        elif query.data.startswith('discord_decor_'):
            # Пример обработки, адаптируйте под свои данные
            item_map = {
                'discord_decor_bzn_5': ("Discord Прикраси (Без Nitro)", "5$", "145"),
                'discord_decor_bzn_10': ("Discord Прикраси (Без Nitro)", "10$", "295"),
                'discord_decor_bzn_15': ("Discord Прикраси (Без Nitro)", "15$", "440"),
                'discord_decor_bzn_20': ("Discord Прикраси (Без Nitro)", "20$", "590"),
                'discord_decor_bzn_25': ("Discord Прикраси (Без Nitro)", "25$", "740"),
                'discord_decor_bzn_30': ("Discord Прикраси (Без Nitro)", "30$", "885"),
                'discord_decor_zn_5': ("Discord Прикраси (З Nitro)", "5$", "295"),
                'discord_decor_zn_10': ("Discord Прикраси (З Nitro)", "10$", "590"),
                'discord_decor_zn_15': ("Discord Прикраси (З Nitro)", "15$", "885"),
                'discord_decor_zn_20': ("Discord Прикраси (З Nitro)", "20$", "1180"),
                'discord_decor_zn_25': ("Discord Прикраси (З Nitro)", "25$", "1475"),
                'discord_decor_zn_30': ("Discord Прикраси (З Nitro)", "30$", "1770"),
            }
            service, period, price = item_map.get(query.data, ("Невідомий товар", "Невідомий період", "0"))
            order_text = f"🛍️ Ваше замовлення:\n▫️ {service} ({period}) - {price} UAH\n💳 Всього: {price} UAH"
            # Сохраняем заказ
            self.active_conversations[user_id] = {
                'type': 'digital_order',
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            # Сохраняем в БД
            self.save_active_conversation(user_id, 'digital_order', None, order_text)
            # Обновляем статистику
            self.bot_statistics['total_orders'] += 1
            self.save_stats()
            # Конвертируем сумму в USD для оплаты
            uah_amount = float(price)
            usd_amount = convert_uah_to_usd(uah_amount)
            if usd_amount <= 0:
                 await query.edit_message_text("❌ Сума для оплати занадто мала.")
                 return
            # Сохраняем сумму в USD и детали заказа в контексте пользователя
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
            confirmation_text = f"""✅ Ваше замовлення прийнято!
{order_text}
Будь ласка, оберіть метод оплати 👇"""
            await query.edit_message_text(confirmation_text.strip(), reply_markup=reply_markup)

        # --- Добавлено: Обработка кнопки "Оплатить" после заказа из файла ---
        elif query.data == 'proceed_to_payment':
            # Проверяем, есть ли активный заказ
            if user_id in self.active_conversations and 'order_details' in self.active_conversations[user_id]:
                order_text = self.active_conversations[user_id]['order_details']
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
                # Сохраняем сумму в USD и детали заказа в контексте пользователя
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
            else:
                await query.edit_message_text("❌ Не знайдено активного замовлення.")

        # --- Отмена заказа ---
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

    # --- Добавлено/Обновлено: Обработчик callback кнопок, связанных с оплатой ---
    async def payment_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок, связанных с оплатой"""
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data

        # Проверяем, есть ли сумма в контексте (для оплаты из бота)
        usd_amount = context.user_data.get('payment_amount_usd')
        order_details = context.user_data.get('order_details_for_payment')

        # --- Обработка кнопки "Оплатить" после заказа из файла ---
        if data == 'proceed_to_payment':
            # Проверяем, есть ли активный заказ
            if user_id in self.active_conversations and 'order_details' in self.active_conversations[user_id]:
                order_text = self.active_conversations[user_id]['order_details']
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
                # Сохраняем сумму в USD и детали заказа в контексте пользователя
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
            else:
                await query.edit_message_text("❌ Не знайдено активного замовлення для оплати.")
            return

        # --- Основная логика оплаты ---
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
                f"💳 Будь ласка, здійсніть оплату {usd_amount}$ карткою на реквізити магазину."
                f"\n(Тут будуть реквізити)"
                f"\nПісля оплати натисніть кнопку '✅ Оплачено'.",
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
            await query.edit_message_text(f"🪙 Оберіть криптовалюту для оплати {usd_amount}$:", reply_markup=reply_markup)
            context.user_data.pop('nowpayments_invoice_id', None) # Очищаем предыдущий ID

        # --- Выбор конкретной криптовалюты ---
        elif data.startswith('pay_crypto_'):
            currency_code = data.split('_', 2)[2] # pay_crypto_usdttrc20 -> usdttrc20
            currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == currency_code), currency_code)

            try:
                # --- Вызов функции оплаты NOWPayments ---
                pay_url, invoice_id = await self.pay_with_nowpayments(usd_amount, currency_code, order_details)
                # Сохраняем ID инвойса для возможной проверки статуса
                context.user_data['nowpayments_invoice_id'] = invoice_id
                keyboard = [
                    [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                    [InlineKeyboardButton("🔄 Перевірити статус", callback_data='check_payment_status')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_crypto_selection')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(f"🪙 Оплата {usd_amount}$ в {currency_name}:", reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Помилка створення інвойсу NOWPayments: {e}")
                await query.edit_message_text(f"❌ Помилка створення посилання для оплати: {e}")

        # --- Проверка статуса оплаты NOWPayments ---
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
                    await self.request_account_data(query, context) # Передаем query
                elif payment_status in ['pending', 'waiting']:
                     # Повторно показываем кнопки оплаты
                    currency_code = "usdttrc20" # Или сохранить код валюты в context
                    currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == currency_code), currency_code)
                    pay_url = status_data.get("invoice_url", "#")
                    keyboard = [
                        [InlineKeyboardButton("🔗 Перейти до оплати", url=pay_url)],
                        [InlineKeyboardButton("🔄 Перевірити статус", callback_data='check_payment_status')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_crypto_selection')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(f"⏳ Оплата ще не завершена. Статус: {payment_status}.\n🪙 Оплата {usd_amount}$ в {currency_name}:", reply_markup=reply_markup)
                else:
                    # Оплата не прошла или отменена
                    keyboard = [
                        [InlineKeyboardButton("💳 Інший метод оплати", callback_data='back_to_payment_methods')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(f"❌ Оплата не пройшла або була скасована. Статус: {payment_status}.", reply_markup=reply_markup)
            except requests.exceptions.RequestException as e:
                logger.error(f"Помилка мережі при перевірці статусу NOWPayments: {e}")
                await query.edit_message_text(f"❌ Помилка перевірки статусу оплати: проблема з мережею.")
            except Exception as e:
                logger.error(f"Помилка перевірки статусу NOWPayments: {e}")
                await query.edit_message_text(f"❌ Помилка перевірки статусу оплати: {e}")

        # --- Ручне підтвердження оплати ---
        elif data == 'manual_payment_confirmed':
            # Здесь можно добавить логику проверки оплаты вручную или просто перейти к следующему шагу
            await query.edit_message_text("✅ Оплата підтверджена вручну.")
            # Переходим к сбору данных аккаунта
            await self.request_account_data(query, context) # Передаем query

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
            await query.edit_message_text(f"💳 Оберіть метод оплати для суми {usd_amount}$:", reply_markup=reply_markup)
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
            await query.edit_message_text(f"🪙 Оберіть криптовалюту для оплати {usd_amount}$:", reply_markup=reply_markup)
            context.user_data.pop('nowpayments_invoice_id', None) # Очищаем предыдущий ID

    # --- Добавлено: Функция оплаты через NOWPayments ---
    async def pay_with_nowpayments(self, amount: float, currency: str, order_description: str) -> tuple[str, str]:
        """Создает инвойс в NOWPayments и возвращает URL для оплаты и ID инвойса."""
        if not NOWPAYMENTS_API_KEY:
            raise ValueError("NOWPAYMENTS_API_KEY не установлен в переменных окружения.")

        url = "https://api.nowpayments.io/v1/invoice"
        payload = {
            "price_amount": amount,
            "price_currency": "usd",
            "order_id": f"order_{int(time.time())}", # Уникальный ID заказа
            "order_description": order_description,
            "pay_currency": currency,
            "ipn_callback_url": f"{WEBHOOK_URL}/ipn", # URL для уведомлений от NOWPayments (если нужен)
            "success_url": f"{WEBHOOK_URL}/success", # URL после успешной оплаты (если нужен)
            "cancel_url": f"{WEBHOOK_URL}/cancel", # URL после отмены оплаты (если нужен)
        }
        headers = {
            "Authorization": f"Bearer {NOWPAYMENTS_API_KEY}",
            "Content-Type": "application/json"
        }

        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        invoice = response.json()
        pay_url = invoice.get("invoice_url", "Помилка отримання посилання")
        invoice_id = invoice.get("invoice_id", "Невідомий ID рахунку")
        return pay_url, invoice_id

    # --- Добавлено: Запрос данных аккаунта ---
    async def request_account_data(self, query_or_update, context: ContextTypes.DEFAULT_TYPE):
        """Запрашивает у пользователя логин и пароль от аккаунта"""
        # Определяем источник (query или update)
        if hasattr(query_or_update, 'from_user'):
            user_id = query_or_update.from_user.id
            is_query = True
        else: # Это Update (например, из manual_payment_confirmed)
            user_id = query_or_update.effective_user.id
            is_query = False
            
        order_details = context.user_data.get('order_details_for_payment', '')

        # Определяем тип заказа (примерная логика, адаптируйте под свою структуру)
        is_digital = 'digital_order' in self.active_conversations.get(user_id, {}).get('type', '') or 'Discord Прикраси' in order_details
        item_type = "акаунту Discord" if is_digital else "акаунту"

        # Сообщаем пользователю и переходим в состояние ожидания данных
        message_text = (
            f"✅ Оплата пройшла успішно!\n"
            f"Будь ласка, надішліть мені логін та пароль від {item_type}.\n"
            f"Наприклад: `login:password` або `login password`"
        )
        if is_query:
            await query_or_update.edit_message_text(message_text, parse_mode='Markdown')
        else:
            await query_or_update.message.reply_text(message_text, parse_mode='Markdown')

        context.user_data['awaiting_account_data'] = True
        # Сохраняем детали заказа для передачи основателям позже
        context.user_data['account_details_order'] = order_details

    # --- Обработчики сообщений ---
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text

        self.ensure_user_exists(user)
        self.save_message_to_db(user_id, message_text, True)

        # 🔴 КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: сначала проверяем владельца
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await self.handle_owner_message(update, context)
            return

        # --- Добавлено: Проверка состояния ожидания данных аккаунта ---
        if context.user_data.get('awaiting_account_data'):
            await self.handle_account_data_message(update, context)
            return
        # --- Конец добавления ---

        # --- Проверяем существование диалога ---
        if user_id not in self.active_conversations:
            await update.message.reply_text("Будь ласка, скористайтесь меню для початку діалогу.")
            return

        # --- Сохраняем последнее сообщение ---
        self.active_conversations[user_id]['last_message'] = message_text
        self.save_active_conversation(user_id, self.active_conversations[user_id]['type'], self.active_conversations[user_id].get('assigned_owner'), message_text)

        # --- Пересылаем сообщение основателю ---
        assigned_owner = self.active_conversations[user_id].get('assigned_owner')
        if assigned_owner:
            try:
                client_info = self.active_conversations[user_id]['user_info']
                forward_text = f"Нове повідомлення від клієнта {client_info.first_name} (@{client_info.username or 'не вказано'}):\n{message_text}"
                await context.bot.send_message(chat_id=assigned_owner, text=forward_text)
                self.save_message_to_db(assigned_owner, forward_text, False)
            except Exception as e:
                logger.error(f"❌ Ошибка пересылки сообщения основателю {assigned_owner}: {e}")
                await update.message.reply_text("❌ Помилка відправки повідомлення засновнику. Спробуйте ще раз пізніше.")
        else:
            # Ищем свободного основателя и назначаем диалог
            assigned = False
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                if owner_id not in self.owner_client_map:
                    self.active_conversations[user_id]['assigned_owner'] = owner_id
                    self.owner_client_map[owner_id] = user_id
                    self.save_active_conversation(user_id, self.active_conversations[user_id]['type'], owner_id, message_text)
                    # Отправляем уведомление основателю
                    try:
                        client_info = self.active_conversations[user_id]['user_info']
                        notification_text = f"🔔 Новий діалог від клієнта {client_info.first_name} (@{client_info.username or 'не вказано'}):\n{message_text}"
                        await context.bot.send_message(chat_id=owner_id, text=notification_text)
                        self.save_message_to_db(owner_id, notification_text, False)
                        # Отправляем основателю последние сообщения
                        try:
                            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        SELECT message, is_from_user, created_at
                                        FROM messages
                                        WHERE user_id = %s
                                        ORDER BY created_at DESC
                                        LIMIT 5
                                    """, (user_id,))
                                    recent_msgs = cur.fetchall()
                            history_text = f"💬 Історія останніх повідомлень від клієнта:\n\n"
                            for msg in reversed(recent_msgs): # От старых к новым
                                sender = "Клієнт" if msg['is_from_user'] else "Бот"
                                history_text += f"{sender} ({msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')}): {msg['message']}\n"
                            await context.bot.send_message(chat_id=owner_id, text=history_text)
                        except Exception as e:
                            logger.error(f"❌ Ошибка получения истории для нового диалога: {e}")
                        # Отправляем основателю последнее сообщение и предлагаем ответить
                        await context.bot.send_message(chat_id=owner_id, text=f"💬 Останнє повідомлення від клієнта:\n{message_text}\nНапишіть відповідь:")
                        assigned = True
                        break
                    except Exception as e:
                        logger.error(f"❌ Ошибка уведомления основателя {owner_id}: {e}")
                        # Пробуем следующего основателя
            if not assigned:
                await update.message.reply_text("⏳ Всі засновники зараз зайняті. Ваше повідомлення поставлено в чергу. Засновник зв'яжеться з вами найближчим часом.")

    # --- Добавлено: Обработка сообщения с логином и паролем ---
    async def handle_account_data_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает сообщение с логином и паролем от пользователя"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text

        # Проверяем, ожидаем ли мы данные аккаунта от этого пользователя
        if not context.user_data.get('awaiting_account_data'):
            # Если нет, просто передаем сообщение как обычно (или игнорируем)
            await update.message.reply_text("❌ Неочікуване повідомлення. Якщо у вас є питання, скористайтесь меню.")
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
        account_info_message = (
            f"🔐 Нові дані акаунту від клієнта!\n"
            f"👤 Клієнт: {user.first_name}\n"
            f"🆔 ID: {user_id}\n"
            f"🛍️ Замовлення: {order_details}\n"
            f"🔑 Логін: `{login}`\n"
            f"🔓 Пароль: `{password}`"
        )

        # Отправляем основателям
        success_count = 0
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(chat_id=owner_id, text=account_info_message, parse_mode='Markdown')
                success_count += 1
            except Exception as e:
                logger.error(f"❌ Не вдалося надіслати повідомлення основателю {owner_id}: {e}")

        if success_count > 0:
            await update.message.reply_text("✅ Дані акаунту успішно надіслано! Засновник магазину зв'яжеться з вами найближчим часом.")
        else:
            await update.message.reply_text("❌ Не вдалося надіслати повідомлення клієнту.")

        # Очищаем состояние пользователя
        context.user_data.pop('awaiting_account_data', None)
        context.user_data.pop('payment_amount_usd', None)
        context.user_data.pop('order_details_for_payment', None)
        context.user_data.pop('account_details_order', None)
        context.user_data.pop('nowpayments_invoice_id', None)
        # Удаляем активный диалог, так как процесс завершен
        if user_id in self.active_conversations:
            del self.active_conversations[user_id]
        self.delete_active_conversation(user_id) # Если используется БД

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик документов (для заказов из файлов)"""
        user = update.effective_user
        user_id = user.id
        document = update.message.document

        self.ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id in self.active_conversations:
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
            order_text += f"\n▫️ {item['service']} {item.get('plan', '')} ({item['period']}) - {item['price']} UAH"
        order_text += f"\n💳 Всього: {order_data['total']} UAH"

        # Определяем тип заказа (простая логика, можно усложнить)
        has_digital = any("Прикраси" in item.get('service', '') for item in order_data['items']) # Обновлено
        conversation_type = 'digital_order' if has_digital else 'subscription_order'

        # Создаем запись о заказе
        self.active_conversations[user_id] = {
            'type': conversation_type,
            'user_info': user,
            'assigned_owner': None,
            'order_details': order_text,
            'last_message': order_text
        }

        # Сохраняем в БД
        self.save_active_conversation(user_id, conversation_type, None, order_text)

        # Обновляем статистику
        self.bot_statistics['total_orders'] += 1
        self.save_stats()

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

    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка сообщений от основателей"""
        owner = update.effective_user
        owner_id = owner.id
        message_text = update.message.text

        self.save_message_to_db(owner_id, message_text, True)

        # Проверяем, назначен ли клиент основателю
        if owner_id not in self.owner_client_map:
            await update.message.reply_text("❌ У вас немає активного діалогу з клієнтом.")
            return

        client_id = self.owner_client_map[owner_id]

        # Проверяем, существует ли диалог клиента
        if client_id not in self.active_conversations:
            del self.owner_client_map[owner_id]
            await update.message.reply_text("❌ Діалог із клієнтом був завершений.")
            return

        # Отправляем сообщение клиенту
        try:
            await context.bot.send_message(chat_id=client_id, text=message_text)
            self.save_message_to_db(client_id, message_text, False)
            # Обновляем последнее сообщение в диалоге
            self.active_conversations[client_id]['last_message'] = message_text
            self.save_active_conversation(client_id, self.active_conversations[client_id]['type'], owner_id, message_text)
        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения клиенту {client_id}: {e}")
            await update.message.reply_text("❌ Помилка відправки повідомлення клієнту.")

    # --- Обработчик ошибок ---
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(msg="Exception while handling an update:", exc_info=context.error)

        # Отправляем сообщение пользователю о возникшей ошибке (опционально)
        # try:
        #     if isinstance(update, Update) and update.effective_message:
        #         await update.effective_message.reply_text("❌ Виникла помилка. Спробуйте ще раз пізніше.")
        # except:
        #     pass # Игнорируем ошибки при отправке сообщения об ошибке

    # --- Установка меню команд ---
    async def set_commands_menu(self):
        """Установка стандартного меню команд"""
        owner_commands = [BotCommandScopeChat(chat_id=OWNER_ID_1), BotCommandScopeChat(chat_id=OWNER_ID_2)]
        user_commands = [BotCommandScopeChat(chat_id='*')] # Для всех остальных

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
                ('pay', 'Оплатити замовлення з сайту') # Команда /pay
            ], scope=user_commands[0])

            logger.info("✅ Меню команд установлено")
        except Exception as e:
            logger.error(f"❌ Ошибка установки меню команд: {e}")

    # --- Запуск приложения ---
    async def start_application(self):
        """Запуск приложения Telegram"""
        try:
            await self.set_commands_menu()
            await self.application.initialize()
            logger.info("✅ Приложение Telegram инициализировано")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации приложения Telegram: {e}")
            raise

    async def start_polling(self):
        """Запуск polling"""
        try:
            if self.application.running:
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

# --- Flask обработчики ---
flask_app = Flask(__name__)
CORS(flask_app) # Разрешаем CORS для всех доменов

@flask_app.route('/', methods=['GET'])
def index():
    return '<h1>✅ SecureShop Telegram Bot is running!</h1><p>Webhook is active.</p>'

@flask_app.route('/webhook', methods=['POST'])
async def webhook():
    """Обработчик webhook от Telegram"""
    # Логика webhook будет реализована при необходимости
    return '', 200

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
        await bot_instance.application.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"✅ Webhook установлен на {WEBHOOK_URL}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка настройки webhook: {e}")
        return False

def bot_thread():
    """Функция запуска бота в отдельном потоке"""
    global bot_running
    with bot_lock:
        if bot_running:
            logger.warning("🔄 Бот уже запущен в другом потоке")
            return
        bot_running = True

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        logger.info("🚀 Запуск Telegram бота...")
        loop.run_until_complete(bot_instance.start_application())
        logger.info("✅ Telegram бот запущен")

        if USE_POLLING:
            logger.info("🔄 Используется Polling...")
            loop.run_until_complete(bot_instance.start_polling())
        else:
            logger.info("🌐 Используется Webhook...")
            success = loop.run_until_complete(setup_webhook())
            if success:
                # Здесь должна быть логика запуска Flask сервера, если он нужен
                # Например, запуск flask_app.run(port=PORT) в отдельном потоке
                pass
            else:
                logger.error("❌ Не удалось настроить Webhook. Бот не запущен.")

    except KeyboardInterrupt:
        logger.info("🛑 Получен сигнал завершения работы (Ctrl+C)")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в bot_thread: {e}")
        logger.warning("🕒 Ожидаем 30 секунд перед повторной попыткой...")
        time.sleep(30)
        bot_thread() # Повторный запуск
    finally:
        try:
            if not loop.is_closed():
                loop.close()
        except:
            pass
        with bot_lock:
            bot_running = False
        logger.warning("🔁 Перезапускаем поток бота...")
        time.sleep(5)
        bot_thread() # Повторный запуск потока

def auto_save_stats():
    """Автосохранение статистики и буфера сообщений"""
    while True:
        time.sleep(BUFFER_FLUSH_INTERVAL)
        try:
            bot_instance.save_stats()
            bot_instance.flush_message_buffer()
            logger.info("✅ Статистика и буфер сообщений автосохранены")
        except Exception as e:
            logger.error(f"❌ Ошибка автосохранения: {e}")

def main():
    # Задержка для Render.com, чтобы избежать конфликтов
    if os.environ.get('RENDER'):
        logger.info("⏳ Ожидаем 10 секунд для предотвращения конфликтов...")
        time.sleep(10)

    # Запускаем автосохранение в отдельном потоке
    autosave_thread = threading.Thread(target=auto_save_stats)
    autosave_thread.daemon = True
    autosave_thread.start()

    # Запускаем бота в отдельном потоке
    bot_t = threading.Thread(target=bot_thread)
    bot_t.daemon = True
    bot_t.start()

    # Основной цикл для проверки состояния (если нужен)
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
