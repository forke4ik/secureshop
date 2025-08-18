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
import products  # Импортируем файл с ассортиментом

# Настройка логирования: выводим только WARNING и выше для библиотек, INFO для нашего кода
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

bot_running = False
bot_lock = threading.Lock()
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
OWNER_ID_1 = int(os.getenv('OWNER_ID_1', '7106925462'))
OWNER_ID_2 = int(os.getenv('OWNER_ID_2', '6279578957'))
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://your-app-url.onrender.com')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840))
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://user:password@host:port/dbname')
STATS_FILE = "bot_stats.json"
BUFFER_FLUSH_INTERVAL = 300
BUFFER_MAX_SIZE = 50
message_buffer = []
active_conv_buffer = []
user_cache = set()
history_cache = {}

# Конфигурация оплаты
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY', 'YOUR_NOWPAYMENTS_API_KEY_HERE')
CARD_NUMBER = os.getenv('CARD_NUMBER', '5355 2800 4715 6045')
EXCHANGE_RATE_UAH_TO_USD = float(os.getenv('EXCHANGE_RATE_UAH_TO_USD', '41.26'))  # Курс UAH к USD

# Доступные криптовалюты для оплаты
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

def flush_message_buffer():
    global message_buffer
    if not message_buffer:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TEMP TABLE temp_messages (
                        user_id BIGINT,
                        message TEXT,
                        is_from_user BOOLEAN
                    ) ON COMMIT DROP;
                """)
                for msg in message_buffer:
                    cur.execute("INSERT INTO temp_messages (user_id, message, is_from_user) VALUES (%s, %s, %s)", msg)
                cur.execute("""
                    INSERT INTO messages (user_id, message, is_from_user)
                    SELECT user_id, message, is_from_user
                    FROM temp_messages
                """)
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
                    CREATE TEMP TABLE temp_active_convs (
                        user_id BIGINT,
                        conversation_type VARCHAR(50),
                        assigned_owner BIGINT,
                        last_message TEXT
                    ) ON COMMIT DROP;
                """)
                for conv in latest_convs.values():
                    cur.execute("INSERT INTO temp_active_convs VALUES (%s, %s, %s, %s)", conv)
                cur.execute("""
                    INSERT INTO active_conversations AS ac
                    (user_id, conversation_type, assigned_owner, last_message)
                    SELECT user_id, conversation_type, assigned_owner, last_message
                    FROM temp_active_convs
                    ON CONFLICT (user_id) DO UPDATE
                    SET conversation_type = EXCLUDED.conversation_type,
                        assigned_owner = EXCLUDED.assigned_owner,
                        last_message = EXCLUDED.last_message,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE ac.updated_at < EXCLUDED.updated_at;
                """)
    except Exception as e:
        logger.error(f"❌ Ошибка сброса буфера диалогов: {e}")
    finally:
        active_conv_buffer = []

def buffer_flush_thread():
    while True:
        time.sleep(BUFFER_FLUSH_INTERVAL)
        flush_message_buffer()
        flush_active_conv_buffer()

def init_db():
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
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации базы данных: {e}")

