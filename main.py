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
# Импорт BotCommandScopeDefault добавлен здесь
from telegram.constants import BotCommandScopeDefault
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
# - Добавленные/обновленные переменные окружения -
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY') # "FTD5K08-DE94C4F-M9RB0XS-XSGBA26"
EXCHANGE_RATE_UAH_TO_USD = float(os.getenv('EXCHANGE_RATE_UAH_TO_USD', 41.26))
# - Конец добавленных переменных -
# Путь к файлу с данными
STATS_FILE = "bot_stats.json"
# Оптимизация: буферизация запросов к БД
BUFFER_FLUSH_INTERVAL = 300 # 5 минут
BUFFER_MAX_SIZE = 50

# Глобальные переменные для данных (в памяти, для быстрого доступа)
bot_statistics = {
    'total_orders': 0,
    'total_questions': 0,
    'active_chats': 0,
    'last_reset': datetime.now().isoformat()
}
message_buffer = []
active_conv_buffer = []
user_cache = set()
# Оптимизация: кэш для истории сообщений
history_cache = {}
# - Добавленные/обновлённые словари и списки -
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
# - Конец добавленных словарей -

# Словари для хранения данных (в памяти, для быстрого доступа)
active_conversations = {} # {user_id: {...}}
owner_client_map = {} # {owner_id: client_id}

# Глобальные переменные для приложения
telegram_app = None
flask_app = Flask(__name__)
CORS(flask_app) # Разрешаем CORS для всех доменов

# - Добавленные/обновлённые вспомогательные функции -
def get_uah_amount_from_order_text(order_text: str) -> float:
    """Извлекает сумму в UAH из текста заказа."""
    match = re.search(r'💳 Всього: (\d+) UAH', order_text)
    if match:
        return float(match.group(1))
    return 0.0

def load_stats():
    """Загрузка статистики из файла"""
    global bot_statistics
    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            bot_statistics = json.load(f)
        logger.info("✅ Статистика загружена")
    except FileNotFoundError:
        logger.warning("⚠️ Файл статистики не найден, используется по умолчанию")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки статистики: {e}")

def save_stats():
    """Сохранение статистики в файл"""
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(bot_statistics, f, ensure_ascii=False, indent=4)
        logger.debug("💾 Статистика сохранена")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения статистики: {e}")

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message):
    """Сохранение активного диалога в буфер"""
    active_conv_buffer.append({
        'user_id': user_id,
        'conversation_type': conversation_type,
        'assigned_owner': assigned_owner,
        'last_message': last_message
    })
    # Если буфер полный, сбрасываем его
    if len(active_conv_buffer) >= BUFFER_MAX_SIZE:
        flush_active_conv_buffer()

def flush_active_conv_buffer():
    """Сброс буфера активных диалогов в БД"""
    global active_conv_buffer
    if not active_conv_buffer:
        return
    try:
        # Группируем данные по user_id, оставляя последнюю запись для каждого
        latest_convs = {}
        for conv in active_conv_buffer:
            latest_convs[conv['user_id']] = conv

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Создаем временную таблицу
                cur.execute("""
                    CREATE TEMP TABLE temp_active_convs (
                        user_id BIGINT,
                        conversation_type VARCHAR(50),
                        assigned_owner BIGINT,
                        last_message TEXT
                    ) ON COMMIT DROP;
                """)
                # Заполняем временную таблицу
                cur.executemany("""
                    INSERT INTO temp_active_convs (user_id, conversation_type, assigned_owner, last_message)
                    VALUES (%s, %s, %s, %s);
                """, list(latest_convs.values()))
                # Обновляем или вставляем данные из временной таблицы в основную
                cur.execute("""
                    INSERT INTO active_conversations (user_id, conversation_type, assigned_owner, last_message)
                    SELECT user_id, conversation_type, assigned_owner, last_message FROM temp_active_convs
                    ON CONFLICT (user_id) DO UPDATE SET
                        conversation_type = EXCLUDED.conversation_type,
                        assigned_owner = EXCLUDED.assigned_owner,
                        last_message = EXCLUDED.last_message,
                        updated_at = CURRENT_TIMESTAMP;
                """)
                conn.commit()
        logger.info(f"✅ Активные диалоги ({len(latest_convs)}) сброшены в БД")
        active_conv_buffer.clear()
    except Exception as e:
        logger.error(f"❌ Ошибка сброса активных диалогов: {e}")

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
        logger.debug(f"👤 Пользователь {user.first_name} ({user.id}) добавлен/обновлён в БД")
    except Exception as e:
        logger.error(f"❌ Ошибка добавления пользователя {user.id}: {e}")

def save_message(user_id, message, is_from_user):
    """Сохранение сообщения в буфер"""
    message_buffer.append((user_id, message, is_from_user))
    # Если буфер полный, сбрасываем его
    if len(message_buffer) >= BUFFER_MAX_SIZE:
        flush_message_buffer()

def flush_message_buffer():
    """Сброс буфера сообщений в БД"""
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
                cur.executemany("""
                    INSERT INTO temp_messages (user_id, message, is_from_user)
                    VALUES (%s, %s, %s);
                """, message_buffer)
                # Вставляем данные из временной таблицы в основную
                cur.execute("""
                    INSERT INTO messages (user_id, message, is_from_user)
                    SELECT user_id, message, is_from_user FROM temp_messages;
                """)
                conn.commit()
        logger.info(f"✅ Сообщения ({len(message_buffer)}) сброшены в БД")
        message_buffer.clear()
    except Exception as e:
        logger.error(f"❌ Ошибка сброса сообщений: {e}")

