# main.py (обновленный с интеграцией /payout, исправлениями и недостающими функциями)
import logging
import os
import asyncio
import threading
import time
import json
import re
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, BotCommandScopeChat, BotCommandScopeDefault, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict, BadRequest
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg
from psycopg.rows import dict_row

# --- Настройки логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в переменных окружения")

# URL вебхука (если используется webhook)
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # Например: https://yourdomain.com/webhook
WEBHOOK_PORT = int(os.getenv("PORT", "8443")) # Порт, который слушает Flask

# URL базы данных PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не установлен в переменных окружения")

# ID основателей (администраторов)
OWNER_ID_1 = int(os.getenv("OWNER_ID_1"))
OWNER_ID_2 = int(os.getenv("OWNER_ID_2"))

# Использовать polling или webhook
USE_POLLING = os.getenv("USE_POLLING", "True").lower() == "true"

# NOWPayments API
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
if not NOWPAYMENTS_API_KEY:
    raise ValueError("NOWPAYMENTS_API_KEY не установлен в переменных окружения")

# Курсы валют
EXCHANGE_RATE_UAH_TO_USD = float(os.getenv('EXCHANGE_RATE_UAH_TO_USD', 41.26)) # Примерный курс, можно заменить на динамический

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

# Реквизиты карты (из оплата.txt)
CARD_DETAILS = "1234 5678 9012 3456" # Тестовые реквизиты

# --- Инициализация Flask приложения ---
flask_app = Flask(__name__)
CORS(flask_app) # Разрешаем CORS для всех доменов

# --- Глобальные переменные ---
bot_statistics = {'total_users': 0, 'total_orders': 0, 'total_questions': 0, 'last_reset': datetime.now().isoformat()}
active_conversations = {}
owner_client_map = {}
telegram_app = None
bot_running = False