def ensure_user_exists(user):
    if user.id in user_cache:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, first_name, last_name, language_code, is_bot)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                    SET username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        language_code = EXCLUDED.language_code,
                        is_bot = EXCLUDED.is_bot,
                        updated_at = CURRENT_TIMESTAMP;
                """, (user.id, user.username, user.first_name, user.last_name, user.language_code, user.is_bot))
        user_cache.add(user.id)
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения пользователя: {e}")

def save_message(user_id, message_text, is_from_user):
    global message_buffer
    message_buffer.append((user_id, message_text, is_from_user))
    if len(message_buffer) >= BUFFER_MAX_SIZE:
        flush_message_buffer()

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message):
    global active_conv_buffer
    updated = False
    for i, record in enumerate(active_conv_buffer):
        if record[0] == user_id:
            active_conv_buffer[i] = (user_id, conversation_type, assigned_owner, last_message)
            updated = True
            break
    if not updated:
        active_conv_buffer.append((user_id, conversation_type, assigned_owner, last_message))
    if len(active_conv_buffer) >= BUFFER_MAX_SIZE:
        flush_active_conv_buffer()

def delete_active_conversation(user_id):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations WHERE user_id = %s", (user_id,))
    except Exception as e:
        logger.error(f"❌ Ошибка удаления активного диалога для {user_id}: {e}")

def get_conversation_history(user_id, limit=50):
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
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Ошибка получения пользователей: {e}")
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

init_db()
threading.Thread(target=buffer_flush_thread, daemon=True).start()

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки статистики: {e}")
            return default_stats()
    return default_stats()

def default_stats():
    return {
        'total_users': 0,
        'active_users': [],
        'total_orders': 0,
        'total_questions': 0,
        'first_start': datetime.now().isoformat(),
        'last_save': datetime.now().isoformat()
    }

def save_stats():
    try:
        bot_statistics['last_save'] = datetime.now().isoformat()
        with open(STATS_FILE, 'w') as f:
            json.dump(bot_statistics, f, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения статистики: {e}")

bot_statistics = load_stats()
active_conversations = {}
owner_client_map = {}
telegram_app = None
flask_app = Flask(__name__)
CORS(flask_app)

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
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
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("order", self.order_command))
        self.application.add_handler(CommandHandler("question", self.question_command))
        self.application.add_handler(CommandHandler("chats", self.show_active_chats))
        self.application.add_handler(CommandHandler("history", self.show_conversation_history))
        self.application.add_handler(CommandHandler("dialog", self.start_dialog_command))
        self.application.add_handler(CommandHandler("pay", self.pay_command))
        self.application.add_handler(CommandHandler("clear", self.clear_active_conversations_command))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.application.add_error_handler(self.error_handler)

    async def initialize(self):
        try:
            await self.application.initialize()
            await self.set_commands_menu()
            self.initialized = True
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации Telegram Application: {e}")
            raise

    async def start_polling(self):
        try:
            if self.application.updater.running:
                return
            await self.application.start()
            await self.application.updater.start_polling(
                poll_interval=1.0, timeout=10, bootstrap_retries=-1,
                read_timeout=10, write_timeout=10, connect_timeout=10, pool_timeout=10
            )
        except Conflict as e:
            logger.error(f"🚨 Конфликт: {e}")
            await asyncio.sleep(15)
            await self.start_polling()
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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        ensure_user_exists(user)
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            owner_name = "@HiGki2pYYY" if user.id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(f"Добро пожаловать, {user.first_name}! ({owner_name})\nВы вошли как основатель магазина.")
            return
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        welcome_message = f"Ласкаво просимо, {user.first_name}! 👋\nЯ бот-помічник нашого магазину. Будь ласка, оберіть, що вас цікавить:"
        await update.message.reply_text(welcome_message.strip(), reply_markup=reply_markup)

    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
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
        
        # Обработка заказа из мини-приложения
        order_text = f"🛍️ Замовлення з сайту (#{order_id}):\n"
        total = 0
        order_details = []
        for item in items:
            service_abbr = item[0]
            plan_abbr = item[1]
            period = item[2].strip()
            price = item[3]
            service_name = products.SERVICE_MAP.get(service_abbr, service_abbr)
            plan_name = products.PLAN_MAP.get(plan_abbr, plan_abbr)
            try:
                price_num = int(price)
                total += price_num
                order_type = 'digital_order' if service_abbr == 'DisU' else 'subscription_order'
                display_period = period.replace("м", "міс.").replace(" ", "")
                item_text = f"▫️ {service_name} {plan_name} ({display_period}) - {price_num} UAH\n"
                order_text += item_text
                order_details.append({
                    'service': service_name,
                    'plan': plan_name,
                    'period': display_period,
                    'price': price_num
                })
            except ValueError:
                continue
        if total == 0:
            await update.message.reply_text("❌ Не вдалося обробити замовлення. Перевірте формат товарів.")
            return
        order_text += f"\n💳 Всього: {total} UAH"
        conversation_type = 'digital_order' if any(item[0] == 'DisU' for item in items) else 'subscription_order'
        
        # Сохраняем информацию о заказе для последующего использования
        context.user_data['pending_payment'] = {
            'order_id': order_id,
            'items': order_details,
            'total_uah': total,
            'total_usd': round(total / EXCHANGE_RATE_UAH_TO_USD, 2)
        }
        
        # Отображаем кнопки оплаты
        keyboard = [
            [InlineKeyboardButton("💳 Оплата по карте", callback_data=f'pay_card_{total}')],
            [InlineKeyboardButton("₿ Оплата криптовалютой", callback_data=f'pay_crypto_{total}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"{order_text}\n\nВыберите способ оплаты:",
            reply_markup=reply_markup
        )
        
        # Сохраняем активный диалог (не обязательно, но можно)
        # active_conversations[user_id] = {
        #     'type': conversation_type,
        #     'user_info': user,
        #     'assigned_owner': None,
        #     'order_details': order_text,
        #     'last_message': order_text,
        #     'from_website': True
        # }
        # save_active_conversation(user_id, conversation_type, None, order_text)
        # bot_statistics['total_orders'] += len(items)
        # save_stats()
        # await self.forward_order_to_owners(context, user_id, user, order_text)

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        document = update.message.document
        if document.mime_type != 'application/json':
            await update.message.reply_text("ℹ️ Будь ласка, надішліть файл у форматі JSON.")
            return
        file = await context.bot.get_file(document.file_id)
        file_path = file.file_path
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
        if 'items' not in order_data or 'total' not in order_data:
            await update.message.reply_text("❌ У файлі відсутні обов'язкові поля (items, total).")
            return
        order_text = "🛍️ Замовлення з сайту (з файлу):\n"
        for item in order_data['items']:
            order_text += f"▫️ {item['service']} {item.get('plan', '')} ({item['period']}) - {item['price']} UAH\n"
        order_text += f"\n💳 Всього: {order_data['total']} UAH"
        has_digital = any("Прикраси" in item.get('service', '') for item in order_data['items'])
        conversation_type = 'digital_order' if has_digital else 'subscription_order'
        user_id = user.id
        active_conversations[user_id] = {
            'type': conversation_type,
            'user_info': user,
            'assigned_owner': None,
            'order_details': order_text,
            'last_message': order_text,
            'from_website': True
        }
        save_active_conversation(user_id, conversation_type, None, order_text)
        bot_statistics['total_orders'] += len(order_data['items'])
        save_stats()
        await self.forward_order_to_owners(context, user_id, user, order_text)
        await update.message.reply_text(
            "✅ Ваше замовлення прийнято! Засновник магазину зв'яжеться з вами найближчим часом.\n"
            "Ви можете продовжити з іншим запитанням або замовленням."
        )

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        if isinstance(update, Update):
            message = update.message
        else:
            message = update
        help_text = """
👋 Доброго дня! Я бот магазину SecureShop.
🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій. Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання по нашому сервісу можна задавати цілодобово.
📌 Список доступних команд:
/start - Головне меню
/order - Зробити замовлення
/question - Поставити запитання
/channel - Наш канал з асортиментом, оновленнями та розіграшами
/stop - Завершити поточний діалог
/help - Ця довідка
💬 Якщо у вас виникли питання, не соромтеся звертатися!
        """
        await message.reply_text(help_text.strip())

    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [[InlineKeyboardButton("📢 Перейти в SecureShopUA", url="https://t.me/SecureShopUA")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = """
📢 Наш головний канал з асортиментом, оновленнями та розіграшами:
👉 Тут ви знайдете:
- 🆕 Актуальні товари та послуги
- 🔥 Спеціальні пропозиції та знижки
- 🎁 Розіграші та акції
- ℹ️ Важливі оновлення сервісу
Приєднуйтесь, щоб бути в курсі всіх новин! 👇
        """
        await update.message.reply_text(message_text.strip(), reply_markup=reply_markup)

    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
            [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        await update.message.reply_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def question_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)
        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог.\n"
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
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n"
            "Щоб завершити цей діалог пізніше, використайте команду /stop."
        )

    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        user_name = user.first_name
        if user_id in [OWNER_ID_1, OWNER_ID_2] and user_id in owner_client_map:
            client_id = owner_client_map[user_id]
            client_info = active_conversations.get(client_id, {}).get('user_info')
            if client_id in active_conversations:
                order_details = active_conversations[client_id].get('order_details', '')
                del active_conversations[client_id]
            del owner_client_map[user_id]
            delete_active_conversation(client_id)
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text="Діалог завершено представником магазину. Якщо у вас є нові питання, будь ласка, скористайтесь командою /start."
                )
                if client_info:
                    await update.message.reply_text(f"✅ Ви успішно завершили діалог з клієнтом {client_info.first_name}.")
                else:
                    await update.message.reply_text(f"✅ Ви успішно завершили діалог з клієнтом ID {client_id}.")
            except Exception as e:
                if "Chat_id is empty" in str(e):
                    logger.error(f"Помилка сповіщення клієнта {client_id}: невірний chat_id")
                else:
                    logger.error(f"Помилка при сповіщенні клієнта {client_id} про завершення діалогу: {e}")
                await update.message.reply_text("Не вдалося сповістити клієнта (можливо, він заблокував бота), але діалог було завершено з вашого боку.")
            return
        if user_id in active_conversations:
            if 'assigned_owner' in active_conversations[user_id] and active_conversations[user_id]['assigned_owner'] is not None:
                owner_id = active_conversations[user_id]['assigned_owner']
                try:
                    await context.bot.send_message(
                        chat_id=owner_id,
                        text=f"ℹ️ Клієнт {user_name} завершив діалог командою /stop."
                    )
                    if owner_id in owner_client_map:
                        del owner_client_map[owner_id]
                except Exception as e:
                    if "Chat_id is empty" in str(e):
                        logger.error(f"Помилка сповіщення власника {owner_id}: невірний chat_id")
                    else:
                        logger.error(f"Помилка сповіщення власника {owner_id}: {e}")
            del active_conversations[user_id]
            delete_active_conversation(user_id)
            await update.message.reply_text(
                "✅ Ваш діалог завершено.\n"
                "Ви можете розпочати новий діалог за допомогою /start."
            )
            return
        await update.message.reply_text(
            "ℹ️ У вас немає активного діалогу для завершення.\n"
            "Щоб розпочати новий діалог, використовуйте /start."
        )

    async def clear_active_conversations_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return
        deleted_count = clear_all_active_conversations()
        active_conversations.clear()
        owner_client_map.clear()
        await update.message.reply_text(f"✅ Усі активні діалоги очищено з бази даних. Видалено записів: {deleted_count}")

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        total_users_db = get_total_users_count()
        first_start = datetime.fromisoformat(bot_statistics['first_start'])
        last_save = datetime.fromisoformat(bot_statistics['last_save'])
        uptime = datetime.now() - first_start
        stats_message = f"""