class TelegramBot:
    def __init__(self):
        self.application = None
        self.loop = None

    async def initialize(self):
        """Инициализация Telegram Application"""
        try:
            # Создание Application
            self.application = Application.builder().token(BOT_TOKEN).build()

            # Добавление обработчиков
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("order", self.order_command))
            self.application.add_handler(CommandHandler("question", self.question_command))
            self.application.add_handler(CommandHandler("channel", self.channel_command))
            self.application.add_handler(CommandHandler("stop", self.stop_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(CommandHandler("stats", self.stats_command))
            self.application.add_handler(CommandHandler("history", self.history_command))
            self.application.add_handler(CommandHandler("chats", self.chats_command))
            self.application.add_handler(CommandHandler("clear", self.clear_command))
            # - Добавленные обработчики -
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
            # - Добавлено: Обработчик callback кнопок оплаты -
            self.application.add_handler(CallbackQueryHandler(self.payment_callback_handler, pattern='^(pay_|check_payment_status|manual_payment_confirmed|back_to_)'))
            # - Конец добавленных обработчиков -

            # Установка меню команд
            await self.set_commands_menu()

            logger.info("✅ Telegram Application инициализирована")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации Telegram Application: {e}")
            raise

    async def set_commands_menu(self):
        """Установка стандартного меню команд"""
        owner_commands = [
            BotCommandScopeChat(chat_id=OWNER_ID_1),
            BotCommandScopeChat(chat_id=OWNER_ID_2)
        ]
        # Используем BotCommandScopeDefault вместо BotCommandScopeChat(chat_id='*')
        user_commands = [BotCommandScopeDefault()] # Для всех остальных

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

            # Команды для обычных пользователей
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

    # - Добавленные/обновлённые обработчики команд -
    async def continue_dialog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /dialog для продолжения диалога (для основателей)"""
        user = update.effective_user
        user_id = user.id

        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ Ця команда доступна тільки засновникам магазину.")
            return

        # Проверяем, есть ли активный диалог с клиентом
        if user_id in owner_client_map:
            client_id = owner_client_map[user_id]
            client_info = active_conversations.get(client_id, {}).get('user_info')
            if client_info:
                client_name = client_info.first_name
                client_username = client_info.username or "не вказано"
                await update.message.reply_text(
                    f"💬 Ви вже ведете діалог з клієнтом {client_name} (@{client_username})."
                    f"\nНапишіть повідомлення, щоб продовжити."
                )
                return
            else:
                # Если информация о клиенте потеряна, очищаем связь
                del owner_client_map[user_id]

        # Если активного диалога нет, предлагаем выбрать из активных
        active_orders = {k: v for k, v in active_conversations.items() if v.get('type') in ['order', 'subscription_order', 'digital_order', 'manual']}
        active_questions = {k: v for k, v in active_conversations.items() if v.get('type') == 'question'}

        if not active_orders and not active_questions:
            await update.message.reply_text("📭 Немає активних діалогів.")
            return

        keyboard = []
        for client_id, conv_data in {**active_orders, **active_questions}.items():
            client_info = conv_data.get('user_info')
            if client_info:
                conv_type = "🛍️" if conv_data.get('type') in ['order', 'subscription_order', 'digital_order', 'manual'] else "❓"
                button_text = f"{conv_type} {client_info.first_name}"
                if client_info.username:
                    button_text += f" (@{client_info.username})"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f'dialog_{client_id}')])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("💬 Оберіть діалог для продовження:", reply_markup=reply_markup)

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
                # Извлекаем сумму в UAH
                uah_amount = get_uah_amount_from_order_text(order_text)
                if uah_amount > 0:
                    # Сохраняем заказ в контексте пользователя
                    context.user_data['pending_order_details'] = order_text
                    # Переходим к выбору оплаты
                    await self.proceed_to_payment(update, context, uah_amount)
                    return
                else:
                    await update.message.reply_text("❌ Не вдалося визначити суму замовлення.")
                    return
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
            order_text = f"🛍️ Замовлення з сайту (#{order_id}):\n"
            order_text += "\n".join(order_details)
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

            # Отправляем подтверждение пользователю
            keyboard = [
                [InlineKeyboardButton("💳 Оплатити", callback_data='proceed_to_payment')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            confirmation_text = f"""✅ Ваше замовлення прийнято!\n{order_text}\nБудь ласка, оберіть дію 👇"""
            await update.message.reply_text(confirmation_text.strip(), reply_markup=reply_markup)

    # - Конец добавленных/обновлённых обработчиков -

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        user_id = user.id
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        welcome_text = f"""👋 Доброго дня, {user.first_name}!
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

    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает информацию о канале"""
        channel_text = """📢 Наш канал: @SecureShopChannel
Тут ви можете переглянути:
- Асортимент товарів
- Оновлення магазину
- Розіграші та акції
Приєднуйтесь, щоб бути в курсі всіх новин!"""
        await update.message.reply_text(channel_text)

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop - завершение диалога"""
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
                # Сохраняем в БД
                save_active_conversation(client_id, None, None, None) # Удаляем запись
            # Удаляем связь владелец-клиент
            del owner_client_map[user_id]
            # Обновляем статистику
            bot_statistics['active_chats'] = max(0, bot_statistics['active_chats'] - 1)
            save_stats()
            # Отправляем подтверждение основателю
            await update.message.reply_text("✅ Діалог із клієнтом завершено.")
            # Отправляем подтверждение клиенту
            if client_info:
                try:
                    keyboard = [
                        [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                        [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                        [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    confirmation_text = "✅ Ваш діалог із засновником магазину завершено."
                    await context.bot.send_message(chat_id=client_id, text=confirmation_text, reply_markup=reply_markup)
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения клиенту {client_id}: {e}")
            return

        # Для обычных пользователей: завершение своего диалога
        if user_id in active_conversations:
            conversation_type = active_conversations[user_id].get('type', 'unknown')
            # Удаляем диалог из активных (в памяти)
            del active_conversations[user_id]
            # Сохраняем в БД
            save_active_conversation(user_id, None, None, None) # Удаляем запись
            # Обновляем статистику
            bot_statistics['active_chats'] = max(0, bot_statistics['active_chats'] - 1)
            save_stats()
            # Отправляем подтверждение пользователю
            if conversation_type in ['order', 'subscription_order', 'digital_order', 'manual']:
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
        else:
            await update.message.reply_text("📭 У вас немає активного діалогу.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает справку и информацию о сервисе"""
        # Универсальный метод для обработки команды и кнопки
        if isinstance(update, Update):
            message = update.message
        else:
            message = update # для вызова из кнопки

        help_text = f"""👋 Доброго дня! Я бот магазину SecureShop.
🤖 Я бот магазину SecureShop.

📌 Список доступних команд:
/start - Головне меню
/order - Зробити замовлення
/question - Поставити запитання
/channel - Наш канал з асортиментом, оновленнями та розіграшами
/stop - Завершити поточний діалог
/help - Ця довідка
💬 Якщо у вас виникли питання, не соромтеся звертатися!"""
        await message.reply_text(help_text.strip())

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статистику бота (для основателей)"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ Ця команда доступна тільки засновникам магазину.")
            return

        load_stats() # Перезагружаем статистику из файла
        stats_text = f"""📊 Статистика бота:
📦 Всього замовлень: {bot_statistics['total_orders']}
❓ Всього запитань: {bot_statistics['total_questions']}
💬 Активних чатів: {bot_statistics['active_chats']}
🕒 Останнє скидання: {bot_statistics['last_reset']}"""
        await update.message.reply_text(stats_text)

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает историю сообщений с пользователем (для основателей)"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ Ця команда доступна тільки засновникам магазину.")
            return

        # Проверяем, есть ли активный диалог
        if user_id not in owner_client_map:
            await update.message.reply_text("📭 У вас немає активного діалогу.")
            return

        client_id = owner_client_map[user_id]
        # Проверяем кэш
        if client_id in history_cache:
            messages = history_cache[client_id]
        else:
            try:
                with psycopg.connect(DATABASE_URL) as conn:
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute("""
                            SELECT message, is_from_user, created_at
                            FROM messages
                            WHERE user_id = %s
                            ORDER BY created_at ASC
                        """, (client_id,))
                        messages = cur.fetchall()
                # Сохраняем в кэш
                history_cache[client_id] = messages
            except Exception as e:
                logger.error(f"❌ Ошибка получения истории для {client_id}: {e}")
                await update.message.reply_text("❌ Помилка отримання історії.")
                return

        if not messages:
            await update.message.reply_text("📭 Історія повідомлень порожня.")
            return

        history_text = "📜 Історія повідомлень:\n\n"
        for msg in messages[-20:]: # Показываем последние 20 сообщений
            sender = "👤 Клієнт" if msg['is_from_user'] else "🤖 Бот"
            timestamp = msg['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            history_text += f"[{timestamp}] {sender}: {msg['message']}\n\n"

        await update.message.reply_text(history_text, parse_mode='Markdown')

    async def chats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает список активных чатов (для основателей)"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ Ця команда доступна тільки засновникам магазину.")
            return

        active_orders = {k: v for k, v in active_conversations.items() if v.get('type') in ['order', 'subscription_order', 'digital_order', 'manual']}
        active_questions = {k: v for k, v in active_conversations.items() if v.get('type') == 'question'}

        if not active_orders and not active_questions:
            await update.message.reply_text("📭 Немає активних чатів.")
            return

        chats_text = "💬 Активні чати:\n\n"
        if active_orders:
            chats_text += "🛍️ Активні замовлення:\n"
            for client_id, conv_data in active_orders.items():
                client_info = conv_data.get('user_info')
                if client_info:
                    assigned_owner = conv_data.get('assigned_owner')
                    owner_mark = " (🔹)" if assigned_owner == OWNER_ID_1 else " (🔸)" if assigned_owner == OWNER_ID_2 else ""
                    chats_text += f"- {client_info.first_name} (@{client_info.username or 'не вказано'}) {owner_mark}\n"
            chats_text += "\n"
        if active_questions:
            chats_text += "❓ Активні запитання:\n"
            for client_id, conv_data in active_questions.items():
                client_info = conv_data.get('user_info')
                if client_info:
                    assigned_owner = conv_data.get('assigned_owner')
                    owner_mark = " (🔹)" if assigned_owner == OWNER_ID_1 else " (🔸)" if assigned_owner == OWNER_ID_2 else ""
                    chats_text += f"- {client_info.first_name} (@{client_info.username or 'не вказано'}) {owner_mark}\n"

        await update.message.reply_text(chats_text)

    async def clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Очищает список активных чатов (для основателей)"""
        user_id = update.effective_user.id
        if user_id not in [OWNER_ID_1, OWNER_ID_2]:
            await update.message.reply_text("❌ Ця команда доступна тільки засновникам магазину.")
            return

        global active_conversations, owner_client_map
        active_conversations.clear()
        owner_client_map.clear()
        # Очищаем буфер активных диалогов
        active_conv_buffer.clear()
        # Обновляем статистику
        bot_statistics['active_chats'] = 0
        bot_statistics['last_reset'] = datetime.now().isoformat()
        save_stats()
        await update.message.reply_text("✅ Список активних чатів очищено.")

    # - Добавленные/обновлённые обработчики callback кнопок -
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок"""
        query = update.callback_query
        user = query.from_user
        user_id = user.id
        data = query.data

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        await query.answer()

        # Назад в главное меню
        if data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню заказов
        elif data == 'order':
            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Кнопка "Назад" в главное меню
        elif data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Підписок
        elif data == 'order_subscriptions':
            keyboard = [
                [InlineKeyboardButton("💬 ChatGPT Plus", callback_data='category_chatgpt')],
                [InlineKeyboardButton("🎮 Discord Nitro", callback_data='category_discord')],
                [InlineKeyboardButton("🎓 Duolingo Max", callback_data='category_duolingo')],
                [InlineKeyboardButton("🎨 Picsart AI", callback_data='category_picsart')],
                [InlineKeyboardButton("📊 Canva Pro", callback_data='category_canva')],
                [InlineKeyboardButton("📺 Netflix", callback_data='category_netflix')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text("💳 Оберіть підписку:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Цифрових товарів
        elif data == 'order_digital':
            keyboard = [
                [InlineKeyboardButton("🎮 Discord Прикраси", callback_data='category_discord_decor')], # Обновлено
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # - Меню Підписок -
        # Меню ChatGPT
        elif data == 'category_chatgpt':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_1')],
                [InlineKeyboardButton("12 місяців - 6500 UAH", callback_data='chatgpt_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("💬 Оберіть варіант ChatGPT Plus:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю ChatGPT Individual
        elif data == 'chatgpt_ind':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_ind_1')],
                [InlineKeyboardButton("12 місяців - 6500 UAH", callback_data='chatgpt_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_chatgpt')]
            ]
            await query.edit_message_text("💬 ChatGPT Plus (Individual):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю ChatGPT Team
        elif data == 'chatgpt_team':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 950 UAH", callback_data='chatgpt_team_1')],
                [InlineKeyboardButton("12 місяців - 9500 UAH", callback_data='chatgpt_team_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_chatgpt')]
            ]
            await query.edit_message_text("💬 ChatGPT Plus (Team):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Discord
        elif data == 'category_discord':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 170 UAH", callback_data='discord_1')],
                [InlineKeyboardButton("3 місяці - 470 UAH", callback_data='discord_3')],
                [InlineKeyboardButton("12 місяців - 1700 UAH", callback_data='discord_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎮 Оберіть варіант Discord Nitro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Duolingo
        elif data == 'category_duolingo':
            keyboard = [
                [InlineKeyboardButton("🎓 Duolingo Individual", callback_data='duolingo_ind')],
                [InlineKeyboardButton("👨‍👩‍👧‍👦 Duolingo Family", callback_data='duolingo_fam')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎓 Оберіть варіант Duolingo Max:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Duolingo Individual
        elif data == 'duolingo_ind':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 550 UAH", callback_data='duolingo_ind_1')],
                [InlineKeyboardButton("12 місяців - 5500 UAH", callback_data='duolingo_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text("🎓 Duolingo Max (Individual):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Duolingo Family
        elif data == 'duolingo_fam':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 850 UAH", callback_data='duolingo_fam_1')],
                [InlineKeyboardButton("12 місяців - 8500 UAH", callback_data='duolingo_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_duolingo')]
            ]
            await query.edit_message_text("👨‍👩‍👧‍👦 Duolingo Max (Family):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Picsart
        elif data == 'category_picsart':
            keyboard = [
                [InlineKeyboardButton("🎨 Picsart Plus", callback_data='picsart_plus')],
                [InlineKeyboardButton("🖼️ Picsart Pro", callback_data='picsart_pro')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("🎨 Оберіть варіант Picsart AI:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Picsart Plus
        elif data == 'picsart_plus':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 400 UAH", callback_data='picsart_plus_1')],
                [InlineKeyboardButton("12 місяців - 4000 UAH", callback_data='picsart_plus_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text("🎨 Picsart AI Plus:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Picsart Pro
        elif data == 'picsart_pro':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 600 UAH", callback_data='picsart_pro_1')],
                [InlineKeyboardButton("12 місяців - 6000 UAH", callback_data='picsart_pro_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_picsart')]
            ]
            await query.edit_message_text("🖼️ Picsart AI Pro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Canva
        elif data == 'category_canva':
            keyboard = [
                [InlineKeyboardButton("📊 Canva Pro Individual", callback_data='canva_pro_ind')],
                [InlineKeyboardButton("🏢 Canva Pro Team", callback_data='canva_pro_team')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📊 Оберіть варіант Canva Pro:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Canva Pro Individual
        elif data == 'canva_pro_ind':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 450 UAH", callback_data='canva_pro_ind_1')],
                [InlineKeyboardButton("12 місяців - 4500 UAH", callback_data='canva_pro_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_canva')]
            ]
            await query.edit_message_text("📊 Canva Pro (Individual):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Canva Pro Team
        elif data == 'canva_pro_team':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 750 UAH", callback_data='canva_pro_team_1')],
                [InlineKeyboardButton("12 місяців - 7500 UAH", callback_data='canva_pro_team_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_canva')]
            ]
            await query.edit_message_text("🏢 Canva Pro (Team):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Меню Netflix
        elif data == 'category_netflix':
            keyboard = [
                [InlineKeyboardButton("📱 Netflix Mobile", callback_data='netflix_mobile')],
                [InlineKeyboardButton("💻 Netflix Basic", callback_data='netflix_basic')],
                [InlineKeyboardButton("📺 Netflix Standard", callback_data='netflix_standard')],
                [InlineKeyboardButton("🎬 Netflix Premium", callback_data='netflix_premium')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
            ]
            await query.edit_message_text("📺 Оберіть варіант Netflix:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Netflix Mobile
        elif data == 'netflix_mobile':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 350 UAH", callback_data='netflix_mobile_1')],
                [InlineKeyboardButton("12 місяців - 3500 UAH", callback_data='netflix_mobile_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_netflix')]
            ]
            await query.edit_message_text("📱 Netflix Mobile:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Netflix Basic
        elif data == 'netflix_basic':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 450 UAH", callback_data='netflix_basic_1')],
                [InlineKeyboardButton("12 місяців - 4500 UAH", callback_data='netflix_basic_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_netflix')]
            ]
            await query.edit_message_text("💻 Netflix Basic:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Netflix Standard
        elif data == 'netflix_standard':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 550 UAH", callback_data='netflix_standard_1')],
                [InlineKeyboardButton("12 місяців - 5500 UAH", callback_data='netflix_standard_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_netflix')]
            ]
            await query.edit_message_text("📺 Netflix Standard:", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Netflix Premium
        elif data == 'netflix_premium':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='netflix_premium_1')],
                [InlineKeyboardButton("12 місяців - 6500 UAH", callback_data='netflix_premium_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_netflix')]
            ]
            await query.edit_message_text("🎬 Netflix Premium:", reply_markup=InlineKeyboardMarkup(keyboard))

        # - Меню Цифрових товарів -
        # Меню Discord Прикраси
        elif data == 'category_discord_decor':
            keyboard = [
                [InlineKeyboardButton("🎮 Discord Прикраси (Без Nitro)", callback_data='discord_decor_without_nitro')],
                [InlineKeyboardButton("🎮 Discord Прикраси (З Nitro)", callback_data='discord_decor_with_nitro')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')]
            ]
            await query.edit_message_text("🎮 Оберіть тип прикраси Discord:", reply_markup=InlineKeyboardMarkup(keyboard)) # Обновлено

        # Подменю Discord Прикраси Без Nitro
        elif data == 'discord_decor_without_nitro':
            keyboard = [
                [InlineKeyboardButton("6$ - 180 UAH", callback_data='discord_decor_bzn_6')],
                [InlineKeyboardButton("8$ - 240 UAH", callback_data='discord_decor_bzn_8')],
                [InlineKeyboardButton("9$ - 265 UAH", callback_data='discord_decor_bzn_9')],
                [InlineKeyboardButton("12$ - 355 UAH", callback_data='discord_decor_bzn_12')],
                [InlineKeyboardButton("18$ - 530 UAH", callback_data='discord_decor_bzn_18')],
                [InlineKeyboardButton("24$ - 705 UAH", callback_data='discord_decor_bzn_24')],
                [InlineKeyboardButton("29$ - 855 UAH", callback_data='discord_decor_bzn_29')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord_decor')]
            ]
            await query.edit_message_text("🎮 Оберіть прикраси Discord (Без Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))

        # Подменю Discord Прикраси З Nitro
        elif data == 'discord_decor_with_nitro':
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
            await query.edit_message_text("🎮 Оберіть прикраси Discord (З Nitro):", reply_markup=InlineKeyboardMarkup(keyboard))

        # - Выбор товара -
        # ChatGPT
        elif data.startswith('chatgpt_'):
            plan = "ChatGPT Plus"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 650, '12': 6500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"chatgpt_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Discord
        elif data.startswith('discord_'):
            plan = "Discord Nitro"
            period_map = {'1': '1 місяць', '3': '3 місяці', '12': '12 місяців'}
            price_map = {'1': 170, '3': 470, '12': 1700}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"discord_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Duolingo Individual
        elif data.startswith('duolingo_ind_'):
            plan = "Duolingo Max (Individual)"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 550, '12': 5500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"duolingo_ind_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Duolingo Family
        elif data.startswith('duolingo_fam_'):
            plan = "Duolingo Max (Family)"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 850, '12': 8500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"duolingo_fam_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Picsart Plus
        elif data.startswith('picsart_plus_'):
            plan = "Picsart AI Plus"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 400, '12': 4000}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"picsart_plus_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Picsart Pro
        elif data.startswith('picsart_pro_'):
            plan = "Picsart AI Pro"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 600, '12': 6000}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"picsart_pro_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Canva Pro Individual
        elif data.startswith('canva_pro_ind_'):
            plan = "Canva Pro (Individual)"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 450, '12': 4500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"canva_pro_ind_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Canva Pro Team
        elif data.startswith('canva_pro_team_'):
            plan = "Canva Pro (Team)"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 750, '12': 7500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"canva_pro_team_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Netflix Mobile
        elif data.startswith('netflix_mobile_'):
            plan = "Netflix Mobile"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 350, '12': 3500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"netflix_mobile_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Netflix Basic
        elif data.startswith('netflix_basic_'):
            plan = "Netflix Basic"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 450, '12': 4500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"netflix_basic_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Netflix Standard
        elif data.startswith('netflix_standard_'):
            plan = "Netflix Standard"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 550, '12': 5500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"netflix_standard_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Netflix Premium
        elif data.startswith('netflix_premium_'):
            plan = "Netflix Premium"
            period_map = {'1': '1 місяць', '12': '12 місяців'}
            price_map = {'1': 650, '12': 6500}
            period_key = data.split('_')[-1]
            period = period_map.get(period_key, period_key)
            price = price_map.get(period_key, 0)
            selected_item = f"netflix_premium_{period_key}"
            item = {'name': f"{plan} ({period})", 'price': price}
            await self.process_order_selection(query, user, selected_item, item)

        # Discord Прикраси Без Nitro
        elif data.startswith('discord_decor_bzn_'):
            items = {
                'discord_decor_bzn_6': {'name': "Discord Прикраси (Без Nitro) 6$", 'price': 180},
                'discord_decor_bzn_8': {'name': "Discord Прикраси (Без Nitro) 8$", 'price': 240},
                'discord_decor_bzn_9': {'name': "Discord Прикраси (Без Nitro) 9$", 'price': 265},
                'discord_decor_bzn_12': {'name': "Discord Прикраси (Без Nitro) 12$", 'price': 355},
                'discord_decor_bzn_18': {'name': "Discord Прикраси (Без Nitro) 18$", 'price': 530}, # Обновлено
                'discord_decor_bzn_24': {'name': "Discord Прикраси (Без Nitro) 24$", 'price': 705}, # Обновлено
                'discord_decor_bzn_29': {'name': "Discord Прикраси (Без Nitro) 29$", 'price': 855}, # Обновлено
            }
            item = items.get(data)
            if not item:
                await query.edit_message_text("❌ Помилка: товар не знайдено.")
                return
            await self.process_order_selection(query, user, data, item)

        # Discord Прикраси З Nitro
        elif data.startswith('discord_decor_zn_'):
            items = {
                'discord_decor_zn_5': {'name': "Discord Прикраси (З Nitro) 5$", 'price': 145},
                'discord_decor_zn_7': {'name': "Discord Прикраси (З Nitro) 7$", 'price': 205},
                'discord_decor_zn_8_5': {'name': "Discord Прикраси (З Nitro) 8.5$", 'price': 250},
                'discord_decor_zn_9': {'name': "Discord Прикраси (З Nitro) 9$", 'price': 265},
                'discord_decor_zn_14': {'name': "Discord Прикраси (З Nitro) 14$", 'price': 410},
                'discord_decor_zn_22': {'name': "Discord Прикраси (З Nitro) 22$", 'price': 650},
                'discord_decor_zn_25': {'name': "Discord Прикраси (З Nitro) 25$", 'price': 740},
                'discord_decor_zn_30': {'name': "Discord Прикраси (З Nitro) 30$", 'price': 885}
            }
            item = items.get(data)
            if not item:
                await query.edit_message_text("❌ Помилка: товар не знайдено.")
                return
            await self.process_order_selection(query, user, data, item)

        # - Обработка вопроса -
        elif data == 'question':
            # Проверяем активные диалоги
            if user_id in active_conversations:
                await query.edit_message_text(
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

            await query.edit_message_text(
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
            )

        # - Обработка помощи -
        elif data == 'help':
            help_text = f"""👋 Доброго дня! Я бот магазину SecureShop.
🤖 Я бот магазину SecureShop.

📌 Список доступних команд:
/start - Головне меню
/order - Зробити замовлення
/question - Поставити запитання
/channel - Наш канал з асортиментом, оновленнями та розіграшами
/stop - Завершити поточний діалог
/help - Ця довідка
💬 Якщо у вас виникли питання, не соромтеся звертатися!"""
            await query.edit_message_text(help_text.strip())

        # - Продолжение диалога (для основателей) -
        elif data.startswith('dialog_'):
            if user_id not in [OWNER_ID_1, OWNER_ID_2]:
                await query.edit_message_text("❌ Ця дія доступна тільки засновникам магазину.")
                return

            client_id = int(data.split('_')[1])
            if client_id not in active_conversations:
                await query.edit_message_text("❌ Діалог не знайдено.")
                return

            client_info = active_conversations[client_id]['user_info']
            # Назначаем владельца
            active_conversations[client_id]['assigned_owner'] = user_id
            # Сохраняем в БД
            save_active_conversation(client_id, active_conversations[client_id]['type'], user_id, active_conversations[client_id]['last_message'])
            # Обновляем связь владелец-клиент
            owner_client_map[user_id] = client_id
            # Обновляем статистику
            bot_statistics['active_chats'] += 1
            save_stats()

            # Отправляем последнее сообщение и предлагаем ответить
            await context.bot.send_message(
                chat_id=user_id,
                text=f"💬 Последнее сообщение от клиента:\n{active_conversations[client_id]['last_message']}\nНапишите ответ:"
            )
            await query.edit_message_text(f"✅ Ви ведете діалог із {client_info.first_name} (@{client_info.username or 'не вказано'})")

        # - Передача диалога другому основателю -
        elif data.startswith('transfer_'):
            client_id = int(data.split('_')[1])
            current_owner = user_id
            other_owner = OWNER_ID_2 if current_owner == OWNER_ID_1 else OWNER_ID_1
            other_owner_name = "@oc33t" if other_owner == OWNER_ID_2 else "@HiGki2pYYY"

            if client_id in active_conversations:
                active_conversations[client_id]['assigned_owner'] = other_owner
                # Сохраняем в БД
                save_active_conversation(client_id, active_conversations[client_id]['type'], other_owner, active_conversations[client_id]['last_message'])
                # Обновляем связь владелец-клиент
                if current_owner in owner_client_map:
                    del owner_client_map[current_owner]
                owner_client_map[other_owner] = client_id
                await query.edit_message_text(f"🔄 Діалог передано {other_owner_name}")
                # Уведомляем другого основателя
                try:
                    await context.bot.send_message(
                        chat_id=other_owner,
                        text=f"🔄 Діалог із клієнтом {active_conversations[client_id]['user_info'].first_name} передано вам."
                    )
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления основателя {other_owner}: {e}")
            else:
                await query.edit_message_text("❌ Діалог не знайдено.")

        # - Завершение диалога основателем -
        elif data.startswith('end_chat_'):
            client_id = int(data.split('_')[2])
            owner_id = user_id

            # Удаляем диалог клиента из активных (в памяти)
            if client_id in active_conversations:
                # Сохраняем детали заказа перед удалением
                order_details = active_conversations[client_id].get('order_details', '')
                del active_conversations[client_id]
                # Сохраняем в БД
                save_active_conversation(client_id, None, None, None) # Удаляем запись
            # Удаляем связь владелец-клиент
            if owner_id in owner_client_map:
                del owner_client_map[owner_id]
            # Обновляем статистику
            bot_statistics['active_chats'] = max(0, bot_statistics['active_chats'] - 1)
            save_stats()

            await query.edit_message_text("✅ Діалог із клієнтом завершено.")
            # Отправляем подтверждение клиенту
            client_info = active_conversations.get(client_id, {}).get('user_info')
            if client_info:
                try:
                    keyboard = [
                        [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                        [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                        [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    confirmation_text = "✅ Ваш діалог із засновником магазину завершено."
                    await context.bot.send_message(chat_id=client_id, text=confirmation_text, reply_markup=reply_markup)
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки сообщения клиенту {client_id}: {e}")

        # - Переход к оплате -
        elif data == 'proceed_to_payment':
            order_text = context.user_data.get('pending_order_details') or \
                         (active_conversations.get(user_id, {}).get('order_details') if user_id in active_conversations else None)
            if not order_text:
                await query.edit_message_text("❌ Не знайдено активного замовлення.")
                return
            # Извлекаем сумму в UAH
            uah_amount = get_uah_amount_from_order_text(order_text)
            if uah_amount > 0:
                # Сохраняем заказ в контексте пользователя
                context.user_data['pending_order_details'] = order_text
                # Переходим к выбору оплаты
                await self.proceed_to_payment(update, context, uah_amount)
            else:
                await query.edit_message_text("❌ Не вдалося визначити суму замовлення.")

        # - Назад к выбору товара -
        elif data == 'back_to_order':
            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text("📦 Оберіть тип товару:", reply_markup=InlineKeyboardMarkup(keyboard))

    # - Конец добавленных/обновлённых обработчиков -

    async def process_order_selection(self, query, user, selected_item, item):
        """Обработка выбора товара"""
        user_id = user.id
        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем активные диалоги
        if user_id in active_conversations:
            await query.edit_message_text(
                "❗ У вас вже є активний діалог."
                "Будь ласка, продовжуйте писати в поточному діалозі або завершіть його командою /stop, "
                "якщо хочете почати новий діалог."
            )
            return

        # Формируем текст заказа
        order_text = f"🛍️ Ваше замовлення:\n▫️ {item['name']} - {item['price']} UAH\n💳 Всього: {item['price']} UAH"

        # Создаем запись о заказе
        conversation_type = 'digital_order' if 'Прикраси' in item['name'] else 'subscription_order'
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
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_order')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        confirmation_text = f"""✅ Ваше замовлення прийнято!\n{order_text}\nБудь ласка, оберіть дію 👇"""
        await query.edit_message_text(confirmation_text.strip(), reply_markup=reply_markup)

    async def proceed_to_payment(self, update, context, uah_amount: float):
        """Переход к выбору метода оплаты"""
        # Рассчитываем сумму в USD
        usd_amount = round(uah_amount / EXCHANGE_RATE_UAH_TO_USD, 2)
        # Сохраняем сумму в контексте
        context.user_data['payment_amount_uah'] = uah_amount
        context.user_data['payment_amount_usd'] = usd_amount

        # Создаем клавиатуру с методами оплаты
        keyboard = [
            [InlineKeyboardButton("💳 Ручна оплата (PrivatBank, Monobank тощо)", callback_data='manual_payment')],
            [InlineKeyboardButton("🪙 Криптовалюта", callback_data='crypto_payment')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')],
            [InlineKeyboardButton("❓ Запитання", callback_data='question')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.edit_message_text(
                f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                reply_markup=reply_markup
            )
        else:
            # Для команды /pay
            await update.message.reply_text(
                f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                reply_markup=reply_markup
            )

    # - Добавленные методы обработки оплаты -
    async def payment_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок оплаты"""
        query = update.callback_query
        user = query.from_user
        user_id = user.id
        data = query.data

        await query.answer()

        # Ручная оплата
        if data == 'manual_payment':
            uah_amount = context.user_data.get('payment_amount_uah', 0)
            usd_amount = context.user_data.get('payment_amount_usd', 0)
            if uah_amount <= 0:
                await query.edit_message_text("❌ Помилка: сума оплати не визначена.")
                return

            # Отправляем реквизиты для оплаты
            payment_details_text = f"""💳 Ручна оплат

ь:
Для оплати суми {uah_amount} UAH ({usd_amount}$) перекажіть кошти на наступний рахунок:

**ПриватБанк (UAH):**
Карта: `5169360016447834`
Отримувач: `Бондаренко Артем Олександрович`

**Monobank (UAH):**
Карта: `5375411204320270`
Отримувач: `Бондаренко Артем Олександрович`

Після оплати, будь ласка, надішліть скріншот підтвердження оплати або номер транзакції."""
            await query.edit_message_text(payment_details_text, parse_mode='Markdown')

            # Спрашиваем подтверждение оплаты
            keyboard = [
                [InlineKeyboardButton("✅ Я оплатив", callback_data='manual_payment_confirmed')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='proceed_to_payment')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text("Після здійснення оплати, натисніть кнопку нижче:", reply_markup=reply_markup)
            # Устанавливаем флаг ожидания подтверждения
            context.user_data['awaiting_manual_payment_confirmation'] = True

        # Подтверждение ручной оплаты
        elif data == 'manual_payment_confirmed':
            # Проверяем, есть ли активный заказ
            order_details = context.user_data.get('pending_order_details') or \
                            (active_conversations.get(user_id, {}).get('order_details') if user_id in active_conversations else None)
            if not order_details:
                await query.edit_message_text("❌ Не знайдено активного замовлення.")
                return

            # Отправляем уведомление основателям
            success_count = 0
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                try:
                    payment_confirmation_message = f"💳 Нове підтвердження ручної оплати від клієнта!\n" \
                                                   f"👤 Клієнт: {user.first_name}\n" \
                                                   f"🆔 ID: {user_id}\n" \
                                                   f"🛍️ Замовлення: {order_details}"
                    await context.bot.send_message(chat_id=owner_id, text=payment_confirmation_message)
                    success_count += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки уведомления основателю {owner_id}: {e}")

            if success_count > 0:
                # Определяем тип товара (простая логика)
                item_type = "цифрового товару" if "Прикраси" in order_details else "підписки"
                # Отправляем сообщение клиенту с просьбой прислать логин/пароль
                message_text = f"✅ Оплата пройшла успішно!\n" \
                               f"Будь ласка, надішліть мені логін та пароль від {item_type}.\n" \
                               f"Наприклад: `login:password` або `login password`"
                await query.edit_message_text(message_text, parse_mode='Markdown')
                context.user_data['awaiting_account_data'] = True
                # Сохраняем детали заказа для передачи основателям позже
                context.user_data['account_details_order'] = order_details
            else:
                await query.edit_message_text("❌ Не вдалося надіслати підтвердження основателям. Спробуйте пізніше.")

            # Очищаем временные состояния
            context.user_data.pop('awaiting_manual_payment_confirmation', None)
            context.user_data.pop('payment_amount_uah', None)
            context.user_data.pop('payment_amount_usd', None)

        # Криптовалюта
        elif data == 'crypto_payment':
            uah_amount = context.user_data.get('payment_amount_uah', 0)
            usd_amount = context.user_data.get('payment_amount_usd', 0)
            if uah_amount <= 0:
                await query.edit_message_text("❌ Помилка: сума оплати не визначена.")
                return

            # Создаем клавиатуру с доступными криптовалютами
            keyboard = []
            for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'pay_{currency_code}')])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='proceed_to_payment')])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                reply_markup=reply_markup
            )
            # Устанавливаем флаг ожидания выбора криптовалюты
            context.user_data['awaiting_crypto_currency_selection'] = True

        # Выбор конкретной криптовалюты
        elif data.startswith('pay_'):
            currency_code = data.split('_')[1]
            currency_name = next((name for name, code in AVAILABLE_CURRENCIES.items() if code == currency_code), currency_code)
            usd_amount = context.user_data.get('payment_amount_usd', 0)
            if usd_amount <= 0:
                await query.edit_message_text("❌ Помилка: сума оплати не визначена.")
                return

            # Создаем инвойс через NOWPayments
            try:
                headers = {
                    'x-api-key': NOWPAYMENTS_API_KEY,
                    'Content-Type': 'application/json'
                }
                payload = {
                    'price_amount': usd_amount,
                    'price_currency': 'usd',
                    'pay_currency': currency_code,
                    'order_id': f'order_{int(time.time())}_{user_id}', # Уникальный ID заказа
                    'order_description': f'Payment for order from user {user.first_name} (@{user.username or "N/A"})',
                    'ipn_callback_url': f'{WEBHOOK_URL}/nowpayments-ipn', # URL для IPN
                    'success_url': f'{WEBHOOK_URL}/payment-success',
                    'cancel_url': f'{WEBHOOK_URL}/payment-cancel'
                }
                response = requests.post('https://api.nowpayments.io/v1/invoice', json=payload, headers=headers)
                response.raise_for_status()
                invoice_data = response.json()

                invoice_url = invoice_data.get('invoice_url')
                invoice_id = invoice_data.get('invoice_id')

                if invoice_url and invoice_id:
                    # Сохраняем ID инвойса
                    context.user_data['nowpayments_invoice_id'] = invoice_id

                    # Отправляем ссылку на оплату
                    payment_text = f"🪙 Оплата криптовалютою ({currency_name}):\n" \
                                   f"Будь ласка, перейдіть за посиланням нижче для оплати:\n" \
                                   f"[Оплатити]({invoice_url})\n\n" \
                                   f"Після оплати, натисніть кнопку нижче для перевірки статусу."
                    keyboard = [
                        [InlineKeyboardButton("🔄 Перевірити статус оплати", callback_data='check_payment_status')],
                        [InlineKeyboardButton("⬅️ Назад до вибору криптовалюти", callback_data='back_to_crypto_selection')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(payment_text, parse_mode='Markdown', reply_markup=reply_markup)
                else:
                    await query.edit_message_text("❌ Помилка створення інвойсу. Спробуйте пізніше.")
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ NOWPayments API error: {e}")
                await query.edit_message_text("❌ Помилка з'єднання з сервісом оплати. Спробуйте пізніше.")
            except Exception as e:
                logger.error(f"❌ Неизвестная ошибка при создании инвойса: {e}")
                await query.edit_message_text("❌ Невідома помилка. Спробуйте пізніше.")

        # Проверка статуса оплаты
        elif data == 'check_payment_status':
            invoice_id = context.user_data.get('nowpayments_invoice_id')
            if not invoice_id:
                await query.edit_message_text("❌ Не знайдено ID інвойсу.")
                return

            try:
                headers = {
                    'x-api-key': NOWPAYMENTS_API_KEY,
                    'Content-Type': 'application/json'
                }
                response = requests.get(f'https://api.nowpayments.io/v1/invoice/{invoice_id}', headers=headers)
                response.raise_for_status()
                invoice_status_data = response.json()

                payment_status = invoice_status_data.get('payment_status', 'waiting')
                # Статусы: waiting, confirming, confirmed, sending, partially_paid, finished, failed, refunded, expired
                if payment_status in ['confirmed', 'finished']:
                    # Оплата прошла успешно
                    order_details = context.user_data.get('pending_order_details') or \
                                    (active_conversations.get(user_id, {}).get('order_details') if user_id in active_conversations else None)
                    if not order_details:
                        await query.edit_message_text("❌ Не знайдено активного замовлення.")
                        return

                    # Отправляем уведомление основателям
                    success_count = 0
                    for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                        try:
                            payment_confirmation_message = f"🪙 Нова оплата криптовалютою від клієнта!\n" \
                                                           f"👤 Клієнт: {user.first_name}\n" \
                                                           f"🆔 ID: {user_id}\n" \
                                                           f"🛍️ Замовлення: {order_details}\n" \
                                                           f"💰 Сума: {invoice_status_data.get('price_amount', 'N/A')} {invoice_status_data.get('price_currency', 'USD')}\n" \
                                                           f"💱 Криптовалюта: {invoice_status_data.get('pay_currency', 'N/A')}"
                            await context.bot.send_message(chat_id=owner_id, text=payment_confirmation_message)
                            success_count += 1
                        except Exception as e:
                            logger.error(f"❌ Ошибка отправки уведомления основателю {owner_id}: {e}")

                    if success_count > 0:
                        # Определяем тип товара (простая логика)
                        item_type = "цифрового товару" if "Прикраси" in order_details else "підписки"
                        # Отправляем сообщение клиенту с просьбой прислать логин/пароль
                        message_text = f"✅ Оплата пройшла успішно!\n" \
                                       f"Будь ласка, надішліть мені логін та пароль від {item_type}.\n" \
                                       f"Наприклад: `login:password` або `login password`"
                        await query.edit_message_text(message_text, parse_mode='Markdown')
                        context.user_data['awaiting_account_data'] = True
                        # Сохраняем детали заказа для передачи основателям позже
                        context.user_data['account_details_order'] = order_details
                    else:
                        await query.edit_message_text("❌ Не вдалося надіслати підтвердження основателям. Спробуйте пізніше.")

                    # Очищаем временные состояния
                    context.user_data.pop('awaiting_crypto_currency_selection', None)
                    context.user_data.pop('nowpayments_invoice_id', None)
                    context.user_data.pop('payment_amount_uah', None)
                    context.user_data.pop('payment_amount_usd', None)

                elif payment_status in ['failed', 'refunded', 'expired']:
                    await query.edit_message_text("❌ Оплата не пройшла або була скасована. Спробуйте ще раз.")
                    # Очищаем временные состояния
                    context.user_data.pop('awaiting_crypto_currency_selection', None)
                    context.user_data.pop('nowpayments_invoice_id', None)
                else:
                    # Оплата еще не завершена
                    await query.edit_message_text(
                        f"⏳ Оплата ще не завершена. Поточний статус: {payment_status}.\n"
                        f"Будь ласка, зачекайте трохи або перевірте статус пізніше."
                    )
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ NOWPayments API error при проверке статуса: {e}")
                await query.edit_message_text("❌ Помилка з'єднання з сервісом оплати. Спробуйте пізніше.")
            except Exception as e:
                logger.error(f"❌ Неизвестная ошибка при проверке статуса оплаты: {e}")
                await query.edit_message_text("❌ Невідома помилка. Спробуйте пізніше.")

        # Назад до вибору криптовалюти
        elif data == 'back_to_crypto_selection':
            uah_amount = context.user_data.get('payment_amount_uah', 0)
            usd_amount = context.user_data.get('payment_amount_usd', 0)
            if uah_amount <= 0:
                await query.edit_message_text("❌ Помилка: сума оплати не визначена.")
                return

            # Создаем клавиатуру с доступными криптовалютами
            keyboard = []
            for currency_name, currency_code in AVAILABLE_CURRENCIES.items():
                keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'pay_{currency_code}')])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='proceed_to_payment')])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💳 Оберіть метод оплати для суми {usd_amount}$:",
                reply_markup=reply_markup
            )
            # Восстанавливаем флаг ожидания выбора криптовалюты
            context.user_data['awaiting_crypto_currency_selection'] = True

        # Назад до вибору способу оплати
        elif data == 'back_to_payment_methods':
            uah_amount = context.user_data.get('payment_amount_uah', 0)
            usd_amount = context.user_data.get('payment_amount_usd', 0)
            if uah_amount <= 0:
                await query.edit_message_text("❌ Помилка: сума оплати не визначена.")
                return
            await self.proceed_to_payment(update, context, uah_amount)

    async def handle_account_data_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик сообщения с логином/паролем после оплаты"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text.strip()

        # Проверяем, ожидаем ли мы данные аккаунта
        if not context.user_data.get('awaiting_account_data', False):
            return # Игнорируем сообщение

        # Проверяем формат (login:password или login password)
        parts = re.split(r'[:\s]+', message_text)
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Неправильний формат. Будь ласка, надішліть логін та пароль у форматі `login:password` або `login password`.",
                parse_mode='Markdown'
            )
            return

        login, password = parts[0], parts[1]

        # Получаем детали заказа
        order_details = context.user_data.get('account_details_order', 'Невідомий заказ')

        # Формируем сообщение для основателей
        account_info_message = f"🔐 Нові дані акаунту від клієнта!\n" \
                               f"👤 Клієнт: {user.first_name}\n" \
                               f"🆔 ID: {user_id}\n" \
                               f"🛍️ Замовлення: {order_details}\n" \
                               f"🔑 Логін: `{login}`\n" \
                               f"🔓 Пароль: `{password}`"

        # Отправляем основателям
        success_count = 0
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(chat_id=owner_id, text=account_info_message, parse_mode='Markdown')
                success_count += 1
            except Exception as e:
                logger.error(f"❌ Ошибка отправки данных аккаунта основателю {owner_id}: {e}")

        if success_count > 0:
            await update.message.reply_text("✅ Дані акаунту успішно передано засновникам магазину!")
        else:
            await update.message.reply_text("❌ Не вдалося передати дані акаунту. Спробуйте пізніше.")

        # Очищаем состояние
        context.user_data.pop('awaiting_account_data', None)
        context.user_data.pop('account_details_order', None)
        context.user_data.pop('pending_order_details', None)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user = update.effective_user
        user_id = user.id
        message_text = update.message.text

        # Гарантируем наличие пользователя в БД
        ensure_user_exists(user)

        # Проверяем, ожидаем ли мы данные аккаунта после оплаты
        if context.user_data.get('awaiting_account_data', False):
            await self.handle_account_data_message(update, context)
            return

        # Проверяем, есть ли активный диалог
        if user_id not in active_conversations:
            # Если нет активного диалога, предлагаем начать
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "📭 У вас немає активного діалогу. Оберіть дію нижче 👇",
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
                forward_text = f"Нове повідомлення від клієнта {client_info.first_name} (@{client_info.username or 'не вказано'}):\n{message_text}"
                await context.bot.send_message(chat_id=assigned_owner, text=forward_text)
            except Exception as e:
                logger.error(f"❌ Ошибка пересылки сообщения основателю {assigned_owner}: {e}")
                await update.message.reply_text("❌ Помилка відправки повідомлення. Спробуйте пізніше.")
        else:
            # Если диалог не назначен, уведомляем обоих основателей
            notification_sent = False
            for owner_id in [OWNER_ID_1, OWNER_ID_2]:
                try:
                    # Получаем информацию о пользователе
                    client_info = active_conversations[user_id]['user_info']
                    notification_text = f"🔔 Нове повідомлення від клієнта {client_info.first_name} (@{client_info.username or 'не вказано'}):\n{message_text}\n\nБудь ласка, призначте собі цей діалог."
                    keyboard = [
                        [InlineKeyboardButton("✅ Призначити собі", callback_data=f'dialog_{user_id}')],
                        [InlineKeyboardButton("🔄 Передати іншому", callback_data=f'transfer_{user_id}')],
                        [InlineKeyboardButton("🔚 Завершити діалог", callback_data=f'end_chat_{user_id}')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(chat_id=owner_id, text=notification_text, reply_markup=reply_markup)
                    notification_sent = True
                except Exception as e:
                    logger.error(f"❌ Ошибка уведомления основателя {owner_id}: {e}")

            if not notification_sent:
                await update.message.reply_text("❌ Помилка відправки повідомлення. Спробуйте пізніше.")

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

            # Парсим JSON
            order_data = json.loads(file_content)

            # Проверяем структуру
            required_fields = ['order_id', 'items', 'total']
            if not all(field in order_data for field in required_fields):
                await update.message.reply_text("❌ Неправильна структура JSON файлу.")
                return

            if not isinstance(order_data['items'], list) or not order_data['items']:
                await update.message.reply_text("❌ Список товарів порожній або має неправильний формат.")
                return

            # Формируем текст заказа
            order_text = f"🛍️ Замовлення з сайту (#{order_data['order_id']}):\n"
            for item in order_data['items']:
                order_text += f"▫️ {item['service']} {item.get('plan', '')} ({item['period']}) - {item['price']} UAH\n"
            order_text += f"💳 Всього: {order_data['total']} UAH"

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
            confirmation_text = f"""✅ Ваше замовлення прийнято!\n{order_text}\nБудь ласка, оберіть дію 👇"""
            await update.message.reply_text(confirmation_text.strip(), reply_markup=reply_markup)

        except json.JSONDecodeError:
            await update.message.reply_text("❌ Неправильний формат JSON файлу.")
        except Exception as e:
            logger.error(f"❌ Ошибка обработки файла от {user_id}: {e}")
            await update.message.reply_text("❌ Помилка обробки файлу. Спробуйте пізніше.")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(msg="Exception while handling an update:", exc_info=context.error)

        # Формируем текст ошибки
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)

        # Отправляем сообщение об ошибке основателям (опционально)
        error_message = f"🚨 Критическая ошибка в боте:\n{tb_string}"
        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                # Ограничиваем длину сообщения
                if len(error_message) > 4096:
                    error_message = error_message[:4000] + "\n... (обрезано)"
                await context.bot.send_message(chat_id=owner_id, text=f"```\n{error_message}\n```", parse_mode='Markdown')
            except Exception as e:
                logger.error(f"❌ Ошибка отправки уведомления об ошибке основателю {owner_id}: {e}")

    async def start_polling(self):
        """Запуск polling"""
        global bot_running
        with bot_lock:
            if bot_running:
                logger.warning("🛑 Бот уже запущен! Пропускаем повторный запуск")
                return
            bot_running = True

        try:
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
        global bot_running
        with bot_lock:
            if not bot_running:
                logger.warning("🛑 Бот не запущен! Пропускаем остановку")
                return
            bot_running = False

        try:
            logger.info("🔄 Остановка polling режима...")
            await self.application.updater.stop()
            await self.application.stop()
            logger.info("✅ Polling остановлен")
        except Exception as e:
            logger.error(f"❌ Ошибка остановки polling: {e}")

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
            url=WEBHOOK_URL,
            max_connections=40,
            drop_pending_updates=True
        )
        logger.info(f"🌐 Webhook установлен: {WEBHOOK_URL}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка настройки webhook: {e}")
        return False

async def start_bot():
    """Основная функция запуска бота"""
    global bot_instance
    try:
        # Инициализация базы данных
        init_db()
        # Загрузка статистики
        load_stats()

        # Инициализация бота
        bot_instance = TelegramBot()
        await bot_instance.initialize()

        # Запуск
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
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в bot_thread: {e}")
    finally:
        loop.close()

# - Flask endpoints -
@flask_app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "ok", "message": "SecureShop Bot is running"}), 200

@flask_app.route('/nowpayments-ipn', methods=['POST'])
def nowpayments_ipn():
    """Обработчик IPN от NOWPayments"""
    try:
        # Логируем полученные данные
        data = request.json
        logger.info(f"NOWPayments IPN received: {data}")

        # Здесь можно добавить логику обработки IPN
        # Например, обновление статуса заказа в БД и уведомление пользователя/основателя
        # data = request.json
        # logger.info(f"NOWPayments IPN received: {data}")
        # return jsonify({'status': 'ok'}), 200
        pass # Заглушка
    except Exception as e:
        logger.error(f"❌ Ошибка обработки NOWPayments IPN: {e}")
    return jsonify({'status': 'ok'}), 200

@flask_app.route('/payment-success', methods=['GET'])
def payment_success():
    """Страница успешной оплаты"""
    return jsonify({"status": "success", "message": "Payment was successful"}), 200

@flask_app.route('/payment-cancel', methods=['GET'])
def payment_cancel():
    """Страница отмены оплаты"""
    return jsonify({"status": "cancelled", "message": "Payment was cancelled"}), 200

@flask_app.route('/health', methods=['GET'])
def health_check():
    """Проверка состояния приложения"""
    status = "healthy" if bot_running else "unhealthy"
    return jsonify({"status": status, "timestamp": datetime.now().isoformat()}), 200

def main():
    """Главная функция"""
    try:
        # Запускаем Flask сервер в отдельном потоке
        from threading import Thread
        flask_thread = Thread(target=lambda: flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False))
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

    except Exception as e:
        logger.error(f"❌ Критическая ошибка в main: {e}")

if __name__ == '__main__':
    main()