# --- Вспомогательные функции для работы с БД ---
def get_db_connection():
    """Создает и возвращает соединение с БД."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    """Инициализирует структуру базы данных."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Создание таблицы пользователей
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGINT PRIMARY KEY,
                        first_name TEXT,
                        last_name TEXT,
                        username TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Создание таблицы активных диалогов
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS active_conversations (
                        user_id BIGINT PRIMARY KEY REFERENCES users(id),
                        type TEXT NOT NULL, -- 'question', 'subscription_order', 'digital_order'
                        assigned_owner BIGINT REFERENCES users(id),
                        last_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Создание таблицы истории сообщений
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS message_history (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        message TEXT NOT NULL,
                        is_from_user BOOLEAN NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Создание таблицы статистики (одна строка)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_stats (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        data JSONB NOT NULL
                    )
                """)
                # Вставка начальной строки статистики, если её нет
                cur.execute("INSERT INTO bot_stats (id, data) VALUES (1, %s) ON CONFLICT (id) DO NOTHING", (json.dumps(bot_statistics),))
            conn.commit()
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")

def ensure_user_exists(user: User):
    """Гарантирует, что пользователь существует в БД."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, first_name, last_name, username)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        username = EXCLUDED.username,
                        last_active = CURRENT_TIMESTAMP
                """, (user.id, user.first_name, user.last_name, user.username))
            conn.commit()
        logger.debug(f"👤 Пользователь {user.id} гарантирован в БД")
    except Exception as e:
        logger.error(f"❌ Ошибка ensure_user_exists для {user.id}: {e}")

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message):
    """Сохраняет или обновляет активный диалог в БД."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO active_conversations (user_id, type, assigned_owner, last_message, updated_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id) DO UPDATE SET
                        type = EXCLUDED.type,
                        assigned_owner = EXCLUDED.assigned_owner,
                        last_message = EXCLUDED.last_message,
                        updated_at = CURRENT_TIMESTAMP
                """, (user_id, conversation_type, assigned_owner, last_message))
            conn.commit()
        logger.debug(f"💾 Активный диалог для {user_id} сохранен в БД")
    except Exception as e:
        logger.error(f"❌ Ошибка save_active_conversation для {user_id}: {e}")

def save_message(user_id, message_text, is_from_user):
    """Сохраняет сообщение в историю."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO message_history (user_id, message, is_from_user, timestamp)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                """, (user_id, message_text, is_from_user))
            conn.commit()
        logger.debug(f"💾 Сообщение от {user_id} сохранено в БД")
    except Exception as e:
        logger.error(f"❌ Ошибка save_message для {user_id}: {e}")

def load_stats():
    """Загружает статистику из БД."""
    global bot_statistics
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM bot_stats WHERE id = 1")
                row = cur.fetchone()
                if row:
                    bot_statistics = row['data']
                    logger.debug("📈 Статистика загружена из БД")
                else:
                    logger.warning("⚠️ Статистика не найдена в БД, используются значения по умолчанию")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки статистики: {e}")

def save_stats():
    """Сохраняет статистику в БД."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bot_stats SET data = %s WHERE id = 1", (json.dumps(bot_statistics),))
            conn.commit()
        logger.debug("💾 Статистика сохранена в БД")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения статистики: {e}")

def get_uah_amount_from_order_text(order_text: str) -> float:
    """Извлекает сумму в UAH из текста заказа."""
    match = re.search(r'💳 Всього: (\d+(?:\.\d+)?) UAH', order_text)
    if match:
        return float(match.group(1))
    return 0.0

def convert_uah_to_usd(uah_amount: float) -> float:
    """Конвертирует сумму из UAH в USD по фиксированному курсу."""
    if uah_amount <= 0:
        return 0.0
    return round(uah_amount / EXCHANGE_RATE_UAH_TO_USD, 2)

# --- NOWPayments API ---
async def create_nowpayments_invoice(amount: float, currency_in: str = 'usd', currency_out: str = 'btc') -> dict:
    """Создает инвойс в NOWPayments."""
    url = "https://api.nowpayments.io/v1/invoice"
    headers = {
        "Authorization": f"Bearer {NOWPAYMENTS_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "price_amount": amount,
        "price_currency": currency_in,
        "order_id": f"order_{int(time.time())}",
        "order_description": "Оплата товарів в SecureShop",
        "ipn_callback_url": f"{WEBHOOK_URL}/nowpayments-ipn" if WEBHOOK_URL else None,
        "success_url": f"https://t.me/{(await telegram_app.bot.get_me()).username}",
        "cancel_url": f"https://t.me/{(await telegram_app.bot.get_me()).username}",
        "currency_out": currency_out
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.info(f"✅ NOWPayments инвойс создан: {data}")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка сети NOWPayments: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка создания инвойса NOWPayments: {e}")
        raise

async def get_nowpayments_invoice_status(invoice_id: str) -> dict:
    """Получает статус инвойса из NOWPayments."""
    url = f"https://api.nowpayments.io/v1/invoice/{invoice_id}"
    headers = {
        "Authorization": f"Bearer {NOWPAYMENTS_API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"🔄 NOWPayments статус инвойса {invoice_id}: {data}")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка сети NOWPayments при проверке статуса: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка проверки статуса NOWPayments инвойса {invoice_id}: {e}")
        raise

# --- Основной класс бота ---
class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self.application = Application.builder().token(self.token).build()
        self.loop = None # Будет установлен в bot_thread

    async def initialize(self):
        """Инициализирует обработчики команд и сообщений."""
        # --- Обработчики команд ---
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("order", self.order))
        self.application.add_handler(CommandHandler("question", self.question_command))
        self.application.add_handler(CommandHandler("channel", self.channel))
        self.application.add_handler(CommandHandler("stop", self.stop))
        self.application.add_handler(CommandHandler("pay", self.pay_command))
        self.application.add_handler(CommandHandler("payout", self.payout_command))
        # Команды для владельцев
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("chats", self.show_active_chats)) # Исправлено
        self.application.add_handler(CommandHandler("history", self.show_conversation_history)) # Исправлено
        self.application.add_handler(CommandHandler("clear", self.clear_active_chats)) # Исправлено
        self.application.add_handler(CommandHandler("dialog", self.continue_dialog_command)) # Исправлено

        # --- Обработчики callback кнопок ---
        # Обработчик callback кнопок оплаты (только для данных, начинающихся с pay_ или payout_)
        self.application.add_handler(CallbackQueryHandler(self.payment_callback_handler, pattern=r'^(pay_|payout_)'))
        # Основной обработчик для остальных кнопок
        self.application.add_handler(CallbackQueryHandler(self.button_handler))

        # --- Обработчик текстовых сообщений (должен быть последним) ---
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(MessageHandler(filters.Document.FileExtension("json"), self.handle_json_file))

        # --- Обработчик ошибок ---
        self.application.add_error_handler(self.error_handler)

        logger.info("✅ Обработчики команд и сообщений зарегистрированы")

    async def start_polling(self):
        """Запускает бота в режиме polling."""
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()

    async def stop_polling(self):
        """Останавливает бота в режиме polling."""
        try:
            await self.application.updater.stop()
            await self.application.stop()
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
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')],
            [InlineKeyboardButton("📢 Наш канал", callback_data='channel')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        welcome_text = f"""👋 Привіт, {user.first_name}!

Ласкаво просимо до магазину **SecureShop**!

Тут ви можете:
▫️ Зробити замовлення підписок або цифрових товарів
▫️ Задати питання нашим менеджерам
▫️ Отримати інформацію про наші товари

Оберіть дію 👇"""
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

        # Обновляем статистику
        global bot_statistics
        if user.id not in [u for u in [OWNER_ID_1, OWNER_ID_2]]: # Простая проверка на новых пользователей
             bot_statistics['total_users'] += 1
             save_stats()

    async def order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /order"""
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

        keyboard = [
            [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
            [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("🛍️ Оберіть тип замовлення:", reply_markup=reply_markup)

    async def question_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /question"""
        user = update.effective_user
        user_id = user.id
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже ф активний діалог."
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
        global bot_statistics
        bot_statistics['total_questions'] += 1
        save_stats()

        await update.message.reply_text(
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
            "Щоб завершити цей діалог пізніше, використайте команду /stop."
        )

    async def channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /channel"""
        channel_link = "https://t.me/+KzJ3F1D00b0zZjFi" # Замените на вашу ссылку
        await update.message.reply_text(f"📢 Наш канал: {channel_link}")

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Показывает справку и информацию о сервисе"""
        # Универсальный метод для обработки команды и кнопки
        if isinstance(update, Update):
            message = update.message
        else:
            message = update # для вызова из кнопки

        user_id = message.from_user.id
        is_owner = user_id in [OWNER_ID_1, OWNER_ID_2]

        help_text = """👋 Доброго дня! Я бот магазину SecureShop.

🛒 **Як зробити замовлення:**
1. Натисніть кнопку "🛒 Зробити замовлення" або команду /order
2. Оберіть тип товару
3. Надішліть деталі замовлення
4. Оплатіть замовлення
5. Отримайте логіни та паролі

❓ **Як задати питання:**
1. Натисніть кнопку "❓ Поставити запитання" або команду /question
2. Напишіть ваше запитання
3. Очікуйте відповідь від нашого менеджера

💳 **Оплата:**
- Картка (ручна оплата)
- Криптовалюта (через NOWPayments)

📢 **Наш канал:** /channel - Наш канал з асортиментом, оновленнями та розіграшами

/stop - Завершити поточний діалог
/help - Ця довідка"""

        if is_owner:
            help_text += """\n\n🔐 **Команди основателя:**
/stats - Статистика бота
/history - Історія повідомлень клієнта
/chats - Активні чати
/clear - Очистити активні чати
/dialog - Продовжити діалог з клієнтом
/payout - Створити платіж для користувача"""

        help_text += "\n\n💬 Якщо у вас виникли питання, не соромтеся звертатися!"

        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.edit_message_text(help_text.strip(), parse_mode='Markdown')
        else:
            await message.reply_text(help_text.strip(), parse_mode='Markdown')

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop"""
        user_id = update.effective_user.id

        if user_id in active_conversations:
            # Удаляем из активных диалогов
            del active_conversations[user_id]
            # Удаляем из БД
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM active_conversations WHERE user_id = %s", (user_id,))
                    conn.commit()
                logger.info(f"🛑 Активный диалог для {user_id} завершен")
            except Exception as e:
                logger.error(f"❌ Ошибка удаления диалога из БД для {user_id}: {e}")

            await update.message.reply_text("✅ Ваш діалог успішно завершено. Дякуємо за звернення!")
        else:
            await update.message.reply_text("ℹ️ У вас немає активного діалогу для завершення.")

    # --- Добавленные методы для оплаты ---
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
                await update.message.reply_text(
                    "❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ... "
                    "або використовуйте цю команду після оформлення замовлення в боті."
                )
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
            items = []
            item_pattern = r'(\w+)-(\w+)-([\w$]+)-(\d+(?:\.\d+)?)'
            matches = re.findall(item_pattern, items_str)
            if not matches:
                await update.message.reply_text("❌ Не вдалося розпізнати товари у команді.")
                return

            for service_abbr, plan_abbr, period, price in matches:
                items.append({
                    'service': service_abbr,
                    'plan': plan_abbr,
                    'period': period,
                    'price': float(price)
                })

            # Формируем текст заказа
            order_text = f"🛍️ Замовлення #{order_id} (з команди /pay):\n"
            total_uah = 0.0
            for item in items:
                order_text += f"▫️ {item['service']} {item.get('plan', '')} ({item['period']}) - {item['price']} UAH\n"
                total_uah += item['price']
            order_text += f"💳 Всього: {total_uah} UAH"

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
        await update.message.reply_text(f"💳 Оберіть метод оплати для суми {usd_amount}$:", reply_markup=reply_markup)

    # --- Логика оплаты ---
    async def payment_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback кнопок, связанных с оплатой"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id # ID того, кто нажал кнопку
    data = query.data
    logger.info(f"📥 Callback received: data='{data}', from user_id={user_id}")

    # - Логика для /pay (для пользователя) -
    if data.startswith('pay_'):
        logger.info(f"➡️ Передача callback 'pay_' в существующую логику для пользователя {user_id}")
        # Проверяем, является ли пользователь основателем
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await query.edit_message_text("❌ У вас немає прав для цієї дії.")
            return

        # Проверяем, есть ли сумма в контексте
        usd_amount = context.user_data.get('payment_amount_usd')
        order_details = context.user_data.get('order_details_for_payment')
        if not usd_amount or not order_details:
            await query.edit_message_text("❌ Помилка: інформація про платіж втрачена. Спробуйте ще раз.")
            logger.error(f"⚠️ Данные pay потеряны для пользователя {user_id}")
            return

            # - Оплата карткой -
            if data == 'pay_card':
                # Создаем временную ссылку или просто сообщаем пользователю
                keyboard = [
                    [InlineKeyboardButton("✅ Оплачено", callback_data='manual_payment_confirmed')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_payment_methods')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"💳 Реквізити для оплати {usd_amount}$:\n"
                    f"`{CARD_DETAILS}`\n"
                    "(Тестові реквізити)\n\n"
                    "Після оплати натисніть кнопку '✅ Оплачено'.",
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
                logger.info(f"💳 Показаны реквизиты для карты pay пользователю {user_id}")

            # - Оплата криптовалютою -
            elif data == 'pay_crypto':
                # Отображаем список доступных криптовалют
                keyboard = []
                for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                    keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'pay_crypto_{currency_code}')])
                keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='back_to_payment_methods')])
                keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(f"🪙 Оберіть криптовалюту для оплати {usd_amount}$:", reply_markup=reply_markup)
                context.user_data.pop('nowpayments_invoice_id', None)
                logger.info(f"🪙 Показан выбор криптовалюты для pay пользователю {user_id}")

            # - Выбор конкретной криптовалюты для оплаты -
            elif data.startswith('pay_crypto_'):
                currency_code = data.split('_', 2)[2]
                currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == currency_code), currency_code.upper())

                try:
                    # Создаем инвойс в NOWPayments
                    invoice_data = await create_nowpayments_invoice(usd_amount, 'usd', currency_code)
                    invoice_id = invoice_data.get('id')
                    invoice_url = invoice_data.get('invoice_url')

                    if not invoice_id or not invoice_url:
                        raise Exception("NOWPayments не вернул ID или URL инвойса")

                    # Сохраняем ID инвойса в контексте
                    context.user_data['nowpayments_invoice_id'] = invoice_id

                    keyboard = [
                        [InlineKeyboardButton("🔗 Перейти до оплати", url=invoice_url)],
                        [InlineKeyboardButton("🔄 Перевірити статус", callback_data='check_payment_status')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_crypto_selection')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"🪙 Оплата {usd_amount}$ в {currency_name}:\n"
                        f"ID інвойсу: `{invoice_id}`\n\n"
                        "Натисніть кнопку нижче для переходу до оплати.",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    logger.info(f"🪙 NOWPayments инвойс создан для pay {user_id}: {invoice_id}")

                except Exception as e:
                    error_msg = f"❌ Помилка створення посилання для оплати: {e}"
                    await query.edit_message_text(error_msg)
                    logger.error(f"Ошибка создания инвойса NOWPayments для pay: {e}")

            # - Назад до вибору методу оплати -
            elif data == 'back_to_payment_methods':
                # Повторно отображаем выбор метода оплаты
                keyboard = [
                    [InlineKeyboardButton("💳 Оплата карткою", callback_data='pay_card')],
                    [InlineKeyboardButton("🪙 Криптовалюта", callback_data='pay_crypto')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(f"💳 Оберіть метод оплати для суми {usd_amount}$:", reply_markup=reply_markup)
                # Очищаем временные состояния
                context.user_data.pop('nowpayments_invoice_id', None)
                logger.info(f"⬅️ Назад к выбору метода оплаты для pay пользователя {user_id}")

            # - Назад до вибору криптовалют -
            elif data == 'back_to_crypto_selection':
                # Отображаем список доступных криптовалют
                keyboard = []
                for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                    keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'pay_crypto_{currency_code}')])
                keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='back_to_payment_methods')])
                keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(f"🪙 Оберіть криптовалюту для оплати {usd_amount}$:", reply_markup=reply_markup)
                context.user_data.pop('nowpayments_invoice_id', None)
                logger.info(f"⬅️ Назад к выбору криптовалюты для pay пользователя {user_id}")

            # - Перевірка статуса оплати -
            elif data == 'check_payment_status':
                invoice_id = context.user_data.get('nowpayments_invoice_id')
                if not invoice_id:
                    await query.edit_message_text("❌ Не знайдено ID інвойсу для перевірки.")
                    return

                try:
                    status_data = await get_nowpayments_invoice_status(invoice_id)
                    payment_status = status_data.get('payment_status', 'unknown')

                    if payment_status == 'finished':
                        # Оплата успешна
                        success_msg = f"✅ Оплата інвойсу `{invoice_id}` успішно завершена!\nДякуємо за покупку!"
                        keyboard = [
                            [InlineKeyboardButton("🛒 Продовжити", callback_data='request_account_data')],
                            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await query.edit_message_text(success_msg, parse_mode='Markdown', reply_markup=reply_markup)

                        # Переходим к сбору данных аккаунта
                        # await self.request_account_data(update, context) # Отложим до нажатия кнопки
                        logger.info(f"✅ Оплата инвойса {invoice_id} успешно завершена для пользователя {user_id}")

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
                        logger.info(f"⏳ Статус оплаты инвойса {invoice_id}: {payment_status} (проверка пользователем {user_id})")

                    else: # cancelled, expired, etc.
                        keyboard = [
                            [InlineKeyboardButton("💳 Інший метод оплати", callback_data='back_to_payment_methods')],
                            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
                            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await query.edit_message_text(
                            f'❌ Оплата не пройшла або була скасована. Статус: `{payment_status}`.',
                            parse_mode='Markdown',
                            reply_markup=reply_markup
                        )
                        logger.info(f"❌ Оплата инвойса {invoice_id} не удалась или отменена. Статус: {payment_status} (проверка пользователем {user_id})")

                except Exception as e:
                    error_msg = f"❌ Помилка перевірки статусу оплати: {e}"
                    await query.edit_message_text(error_msg)
                    logger.error(f"Ошибка проверки статуса NOWPayments для инвойса {invoice_id}: {e}")

            # - Ручне підтвердження оплати -
            elif data == 'manual_payment_confirmed':
                # Здесь можно добавить логику проверки оплаты вручную или просто перейти к следующему шагу
                await query.edit_message_text("✅ Оплата підтверджена вручну.")
                # Переходим к сбору данных аккаунта
                await self.request_account_data(update, context)
                logger.info(f"✅ Ручное подтверждение оплаты pay для пользователя {user_id}")

        # - Логика для /payout -
        elif data.startswith('payout_'):
        logger.info(f"🎯 Начало обработки payout callback: '{data}' от пользователя {user_id}")
        # Проверяем, является ли пользователь основателем
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await query.edit_message_text("❌ У вас немає прав для цієї дії.")
            return

            # Извлекаем данные из контекста
            target_user_id = context.user_data.get('payout_target_user_id')
            # uah_amount = context.user_data.get('payout_amount_uah') # Не используется напрямую
            usd_amount = context.user_data.get('payout_amount_usd')

            if not target_user_id or not usd_amount:
                await query.edit_message_text("❌ Помилка: інформація про платіж втрачена. Спробуйте ще раз.")
                logger.error(f"⚠️ Данные payout потеряны для пользователя {user_id}")
                return

            # - Оплата карткою -
            if data == 'payout_card':
                # Создаем временную ссылку или просто сообщаем пользователю
                keyboard = [
                    [InlineKeyboardButton("✅ Оплачено", callback_data='payout_manual_payment_confirmed')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                    [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"💳 Реквізити для оплати {usd_amount}$ користувачу <@{target_user_id}>:\n"
                    f"`{CARD_DETAILS}`\n"
                    "(Тестові реквізити)\n\n"
                    "Після оплати натисніть кнопку '✅ Оплачено'.",
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
                logger.info(f"💳 Показаны реквизиты для карты payout пользователю {user_id}")

            # - Оплата криптовалютою -
            elif data == 'payout_crypto':
                # Отображаем список доступных криптовалют
                keyboard = []
                for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                    keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'payout_crypto_{currency_code}')])
                keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')])
                keyboard.append([InlineKeyboardButton("❓ Запитання", callback_data='question')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(f"🪙 Оберіть криптовалюту для оплати {usd_amount}$ користувачу <@{target_user_id}>:", reply_markup=reply_markup)
                logger.info(f"🪙 Показан выбор криптовалюты для payout пользователю {user_id}")

            # - Выбор конкретной криптовалюты для оплаты -
            elif data.startswith('payout_crypto_'):
                currency_code = data.split('_', 2)[2]
                currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == currency_code), currency_code.upper())

                try:
                    # Создаем инвойс в NOWPayments
                    invoice_data = await create_nowpayments_invoice(usd_amount, 'usd', currency_code)
                    invoice_id = invoice_data.get('id')
                    invoice_url = invoice_data.get('invoice_url')

                    if not invoice_id or not invoice_url:
                        raise Exception("NOWPayments не вернул ID или URL инвойса")

                    # Сохраняем ID инвойса в контексте
                    context.user_data['payout_nowpayments_invoice_id'] = invoice_id

                    keyboard = [
                        [InlineKeyboardButton("🔗 Перейти до оплати", url=invoice_url)],
                        [InlineKeyboardButton("🔄 Перевірити статус", callback_data='payout_check_payment_status')],
                        [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                        [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(
                        f"🪙 Оплата {usd_amount}$ в {currency_name} користувачу <@{target_user_id}>:\n"
                        f"ID інвойсу: `{invoice_id}`\n\n"
                        "Натисніть кнопку нижче для переходу до оплати.",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    logger.info(f"🪙 NOWPayments инвойс создан для payout {user_id}: {invoice_id}")

                except Exception as e:
                    error_msg = f"❌ Помилка створення посилання для оплати: {e}"
                    await query.edit_message_text(error_msg)
                    logger.error(f"Ошибка создания инвойса NOWPayments для payout: {e}")

            # - Перевірка статуса оплати -
            elif data == 'payout_check_payment_status':
                invoice_id = context.user_data.get('payout_nowpayments_invoice_id')
                if not invoice_id:
                    await query.edit_message_text("❌ Не знайдено ID інвойсу для перевірки.")
                    return

                try:
                    status_data = await get_nowpayments_invoice_status(invoice_id)
                    payment_status = status_data.get('payment_status', 'unknown')

                    if payment_status == 'finished':
                        # Оплата успешна
                        success_msg = f"✅ Оплата інвойсу `{invoice_id}` успішно завершена!\nДякуємо за покупку!"
                        keyboard = [
                            [InlineKeyboardButton("✅ Підтвердити вручну", callback_data='payout_manual_payment_confirmed')],
                            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await query.edit_message_text(
                            success_msg + "\n\n❗ Для завершення процесу, підтвердіть оплату вручну.",
                            parse_mode='Markdown',
                            reply_markup=reply_markup
                        )
                        # Очищаем контекст после успешной оплаты
                        # context.user_data.pop('payout_target_user_id', None)
                        # context.user_data.pop('payout_amount_uah', None)
                        # context.user_data.pop('payout_amount_usd', None)
                        # context.user_data.pop('payout_nowpayments_invoice_id', None)
                        logger.info(f"✅ Оплата инвойса {invoice_id} успешно завершена по проверке статуса пользователем {user_id}")

                    elif payment_status in ['waiting', 'confirming', 'confirmed']:
                        keyboard = [
                            [InlineKeyboardButton("🔄 Перевірити ще раз", callback_data='payout_check_payment_status')],
                            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await query.edit_message_text(
                            f"⏳ Статус оплати: {payment_status}. Будь ласка, зачекайте або перевірте ще раз.",
                            reply_markup=reply_markup
                        )
                        logger.info(f"⏳ Статус оплаты инвойса {invoice_id}: {payment_status} (проверка пользователем {user_id})")

                    else: # cancelled, expired, partially_paid, etc.
                        keyboard = [
                            [InlineKeyboardButton("💳 Інший метод оплати", callback_data='payout_cancel')], # Просто отмена, можно добавить повторный выбор
                            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
                            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        fail_msg = f"❌ Оплата не пройшла або була скасована. Статус: `{payment_status}`."
                        await query.edit_message_text(fail_msg, parse_mode='Markdown', reply_markup=reply_markup)
                        logger.info(f"❌ Оплата инвойса {invoice_id} не удалась или отменена. Статус: {payment_status} (проверка пользователем {user_id})")

                except Exception as e:
                    error_msg = f"❌ Помилка перевірки статусу оплати: {e}"
                    await query.edit_message_text(error_msg)
                    logger.error(f"Ошибка проверки статуса NOWPayments для payout инвойса {invoice_id}: {e}")

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
                logger.info(f"✅ Ручное подтверждение оплаты payout для пользователя {user_id}")

            # - Отмена payout -
            elif data == 'payout_cancel':
                await query.edit_message_text("↩️ Створення платежу скасовано.")
                # Очищаем контекст
                context.user_data.pop('payout_target_user_id', None)
                context.user_data.pop('payout_amount_uah', None)
                context.user_data.pop('payout_amount_usd', None)
                context.user_data.pop('payout_nowpayments_invoice_id', None)
                logger.info(f"↩️ Payout отменен пользователем {user_id}")

    async def request_account_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрашивает у пользователя логин и пароль от аккаунта"""
        query = update.callback_query
        user_id = query.from_user.id if update.callback_query else update.effective_user.id

        keyboard = [
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Отправляем запрос
        request_text = "🔒 Для отримання товару, будь ласка, надішліть логін та пароль від вашого аккаунта.\n" \
                       "Наприклад: `login:password` або `email:password`"
        if query:
            await query.edit_message_text(request_text, parse_mode='Markdown', reply_markup=reply_markup)
        else:
            await update.message.reply_text(request_text, parse_mode='Markdown', reply_markup=reply_markup)

    # --- Обработка оплаты после выбора товара ---
    async def proceed_to_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка оплаты после выбора товара"""
        query = update.callback_query
        user_id = query.from_user.id

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

    # --- Обработчик callback кнопок ---
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id
        data = query.data

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        logger.info(f"📥 Callback received: data='{data}', from user_id={user_id}")

        # --- Обработка кнопок главного меню ---
        if data == 'order':
            # Проверяем активные диалоги
            if user_id in active_conversations:
                await query.answer("❗ У вас вже є активний діалог.", show_alert=True)
                return

            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("🛍️ Оберіть тип замовлення:", reply_markup=reply_markup)

        elif data == 'question':
            # Проверяем активные диалоги
            if user_id in active_conversations:
                await query.answer("❗ У вас вже є активний діалог."
                                   "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                                   "якщо хочете почати новий діалог.", show_alert=True)
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
            global bot_statistics
            bot_statistics['total_questions'] += 1
            save_stats()

            await query.edit_message_text(
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
                "Щоб завершити цей діалог пізніше, використайте команду /stop."
            )

        elif data == 'help':
            await self.show_help(query, context)

        elif data == 'channel':
            channel_link = "https://t.me/+KzJ3F1D00b0zZjFi" # Замените на вашу ссылку
            await query.edit_message_text(f"📢 Наш канал: {channel_link}")

        elif data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')],
                [InlineKeyboardButton("📢 Наш канал", callback_data='channel')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            welcome_text = f"""👋 Привіт, {user.first_name}!

Ласкаво просимо до магазину **SecureShop**!

Тут ви можете:
▫️ Зробити замовлення підписок або цифрових товарів
▫️ Задати питання нашим менеджерам
▫️ Отримати інформацію про наші товари

Оберіть дію 👇"""
            await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

        # --- Обработка кнопок заказа ---
        elif data == 'order_subscriptions':
            await query.edit_message_text(
                "📝 Напишіть деталі замовлення підписки.\n"
                "Наприклад: `Discord Full (1 місяць) - 170 UAH`",
                parse_mode='Markdown'
            )
            # Создаем запись о заказе
            active_conversations[user_id] = {
                'type': 'subscription_order',
                'user_info': user,
                'assigned_owner': None,
                'last_message': "Нове замовлення підписки"
            }
            # Сохраняем в БД
            save_active_conversation(user_id, 'subscription_order', None, "Нове замовлення підписки")

        elif data == 'order_digital':
            await query.edit_message_text(
                "📝 Напишіть деталі замовлення цифрового товару.\n"
                "Наприклад: `Discord Прикраси (Boost Nitro) - 180 UAH`",
                parse_mode='Markdown'
            )
            # Создаем запись о заказе
            active_conversations[user_id] = {
                'type': 'digital_order',
                'user_info': user,
                'assigned_owner': None,
                'last_message': "Нове замовлення цифрового товару"
            }
            # Сохраняем в БД
            save_active_conversation(user_id, 'digital_order', None, "Нове замовлення цифрового товару")

        # --- Обработка оплаты после выбора товара ---
        elif data == 'proceed_to_payment':
            await self.proceed_to_payment(update, context)

        # --- Добавленные/обновлённые вспомогательные функции ---
        elif data == 'request_account_data':
            await self.request_account_data(update, context)

    # --- Обработчик текстовых сообщений ---
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

        # Проверяем существование диалога
        if user_id not in active_conversations:
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "Будь ласка, оберіть дію або використайте /start, щоб розпочати.",
                reply_markup=reply_markup
            )
            return

        # Сохраняем последнее сообщение
        active_conversations[user_id]['last_message'] = message_text
        # Сохраняем сообщение от пользователя
        save_message(user_id, message_text, True)
        # Обновляем активный диалог в БД
        save_active_conversation(
            user_id,
            active_conversations[user_id]['type'],
            active_conversations[user_id].get('assigned_owner'),
            message_text
        )

        # Логика обработки сообщений в зависимости от типа диалога
        conversation_type = active_conversations[user_id]['type']

        if conversation_type in ['subscription_order', 'digital_order']:
            # Обработка заказа
            order_text = message_text
            # Извлекаем сумму в UAH
            uah_amount = get_uah_amount_from_order_text(order_text)
            if uah_amount <= 0:
                 # Простая логика определения суммы
                 match_price = re.search(r'(\d+(?:\.\d+)?)\s*(UAH|грн)', order_text, re.IGNORECASE)
                 if match_price:
                     uah_amount = float(match_price.group(1))
                 else:
                     await update.message.reply_text("❌ Не вдалося визначити суму для оплати. Вкажіть суму в UAH.")
                     return

            # Конвертируем в USD
            usd_amount = convert_uah_to_usd(uah_amount)
            if usd_amount <= 0:
                await update.message.reply_text("❌ Сума для оплати занадто мала.")
                return

            # Сохраняем детали заказа и сумму в контексте
            active_conversations[user_id]['order_details'] = order_text
            context.user_data['payment_amount_usd'] = usd_amount
            context.user_data['order_details_for_payment'] = order_text

            # Обновляем статистику
            global bot_statistics
            bot_statistics['total_orders'] += 1
            save_stats()

            # Сохраняем в БД
            save_active_conversation(user_id, conversation_type, None, order_text)

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

        elif conversation_type == 'question':
            # Пересылаем вопрос основателям
            await self.forward_question_to_owners(context, user_id, user, message_text)
            await update.message.reply_text("✅ Ваше запитання надіслано. Очікуйте відповіді від нашого менеджера.")

    async def handle_json_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик JSON файлов с заказами"""
        user = update.effective_user
        user_id = user.id

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог."
                "Будь ласка, завершіть його командою /stop, "
                "якщо хочете зробити нове замовлення з файлу."
            )
            return

        # Получаем файл
        file = await update.message.document.get_file()
        file_path = file.file_path

        # Скачиваем содержимое
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        response = requests.get(url)
        if response.status_code != 200:
            logger.error(f"Не вдалося завантажити файл: {response.status_code}")
            await update.message.reply_text("❌ Не вдалося обробити файл. Спробуйте ще раз.")
            return

        try:
            order_data = response.json()
        except Exception as e:
            logger.error(f"Помилка парсингу JSON: {e}")
            await update.message.reply_text("❌ Файл має неправильний формат JSON.")
            return

        # Проверяем структуру заказа
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
        global bot_statistics
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

    # --- Команды для основателей ---
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stats для основателей"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            return # Игнорируем для обычных пользователей

        # Загружаем актуальную статистику из БД
        load_stats()

        stats_text = f"""📈 **Статистика бота SecureShop:**

👥 Користувачів: {bot_statistics['total_users']}
🛒 Замовлень: {bot_statistics['total_orders']}
❓ Запитань: {bot_statistics['total_questions']}
⏰ Останнє скидання: {bot_statistics['last_reset']}"""
        await update.message.reply_text(stats_text, parse_mode='Markdown')

    async def show_active_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает активные чаты для основателей с кнопкой продолжить"""
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        try:
            with get_db_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("""
                        SELECT ac.*, u.first_name, u.username
                        FROM active_conversations ac
                        JOIN users u ON ac.user_id = u.id
                        ORDER BY ac.updated_at DESC
                    """)
                    active_chats = cur.fetchall()

                    if not active_chats:
                        await update.message.reply_text("ℹ️ Нет активных чатов.")
                        return

                    message = "🔄 Активные чаты:"
                    keyboard = []
                    for chat in active_chats:
                        # Получаем актуальную информацию о пользователе
                        client_info = {'first_name': chat['first_name'] or 'Неизвестный', 'username': chat['username'] or 'N/A'}
                        # Добавим информацию о назначенном владельце
                        owner_info = "Не назначен"
                        if chat['assigned_owner']:
                            if chat['assigned_owner'] == OWNER_ID_1:
                                owner_info = "@HiGki2pYYY"
                            elif chat['assigned_owner'] == OWNER_ID_2:
                                owner_info = "@oc33t"
                            else:
                                owner_info = f"ID:{chat['assigned_owner']}"

                        message += f"\n\n▫️ **Клієнт:** {client_info['first_name']} (@{client_info['username']})"
                        message += f"\n  ▫️ Тип: {chat['type']}"
                        message += f"\n  ▫️ Призначено: {owner_info}"
                        message += f"\n  ▫️ Останнє повідомлення: {chat['last_message'][:50]}..."
                        # Добавляем кнопку "Продолжить"
                        keyboard.append([InlineKeyboardButton(
                            f"Продовжити діалог з {client_info['first_name']}",
                            callback_data=f'continue_chat_{chat["user_id"]}'
                        )])
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(message.strip(), reply_markup=reply_markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"❌ Ошибка получения активных чатов: {e}")
            await update.message.reply_text("❌ Произошла ошибка при получении активных чатов.")

    async def show_conversation_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает историю переписки с пользователем"""
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        if not context.args:
            await update.message.reply_text("❌ Вкажіть ID користувача. Наприклад: /history 123456789")
            return

        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Неправильний формат ID користувача.")
            return

        try:
            with get_db_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    # Получаем историю сообщений
                    cur.execute("""
                        SELECT * FROM message_history
                        WHERE user_id = %s
                        ORDER BY timestamp ASC
                    """, (target_user_id,))
                    messages = cur.fetchall()

                    if not messages:
                        await update.message.reply_text(f"ℹ️ Історія повідомлень для користувача {target_user_id} порожня.")
                        return

                    # Получаем информацию о пользователе
                    cur.execute("SELECT first_name, username FROM users WHERE id = %s", (target_user_id,))
                    user_info = cur.fetchone()
                    user_display = f"{user_info['first_name']} (@{user_info['username']})" if user_info else f"ID:{target_user_id}"

                    history_text = f"📖 **Історія переписки з {user_display}:**\n\n"
                    for msg in messages:
                        sender = "Клієнт" if msg['is_from_user'] else "Менеджер"
                        timestamp = msg['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                        history_text += f"**[{timestamp}] {sender}:** {msg['message']}\n"

                    # Разбиваем длинный текст, если нужно
                    if len(history_text) > 4096:
                        parts = [history_text[i:i+4096] for i in range(0, len(history_text), 4096)]
                        for part in parts:
                            await update.message.reply_text(part, parse_mode='Markdown')
                    else:
                        await update.message.reply_text(history_text, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"❌ Ошибка получения истории для {target_user_id}: {e}")
            await update.message.reply_text("❌ Произошла ошибка при получении истории.")

    async def clear_active_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Очищает все активные диалоги (БД)"""
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM active_conversations")
                    conn.commit()
                    # Также очищаем локальный кэш
                    active_conversations.clear()
            await update.message.reply_text("✅ Всі активні діалоги очищено.")
            logger.info(f"🗑️ Активные диалоги очищены администратором {owner_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка очистки активных диалогов админом {owner_id}: {e}")
            await update.message.reply_text("❌ Помилка при очищенні активних діалогів.")

    async def continue_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /dialog для основателей"""
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        if not context.args:
            await update.message.reply_text("❌ Вкажіть ID користувача. Наприклад: /dialog 123456789")
            return

        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Неправильний формат ID користувача.")
            return

        try:
            with get_db_connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    # Проверяем, существует ли пользователь
                    cur.execute("SELECT first_name, username FROM users WHERE id = %s", (target_user_id,))
                    user_info = cur.fetchone()
                    if not user_info:
                        await update.message.reply_text("❌ Користувача з таким ID не знайдено.")
                        return

                    # Проверяем, есть ли активный диалог
                    cur.execute("SELECT type, last_message FROM active_conversations WHERE user_id = %s", (target_user_id,))
                    conversation = cur.fetchone()
                    if not conversation:
                        # Создаем новый диалог, если его нет
                        conv_type = 'manual_dialog' # Тип для ручного начала диалога
                        last_message = "Діалог розпочато вручну адміністратором"
                        cur.execute("""
                            INSERT INTO active_conversations (user_id, type, assigned_owner, last_message, updated_at)
                            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (user_id) DO UPDATE SET
                                type = EXCLUDED.type,
                                assigned_owner = EXCLUDED.assigned_owner,
                                last_message = EXCLUDED.last_message,
                                updated_at = CURRENT_TIMESTAMP
                        """, (target_user_id, conv_type, owner_id, last_message))
                        conn.commit()

                        # Также обновляем локальный кэш
                        active_conversations[target_user_id] = {
                            'type': conv_type,
                            'user_info': User(id=target_user_id, first_name=user_info['first_name'], username=user_info['username']),
                            'assigned_owner': owner_id,
                            'last_message': last_message
                        }

                    else:
                        # Если диалог есть, обновляем владельца
                        cur.execute("""
                            UPDATE active_conversations
                            SET assigned_owner = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = %s
                        """, (owner_id, target_user_id))
                        conn.commit()

                        # Также обновляем локальный кэш
                        if target_user_id in active_conversations:
                            active_conversations[target_user_id]['assigned_owner'] = owner_id

                    user_display = f"{user_info['first_name']} (@{user_info['username']})" if user_info else f"ID:{target_user_id}"
                    await update.message.reply_text(f"✅ Ви почали діалог з {user_display}. Тепер всі повідомлення від цього користувача будуть пересилатися вам.")

                    # Устанавливаем связь владелец-клиент
                    owner_client_map[owner_id] = target_user_id

        except Exception as e:
            logger.error(f"❌ Ошибка продолжения диалога с {target_user_id} для админа {owner_id}: {e}")
            await update.message.reply_text("❌ Помилка при спробі продовжити діалог.")

    async def payout_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /payout для основателей"""
        logger.info("📥 /payout команда получена")
        user = update.effective_user
        user_id = user.id

        # Проверяем, является ли пользователь основателем
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає прав для цієї команди.")
            return

        # Проверяем аргументы команды
        if len(context.args) < 2:
            await update.message.reply_text("❌ Неправильний формат команди. Використовуйте: /payout <user_id> <amount_uah>")
            return

        try:
            target_user_id = int(context.args[0])
            uah_amount = float(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Неправильний формат ID користувача або суми.")
            return

        if uah_amount <= 0:
            await update.message.reply_text("❌ Сума повинна бути більше 0.")
            return

        # Конвертируем в USD
        usd_amount = convert_uah_to_usd(uah_amount)
        if usd_amount <= 0:
            await update.message.reply_text("❌ Сума для оплати занадто мала.")
            return

        # Сохраняем данные в контексте
        # - Исправлено: сохраняем target_user_id -
        context.user_data['payout_target_user_id'] = target_user_id
        # -context.user_data['payout_amount_uah'] = uah_amount
        context.user_data['payout_amount_usd'] = usd_amount
        logger.info(f"💾 /payout: Данные сохранены в context.user_data для {user_id}")

        # Предлагаем выбрать метод оплаты
        keyboard = [
            [InlineKeyboardButton("💳 Оплата карткою", callback_data='payout_card')],
            [InlineKeyboardButton("🪙 Криптовалюта", callback_data='payout_crypto')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='payout_cancel')],
            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"💳 Створення платежу для користувача <@{target_user_id}> на суму {usd_amount}$.\n"
            "Оберіть метод оплати:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    # --- Вспомогательные функции для владельцев ---
    async def forward_question_to_owners(self, context: ContextTypes.DEFAULT_TYPE, client_id: int, client_info: User, question_text: str):
        """Пересылает вопрос основателям"""
        bot = context.bot
        client_name = client_info.first_name or "Клієнт"
        message_to_send = f"❓ **Нове запитання від {client_name} (ID: {client_id}):**\n\n{question_text}"

        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                keyboard = [
                    [InlineKeyboardButton("💬 Відповісти", callback_data=f'reply_to_{client_id}')],
                    [InlineKeyboardButton("👤 Історія", callback_data=f'history_{client_id}')],
                    [InlineKeyboardButton("📌 Призначити собі", callback_data=f'assign_{client_id}')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await bot.send_message(chat_id=owner_id, text=message_to_send, parse_mode='Markdown', reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"❌ Не удалось отправить вопрос основателю {owner_id}: {e}")

    async def forward_order_to_owners(self, context: ContextTypes.DEFAULT_TYPE, client_id: int, client_info: User, order_text: str):
        """Пересылает заказ основателям"""
        bot = context.bot
        client_name = client_info.first_name or "Клієнт"
        message_to_send = f"🛍️ **Нове замовлення від {client_name} (ID: {client_id}):**\n\n{order_text}"

        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                keyboard = [
                    [InlineKeyboardButton("💬 Відповісти", callback_data=f'reply_to_{client_id}')],
                    [InlineKeyboardButton("👤 Історія", callback_data=f'history_{client_id}')],
                    [InlineKeyboardButton("📌 Призначити собі", callback_data=f'assign_{client_id}')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await bot.send_message(chat_id=owner_id, text=message_to_send, parse_mode='Markdown', reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"❌ Не удалось отправить заказ основателю {owner_id}: {e}")

    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик сообщений от владельцев"""
        owner_id = update.effective_user.id
        message_text = update.message.text

        # Проверяем, назначен ли этот владелец к какому-то клиенту
        target_client_id = owner_client_map.get(owner_id)

        if not target_client_id:
            await update.message.reply_text(
                "ℹ️ Ви не призначені до жодного клієнта. Використовуйте /chats або /dialog <user_id>."
            )
            return

        try:
            # Отправляем сообщение клиенту
            await context.bot.send_message(
                chat_id=target_client_id,
                text=f"👤 **Менеджер:** {message_text}",
                parse_mode='Markdown'
            )
            # Сохраняем сообщение от владельца
            save_message(target_client_id, message_text, False)
            await update.message.reply_text("✅ Повідомлення надіслано клієнту.")
        except BadRequest as e:
            if "Chat not found" in str(e) or "bot was blocked by the user" in str(e):
                await update.message.reply_text("❌ Клієнт заблокував бота або чат не знайдено.")
                # Удаляем из активных диалогов
                if target_client_id in active_conversations:
                    del active_conversations[target_client_id]
                try:
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM active_conversations WHERE user_id = %s", (target_client_id,))
                        conn.commit()
                except Exception as db_e:
                    logger.error(f"❌ Ошибка удаления диалога из БД для {target_client_id}: {db_e}")
                # Удаляем из owner_client_map
                if owner_id in owner_client_map:
                    del owner_client_map[owner_id]
            else:
                logger.error(f"BadRequest при отправке сообщения клиенту {target_client_id}: {e}")
                await update.message.reply_text("❌ Помилка при надсиланні повідомлення клієнту.")
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения клиенту {target_client_id}: {e}")
            await update.message.reply_text("❌ Помилка при надсиланні повідомлення клієнту.")

    # --- Обработчик ошибок ---
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.warning(f'Update {update} caused error {context.error}')

# --- Webhook handlers ---
@flask_app.route('/webhook', methods=['POST'])
async def webhook():
    """Обработчик вебхука Telegram"""
    if telegram_app:
        json_string = request.get_data().decode('utf-8')
        update = Update.de_json(json.loads(json_string), telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({'status': 'ok'})
    else:
        return jsonify({'status': 'bot not initialized'}), 500

@flask_app.route('/nowpayments-ipn', methods=['POST'])
async def nowpayments_ipn():
    """Обработчик IPN уведомлений от NOWPayments"""
    data = await request.get_json()
    logger.info(f"📥 NOWPayments IPN received: {data}")
    # Здесь можно добавить логику обработки уведомлений
    return jsonify({'status': 'ok'})

# --- Функции запуска ---
async def setup_webhook():
    """Настраивает вебхук для бота"""
    try:
        if not WEBHOOK_URL:
            logger.error("❌ WEBHOOK_URL не установлен")
            return False

        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            max_connections=40,
            drop_pending_updates=True
        )
        logger.info(f"✅ Вебхук установлен: {WEBHOOK_URL}/webhook")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка установки вебхука: {e}")
        return False

async def start_bot():
    """Инициализирует и запускает бота"""
    global telegram_app, bot_running
    try:
        # Инициализируем БД
        init_db()
        # Загружаем статистику
        load_stats()

        # Создаем экземпляр бота
        bot_instance = TelegramBot(BOT_TOKEN)
        await bot_instance.initialize()
        telegram_app = bot_instance.application

        if USE_POLLING:
            await setup_webhook() # На случай, если вдруг переменная не та
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
    """Запускает бота в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_instance = None
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

def start_flask():
    """Запускает Flask приложение"""
    flask_app.run(host='0.0.0.0', port=WEBHOOK_PORT)

if __name__ == '__main__':
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.start()
    logger.info("✅ Flask сервер запущен")

    # Запускаем бота в основном потоке
    bot_thread()