📊 Статистика бота:
👤 Усього користувачів (БД): {total_users_db}
🛒 Усього замовлень: {bot_statistics['total_orders']}
❓ Усього запитаннь: {bot_statistics['total_questions']}
⏱️ Перший запуск: {first_start.strftime('%d.%m.%Y %H:%M')}
⏱️ Останнє збереження: {last_save.strftime('%d.%m.%Y %H:%M')}
⏱️ Час роботи: {uptime}
        """
        await update.message.reply_text(stats_message.strip())
        all_users = get_all_users()
        if all_users:
            users_data = []
            for user in all_users:
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
            await update.message.reply_document(
                document=file,
                caption="📊 Экспорт всех пользователей в JSON"
            )
        else:
            await update.message.reply_text("ℹ️ В базе данных нет пользователей для экспорта.")

    async def show_active_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        try:
            flush_active_conv_buffer()
            with psycopg.connect(DATABASE_URL) as conn:
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
            message = "🔄 Активные чаты:\n"
            keyboard = []
            for chat in active_chats:
                client_info = {
                    'first_name': chat['first_name'] or 'Неизвестный',
                    'username': chat['username'] or 'N/A'
                }
                owner_info = "Не назначен"
                if chat['assigned_owner']:
                    if chat['assigned_owner'] == OWNER_ID_1:
                        owner_info = "@HiGki2pYYY"
                    elif chat['assigned_owner'] == OWNER_ID_2:
                        owner_info = "@oc33t"
                    else:
                        owner_info = f"ID: {chat['assigned_owner']}"
                username = f"@{client_info['username']}" if client_info['username'] else "нет"
                message += (
                    f"👤 {client_info['first_name']} ({username})\n"
                    f"   Тип: {chat['conversation_type']}\n"
                    f"   Назначен: {owner_info}\n"
                    f"   Последнее сообщение: {chat['last_message'][:50]}{'...' if len(chat['last_message']) > 50 else ''}\n"
                    f"   [ID: {chat['user_id']}]\n"
                )
                keyboard.append([
                    InlineKeyboardButton(
                        f"Продолжить диалог с {client_info['first_name']}",
                        callback_data=f'continue_chat_{chat["user_id"]}'
                    )
                ])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(message.strip(), reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"❌ Ошибка получения активных чатов: {e}")
            await update.message.reply_text("❌ Произошла ошибка при получении активных чатов.")

    async def show_conversation_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        if not context.args:
            await update.message.reply_text("ℹ️ Использование: /history <user_id>")
            return
        try:
            user_id = int(context.args[0])
            history = get_conversation_history(user_id)
            if not history:
                await update.message.reply_text(f"ℹ️ Нет истории сообщений для пользователя {user_id}.")
                return
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                    user_info = cur.fetchone()
            if not user_info:
                user_info = {'first_name': 'Неизвестный', 'username': 'N/A'}
            message = (
                f"📨 История переписки с пользователем:\n"
                f"👤 {user_info['first_name']} (@{user_info.get('username', 'N/A')})\n"
                f"🆔 ID: {user_id}\n"
            )
            for msg in reversed(history):
                sender = "👤 Клиент" if msg['is_from_user'] else "👨‍💼 Магазин"
                message += f"{sender} [{msg['created_at'].strftime('%d.%m.%Y %H:%M')}]:\n{msg['message']}\n"
            max_length = 4096
            if len(message) > max_length:
                parts = [message[i:i+max_length] for i in range(0, len(message), max_length)]
                for part in parts:
                    await update.message.reply_text(part)
            else:
                await update.message.reply_text(message)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат ID пользователя.")
        except Exception as e:
            logger.error(f"❌ Ошибка получения истории сообщений: {e}")
            await update.message.reply_text("❌ Произошла ошибка при получении истории.")

    async def start_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await update.message.reply_text("❌ Пользователь не найден в базе данных.")
            return
        client_user = User(
            id=client_info['id'],
            first_name=client_info['first_name'],
            last_name=client_info.get('last_name', ''),
            username=client_info.get('username', ''),
            language_code=client_info.get('language_code', ''),
            is_bot=False
        )
        if client_id not in active_conversations:
            active_conversations[client_id] = {
                'type': 'manual',
                'user_info': client_user,
                'assigned_owner': owner_id,
                'last_message': "Диалог начат основателем"
            }
            save_active_conversation(client_id, 'manual', owner_id, "Диалог начат основателем")
        else:
            active_conversations[client_id]['assigned_owner'] = owner_id
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                active_conversations[client_id]['last_message']
            )
        owner_client_map[owner_id] = client_id
        history = get_conversation_history(client_id)
        history_text = "📨 История переписки:\n"
        for msg in reversed(history):
            sender = "👤 Клиент" if msg['is_from_user'] else "👨‍💼 Магазин"
            history_text += f"{sender} [{msg['created_at'].strftime('%d.%m.%Y %H:%M')}]:\n{msg['message']}\n"
        try:
            await update.message.reply_text(history_text[:4096])
            if len(history_text) > 4096:
                parts = [history_text[i:i+4096] for i in range(4096, len(history_text), 4096)]
                for part in parts:
                    await update.message.reply_text(part)
        except Exception as e:
            logger.error(f"Ошибка отправки истории: {e}")
        await update.message.reply_text(
            "💬 Теперь вы можете писать сообщения, и они будут отправлены этому пользователю.\n"
            "Для завершения диалога используйте /stop."
        )

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id
        ensure_user_exists(user)
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
        elif query.data == 'help':
            await self.show_help(query.message)
        elif query.data == 'order_subscriptions':
            keyboard = [
                [InlineKeyboardButton("💬 ChatGPT", callback_data='category_chatgpt')],
                [InlineKeyboardButton("🎮 Discord", callback_data='category_discord')],
                [InlineKeyboardButton("📚 Duolingo", callback_data='category_duolingo')],
                [InlineKeyboardButton("📸 PicsArt", callback_data='category_picsart')],
                [InlineKeyboardButton("🎨 Canva", callback_data='category_canva')],
                [InlineKeyboardButton("📺 Netflix", callback_data='category_netflix')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text("💳 Оберіть категорію підписки:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'order_digital':
            keyboard = [
                [InlineKeyboardButton("🎮 Discord Прикраси", callback_data='category_discord_decor')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'category_chatgpt':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_1')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("💬 Оберіть варіант ChatGPT Plus:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'category_discord':
            keyboard = [
                [InlineKeyboardButton("Nitro Basic", callback_data='discord_basic')],
                [InlineKeyboardButton("Nitro Full", callback_data='discord_full')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎮 Оберіть тип Discord Nitro:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'discord_basic':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 100 UAH", callback_data='discord_basic_1')],
                [InlineKeyboardButton("12 місяців - 900 UAH", callback_data='discord_basic_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text("🔹 Discord Nitro Basic:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'discord_full':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 170 UAH", callback_data='discord_full_1')],
                [InlineKeyboardButton("12 місяців - 1700 UAH", callback_data='discord_full_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text("✨ Discord Nitro Full:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'category_duolingo':
            keyboard = [
                [InlineKeyboardButton("👨‍👩‍👧‍👦 Family", callback_data='duolingo_family')],
                [InlineKeyboardButton("👤 Individual", callback_data='duolingo_individual')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📚 Оберіть тип підписки Duolingo:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'duolingo_family':
            keyboard = [
                [InlineKeyboardButton("12 місяців - 380 UAH (на 1 людину)", callback_data='duolingo_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text("👨‍👩‍👧‍👦 Duolingo Family:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'duolingo_individual':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 200 UAH", callback_data='duolingo_ind_1')],
                [InlineKeyboardButton("12 місяців - 1500 UAH", callback_data='duolingo_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text("👤 Duolingo Individual:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'category_picsart':
            keyboard = [
                [InlineKeyboardButton("✨ PicsArt Plus", callback_data='picsart_plus')],
                [InlineKeyboardButton("🚀 PicsArt Pro", callback_data='picsart_pro')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📸 Оберіть версію PicsArt:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'picsart_plus':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 130 UAH", callback_data='picsart_plus_1')],
                [InlineKeyboardButton("12 місяців - 800 UAH", callback_data='picsart_plus_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text("✨ PicsArt Plus:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'picsart_pro':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 180 UAH", callback_data='picsart_pro_1')],
                [InlineKeyboardButton("12 місяців - 1000 UAH", callback_data='picsart_pro_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text("🚀 PicsArt Pro:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'category_canva':
            keyboard = [
                [InlineKeyboardButton("👤 Individual", callback_data='canva_individual')],
                [InlineKeyboardButton("👨‍👩‍👧‍👦 Family", callback_data='canva_family')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎨 Оберіть тариф Canva:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'canva_individual':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 350 UAH", callback_data='canva_ind_1')],
                [InlineKeyboardButton("12 місяців - 3000 UAH", callback_data='canva_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_canva')]
            ]
            await query.edit_message_text("👤 Canva Individual:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'canva_family':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 850 UAH", callback_data='canva_fam_1')],
                [InlineKeyboardButton("12 місяців - 7500 UAH", callback_data='canva_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_canva')]
            ]
            await query.edit_message_text("👨‍👩‍👧‍👦 Canva Family:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'category_netflix':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 350 UAH", callback_data='netflix_1')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📺 Оберіть варіант Netflix:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'category_discord_decor':
            keyboard = [
                [InlineKeyboardButton("Без Nitro", callback_data='discord_decor_without_nitro')],
                [InlineKeyboardButton("З Nitro", callback_data='discord_decor_with_nitro')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')]
            ]
            await query.edit_message_text("🎮 Оберіть тип прикраси Discord:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data == 'discord_decor_without_nitro':
            keyboard = [
                [InlineKeyboardButton("6$ - 180 UAH", callback_data='discord_decor_bzn_6')],
                [InlineKeyboardButton("8$ - 235 UAH", callback_data='discord_decor_bzn_8')],
                [InlineKeyboardButton("10$ - 295 UAH", callback_data='discord_decor_bzn_10')],
                [InlineKeyboardButton("11$ - 325 UAH", callback_data='discord_decor_bzn_11')],
                [InlineKeyboardButton("12$ - 355 UAH", callback_data='discord_decor_bzn_12')],
                [InlineKeyboardButton("13$ - 385 UAH", callback_data='discord_decor_bzn_13')],
                [InlineKeyboardButton("15$ - 440 UAH", callback_data='discord_decor_bzn_15')],
                [InlineKeyboardButton("16$ - 470 UAH", callback_data='discord_decor_bzn_16')],
                [InlineKeyboardButton("18$ - 530 UAH", callback_data='discord_decor_bzn_18')],
                [InlineKeyboardButton("24$ - 705 UAH", callback_data='discord_decor_bzn_24')],
                [InlineKeyboardButton("29$ - 855 UAH", callback_data='discord_decor_bzn_29')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Discord Прикраси (Без Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))
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
            await query.edit_message_text("🎮 Discord Прикраси (З Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))
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
            context.user_data['selected_product'] = query.data
            product_info = products.SUBSCRIPTION_PRODUCTS.get(query.data, {'name': "Невідомий товар", 'price': 0})
            # Конвертируем цену в USD
            price_usd = round(product_info['price'] / EXCHANGE_RATE_UAH_TO_USD, 2)
            keyboard = [
                [InlineKeyboardButton("✅ Замовити", callback_data='confirm_subscription_order')],
                [InlineKeyboardButton("⬅️ Назад", callback_data=products.SUBSCRIPTION_BACK_MAP.get(query.data, 'order_subscriptions'))]
            ]
            await query.edit_message_text(
                f"🛒 Ви обрали:\n{product_info['name']}\n💵 Ціна: {product_info['price']} UAH ({price_usd}$)\n"
                f"Натисніть \"✅ Замовити\" для підтвердження замовлення.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif query.data in [
            'discord_decor_bzn_6', 'discord_decor_bzn_8', 'discord_decor_bzn_10',
            'discord_decor_bzn_11', 'discord_decor_bzn_12', 'discord_decor_bzn_13',
            'discord_decor_bzn_15', 'discord_decor_bzn_16', 'discord_decor_bzn_18',
            'discord_decor_bzn_24', 'discord_decor_bzn_29',
            'discord_decor_zn_5', 'discord_decor_zn_7', 'discord_decor_zn_8_5',
            'discord_decor_zn_9', 'discord_decor_zn_14', 'discord_decor_zn_22'
        ]:
            context.user_data['selected_product'] = query.data
            product_info = products.DIGITAL_PRODUCTS.get(query.data, {'name': "Невідомий цифровий товар", 'price': 0})
            # Конвертируем цену в USD
            price_usd = round(product_info['price'] / EXCHANGE_RATE_UAH_TO_USD, 2)
            keyboard = [
                [InlineKeyboardButton("✅ Замовити", callback_data='confirm_digital_order')],
                [InlineKeyboardButton("⬅️ Назад", callback_data=products.DIGITAL_BACK_MAP.get(query.data, 'category_discord_decor'))]
            ]
            await query.edit_message_text(
                f"🎮 Ви обрали:\n{product_info['name']}\n💵 Ціна: {product_info['price']} UAH ({price_usd}$)\n"
                f"Натисніть \"✅ Замовити\" для підтвердження замовлення.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif query.data == 'confirm_subscription_order':
            selected_product = context.user_data.get('selected_product')
            if not selected_product:
                await query.edit_message_text("❌ Помилка: товар не обраний")
                return
            product_info = products.SUBSCRIPTION_PRODUCTS.get(selected_product, {'name': "Невідомий товар", 'price': 0})
            # Конвертируем цену в USD
            price_usd = round(product_info['price'] / EXCHANGE_RATE_UAH_TO_USD, 2)
            order_text = f"🛍️ Хочу замовити: {product_info['name']} за {product_info['price']} UAH ({price_usd}$)"
            # Сохраняем информацию о заказе для последующего использования
            context.user_data['pending_payment'] = {
                'product_id': selected_product,
                'product_name': product_info['name'],
                'price_uah': product_info['price'],
                'price_usd': price_usd,
                'type': 'subscription'
            }
            # Отображаем кнопки оплаты
            keyboard = [
                [InlineKeyboardButton("💳 Оплата по карте", callback_data=f'pay_card_{product_info["price"]}')],
                [InlineKeyboardButton("₿ Оплата криптовалютой", callback_data=f'pay_crypto_{product_info["price"]}')]
            ]
            await query.edit_message_text(
                f"{order_text}\n\nВыберите способ оплаты:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif query.data == 'confirm_digital_order':
            selected_product = context.user_data.get('selected_product')
            if not selected_product:
                await query.edit_message_text("❌ Помилка: товар не обраний")
                return
            product_info = products.DIGITAL_PRODUCTS.get(selected_product, {'name': "Невідомий цифровий товар", 'price': 0})
            # Конвертируем цену в USD
            price_usd = round(product_info['price'] / EXCHANGE_RATE_UAH_TO_USD, 2)
            order_text = f"🎮 Хочу замовити: {product_info['name']} за {product_info['price']} UAH ({price_usd}$)"
            # Сохраняем информацию о заказе для последующего использования
            context.user_data['pending_payment'] = {
                'product_id': selected_product,
                'product_name': product_info['name'],
                'price_uah': product_info['price'],
                'price_usd': price_usd,
                'type': 'digital'
            }
            # Отображаем кнопки оплаты
            keyboard = [
                [InlineKeyboardButton("💳 Оплата по карте", callback_data=f'pay_card_{product_info["price"]}')],
                [InlineKeyboardButton("₿ Оплата криптовалютой", callback_data=f'pay_crypto_{product_info["price"]}')]
            ]
            await query.edit_message_text(
                f"{order_text}\n\nВыберите способ оплаты:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            ) 
        elif query.data.startswith('pay_card_'):
    try:
        # Виправлення: правильно отримуємо суму з callback_data
        parts = query.data.split('_')
        amount = int(parts[2]) # parts[0]='pay', parts[1]='card', parts[2]='СУМА'
        
        # Отримуємо дані про замовлення
        pending_payment = context.user_data.get('pending_payment')
        if not pending_payment:
            await query.edit_message_text("❌ Ошибка: информация о заказе отсутствует.")
            return
            
        # Відображаємо номер картки
        await query.edit_message_text(
            f"💳 Оплата по карті:\n`{CARD_NUMBER}`",
            parse_mode='Markdown'
        )
        
        # Якщо це замовлення з бота (не з /pay), повідомляємо власників
        if 'order_id' not in pending_payment: 
            # Це замовлення з бота, створюємо order_text
            product_name = pending_payment.get('product_name', 'Товар')
            price_uah = pending_payment.get('price_uah', 0)
            price_usd = pending_payment.get('price_usd', 0)
            order_text = f"🛍️ Хочу замовити: {product_name} за {price_uah} UAH ({price_usd}$)"
            
            # Зберігаємо активний діалог
            active_conversations[user_id] = {
                'type': pending_payment['type'] + '_order',
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            save_active_conversation(user_id, pending_payment['type'] + '_order', None, order_text)
            bot_statistics['total_orders'] += 1
            save_stats()
            await self.forward_order_to_owners(context, user_id, user, order_text)
            
    except (ValueError, IndexError) as e: # Додано обробку ValueError
        logger.error(f"Ошибка обработки оплаты по карте: {e}")
        await query.edit_message_text("❌ Произошла ошибка при обработке оплаты по карте.")

elif query.data.startswith('pay_crypto_'):
    try:
        # Виправлення: правильно отримуємо суму з callback_data
        parts = query.data.split('_')
        amount = int(parts[2]) # parts[0]='pay', parts[1]='crypto', parts[2]='СУМА'
        
        # Отримуємо дані про замовлення
        pending_payment = context.user_data.get('pending_payment')
        if not pending_payment:
            await query.edit_message_text("❌ Ошибка: информация о заказе отсутствует.")
            return
            
        # Створюємо кнопки для вибору криптовалюти
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f'pay_crypto_invoice_{amount}_{code}')]
            for name, code in AVAILABLE_CURRENCIES.items()
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"Виберіть криптовалюту для оплати {amount} UAH:",
            reply_markup=reply_markup
        )
        
    except (ValueError, IndexError) as e: # Додано обробку ValueError
        logger.error(f"Ошибка обработки выбора криптовалюты: {e}")
        await query.edit_message_text("❌ Произошла ошибка при обработке запроса оплаты криптовалютой.")

elif query.data.startswith('pay_crypto_invoice_'):
    try:
        # parts[0]='pay', parts[1]='crypto', parts[2]='invoice', parts[3]='СУМА', parts[4]='Код_валюти'
        parts = query.data.split('_')
        amount = int(parts[3]) 
        pay_currency = parts[4] 
        
        # Створюємо інвойс через NowPayments
        invoice_data = self.create_invoice(context, amount=amount, pay_currency=pay_currency, currency="uah")
        if "error" in invoice_data:
            await query.edit_message_text(f"❌ Ошибка создания платежа: {invoice_data['error']}")
            return
            
        pay_url = invoice_data.get("invoice_url")
        if pay_url:
            # Відправляємо посилання для оплати
            currency_name = dict((v, k) for k, v in AVAILABLE_CURRENCIES.items()).get(pay_currency, pay_currency)
            await query.edit_message_text(
                f"🔗 Посилання для оплати {amount} UAH в {currency_name}:\n{pay_url}"
            )
            
            # Повідомляємо власників про створення інвойса
            await self.notify_owners_of_invoice_creation(context, user_id, amount, pay_currency, pay_url)
            
            # Якщо це замовлення з бота (не з /pay), повідомляємо власників про замовлення
            pending_payment = context.user_data.get('pending_payment')
            if pending_payment and 'order_id' not in pending_payment:
                # Це замовлення з бота
                product_name = pending_payment.get('product_name', 'Товар')
                price_uah = pending_payment.get('price_uah', 0)
                price_usd = pending_payment.get('price_usd', 0)
                order_text = f"🛍️ Хочу замовити: {product_name} за {price_uah} UAH ({price_usd}$)"
                
                active_conversations[user_id] = {
                    'type': pending_payment['type'] + '_order',
                    'user_info': user,
                    'assigned_owner': None,
                    'order_details': order_text,
                    'last_message': order_text
                }
                save_active_conversation(user_id, pending_payment['type'] + '_order', None, order_text)
                bot_statistics['total_orders'] += 1
                save_stats()
                await self.forward_order_to_owners(context, user_id, user, order_text)
                
        else:
            await query.edit_message_text("❌ Не вдалося отримати посилання для оплати. Спробуйте пізніше або виберіть інший спосіб.")
            
    except (ValueError, IndexError, Exception) as e: # Додано загальну обробку помилок
        logger.error(f"Ошибка обработки инвойса: {e}")
        await query.edit_message_text("❌ Произошла ошибка при создании платежа.")

# ... (решта button_handler)
        elif query.data.startswith('payment_completed_'):
            # Обработка оплаты через NowPayments Webhook
            # Это будет обработано в другом месте (например, в webhook)
            pass
        elif query.data == 'question':
            if user_id in active_conversations:
                await query.answer(
                    "❗ У вас вже є активний діалог.\n"
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
            save_active_conversation(user_id, 'question', None, "Нове запитання")
            bot_statistics['total_questions'] += 1
            save_stats()
            await query.edit_message_text(
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n"
                "Щоб завершити цей діалог пізніше, використайте команду /stop."
            )
        elif query.data.startswith('take_order_'):
            client_id = int(query.data.split('_')[2])
            owner_id = user_id
            if client_id not in active_conversations:
                await query.answer("Діалог вже завершено", show_alert=True)
                return
            active_conversations[client_id]['assigned_owner'] = owner_id
            owner_client_map[owner_id] = client_id
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                active_conversations[client_id]['last_message']
            )
            client_info = active_conversations[client_id]['user_info']
            await query.edit_message_text(f"✅ Ви взяли замовлення від клієнта {client_info.first_name}.")
            if 'order_details' in active_conversations[client_id]:
                order_text = active_conversations[client_id]['order_details']
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=f"📝 Деталі замовлення:\n{order_text}"
                )
            other_owner = OWNER_ID_2 if owner_id == OWNER_ID_1 else OWNER_ID_1
            try:
                await context.bot.send_message(
                    chat_id=other_owner,
                    text=f"ℹ️ Замовлення від клієнта {client_info.first_name} взяв інший представник."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления другого основателя: {e}")
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text=f"✅ Ваш запит прийняв засновник магазину. Очікуйте на відповідь."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления клиента: {e}")
        elif query.data.startswith('transfer_'):
            client_id = int(query.data.split('_')[1])
            current_owner = user_id
            other_owner = OWNER_ID_2 if current_owner == OWNER_ID_1 else OWNER_ID_1
            other_owner_name = "@oc33t" if other_owner == OWNER_ID_2 else "@HiGki2pYYY"
            if client_id in active_conversations:
                active_conversations[client_id]['assigned_owner'] = other_owner
                owner_client_map[other_owner] = client_id
                if current_owner in owner_client_map:
                    del owner_client_map[current_owner]
                save_active_conversation(
                    client_id, 
                    active_conversations[client_id]['type'], 
                    other_owner, 
                    active_conversations[client_id]['last_message']
                )
                client_info = active_conversations[client_id]['user_info']
                last_message = active_conversations[client_id].get('last_message', 'Немає повідомлень')
                await query.edit_message_text(
                    f"✅ Чат с клиентом {client_info.first_name} передан {other_owner_name}"
                )
                await context.bot.send_message(
                    chat_id=other_owner,
                    text=f"📨 Вам передан чат с клиентом:\n"
                         f"👤 {client_info.first_name} (@{client_info.username or 'не указан'})\n"
                         f"🆔 ID: {client_info.id}\n"
                         f"Останнє повідомлення:\n{last_message}\n"
                         f"Для ответа просто напишите сообщение. Для завершения диалога используйте /stop"
                )
        elif query.data.startswith('continue_chat_'):
            client_id = int(query.data.split('_')[2])
            owner_id = user_id
            if client_id not in active_conversations:
                await query.answer("Диалог уже завершен", show_alert=True)
                return
            if 'assigned_owner' in active_conversations[client_id] and \
               active_conversations[client_id]['assigned_owner'] != owner_id:
                other_owner = active_conversations[client_id]['assigned_owner']
                other_owner_name = "@HiGki2pYYY" if other_owner == OWNER_ID_1 else "@oc33t"
                await query.answer(
                    f"Диалог уже ведет {other_owner_name}.",
                    show_alert=True
                )
                return
            active_conversations[client_id]['assigned_owner'] = owner_id
            owner_client_map[owner_id] = client_id
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                active_conversations[client_id]['last_message']
            )
            history = get_conversation_history(client_id)
            history_text = "📨 История переписки:\n"
            for msg in reversed(history):
                sender = "👤 Клиент" if msg['is_from_user'] else "👨‍💼 Магазин"
                history_text += f"{sender} [{msg['created_at'].strftime('%d.%m.%Y %H:%M')}]:\n{msg['message']}\n"
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=history_text[:4096]
                )
                if len(history_text) > 4096:
                    parts = [history_text[i:i+4096] for i in range(4096, len(history_text), 4096)]
                    for part in parts:
                        await context.bot.send_message(chat_id=owner_id, text=part)
            except Exception as e:
                logger.error(f"Ошибка отправки истории: {e}")
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"💬 Последнее сообщение от клиента:\n{active_conversations[client_id]['last_message']}\n"
                     "Напишите ответ:"
            )
            await query.edit_message_text(f"✅ Вы продолжили диалог с клиентом ID: {client_id}.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await self.handle_owner_message(update, context)
            return
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
        message_text = update.message.text
        active_conversations[user_id]['last_message'] = message_text
        save_message(user_id, message_text, True)
        save_active_conversation(
            user_id, 
            active_conversations[user_id]['type'], 
            active_conversations[user_id].get('assigned_owner'), 
            message_text
        )
        await self.forward_to_owner(update, context)

    async def forward_to_owner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in active_conversations:
            logger.warning(f"Попытка переслать сообщение для несуществующего диалога: {user_id}")
            return
        user_info = active_conversations[user_id]['user_info']
        conversation_type = active_conversations[user_id]['type']
        assigned_owner = active_conversations[user_id].get('assigned_owner')
        if not assigned_owner:
            await self.forward_to_both_owners(
                context, 
                user_id, 
                user_info, 
                conversation_type, 
                update.message.text
            )
            return
        await self.forward_to_specific_owner(
            context, 
            user_id, 
            user_info, 
            conversation_type, 
            update.message.text, 
            assigned_owner
        )

    async def forward_to_both_owners(self, context, client_id, client_info, conversation_type, message_text):
        type_emoji = "🛒" if conversation_type in ['subscription_order', 'digital_order'] else "❓"
        type_text_map = {
            'subscription_order': "НОВЕ ЗАМОВЛЕННЯ (Підписка)",
            'digital_order': "НОВЕ ЗАМОВЛЕННЯ (Цифровий товар)",
            'question': "НОВЕ ЗАПИТАННЯ"
        }
        type_text = type_text_map.get(conversation_type, "НОВЕ ПОВІДОМЛЕННЯ")
        forward_message = f"""
{type_emoji} {type_text}!
👤 Клієнт: {client_info.first_name}
📱 Username: @{client_info.username if client_info.username else 'не вказано'}
🆔 ID: {client_info.id}
🌐 Язык: {client_info.language_code or 'не вказано'}
💬 Повідомлення:
{message_text}
---
Натисніть "✅ Взяти", щоб обробити запит.
        """
        keyboard = [
            [InlineKeyboardButton("✅ Взяти", callback_data=f'take_order_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=forward_message.strip(),
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения основателю {owner_id}: {e}")
        await context.bot.send_message(
            chat_id=client_id,
            text="✅ Ваше повідомлення передано засновникам магазину. "
                 "Очікуйте на відповідь найближчим часом."
        )

    async def forward_to_specific_owner(self, context, client_id, client_info, conversation_type, message_text, owner_id):
        type_emoji = "🛒" if conversation_type in ['subscription_order', 'digital_order'] else "❓"
        type_text_map = {
            'subscription_order': "ЗАМОВЛЕННЯ (Підписка)",
            'digital_order': "ЗАМОВЛЕННЯ (Цифровий товар)",
            'question': "ЗАПИТАННЯ"
        }
        type_text = type_text_map.get(conversation_type, "ПОВІДОМЛЕННЯ")
        owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"
        forward_message = f"""
{type_emoji} {type_text} від клієнта:
👤 Користувач: {client_info.first_name}
📱 Username: @{client_info.username if client_info.username else 'не вказано'}
🆔 ID: {client_info.id}
🌐 Язык: {client_info.language_code or 'не вказано'}
💬 Повідомлення:
{message_text}
---
Для відповіді просто напишіть повідомлення в цей чат.
Для завершення діалогу використовуйте /stop.
Призначено: {owner_name}
        """
        keyboard = [
            [InlineKeyboardButton("🔄 Передати іншому засновнику", callback_data=f'transfer_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text=forward_message.strip(),
                reply_markup=reply_markup
            )
            await context.bot.send_message(
                chat_id=client_id,
                text="✅ Ваше повідомлення передано засновнику магазину. "
                     "Очікуйте на відповідь найближчим часом."
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения основателю {owner_id}: {e}")
            other_owner = OWNER_ID_2 if owner_id == OWNER_ID_1 else OWNER_ID_1
            active_conversations[client_id]['assigned_owner'] = other_owner
            owner_client_map[other_owner] = client_id
            save_active_conversation(
                client_id, 
                conversation_type, 
                other_owner, 
                message_text
            )
            await self.forward_to_specific_owner(context, client_id, client_info, conversation_type, message_text, other_owner)

    async def forward_order_to_owners(self, context, client_id, client_info, order_text):
        conversation_type = 'digital_order' if 'Discord Прикраси' in order_text else 'subscription_order'
        active_conversations[client_id]['last_message'] = order_text
        save_active_conversation(client_id, conversation_type, None, order_text)
        source = "з сайту" if active_conversations[client_id].get('from_website', False) else ""
        type_text_map = {
            'subscription_order': "НОВЕ ЗАМОВЛЕННЯ (Підписка)",
            'digital_order': "НОВЕ ЗАМОВЛЕННЯ (Цифровий товар)"
        }
        type_text = type_text_map.get(conversation_type, "НОВЕ ЗАМОВЛЕННЯ")
        forward_message = f"""
