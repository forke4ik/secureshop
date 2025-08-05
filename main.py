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
import psycopg
from psycopg.rows import dict_row
import io
from flask import request, jsonify
import json

@flask_app.route('/api/order', methods=['POST'])
async def api_create_order():
    try:
        data = request.json
        items = data.get('items', [])
        total = data.get('total', 0)
        
        if not items:
            return jsonify({'success': False, 'error': 'Пустий замовлення'}), 400
        
        # Формируем текст заказа
        order_text = "🛍️ Нове замовлення з сайту:\n\n"
        for item in items:
            order_text += f"▫️ {item.get('service', '')} {item.get('plan', '')} ({item.get('period', '')}) - {item.get('price', 0)} UAH\n"
        order_text += f"\n💳 Всього: {total} UAH"
        
        # Здесь должен быть механизм привязки к пользователю
        # Пока будем отправлять обоим владельцам
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await telegram_app.application.bot.send_message(
                    chat_id=owner_id,
                    text=order_text
                )
            except Exception as e:
                logger.error(f"Помилка відправки замовлення власнику {owner_id}: {e}")
        
        return jsonify({'success': True}), 200
        
    except Exception as e:
        logger.error(f"Помилка API /api/order: {e}")
        return jsonify({'success': False, 'error': 'Серверна помилка'}), 500

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN', '8181378677:AAFullvwrNhPJMi_HxgC75qSEKWdKOtCpbw')
OWNER_ID_1 = 7106925462  # @HiGki2pYYY
OWNER_ID_2 = 6279578957  # @oc33t
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://secureshop-3obw.onrender.com')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840))  # 14 минут
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://neondb_owner:npg_1E2GxznybVCR@ep-super-pond-a2ce35gl-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require')

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
                # Создаем временную таблицу
                cur.execute("""
                    CREATE TEMP TABLE temp_messages (
                        user_id BIGINT,
                        message TEXT,
                        is_from_user BOOLEAN
                    ) ON COMMIT DROP;
                """)
                
                # Заполняем временную таблицу
                for msg in message_buffer:
                    cur.execute(
                        "INSERT INTO temp_messages (user_id, message, is_from_user) VALUES (%s, %s, %s)",
                        msg
                    )
                
                # Вставляем из временной таблицы
                cur.execute("""
                    INSERT INTO messages (user_id, message, is_from_user)
                    SELECT user_id, message, is_from_user
                    FROM temp_messages
                """)
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
            latest_convs[user_id] = conv  # Последняя запись для пользователя
        
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Создаем временную таблицу БЕЗ первичного ключа
                cur.execute("""
                    CREATE TEMP TABLE temp_active_convs (
                        user_id BIGINT,
                        conversation_type VARCHAR(50),
                        assigned_owner BIGINT,
                        last_message TEXT
                    ) ON COMMIT DROP;
                """)
                
                # Заполняем временную таблицу
                for conv in latest_convs.values():
                    cur.execute(
                        "INSERT INTO temp_active_convs VALUES (%s, %s, %s, %s)",
                        conv
                    )
                
                # Обновляем основную таблицу
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
                        conversation_type VARCHAR(50),
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
    """Сохраняет сообщение в буфер"""
    global message_buffer
    message_buffer.append((user_id, message_text, is_from_user))
    
    # Сбрасываем буфер при достижении лимита
    if len(message_buffer) >= BUFFER_MAX_SIZE:
        flush_message_buffer()

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message):
    """Сохраняет активный диалог в буфер с обновлением существующих"""
    global active_conv_buffer
    
    # Ищем существующую запись
    updated = False
    for i, record in enumerate(active_conv_buffer):
        if record[0] == user_id:
            # Обновляем существующую запись
            active_conv_buffer[i] = (
                user_id, 
                conversation_type, 
                assigned_owner, 
                last_message
            )
            updated = True
            break
    
    # Если не нашли - добавляем новую
    if not updated:
        active_conv_buffer.append((
            user_id, 
            conversation_type, 
            assigned_owner, 
            last_message
        ))
    
    # Сбрасываем буфер при достижении лимита
    if len(active_conv_buffer) >= BUFFER_MAX_SIZE:
        flush_active_conv_buffer()

