# main.py
import asyncio
import logging
import os
import re
import threading
import time
import json
import random
import string
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import Conflict
import psycopg
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
OWNER_ID_1 = int(os.environ.get("OWNER_ID_1"))
OWNER_ID_2 = int(os.environ.get("OWNER_ID_2"))
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY")
CARD_NUMBER = os.environ.get("CARD_NUMBER")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
USE_POLLING = os.environ.get("USE_POLLING", "True").lower() == "true"
PING_INTERVAL = int(os.environ.get("PING_INTERVAL", "300"))

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
active_conversations = {}
owner_client_map = {}
user_cache = set()
bot_statistics = {
    'first_start': datetime.now().isoformat(),
    'last_save': datetime.now().isoformat(),
    'total_orders': 0,
    'total_questions': 0
}
STATS_FILE = "stats.json"
BUFFER_FLUSH_INTERVAL = 60
message_buffer = []
active_conv_buffer = []

# --- ИМПОРТ ПРОДУКТОВ ---
import products # Убедитесь, что файл products.py находится рядом

# --- ФУНКЦИИ РАБОТЫ С БД ---
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT,
                        text TEXT,
                        is_from_user BOOLEAN,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS active_conversations (
                        user_id BIGINT PRIMARY KEY,
                        type VARCHAR(50),
                        assigned_owner BIGINT,
                        last_message TEXT,
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
                        language_code = EXCLUDED.language_code;
                """, (user.id, user.username, user.first_name, user.last_name, user.language_code, user.is_bot))
        user_cache.add(user.id)
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения пользователя: {e}")

def save_message(user_id, text, is_from_user):
    message_buffer.append((user_id, text, is_from_user))
    if len(message_buffer) >= 10:
        flush_message_buffer()

def flush_message_buffer():
    global message_buffer
    if not message_buffer:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO messages (user_id, text, is_from_user) VALUES (%s, %s, %s)",
                    message_buffer
                )
    except Exception as e:
        logger.error(f"❌ Ошибка сброса буфера сообщений: {e}")
    finally:
        message_buffer = []

def save_active_conversation(user_id, conversation_type, assigned_owner, last_message):
    active_conv_buffer.append((user_id, conversation_type, assigned_owner, last_message))
    if len(active_conv_buffer) >= 5:
        flush_active_conv_buffer()

def flush_active_conv_buffer():
    global active_conv_buffer
    if not active_conv_buffer:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO active_conversations (user_id, type, assigned_owner, last_message, updated_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        type = EXCLUDED.type,
                        assigned_owner = EXCLUDED.assigned_owner,
                        last_message = EXCLUDED.last_message,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE ac.updated_at < EXCLUDED.updated_at;
                """, active_conv_buffer)
    except Exception as e:
        logger.error(f"❌ Ошибка сброса буфера диалогов: {e}")
    finally:
        active_conv_buffer = []

def get_total_users_count():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users;")
                result = cur.fetchone()
                return result[0] if result else 0
    except Exception as e:
        logger.error(f"❌ Ошибка получения количества пользователей: {e}")
        return 0

def get_all_users():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users;")
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"❌ Ошибка получения всех пользователей: {e}")
        return []

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

# --- ФУНКЦИИ СТАТИСТИКИ ---
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки статистики: {e}")
    return bot_statistics

def save_stats():
    bot_statistics['last_save'] = datetime.now().isoformat()
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(bot_statistics, f)
    except Exception as e:
        logger.error(f"Ошибка сохранения статистики: {e}")

# --- ИНИЦИАЛИЗАЦИЯ ---
init_db()
threading.Thread(target=buffer_flush_thread, daemon=True).start()

def buffer_flush_thread():
    while True:
        time.sleep(BUFFER_FLUSH_INTERVAL)
        flush_message_buffer()
        flush_active_conv_buffer()

# --- FLASK ПРИЛОЖЕНИЕ ---
flask_app = Flask(__name__)
CORS(flask_app)