🛒 {type_text} {source}!
👤 Клієнт: {client_info.first_name}
📱 Username: @{client_info.username if client_info.username else 'не вказано'}
🆔 ID: {client_info.id}
🌐 Язык: {client_info.language_code or 'не вказано'}
📋 Деталі замовлення:
{order_text}
---
Натисніть "✅ Взяти", щоб обробити це замовлення.
        """
        keyboard = [
            [InlineKeyboardButton("✅ Взяти", callback_data=f'take_order_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=forward_message.strip(),
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Ошибка отправки владельцу {owner_id}: {e}")

    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner = update.effective_user
        owner_id = owner.id
        ensure_user_exists(owner)
        if owner_id not in owner_client_map:
            owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(
                f"У вас немає активного клієнта для відповіді. ({owner_name})\n"
                f"Дочекайтесь нового повідомлення від клієнта або скористайтесь командою /dialog."
            )
            return
        client_id = owner_client_map[owner_id]
        if client_id not in active_conversations:
            del owner_client_map[owner_id]
            await update.message.reply_text(
                "Діалог з клієнтом завершено або не знайдено."
            )
            return
        try:
            message_text = update.message.text
            save_message(client_id, message_text, False)
            active_conversations[client_id]['last_message'] = message_text
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                message_text
            )
            await context.bot.send_message(
                chat_id=client_id,
                text=f"📩 Відповідь від магазину:\n{message_text}"
            )
            client_info = active_conversations[client_id]['user_info']
            await update.message.reply_text(
                f"✅ Повідомлення надіслано клієнту {client_info.first_name}"
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения клиенту {client_id}: {e}")
            await update.message.reply_text(
                "❌ Помилка при надсиланні повідомлення клієнту. "
                "Можливо, клієнт заблокував бота."
            )

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.warning(f'Update {update} caused error {context.error}')

    def start_ping_service(self):
        if not self.ping_running:
            self.ping_running = True
            ping_thread = threading.Thread(target=self.ping_loop)
            ping_thread.daemon = True
            ping_thread.start()

    def ping_loop(self):
        import requests
        ping_url = f"{WEBHOOK_URL}/ping"
        while self.ping_running:
            try:
                response = requests.get(ping_url, timeout=10)
                if response.status_code == 200:
                    pass
                else:
                    logger.warning(f"⚠️ Ping вернул статус {response.status_code}")
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ Ошибка ping: {e}")
            except Exception as e:
                logger.error(f"❌ Неожиданная ошибка ping: {e}")
            time.sleep(PING_INTERVAL)

    def create_invoice(self, context, amount, pay_currency="usdtsol", currency="uah"):
        """Создает инвойс через NowPayments API."""
        if not NOWPAYMENTS_API_KEY or NOWPAYMENTS_API_KEY == 'YOUR_NOWPAYMENTS_API_KEY_HERE':
            logger.error("NOWPAYMENTS_API_KEY не установлен!")
            return {"error": "API ключ не настроен"}
        url = "https://api.nowpayments.io/v1/invoice"
        headers = {"x-api-key": NOWPAYMENTS_API_KEY}
        # Генерируем уникальный order_id
        order_id = f"order_{int(time.time())}_{user_id}" # user_id доступен из контекста вызова
        payload = {
            "price_amount": amount,
            "price_currency": currency,
            "pay_currency": pay_currency,
            "order_id": order_id,
            "order_description": f"Оплата за заказ"
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status() # Проверка на HTTP ошибки
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса к NowPayments API: {e}")
            return {"error": f"Ошибка API: {e}"}
        except Exception as e:
            logger.error(f"Неожиданная ошибка при создании инвойса: {e}")
            return {"error": f"Внутренняя ошибка: {e}"}

    async def notify_owners_of_invoice_creation(self, context, client_id, amount, pay_currency, pay_url):
        """Уведомляет владельцев о создании инвойса."""
        client_info = active_conversations.get(client_id, {}).get('user_info')
        if not client_info:
            return
        message = f"""