def delete_active_conversation(user_id):
    """Удаляет активный диалог из базы данных"""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM active_conversations 
                    WHERE user_id = %s
                    AND (updated_at < NOW() - INTERVAL '1 MINUTE' OR updated_at IS NULL)
                """, (user_id,))
    except Exception as e:
        logger.error(f"❌ Ошибка удаления активного диалога: {e}")

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

# Инициализируем базу данных при старте
init_db()

# Запускаем поток для сброса буферов
threading.Thread(target=buffer_flush_thread, daemon=True).start()

# Функции для работы с данными
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

# Загружаем статистику
bot_statistics = load_stats()

# Словари для хранения данных
active_conversations = {}
owner_client_map = {}

# Глобальные переменные для приложения
telegram_app = None
flask_app = Flask(__name__)
bot_running = False
bot_lock = threading.Lock()  # Блокировка для управления доступом к боту

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.ping_running = False
        self.initialized = False
        self.polling_task = None
        self.loop = None
    
    async def set_commands_menu(self):
        """Установка стандартного меню команд"""
        commands = [
            ("start", "Головне меню"),
            ("help", "Допомога та інформація"),
            ("order", "Зробити замовлення"),
            ("question", "Поставити запитання"),
            ("pay", "Оплатити замовлення"),
            ("channel", "Наш головний канал"),
            ("stop", "Завершити поточний діалог")
        ]
        
        # Для владельцев добавляем дополнительные команды
        owner_commands = commands + [
            ("stats", "Статистика бота"),
            ("chats", "Активні чати"),
            ("history", "Історія переписки"),
            ("dialog", "Почати діалог з користувачем")
        ]
        
        try:
            # Устанавливаем команды для обычных пользователей
            await self.application.bot.set_my_commands(commands)
            
            # Устанавливаем расширенные команды для владельцев
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                await self.application.bot.set_my_commands(
                    owner_commands,
                    scope=BotCommandScopeChat(owner_id)
                )
        except Exception as e:
            logger.error(f"Ошибка установки команд: {e}")
    
    def setup_handlers(self):
        """Настройка обработчиков команд и сообщений"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("pay", self.pay_command))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("order", self.order_command))
        self.application.add_handler(CommandHandler("question", self.question_command))
        self.application.add_handler(CommandHandler("chats", self.show_active_chats))
        self.application.add_handler(CommandHandler("history", self.show_conversation_history))
        self.application.add_handler(CommandHandler("dialog", self.start_dialog_command))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)
    
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
        """Обработчик команды /start с поддержкой deep linking с сайта"""
        user = update.effective_user
        
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)
        
        # ПРОВЕРКА: Если команда /start была вызвана с параметрами (с сайта)
        if context.args:
            # Склеиваем аргументы в одну строку
            args_str = " ".join(context.args)
            
            # Если команда начинается с "/pay", обрабатываем её
            if args_str.startswith("/pay"):
                # Имитируем вызов команды /pay
                context.args = args_str.split()[1:]  # Убираем "/pay"
                await self.pay_command(update, context)
                return
            
            logger.info(f"Получены параметры deep link от {user.id}: {args_str}")
        
        # ---- Обычный запуск /start без параметров ----
        
        # Обновляем статистику
        if user.id not in bot_statistics['active_users']:
            bot_statistics['total_users'] += 1
            bot_statistics['active_users'].append(user.id)
            save_stats()
        
        # Для основателей
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            owner_name = "@HiGki2pYYY" if user.id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(
                f"Добро пожаловать, {user.first_name}! ({owner_name})\n"
                f"Вы вошли как основатель магазина."
            )
            return
        
        # Главное меню для обычных пользователей
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        welcome_message = f"""
Ласкаво просимо, {user.first_name}! 👋

Я бот-помічник нашого магазину. Будь ласка, оберіть, що вас цікавить:
        """
        
        await update.message.reply_text(
            welcome_message.strip(),
            reply_markup=reply_markup
        )
    
    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /pay для создания заказа"""
        user = update.effective_user
        user_id = user.id
        
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)
        
        # Парсим параметры команды
        try:
            if not context.args:
                await update.message.reply_text(
                    "ℹ️ Для оплаты используйте команду в формате:\n"
                    "/pay service=<название> plan=<тариф> period=<период> price=<цена>\n\n"
                    "Например: /pay service=ChatGPT plan=Plus period=1 месяц price=650"
                )
                return
            
            # Собираем все аргументы в одну строку
            args_str = " ".join(context.args)
            
            # Парсим параметры с помощью регулярных выражений
            params = {}
            pattern = r'(\w+)=([^=]+?)(?=\s+\w+=|$)'
            matches = re.findall(pattern, args_str)
            
            for key, value in matches:
                params[key.lower()] = value.strip()
            
            # Проверяем обязательные параметры
            required = ['service', 'period', 'price']
            for param in required:
                if param not in params:
                    await update.message.reply_text(
                        f"❌ Отсутствует обязательный параметр: {param}\n\n"
                        "Пожалуйста, укажите все необходимые параметры."
                    )
                    return
            
            # Формируем текст заказа
            service = params.get('service', 'Неизвестный сервис')
            plan = params.get('plan', '')
            period = params.get('period', '')
            price = params.get('price', 0)
            
            try:
                price = int(price)
            except ValueError:
                await update.message.reply_text("❌ Неверный формат цены. Цена должна быть числом.")
                return
            
            order_text = f"🛍️ Замовлення:\n\n▫️ {service}"
            if plan:
                order_text += f" {plan}"
            order_text += f" ({period}) - {price} UAH"
            
            # Создаем запись о заказе
            active_conversations[user_id] = {
                'type': 'order',
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text,
                'from_website': True
            }
            
            # Сохраняем в БД
            save_active_conversation(user_id, 'order', None, order_text)
            
            # Обновляем статистику
            bot_statistics['total_orders'] += 1
            save_stats()
            
            # Пересылаем заказ обоим владельцам
            await self.forward_order_to_owners(
                context, 
                user_id, 
                user, 
                order_text
            )
            
            await update.message.reply_text(
                "✅ Ваше замовлення прийнято! Засновник магазину зв'яжеться з вами найближчим часом.\n\n"
                "Ви можете продовжити з іншим запитанням або замовленням."
            )
            
        except Exception as e:
            logger.error(f"Помилка обробки команди /pay: {e}")
            await update.message.reply_text(
                "❌ Сталася помилка при обробці вашого замовлення. Будь ласка, спробуйте ще раз."
            )
    
    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Показывает справку и информацию о сервисе"""
        # Универсальный метод для обработки команды и кнопки
        if isinstance(update, Update):
            message = update.message
        else:
            message = update  # для вызова из кнопки
        
        help_text = """
👋 Доброго дня! Я бот магазину SecureShop.

🔐 Наш сервіс купує підписки на ваш готовий акаунт, а не дає вам свій. Ми дуже стараємось бути з клієнтами, тому відповіді на будь-які питання по нашому сервісу можна задавати цілодобово.

📌 Список доступних команд:
/start - Головне меню
/order - Зробити замовлення
/pay - Оплатити замовлення (для покупок з сайту)
/question - Поставити запитання
/channel - Наш канал з асортиментом, оновленнями та розіграшами
/stop - Завершити поточний діалог
/help - Ця довідка

💬 Якщо у вас виникли питання, не соромтеся звертатися!
        """
        await message.reply_text(help_text.strip())
    
    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отправляет ссылку на основной канал"""
        keyboard = [[
            InlineKeyboardButton(
                "📢 Перейти в SecureShopUA", 
                url="https://t.me/SecureShopUA"
            )
        ]]
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
        await update.message.reply_text(
            message_text.strip(),
            reply_markup=reply_markup
        )
    
    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /order"""
        keyboard = [
            [InlineKeyboardButton("💬 ChatGPT", callback_data='category_chatgpt')],
            [InlineKeyboardButton("🎮 Discord", callback_data='category_discord')],
            [InlineKeyboardButton("📚 Duolingo", callback_data='category_duolingo')],
            [InlineKeyboardButton("📸 PicsArt", callback_data='category_picsart')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        await update.message.reply_text(
            "📦 Оберіть категорію товару:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def question_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /question"""
        user = update.effective_user
        user_id = user.id
        
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)
        
        # Проверяем активные диалоги
        if user_id in active_conversations:
            await update.message.reply_text(
                "❗ У вас вже є активний діалог.\n\n"
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
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n\n"
            "Щоб завершити цей діалог пізніше, використайте команду /stop."
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

            if client_id in active_conversations:
                del active_conversations[client_id]
            if user_id in owner_client_map:
                del owner_client_map[user_id]
            
            # Удаляем из базы данных
            delete_active_conversation(client_id)
            return

        # Для обычных пользователей: завершение своего диалога
        if user_id in active_conversations:
            # Уведомляем основателя, если диалог был назначен и ID валиден
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
                    # Обрабатываем ошибку пустого chat_id
                    if "Chat_id is empty" in str(e):
                        logger.error(f"Помилка сповіщення власника {owner_id}: невірний chat_id")
                    else:
                        logger.error(f"Помилка сповіщення власника {owner_id}: {e}")

            # Удаляем диалог
            del active_conversations[user_id]
            await update.message.reply_text(
                "✅ Ваш діалог завершено.\n\n"
                "Ви можете розпочати новий діалог за допомогою /start."
            )
            
            # Удаляем из базы данных
            delete_active_conversation(user_id)
            return

        # Если нет активного диалога
        await update.message.reply_text(
            "ℹ️ У вас немає активного діалогу для завершення.\n\n"
            "Щоб розпочати новий діалог, використовуйте /start."
        )
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать статистику для основателей"""
        owner_id = update.effective_user.id
        
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
            
        # Получаем общее количество пользователей из базы
        total_users_db = get_total_users_count()
        
        first_start = datetime.fromisoformat(bot_statistics['first_start'])
        last_save = datetime.fromisoformat(bot_statistics['last_save'])
        uptime = datetime.now() - first_start
        
        stats_message = f"""
📊 Статистика бота:

👤 Усього користувачів (файл): {bot_statistics['total_users']}
👤 Усього користувачів (БД): {total_users_db}
🛒 Усього замовлень: {bot_statistics['total_orders']}
❓ Усього запитаннь: {bot_statistics['total_questions']}
⏱️ Перший запуск: {first_start.strftime('%d.%m.%Y %H:%M')}
⏱️ Останнє збереження: {last_save.strftime('%d.%m.%Y %H:%M')}
⏱️ Час роботи: {uptime}
        """
        
        await update.message.reply_text(stats_message.strip())
        
        # Добавляем экспорт пользователей в JSON
        all_users = get_all_users()
        if all_users:
            # Преобразуем в JSON
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
                    active_chats = cur.fetchall()
                    
            if not active_chats:
                await update.message.reply_text("ℹ️ Нет активных чатов.")
                return
                
            message = "🔄 Активные чаты:\n\n"
            keyboard = []
            for chat in active_chats:
                # Получаем актуальную информацию о пользователе
                client_info = {
                    'first_name': chat['first_name'] or 'Неизвестный',
                    'username': chat['username'] or 'N/A'
                }
                
                # Добавим информацию о назначенном владельце
                owner_info = "Не назначен"
                if chat['assigned_owner']:
                    if chat['assigned_owner'] == OWNER_ID_1:
                        owner_info = "@HiGki2pYYY"
                    elif chat['assigned_owner'] == OWNER_ID_2:
                        owner_info = "@oc33t"
                    else:
                        owner_info = f"ID: {chat['assigned_owner']}"
                
                # Форматируем имя пользователя
                username = f"@{client_info['username']}" if client_info['username'] else "нет"
                
                message += (
                    f"👤 {client_info['first_name']} ({username})\n"
                    f"   Тип: {chat['conversation_type']}\n"
                    f"   Назначен: {owner_info}\n"
                    f"   Последнее сообщение: {chat['last_message'][:50]}{'...' if len(chat['last_message']) > 50 else ''}\n"
                    f"   [ID: {chat['user_id']}]\n\n"
                )
                
                # Добавляем кнопку "Продолжить" для каждого чата
                keyboard.append([
                    InlineKeyboardButton(
                        f"Продолжить диалог с {client_info['first_name']}",
                        callback_data=f'continue_chat_{chat["user_id"]}'
                    )
                ])
                
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                message.strip(),
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения активных чатов: {e}")
            await update.message.reply_text("❌ Произошла ошибка при получении активных чатов.")

    async def show_conversation_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает историю переписки с пользователем"""
        owner_id = update.effective_user.id
        
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
            
        # Проверяем, есть ли аргумент команды (user_id)
        if not context.args:
            await update.message.reply_text("ℹ️ Использование: /history <user_id>")
            return
            
        try:
            user_id = int(context.args[0])
            history = get_conversation_history(user_id)
            
            if not history:
                await update.message.reply_text(f"ℹ️ Нет истории сообщений для пользователя {user_id}.")
                return
                
            # Получаем информацию о пользователе
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                    user_info = cur.fetchone()
                    
            if not user_info:
                user_info = {'first_name': 'Неизвестный', 'username': 'N/A'}
            
            message = (
                f"📨 История переписки с пользователем:\n\n"
                f"👤 {user_info['first_name']} (@{user_info.get('username', 'N/A')})\n"
                f"🆔 ID: {user_id}\n\n"
            )
            
            for msg in reversed(history):  # В хронологическом порядке
                sender = "👤 Клиент" if msg['is_from_user'] else "👨‍💼 Магазин"
                message += f"{sender} [{msg['created_at'].strftime('%d.%m.%Y %H:%M')}]:\n{msg['message']}\n\n"
            
            # Разбиваем сообщение на части, если оно слишком длинное
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
        """Команда для начала диалога с пользователем по ID"""
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
            await update.message.reply_text("❌ Пользователь не найден в базе данных.")
            return

        # Создаем объект пользователя Telegram
        client_user = User(
            id=client_info['id'],
            first_name=client_info['first_name'],
            last_name=client_info.get('last_name', ''),
            username=client_info.get('username', ''),
            language_code=client_info.get('language_code', ''),
            is_bot=False
        )

        # Создаем активный диалог (если его нет)
        if client_id not in active_conversations:
            active_conversations[client_id] = {
                'type': 'manual',
                'user_info': client_user,  # Используем объект пользователя
                'assigned_owner': owner_id,
                'last_message': "Диалог начат основателем"
            }
            save_active_conversation(client_id, 'manual', owner_id, "Диалог начат основателем")
        else:
            # Если диалог уже есть, назначаем текущего основателя
            active_conversations[client_id]['assigned_owner'] = owner_id
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                active_conversations[client_id]['last_message']
            )

        # Запоминаем связь основателя и клиента
        owner_client_map[owner_id] = client_id

        # Получаем историю переписки
        history = get_conversation_history(client_id)

        # Формируем сообщение с историей
        history_text = "📨 История переписки:\n\n"
        for msg in reversed(history):  # в хронологическом порядке
            sender = "👤 Клиент" if msg['is_from_user'] else "👨‍💼 Магазин"
            history_text += f"{sender} [{msg['created_at'].strftime('%d.%m.%Y %H:%M')}]:\n{msg['message']}\n\n"

        # Отправляем историю основателю
        try:
            await update.message.reply_text(history_text[:4096])
            # Если история длинная, отправляем остальные части
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
                [InlineKeyboardButton("💬 ChatGPT", callback_data='category_chatgpt')],
                [InlineKeyboardButton("🎮 Discord", callback_data='category_discord')],
                [InlineKeyboardButton("📚 Duolingo", callback_data='category_duolingo')],
                [InlineKeyboardButton("📸 PicsArt", callback_data='category_picsart')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text(
                "📦 Оберіть категорію товару:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Кнопка "Назад" в главное меню
        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text(
                "Головне меню:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Обработка кнопки "help"
        elif query.data == 'help':
            await self.show_help(query.message)
        
        # Меню ChatGPT
        elif query.data == 'category_chatgpt':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_1')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "💬 Оберіть варіант ChatGPT Plus:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Меню Discord
        elif query.data == 'category_discord':
            keyboard = [
                [InlineKeyboardButton("Nitro Basic", callback_data='discord_basic')],
                [InlineKeyboardButton("Nitro Full", callback_data='discord_full')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "🎮 Оберіть тип Discord Nitro:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Подменю Discord Basic
        elif query.data == 'discord_basic':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 100 UAH", callback_data='discord_basic_1')],
                [InlineKeyboardButton("12 місяців - 900 UAH", callback_data='discord_basic_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text(
                "🔹 Discord Nitro Basic:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Подменю Discord Full
        elif query.data == 'discord_full':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 170 UAH", callback_data='discord_full_1')],
                [InlineKeyboardButton("12 місяців - 1700 UAH", callback_data='discord_full_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text(
                "✨ Discord Nitro Full:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Меню Duolingo
        elif query.data == 'category_duolingo':
            keyboard = [
                [InlineKeyboardButton("👨‍👩‍👧‍👦 Family", callback_data='duolingo_family')],
                [InlineKeyboardButton("👤 Individual", callback_data='duolingo_individual')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "📚 Оберіть тип підписки Duolingo:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Подменю Duolingo Family
        elif query.data == 'duolingo_family':
            keyboard = [
                [InlineKeyboardButton("12 місяців - 380 UAH (на 1 людину)", callback_data='duolingo_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text(
                "👨‍👩‍👧‍👦 Duolingo Family:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Подменю Duolingo Individual
        elif query.data == 'duolingo_individual':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 200 UAH", callback_data='duolingo_ind_1')],
                [InlineKeyboardButton("12 місяців - 1500 UAH", callback_data='duolingo_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text(
                "👤 Duolingo Individual:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Меню PicsArt
        elif query.data == 'category_picsart':
            keyboard = [
                [InlineKeyboardButton("✨ PicsArt Plus", callback_data='picsart_plus')],
                [InlineKeyboardButton("🚀 PicsArt Pro", callback_data='picsart_pro')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "📸 Оберіть версію PicsArt:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Подменю PicsArt Plus
        elif query.data == 'picsart_plus':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 130 UAH", callback_data='picsart_plus_1')],
                [InlineKeyboardButton("12 місяців - 800 UAH", callback_data='picsart_plus_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text(
                "✨ PicsArt Plus:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Подменю PicsArt Pro
        elif query.data == 'picsart_pro':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 180 UAH", callback_data='picsart_pro_1')],
                [InlineKeyboardButton("12 місяців - 1000 UAH", callback_data='picsart_pro_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text(
                "🚀 PicsArt Pro:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Обработка выбора товара
        elif query.data in [
            'chatgpt_1',
            'discord_basic_1', 'discord_basic_12',
            'discord_full_1', 'discord_full_12',
            'duolingo_ind_1', 'duolingo_ind_12', 'duolingo_fam_12',
            'picsart_plus_1', 'picsart_plus_12',
            'picsart_pro_1', 'picsart_pro_12'
        ]:
            # Сохраняем выбранный товар в контексте
            context.user_data['selected_product'] = query.data
            
            # Определяем название и цену продукта
            product_info = self.get_product_info(query.data)
            
            keyboard = [
                [InlineKeyboardButton("✅ Замовити", callback_data='confirm_order')],
                [InlineKeyboardButton("⬅️ Назад", callback_data=self.get_back_action(query.data))]
            ]
            
            await query.edit_message_text(
                f"🛒 Ви обрали:\n\n"
                f"{product_info['name']}\n"
                f"💵 Ціна: {product_info['price']} UAH\n\n"
                f"Натисніть \"✅ Замовити\" для підтвердження замовлення.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Подтверждение заказа
        elif query.data == 'confirm_order':
            selected_product = context.user_data.get('selected_product')
            if not selected_product:
                await query.edit_message_text("❌ Помилка: товар не обраний")
                return
                
            product_info = self.get_product_info(selected_product)
            order_text = f"🛍️ Хочу замовити: {product_info['name']} за {product_info['price']} UAH"
            
            # Сохраняем заказ
            active_conversations[user_id] = {
                'type': 'order',
                'user_info': user,
                'assigned_owner': None,
                'order_details': order_text,
                'last_message': order_text
            }
            
            # Сохраняем в БД
            save_active_conversation(user_id, 'order', None, order_text)
            
            # Обновляем статистику
            bot_statistics['total_orders'] += 1
            save_stats()
            
            await query.edit_message_text(
                "✅ Ваше замовлення прийнято! Засновник магазину зв'яжеться з вами найближчим часом.\n\n"
                "Ви можете продовжити з іншим запитанням або замовленням.",
                reply_markup=None
            )
            
            # Пересылаем заказ обоим владельцам
            await self.forward_order_to_owners(
                context, 
                user_id, 
                user, 
                order_text
            )
        
        # Обработка кнопки "question"
        elif query.data == 'question':
            # Проверяем активные диалоги
            if user_id in active_conversations:
                await query.answer(
                    "❗ У вас вже є активний діалог.\n\n"
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
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину.\n\n"
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
            
            # Сохраняем в БД
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                active_conversations[client_id]['last_message']
            )
            
            # Уведомляем основателя
            client_info = active_conversations[client_id]['user_info']
            await query.edit_message_text(
                f"✅ Ви взяли замовлення від клієнта {client_info.first_name}."
            )
            
            # Уведомляем другого основателя
            other_owner = OWNER_ID_2 if owner_id == OWNER_ID_1 else OWNER_ID_1
            try:
                await context.bot.send_message(
                    chat_id=other_owner,
                    text=f"ℹ️ Замовлення від клієнта {client_info.first_name} взяв інший представник."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления другого основателя: {e}")
            
            # Уведомляем клиента
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text=f"✅ Ваш запит прийняв засновник магазину. Очікуйте на відповідь."
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления клиента: {e}")
        
        # Передача диалога другому основателю
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
                
                # Обновляем в БД
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
                
                # Отправляем диалог другому основателю
                await context.bot.send_message(
                    chat_id=other_owner,
                    text=f"📨 Вам передан чат с клиентом:\n\n"
                         f"👤 {client_info.first_name} (@{client_info.username or 'не указан'})\n"
                         f"🆔 ID: {client_info.id}\n\n"
                         f"Останнє повідомлення:\n{last_message}\n\n"
                         f"Для ответа просто напишите сообщение. Для завершения диалога используйте /stop"
                )
        
        # Обработка кнопки "Продолжить диалог" из команды /chats
        elif query.data.startswith('continue_chat_'):
            client_id = int(query.data.split('_')[2])
            owner_id = user_id

            # Проверяем, существует ли активный диалог
            if client_id not in active_conversations:
                await query.answer("Диалог уже завершен", show_alert=True)
                return

            # Проверяем, не назначен ли диалог другому основателю
            if 'assigned_owner' in active_conversations[client_id] and \
               active_conversations[client_id]['assigned_owner'] != owner_id:
                other_owner = active_conversations[client_id]['assigned_owner']
                other_owner_name = "@HiGki2pYYY" if other_owner == OWNER_ID_1 else "@oc33t"
                await query.answer(
                    f"Диалог уже ведет {other_owner_name}.",
                    show_alert=True
                )
                return

            # Назначаем текущего основателя на диалог
            active_conversations[client_id]['assigned_owner'] = owner_id
            owner_client_map[owner_id] = client_id
            
            # Сохраняем в БД
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                active_conversations[client_id]['last_message']
            )
            
            # Получаем историю переписки
            history = get_conversation_history(client_id)
            
            # Формируем сообщение с историей
            history_text = "📨 История переписки:\n\n"
            for msg in reversed(history):  # в хронологическом порядке
                sender = "👤 Клиент" if msg['is_from_user'] else "👨‍💼 Магазин"
                history_text += f"{sender} [{msg['created_at'].strftime('%d.%m.%Y %H:%M')}]:\n{msg['message']}\n\n"
            
            # Отправляем историю основателю
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=history_text[:4096]  # ограничение Telegram
                )
                # Если история длинная, отправляем остальные части
                if len(history_text) > 4096:
                    parts = [history_text[i:i+4096] for i in range(4096, len(history_text), 4096)]
                    for part in parts:
                        await context.bot.send_message(chat_id=owner_id, text=part)
            except Exception as e:
                logger.error(f"Ошибка отправки истории: {e}")
            
            # Отправляем последнее сообщение и предлагаем ответить
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"💬 Последнее сообщение от клиента:\n{active_conversations[client_id]['last_message']}\n\n"
                     "Напишите ответ:"
            )
            
            await query.edit_message_text(f"✅ Вы продолжили диалог с клиентом ID: {client_id}.")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user = update.effective_user
        user_id = user.id
        
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)
        
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
        
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await self.handle_owner_message(update, context)
            return
        
        # Сохраняем последнее сообщение
        message_text = update.message.text
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
        
        await self.forward_to_owner(update, context)
    
    async def forward_to_owner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Пересылка сообщения клиента основателю"""
        user_id = update.effective_user.id
        
        # Проверяем существование диалога
        if user_id not in active_conversations:
            logger.warning(f"Попытка переслать сообщение для несуществующего диалога: {user_id}")
            return
        
        user_info = active_conversations[user_id]['user_info']
        conversation_type = active_conversations[user_id]['type']
        
        assigned_owner = active_conversations[user_id].get('assigned_owner')
        
        # Если заказ/вопрос еще не взят - отправляем обоим основателям
        if not assigned_owner:
            await self.forward_to_both_owners(
                context, 
                user_id, 
                user_info, 
                conversation_type, 
                update.message.text
            )
            return
        
        # Если заказ уже взят - отправляем только назначенному основателю
        await self.forward_to_specific_owner(
            context, 
            user_id, 
            user_info, 
            conversation_type, 
            update.message.text, 
            assigned_owner
        )
    
    async def forward_to_both_owners(self, context, client_id, client_info, conversation_type, message_text):
        """Пересылка сообщения обоим основателям"""
        type_emoji = "🛒" if conversation_type == 'order' else "❓"
        type_text = "ЗАКАЗ" if conversation_type == 'order' else "ВОПРОС"
        
        forward_message = f"""
{type_emoji} НОВЫЙ {type_text}!

👤 Клиент: {client_info.first_name}
📱 Username: @{client_info.username if client_info.username else 'не указан'}
🆔 ID: {client_info.id}
🌐 Язык: {client_info.language_code or 'не указан'}

💬 Сообщение:
{message_text}

---
Нажмите "✅ Взять", чтобы обработать запрос.
        """
        
        keyboard = [
            [InlineKeyboardButton("✅ Взять", callback_data=f'take_order_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Отправляем обоим основателям
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=forward_message.strip(),
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения основателю {owner_id}: {e}")
        
        # Уведомляем клиента
        await context.bot.send_message(
            chat_id=client_id,
            text="✅ Ваше повідомлення передано засновникам магазину. "
                 "Очікуйте на відповідь найближчим часом."
        )
    
    async def forward_to_specific_owner(self, context, client_id, client_info, conversation_type, message_text, owner_id):
        """Пересылка сообщения конкретному основателю"""
        type_emoji = "🛒" if conversation_type == 'order' else "❓"
        type_text = "ЗАКАЗ" if conversation_type == 'order' else "ВОПРОС"
        owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"
        
        forward_message = f"""
{type_emoji} {type_text} от клиента:

👤 Пользователь: {client_info.first_name}
📱 Username: @{client_info.username if client_info.username else 'не указан'}
🆔 ID: {client_info.id}
🌐 Язык: {client_info.language_code or 'не указан'}

💬 Сообщение:
{message_text}

---
Для ответа просто напишите сообщение в этот чат.
Для завершения диалога используйте /stop.
Назначен: {owner_name}
        """
        
        keyboard = [
            [InlineKeyboardButton("🔄 Передать другому основателю", callback_data=f'transfer_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text=forward_message.strip(),
                reply_markup=reply_markup
            )
            
            # Уведомляем клиента
            await context.bot.send_message(
                chat_id=client_id,
                text="✅ Ваше повідомлення передано засновнику магазину. "
                     "Очікуйте на відповідь найближчим часом."
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения основателю {owner_id}: {e}")
            # Если не удалось отправить - пробуем другому основателю
            other_owner = OWNER_ID_2 if owner_id == OWNER_ID_1 else OWNER_ID_1
            active_conversations[client_id]['assigned_owner'] = other_owner
            owner_client_map[other_owner] = client_id
            
            # Обновляем в БД
            save_active_conversation(
                client_id, 
                conversation_type, 
                other_owner, 
                message_text
            )
            
            await self.forward_to_specific_owner(context, client_id, client_info, conversation_type, message_text, other_owner)
    
    async def forward_order_to_owners(self, context, client_id, client_info, order_text):
        """Пересылает заказ обоим владельцам"""
        # Сохраняем последнее сообщение
        active_conversations[client_id]['last_message'] = order_text
        
        # Сохраняем в БД
        save_active_conversation(client_id, 'order', None, order_text)
        
        # Добавляем пометку о сайте, если заказ оттуда
        source = "з сайту" if active_conversations[client_id].get('from_website', False) else ""
        
        forward_message = f"""
🛒 НОВЕ ЗАМОВЛЕННЯ {source}!

👤 Клієнт: {client_info.first_name}
📱 Username: @{client_info.username if client_info.username else 'не вказано'}
🆔 ID: {client_info.id}
🌐 Язык: {client_info.language_code or 'не указан'}

📋 Деталі замовлення:
{order_text}

---
Нажмите "✅ Взять", чтобы обработать этот заказ.
        """
        
        keyboard = [
            [InlineKeyboardButton("✅ Взять", callback_data=f'take_order_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Отправляем обоим основателям
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=forward_message.strip(),
                    reply_markup=reply_markup
                )
                logger.info(f"✅ Уведомление о заказе отправлено владельцу {owner_id}")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки заказа основателю {owner_id}: {e}")
    
    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка сообщений от основателя"""
        owner = update.effective_user
        owner_id = owner.id
        
        # Гарантируем наличие владельца в БД
        ensure_user_exists(owner)
        
        if owner_id not in owner_client_map:
            owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(
                f"У вас нет активного клиента для ответа. ({owner_name})\n"
                f"Дождитесь нового сообщения от клиента или используйте команду /dialog."
            )
            return
        
        client_id = owner_client_map[owner_id]
        
        if client_id not in active_conversations:
            del owner_client_map[owner_id]
            await update.message.reply_text(
                "Диалог с клиентом завершен или не найден."
            )
            return
        
        try:
            # Сохраняем сообщение от основателя
            message_text = update.message.text
            save_message(client_id, message_text, False)
            
            # Обновляем последнее сообщение
            active_conversations[client_id]['last_message'] = message_text
            save_active_conversation(
                client_id, 
                active_conversations[client_id]['type'], 
                owner_id, 
                message_text
            )
            
            await context.bot.send_message(
                chat_id=client_id,
                text=f"📩 Відповідь від магазину:\n\n{message_text}"
            )
            
            client_info = active_conversations[client_id]['user_info']
            await update.message.reply_text(
                f"✅ Сообщение отправлено клиенту {client_info.first_name}"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения клиенту {client_id}: {e}")
            await update.message.reply_text(
                "❌ Ошибка при отправке сообщения клиенту. "
                "Возможно, клиент заблокировал бота."
            )
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.warning(f'Update {update} caused error {context.error}')
    
    def start_ping_service(self):
        """Запуск пинговалки в отдельном потоке"""
        if not self.ping_running:
            self.ping_running = True
            ping_thread = threading.Thread(target=self.ping_loop)
            ping_thread.daemon = True
            ping_thread.start()
            logger.info("🔄 Пинговалка запущена")
    
    def ping_loop(self):
        """Цикл пинга сервиса"""
        import requests
        ping_url = f"{WEBHOOK_URL}/ping"
        
        while self.ping_running:
            try:
                response = requests.get(ping_url, timeout=10)
                if response.status_code == 200:
                    logger.info("✅ Ping успешен - сервис активен")
                else:
                    logger.warning(f"⚠️ Ping вернул статус {response.status_code}")
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ Ошибка ping: {e}")
            except Exception as e:
                logger.error(f"❌ Неожиданная ошибка ping: {e}")
            
            time.sleep(PING_INTERVAL)
    
    def get_product_info(self, product_code):
        """Возвращает информацию о товаре по его коду"""
        products = {
            'chatgpt_1': {'name': "ChatGPT Plus (1 місяць)", 'price': 650},
            'discord_basic_1': {'name': "Discord Nitro Basic (1 місяць)", 'price': 100},
            'discord_basic_12': {'name': "Discord Nitro Basic (12 місяців)", 'price': 900},
            'discord_full_1': {'name': "Discord Nitro Full (1 місяць)", 'price': 170},
            'discord_full_12': {'name': "Discord Nitro Full (12 місяців)", 'price': 1700},
            'duolingo_ind_1': {'name': "Duolingo Individual (1 місяць)", 'price': 200},
            'duolingo_ind_12': {'name': "Duolingo Individual (12 місяців)", 'price': 1500},
            'duolingo_fam_12': {'name': "Duolingo Family (12 місяців)", 'price': 380},
            'picsart_plus_1': {'name': "PicsArt Plus (1 місяць)", 'price': 130},
            'picsart_plus_12': {'name': "PicsArt Plus (12 місяців)", 'price': 800},
            'picsart_pro_1': {'name': "PicsArt Pro (1 місяць)", 'price': 180},
            'picsart_pro_12': {'name': "PicsArt Pro (12 місяців)", 'price': 1000},
        }
        return products.get(product_code, {'name': "Невідомий товар", 'price': 0})
    
    def get_back_action(self, product_code):
        """Возвращает действие для кнопки 'Назад' в зависимости от товара"""
        category_map = {
            'chatgpt_1': 'category_chatgpt',
            'discord_basic_1': 'discord_basic',
            'discord_basic_12': 'discord_basic',
            'discord_full_1': 'discord_full',
            'discord_full_12': 'discord_full',
            'duolingo_ind_1': 'duolingo_individual',
            'duolingo_ind_12': 'duolingo_individual',
            'duolingo_fam_12': 'duolingo_family',
            'picsart_plus_1': 'picsart_plus',
            'picsart_plus_12': 'picsart_plus',
            'picsart_pro_1': 'picsart_pro',
            'picsart_pro_12': 'picsart_pro',
        }
        return category_map.get(product_code, 'order')

bot_instance = TelegramBot()

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

async def setup_webhook():
    if USE_POLLING:
        try:
            await telegram_app.bot.delete_webhook()
            logger.info("🗑️ Webhook удален - используется polling режим")
        except Exception as e:
            logger.error(f"Ошибка удаления webhook: {e}")
        return True
    
    try:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await telegram_app.bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook установлен: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка установки webhook: {e}")
        return False

async def start_bot():
    global telegram_app, bot_running
    
    with bot_lock:
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
        logger.error(f"🚨 Критический конфликт: {e}")
        logger.warning("🕒 Ожидаем 30 секунд перед повторным запуском...")
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

def auto_save_loop():
    """Функция автосохранения статистики"""
    while True:
        time.sleep(300)  # 5 минут
        save_stats()
        logger.info("✅ Статистика автосохранена")

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
    logger.info(f"🔄 РЕЖИМ: {'Polling' if USE_POLLING else 'Webhook'}")
    logger.info(f"👤 Основатель 1: {OWNER_ID_1} (@HiGki2pYYY)")
    logger.info(f"👤 Основатель 2: {OWNER_ID_2} (@oc33t)")
    logger.info(f"💾 DATABASE_URL: {DATABASE_URL[:30]}...")
    
    bot_thread_instance = threading.Thread(target=bot_thread)
    bot_thread_instance.daemon = True
    bot_thread_instance.start()
    
    time.sleep(3)
    
    bot_instance.start_ping_service()
    
    logger.info("🌐 Запуск Flask сервера...")
    flask_app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )

if __name__ == '__main__':
    main()