# --- КЛАСС БОТА ---
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
                await self.application.bot.set_my_commands(
                    owner_commands,
                    scope=BotCommandScopeChat(owner_id)
                )
        except Exception as e:
            logger.error(f"Ошибка установки команд: {e}")

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("order", self.order_command))
        self.application.add_handler(CommandHandler("question", self.question_command))
        self.application.add_handler(CommandHandler("pay", self.pay_command)) # Обработчик /pay
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("chats", self.show_chats))
        self.application.add_handler(CommandHandler("history", self.show_history))
        self.application.add_handler(CommandHandler("dialog", self.start_dialog))
        self.application.add_handler(CommandHandler("clear", self.clear_chats))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))

    async def initialize(self):
        if not self.initialized:
            await self.set_commands_menu()
            self.initialized = True

    async def start_polling(self):
        try:
            await self.application.initialize()
            await self.application.start()
            self.polling_task = asyncio.create_task(self.application.updater.start_polling())
            logger.info("✅ Бот запущен в режиме polling")
        except Exception as e:
            logger.error(f"❌ Ошибка запуска polling: {e}")

    async def stop_polling(self):
        if self.polling_task:
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
        await self.application.stop()
        await self.application.shutdown()

    # --- ОСНОВНЫЕ КОМАНДЫ ---
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        ensure_user_exists(user)
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')],
            [InlineKeyboardButton("📢 Наш канал", callback_data='channel')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "👋 Ласкаво просимо до магазину SecureShop!\n"
            "Оберіть дію нижче:",
            reply_markup=reply_markup
        )

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        if isinstance(update, Update):
            message = update.message
        else:
            message = update
        help_text = """👋 Доброго дня! Я бот магазину SecureShop.
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

    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [[InlineKeyboardButton("📢 Перейти в SecureShopUA", url="https://t.me/SecureShopUA")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = """📢 Наш головний канал з асортиментом, оновленнями та розіграшами:
👉 Тут ви знайдете:
- 🆕 Актуальні товари та послуги
- 🔥 Спеціальні пропозиції та знижки
- 🎁 Розіграші та акції
- ℹ️ Важливі оновлення сервісу
Приєднуйтесь, щоб бути в курсі всіх новин! 👇"""
        await update.message.reply_text(message_text.strip(), reply_markup=reply_markup)

    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
            [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        await update.message.reply_text(
            "📦 Оберіть тип товару:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

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
                del active_conversations[client_id]
                if user_id in owner_client_map:
                    del owner_client_map[user_id]
                save_active_conversation(client_id, None, None, None) # Удаляем из БД
                await update.message.reply_text(f"✅ Діалог з клієнтом {client_info.first_name if client_info else 'Невідомий'} завершено.")
                try:
                    await context.bot.send_message(
                        chat_id=client_id,
                        text="✅ Ваш діалог із підтримкою завершено. Щоб почати новий, напишіть /start."
                    )
                except:
                    pass
                return
            else:
                await update.message.reply_text("ℹ️ Діалог вже завершено.")
                return
        elif user_id in active_conversations:
            assigned_owner = active_conversations[user_id].get('assigned_owner')
            del active_conversations[user_id]
            if assigned_owner and assigned_owner in owner_client_map and owner_client_map[assigned_owner] == user_id:
                del owner_client_map[assigned_owner]
            save_active_conversation(user_id, None, None, None) # Удаляем из БД
            await update.message.reply_text("✅ Ваш діалог із підтримкою завершено. Щоб почати новий, напишіть /start.")
            if assigned_owner:
                try:
                    await context.bot.send_message(
                        chat_id=assigned_owner,
                        text=f"✅ Клієнт {user_name} завершив діалог."
                    )
                except:
                    pass
            return
        else:
            await update.message.reply_text("ℹ️ У вас немає активного діалогу. Щоб почати, напишіть /start.")

    # --- ОБРАБОТКА /PAY КОМАНДЫ ---
    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)

        if not context.args:
            await update.message.reply_text(
                "❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ..."
            )
            return

        order_id = context.args[0]
        items_str = " ".join(context.args[1:])

        # Паттерн для парсинга товаров, генерируемых сайтом
        # Пример: Robl-GC-10$-459
        pattern = r'(\w{2,4})-(\w{2,4})-([\w\s$]+?)-(\d+)'
        items = re.findall(pattern, items_str)

        if not items:
            await update.message.reply_text("❌ Не вдалося розпізнати товари у замовленні. Перевірте формат.")
            return

        order_text = f"🛍️ Замовлення з сайту (#{order_id}):"
        total = 0
        order_details = []
        has_digital = False

        for item in items:
            service_abbr = item[0]
            plan_abbr = item[1]
            period = item[2].strip()
            price_str = item[3]

            service_name = products.SERVICE_MAP.get(service_abbr, service_abbr)
            plan_name = products.PLAN_MAP.get(plan_abbr, plan_abbr)

            try:
                price_num = int(price_str)
                total += price_num

                # Определение типа заказа
                if service_abbr in ["Rob", "PSNT", "PSNI"]:
                     has_digital = True
                elif service_abbr == 'DisU':
                     has_digital = True

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
                logger.warning(f"Некорректная цена для товара: {item}")
                continue

        if total == 0:
            await update.message.reply_text("❌ Не вдалося обробити замовлення. Перевірте формат товарів.")
            return

        order_text += f"💳 Всього: {total} UAH"

        conversation_type = 'digital_order' if has_digital else 'subscription_order'

        context.user_data['pending_payment'] = {
            'order_id': order_id,
            'items': order_details,
            'total_uah': total,
            'from_website': True
        }

        keyboard = [
            [InlineKeyboardButton("💳 Оплата по карті", callback_data=f'pay_card_{total}')],
            [InlineKeyboardButton("₿ Оплата криптовалютю", callback_data=f'pay_crypto_{total}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"{order_text}\nОберіть спосіб оплати:",
            reply_markup=reply_markup
        )

    # --- ОБРАБОТЧИКИ КНОПОК ---
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user = query.from_user
        user_id = user.id
        ensure_user_exists(user)

        # --- ЛОГИКА ДЛЯ ОФОРМЛЕНИЯ ЗАКАЗА ИЗ САЙТА / КОМАНДЫ /pay ---
        if query.data.startswith('pay_card_'):
             try:
                 parts = query.data.split('_')
                 if len(parts) < 3 or not parts[2].isdigit():
                     raise ValueError("Некорректный формат callback_data для оплаты картой")
                 amount = int(parts[2])
                 pending_payment = context.user_data.get('pending_payment')
                 if not pending_payment:
                     await query.edit_message_text("❌ Ошибка: информация о заказе отсутствует.")
                     return

                 keyboard = [[InlineKeyboardButton("✅ Оплачено", callback_data='paid_card')]]
                 reply_markup = InlineKeyboardMarkup(keyboard)

                 await query.edit_message_text(
                     f"💳 Оплата по карті:\n`{CARD_NUMBER}`\n"
                     f"Сума: {amount} UAH\n"
                     f"Після оплати, натисніть кнопку нижче.",
                     parse_mode='Markdown',
                     reply_markup=reply_markup
                 )

                 self._save_order_and_notify_owners(context, user_id, user, pending_payment)

             except (ValueError, IndexError) as e:
                 logger.error(f"Ошибка обработки оплаты по карте: {e}")
                 await query.edit_message_text("❌ Произошла ошибка при обработке оплаты по карте.")

        elif query.data.startswith('pay_crypto_'):
             try:
                 parts = query.data.split('_')
                 if len(parts) < 3 or not parts[2].isdigit():
                     raise ValueError("Некорректный формат callback_data для оплаты криптовалютой")
                 amount = int(parts[2])
                 pending_payment = context.user_data.get('pending_payment')
                 if not pending_payment:
                     await query.edit_message_text("❌ Ошибка: информация о заказе отсутствует.")
                     return

                 keyboard = [
                     [InlineKeyboardButton(name, callback_data=f'invoice_{amount}_{code}')]
                     for name, code in products.AVAILABLE_CURRENCIES.items()
                 ]
                 keyboard.append([InlineKeyboardButton("✅ Оплачено", callback_data='paid_crypto')])
                 reply_markup = InlineKeyboardMarkup(keyboard)

                 await query.edit_message_text(
                     f"Оберіть криптовалюту для оплати {amount} UAH:",
                     reply_markup=reply_markup
                 )

             except (ValueError, IndexError) as e:
                 logger.error(f"Ошибка обработки выбора криптовалюты: {e}")
                 await query.edit_message_text("❌ Произошла ошибка при обработке запроса оплаты криптовалютой.")

        elif query.data.startswith('invoice_'):
             try:
                 parts = query.data.split('_')
                 if len(parts) < 3 or not parts[1].isdigit():
                     raise ValueError("Некорректный формат callback_data для инвойса")
                 amount = int(parts[1])
                 pay_currency_code = parts[2]
                 pending_payment = context.user_data.get('pending_payment')
                 if not pending_payment:
                     await query.edit_message_text("❌ Ошибка: информация о заказе отсутствует.")
                     return

                 order_id_suffix = "unknown"
                 if 'order_id' in pending_payment:
                     order_id_suffix = pending_payment['order_id']
                 elif 'product_id' in pending_payment:
                     order_id_suffix = pending_payment['product_id']

                 invoice_data = self.create_invoice(
                     amount=amount,
                     pay_currency=pay_currency_code,
                     currency="uah",
                     order_id_suffix=order_id_suffix
                 )

                 if "error" in invoice_data:
                     await query.edit_message_text(f"❌ Ошибка создания платежа: {invoice_data['error']}")
                     return

                 pay_url = invoice_data.get("invoice_url")
                 if pay_url:
                     currency_name = dict((v, k) for k, v in products.AVAILABLE_CURRENCIES.items()).get(pay_currency_code, pay_currency_code)

                     keyboard = [[InlineKeyboardButton("✅ Оплачено", callback_data='paid_crypto_invoice')]]
                     reply_markup = InlineKeyboardMarkup(keyboard)

                     await query.edit_message_text(
                         f"🔗 Посилання для оплати {amount} UAH в {currency_name}:\n{pay_url}\n"
                         f"Після оплати, натисніть кнопку нижче.",
                         reply_markup=reply_markup
                     )

                     self._save_order_and_notify_owners(context, user_id, user, pending_payment)

                 else:
                     await query.edit_message_text("❌ Не удалось получить ссылку для оплаты. Пожалуйста, попробуйте позже или выберите другой способ.")

             except (ValueError, IndexError, Exception) as e:
                 logger.error(f"Ошибка обработки крипто-инвойса: {e}")
                 await query.edit_message_text("❌ Произошла ошибка при создании платежа.")

        # --- ЛОГИКА ДЛЯ ПОДТВЕРЖДЕНИЯ ОПЛАТЫ ---
        elif query.data in ['paid_card', 'paid_crypto', 'paid_crypto_invoice']:
            pending_payment = context.user_data.get('pending_payment')
            if pending_payment:
                if 'product_id' in pending_payment:
                    order_text = f"🛍️ Хочу замовити: {pending_payment['product_name']} за {pending_payment['price_uah']} UAH"
                elif 'order_id' in pending_payment:
                    total_uah = pending_payment['total_uah']
                    order_text = "🛍️ Замовлення з сайту:"
                    for item in pending_payment.get('items', []):
                        order_text += f"\n▫️ {item.get('service', 'Невідомий товар')} {item.get('plan', '')} ({item.get('period', '')}) - {item.get('price', 0)} UAH"
                    order_text += f"\n💳 Всього: {total_uah} UAH"
                else:
                    order_text = "🛍️ Замовлення (деталі невідомі)"

                await query.edit_message_text("✅ Дякуємо! Ми отримали вашу оплату. Засновник магазину зв'яжеться з вами найближчим часом.")
                context.user_data.pop('pending_payment', None)

                # Уведомление владельцев
                if hasattr(self.application, 'create_task'):
                    self.application.create_task(self.forward_order_to_owners(context, user_id, user, f"✅ Оплата отримана:\n{order_text}"))
                else:
                    asyncio.create_task(self.forward_order_to_owners(context, user_id, user, f"✅ Оплата отримана:\n{order_text}"))
            else:
                await query.edit_message_text("ℹ️ Інформація про оплату вже оброблена або відсутня.")

        # --- ЛОГИКА ДЛЯ ОТМЕНЫ ОПЛАТЫ ---
        elif query.data == 'cancel_payment':
            pending_payment = context.user_data.get('pending_payment')
            if pending_payment:
                if 'product_id' in pending_payment:
                    order_text = f"🛍️ Хочу замовити: {pending_payment['product_name']} за {pending_payment['price_uah']} UAH"
                    await query.edit_message_text(f"❌ Оплата скасована.{order_text}\nВи можете зробити нове замовлення через /start.")
                    await self.forward_order_to_owners(context, user_id, user, f"❌ Клієнт скасував оплату:\n{order_text}")
                elif 'order_id' in pending_payment:
                    total_uah = pending_payment['total_uah']
                    await query.edit_message_text(f"❌ Оплата скасована.\nЗагальна сума: {total_uah} UAH\nВи можете зробити нове замовлення через /start.")
                    order_summary = "🛍️ Замовлення з сайту (скасовано):"
                    for item in pending_payment.get('items', []):
                        order_summary += f"\n▫️ {item.get('service', 'Невідомий товар')} {item.get('plan', '')} ({item.get('period', '')}) - {item.get('price', 0)} UAH"
                    order_summary += f"\n💳 Всього: {total_uah} UAH"
                    await self.forward_order_to_owners(context, user_id, user, f"❌ Клієнт скасував оплату:\n{order_summary}")
                else:
                    await query.edit_message_text("❌ Оплата скасована.")
                    await self.forward_order_to_owners(context, user_id, user, "❌ Клієнт скасував оплату (деталі невідомі).")
                context.user_data.pop('pending_payment', None)
            else:
                await query.edit_message_text("❌ Оплата скасована.")

        # --- ЛОГИКА ДЛЯ ВЗЯТИЯ ЗАКАЗА ---
        elif query.data.startswith('take_order_'):
            owner_id = user_id
            if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
                await query.answer("❌ У вас немає прав для взяття замовлення.", show_alert=True)
                return
            if owner_id in owner_client_map:
                await query.answer("❗ У вас вже є активний діалог. Завершіть його спочатку.", show_alert=True)
                return
            try:
                client_id = int(query.data.split('_')[2])
            except (IndexError, ValueError):
                await query.answer("❌ Невірний формат даних.", show_alert=True)
                return
            if client_id not in active_conversations:
                await query.answer("❌ Цей діалог вже закритий.", show_alert=True)
                return
            active_conversations[client_id]['assigned_owner'] = owner_id
            owner_client_map[owner_id] = client_id
            save_active_conversation(client_id, active_conversations[client_id]['type'], owner_id, active_conversations[client_id]['last_message'])
            client_info = active_conversations[client_id]['user_info']
            await query.edit_message_text(f"✅ Ви взяли замовлення від клієнта {client_info.first_name}.")
            if 'order_details' in active_conversations[client_id]:
                order_text = active_conversations[client_id]['order_details']
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=f"🛍️ Деталі замовлення:\n{order_text}"
                )

        # --- ЛОГИКА ДЛЯ КНОПОК МЕНЮ ---
        elif query.data == 'order':
            keyboard = [
                [InlineKeyboardButton("💳 Підписки", callback_data='order_subscriptions')],
                [InlineKeyboardButton("🎮 Цифрові товари", callback_data='order_digital')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text(
                "📦 Оберіть тип товару:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')],
                [InlineKeyboardButton("📢 Наш канал", callback_data='channel')]
            ]
            await query.edit_message_text(
                "Головне меню:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif query.data == 'help':
            await self.show_help(query.message)
        elif query.data == 'channel':
            await self.channel_command(query.message)
        # ... (остальные обработчики кнопок меню - order_subscriptions, order_digital и т.д.) ...
        # Для простоты они опущены, но должны быть реализованы аналогично оригиналу
        else:
             await query.edit_message_text("ℹ️ Дія не розпізнана. Спробуйте ще раз через /start.")
             logger.warning(f"Неизвестный callback_data: {query.data}")

    # --- ФУНКЦИИ СОЗДАНИЯ ПЛАТЕЖЕЙ ---
    def create_invoice(self, amount, pay_currency, currency, order_id_suffix):
        url = "https://api.nowpayments.io/v1/invoice"
        headers = {
            "x-api-key": NOWPAYMENTS_API_KEY,
            "Content-Type": "application/json"
        }
        # Генерация уникального order_id
        random_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        full_order_id = f"SS-{order_id_suffix}-{random_suffix}"

        payload = {
            "price_amount": amount,
            "price_currency": currency,
            "pay_currency": pay_currency,
            "order_id": full_order_id,
            "order_description": f"Оплата замовлення #{order_id_suffix} в SecureShop",
            "ipn_callback_url": f"{WEBHOOK_URL}/ipn",
            "success_url": f"{WEBHOOK_URL}/success",
            "cancel_url": f"{WEBHOOK_URL}/cancel"
        }
        try:
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка создания инвойса NOWPayments: {e}")
            return {"error": str(e)}
        except ValueError as e:
            logger.error(f"Ошибка парсинга JSON ответа NOWPayments: {e}")
            return {"error": "Некорректный ответ от NOWPayments"}

    # --- ФУНКЦИИ УВЕДОМЛЕНИЯ ВЛАДЕЛЬЦЕВ ---
    def _save_order_and_notify_owners(self, context, user_id, user, pending_payment):
        if 'product_id' in pending_payment:
            order_text = f"🛍️ Хочу замовити: {pending_payment['product_name']} за {pending_payment['price_uah']} UAH"
            conversation_type = pending_payment['type'] + '_order'
        elif 'order_id' in pending_payment and 'items' in pending_payment:
            order_text = "🛍️ Замовлення з сайту:"
            total_uah = pending_payment['total_uah']
            for item in pending_payment.get('items', []):
                order_text += f"\n▫️ {item.get('service', 'Невідомий товар')} {item.get('plan', '')} ({item.get('period', '')}) - {item.get('price', 0)} UAH"
            order_text += f"\n💳 Всього: {total_uah} UAH"

            has_digital_item = any(
                item.get('service', '') in [
                    products.SERVICE_MAP.get("Rob", "Roblox"),
                    products.SERVICE_MAP.get("PSNT", "PSN (TRY)"),
                    products.SERVICE_MAP.get("PSNI", "PSN (INR)"),
                    products.SERVICE_MAP.get("DisU", "Discord Украшення")
                ]
                for item in pending_payment.get('items', [])
            )
            conversation_type = 'digital_order' if has_digital_item else 'subscription_order'
        else:
            order_text = "🛍️ Замовлення (деталі невідомі)"
            conversation_type = 'unknown_order'

        active_conversations[user_id] = {
            'type': conversation_type,
            'user_info': user,
            'assigned_owner': None,
            'order_details': order_text,
            'last_message': order_text,
            'from_website': pending_payment.get('from_website', False)
        }
        save_active_conversation(user_id, conversation_type, None, order_text)
        bot_statistics['total_orders'] += 1
        save_stats()

        if hasattr(self.application, 'create_task'):
            self.application.create_task(self.forward_order_to_owners(context, user_id, user, order_text))
        else:
            asyncio.create_task(self.forward_order_to_owners(context, user_id, user, order_text))

    async def forward_order_to_owners(self, context, user_id, user, message_text):
        type_text_map = {
            'digital_order': 'НОВЕ ЗАМОВЛЕННЯ (Цифровий товар)',
            'subscription_order': 'НОВЕ ЗАМОВЛЕННЯ (Підписка)',
            'question_order': 'НОВЕ ЗАПИТАННЯ',
            'unknown_order': 'НОВЕ ЗАМОВЛЕННЯ (Невідомий тип)'
        }
        conversation_type = active_conversations.get(user_id, {}).get('type', 'unknown_order')
        type_text = type_text_map.get(conversation_type, "ПОВІДОМЛЕННЯ")
        type_emoji = "🎮" if conversation_type == 'digital_order' else "💳" if conversation_type == 'subscription_order' else "❓"

        owner_name = "@HiGki2pYYY" if OWNER_ID_1 else "@oc33t"
        forward_message = f"""{type_emoji} {type_text} від клієнта:
👤 Користувач: {user.first_name}
📱 Username: @{user.username if user.username else 'не вказано'}
🆔 ID: {user.id}
🌐 Язык: {user.language_code or 'не вказано'}
💬 Повідомлення:
{message_text}
-Для відповіді просто напишіть повідомлення в цей чат.
Для завершення діалогу використовуйте /stop.
Призначено: {owner_name}"""

        keyboard = [[InlineKeyboardButton("✅ Взяти", callback_data=f'take_order_{user_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        for owner_id in [OWNER_ID_1, OWNER_ID_2]:
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=forward_message.strip(),
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f" ❌ Ошибка отправки владельцу {owner_id}: {e}")

    # --- ОБРАБОТКА СООБЩЕНИЙ ---
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)

        if user_id in [OWNER_ID_1, OWNER_ID_2] and user_id in owner_client_map:
            client_id = owner_client_map[user_id]
            if client_id in active_conversations:
                client_info = active_conversations[client_id]['user_info']
                message_text = update.message.text
                active_conversations[client_id]['last_message'] = f"Підтримка: {message_text}"
                save_message(client_id, f"Підтримка: {message_text}", False)
                save_active_conversation(client_id, active_conversations[client_id]['type'], user_id, active_conversations[client_id]['last_message'])
                try:
                    await context.bot.send_message(chat_id=client_id, text=message_text)
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения клиенту {client_id}: {e}")
                    await update.message.reply_text("❌ Не вдалося відправити повідомлення клієнту.")
            else:
                await update.message.reply_text("ℹ️ Діалог із клієнтом завершено.")
                if user_id in owner_client_map:
                    del owner_client_map[user_id]
            return

        if user_id in active_conversations:
            assigned_owner = active_conversations[user_id].get('assigned_owner')
            message_text = update.message.text
            active_conversations[user_id]['last_message'] = message_text
            save_message(user_id, message_text, True)
            save_active_conversation(user_id, active_conversations[user_id]['type'], assigned_owner, message_text)
            if assigned_owner:
                try:
                    await context.bot.send_message(
                        chat_id=assigned_owner,
                        text=f"Повідомлення від {user.first_name}:\n{message_text}"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения владельцу {assigned_owner}: {e}")
                    await update.message.reply_text("❌ Не вдалося відправити повідомлення підтримці.")
            else:
                await update.message.reply_text("⏳ Ваше повідомлення отримано. Очікуйте відповіді від нашого менеджера.")
            return

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

    # --- КОМАНДЫ ДЛЯ ВЛАДЕЛЬЦЕВ ---
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        total_users_db = get_total_users_count()
        first_start = datetime.fromisoformat(bot_statistics['first_start'])
        last_save = datetime.fromisoformat(bot_statistics['last_save'])
        uptime = datetime.now() - first_start
        stats_message = f"""📊 Статистика бота:
👤 Усього користувачів (БД): {total_users_db}
🛒 Усього замовлень: {bot_statistics['total_orders']}
❓ Усього запитаннь: {bot_statistics['total_questions']}
⏱️ Перший запуск: {first_start.strftime('%d.%m.%Y %H:%M')}
⏱️ Останнє збереження: {last_save.strftime('%d.%m.%Y %H:%M')}
⏱️ Час роботи: {uptime}"""
        await update.message.reply_text(stats_message.strip())

    async def show_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        if not active_conversations:
            await update.message.reply_text("ℹ️ Немає активних діалогів.")
            return
        chats_message = "💬 Активні діалоги:\n"
        for user_id, conv_data in active_conversations.items():
            user_info = conv_data.get('user_info', {})
            user_name = user_info.get('first_name', 'Невідомий')
            assigned_owner = conv_data.get('assigned_owner')
            assigned_text = f" (Призначено: @{'HiGki2pYYY' if assigned_owner == OWNER_ID_1 else 'oc33t'})" if assigned_owner else " (Не призначено)"
            chats_message += f"- {user_name} (ID: {user_id}){assigned_text}\n"
        await update.message.reply_text(chats_message.strip())

    async def show_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        if not context.args:
            await update.message.reply_text("❌ Вкажіть ID користувача: /history <user_id>")
            return
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Некорректний ID користувача.")
            return
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT text, is_from_user, created_at FROM messages WHERE user_id = %s ORDER BY created_at ASC", (target_user_id,))
                    messages = cur.fetchall()
            if not messages:
                await update.message.reply_text("ℹ️ Історія повідомлень порожня.")
                return
            history_text = f"Історія діалогу з користувачем ID {target_user_id}:\n"
            for msg_text, is_from_user, created_at in messages:
                sender = "Клієнт" if is_from_user else "Підтримка"
                history_text += f"[{created_at.strftime('%d.%m.%Y %H:%M')}] {sender}: {msg_text}\n"
            await update.message.reply_text(history_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка получения истории: {e}")
            await update.message.reply_text("❌ Помилка отримання історії.")

    async def start_dialog(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        if not context.args:
            await update.message.reply_text("❌ Вкажіть ID користувача: /dialog <user_id>")
            return
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Некорректний ID користувача.")
            return
        if target_user_id in active_conversations:
            await update.message.reply_text("ℹ️ З цим користувачем вже є активний діалог.")
            return
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM users WHERE id = %s", (target_user_id,))
                    user_data = cur.fetchone()
                    if not user_data:
                        await update.message.reply_text("❌ Користувач не знайдений.")
                        return
                    columns = [desc[0] for desc in cur.description]
                    user_info = dict(zip(columns, user_data))
        except Exception as e:
            logger.error(f"Ошибка получения данных пользователя: {e}")
            await update.message.reply_text("❌ Помилка отримання даних користувача.")
            return
        active_conversations[target_user_id] = {
            'type': 'manual_dialog',
            'user_info': user_info,
            'assigned_owner': owner_id,
            'last_message': 'Діалог розпочато вручну',
            'from_website': False
        }
        owner_client_map[owner_id] = target_user_id
        save_active_conversation(target_user_id, 'manual_dialog', owner_id, 'Діалог розпочато вручну')
        await update.message.reply_text(f"✅ Діалог із користувачем {user_info['first_name']} (ID: {target_user_id}) розпочато.")

    async def clear_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        deleted_count = clear_all_active_conversations()
        active_conversations.clear()
        owner_client_map.clear()
        await update.message.reply_text(f"✅ Активні діалоги очищено. Видалено записів: {deleted_count}")

# --- FLASK МАРШРУТЫ ---
@flask_app.route('/ipn', methods=['POST'])
def ipn():
    data = request.json
    logger.info(f"IPN received: {data}")
    return jsonify({'status': 'OK'})

@flask_app.route('/success', methods=['GET'])
def success():
    return jsonify({'message': 'Payment successful'})

@flask_app.route('/cancel', methods=['GET'])
def cancel():
    return jsonify({'message': 'Payment cancelled'})

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

# --- АВТОСОХРАНЕНИЕ СТАТИСТИКИ ---
def auto_save_loop():
    while True:
        time.sleep(300)
        save_stats()

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ ЗАПУСКА ---
bot_instance = None
bot_running = False
bot_lock = threading.Lock()
telegram_app = None

# --- ФУНКЦИИ ЗАПУСКА БОТА ---
async def setup_webhook():
    try:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await telegram_app.bot.set_webhook(webhook_url)
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка установки webhook: {e}")
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
                await setup_webhook() # Устанавливаем webhook даже в режиме polling для совместимости
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

def main():
    global bot_instance
    if os.environ.get('RENDER'):
        # Режим Render (Webhook)
        bot_instance = TelegramBot()
        flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
    else:
        # Режим локальной разработки или VPS (Polling)
        bot_instance = TelegramBot()
        threading.Thread(target=bot_thread, daemon=True).start()
        threading.Thread(target=auto_save_loop, daemon=True).start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("🛑 Бот остановлен.")
            if bot_instance and bot_instance.polling_task:
                asyncio.run(bot_instance.stop_polling())

if __name__ == '__main__':
    main()