💰 Инвойс создан для клиента {client_info.first_name}!
💸 Сумма: {amount} UAH ({round(amount / EXCHANGE_RATE_UAH_TO_USD, 2)}$)
💱 Криптовалюта: {dict((v, k) for k, v in AVAILABLE_CURRENCIES.items()).get(pay_currency, pay_currency)}
🔗 Ссылка: {pay_url}
🔔 Клиент должен оплатить по этой ссылке.
        """
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(chat_id=owner_id, text=message)
            except Exception as e:
                logger.error(f"Ошибка уведомления владельца {owner_id}: {e}")

bot_instance = TelegramBot()

@flask_app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-CSRF-Token'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@flask_app.route('/ping', methods=['GET'])
def ping():
    return jsonify({
        'status': 'alive',
        'message': 'Bot is running',
        'timestamp': time.time(),
        'uptime': time.time() - datetime.fromisoformat(bot_statistics['first_start']).timestamp(),
        'bot_running': bot_running,
        'mode': 'polling' if USE_POLLING else 'webhook'
    }), 200

@flask_app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'bot_token': f"{BOT_TOKEN[:10]}..." if BOT_TOKEN else "Not set",
        'active_conversations': len(active_conversations),
        'owner_client_map': len(owner_client_map),
        'ping_interval': PING_INTERVAL,
        'webhook_url': WEBHOOK_URL,
        'initialized': bot_instance.initialized if bot_instance else False,
        'bot_running': bot_running,
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
            pass
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

def auto_save_loop():
    while True:
        time.sleep(300)
        save_stats()

def main():
    if os.environ.get('RENDER'):
        time.sleep(10)
    auto_save_thread = threading.Thread(target=auto_save_loop)
    auto_save_thread.daemon = True
    auto_save_thread.start()
    bot_thread_instance = threading.Thread(target=bot_thread)
    bot_thread_instance.daemon = True
    bot_thread_instance.start()
    time.sleep(3)
    bot_instance.start_ping_service()
    flask_app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )

async def setup_webhook():
    if USE_POLLING:
        try:
            await telegram_app.bot.delete_webhook()
        except Exception as e:
            logger.error(f"Ошибка удаления webhook: {e}")
        return True
    try:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await telegram_app.bot.set_webhook(webhook_url)
        return True
    except Exception as e:
        logger.error(f"Ошибка установки webhook: {e}")
        return False

async def start_bot():
    global telegram_app, bot_running
    with bot_lock:
        if bot_running:
            return
        try:
            await bot_instance.initialize()
            telegram_app = bot_instance.application
            if USE_POLLING:
                await setup_webhook()
                await bot_instance.start_polling()
                bot_running = True
            else:
                success = await setup_webhook()
                if success:
                    bot_running = True
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
        time.sleep(30)
        bot_thread()
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в bot_thread: {e}")
        time.sleep(15)
        bot_thread()
    finally:
        try:
            if not loop.is_closed():
                loop.close()
        except:
            pass
        time.sleep(5)
        bot_thread()

if __name__ == '__main__':
    main()
