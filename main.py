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
import products

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Не вдалося отримати TELEGRAM_BOT_TOKEN. Встановіть змінну середовища.")

OWNER_ID_1 = int(os.getenv("OWNER_ID_1", 0))
OWNER_ID_2 = int(os.getenv("OWNER_ID_2", 0))
OWNER_IDS = [id for id in [OWNER_ID_1, OWNER_ID_2] if id != 0]

FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", 5001))
USE_POLLING = os.getenv("USE_POLLING", "True").lower() == "true"

DB_CONFIG = {
    'dbname': os.getenv("DB_NAME", "secure_shop_db"),
    'user': os.getenv("DB_USER", "your_db_user"),
    'password': os.getenv("DB_PASSWORD", "your_db_password"),
    'host': os.getenv("DB_HOST", "localhost"),
    'port': os.getenv("DB_PORT", "5432")
}

bot_statistics = {
    'first_start': datetime.now().isoformat(),
    'last_save': datetime.now().isoformat(),
    'total_orders': 0,
    'total_questions': 0
}

try:
    with open('bot_statistics.json', 'r') as f:
        bot_statistics.update(json.load(f))
        logger.info("📊 Статистика завантажена з файлу.")
except FileNotFoundError:
    logger.info("📊 Файл статистики не знайдено. Створюється новий.")

def save_statistics():
    bot_statistics['last_save'] = datetime.now().isoformat()
    try:
        with open('bot_statistics.json', 'w') as f:
            json.dump(bot_statistics, f)
        logger.info("💾 Статистика збережена.")
    except Exception as e:
        logger.error(f"❌ Помилка збереження статистики: {e}")

def get_db_connection():
    return psycopg.connect(**DB_CONFIG, row_factory=dict_row)

def ensure_user_exists(user: User):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, username, first_name, last_name, language_code, is_bot, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        language_code = EXCLUDED.language_code,
                        is_bot = EXCLUDED.is_bot
                """, (user.id, user.username, user.first_name, user.last_name, user.language_code, user.is_bot))
                conn.commit()
                logger.info(f"👤 Користувач {user.first_name} ({user.id}) перевірений/створений у БД.")
    except Exception as e:
        logger.error(f"❌ Помилка роботи з БД (користувач {user.id}): {e}")

def get_total_users_count():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                result = cur.fetchone()
                return result['count'] if result else 0
    except Exception as e:
        logger.error(f"❌ Помилка отримання кількості користувачів: {e}")
        return 0

def get_all_users():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Помилка отримання списку користувачів: {e}")
        return []

def create_invoice(amount: float, pay_currency: str, currency: str = "uah", order_id_suffix: str = "unknown"):
    url = "https://api.nowpayments.io/v1/invoice"
    headers = {
        "x-api-key": products.NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    if not products.NOWPAYMENTS_API_KEY or products.NOWPAYMENTS_API_KEY in ['YOUR_NOWPAYMENTS_API_KEY_HERE', '']:
        logger.error("NOWPAYMENTS_API_KEY не установлен!")
        return {"error": "API ключ не настроен"}
    payload = {
        "price_amount": amount,
        "price_currency": currency,
        "pay_currency": pay_currency,
        "order_id": f"order_{order_id_suffix}",
        "ipn_callback_url": "https://nowpayments.io",
        "cancel_url": "https://t.me/SecureShopBot",
        "success_url": "https://t.me/SecureShopBot",
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        logger.info(f"✅ Інвойс створено: Order ID {data.get('order_id')}")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Помилка створення інвойсу NOWPayments: {e}")
        return {"error": str(e)}
    except json.JSONDecodeError as e:
        logger.error(f"❌ Помилка парсингу JSON NOWPayments: {e}")
        return {"error": "Некоректна відповідь від NOWPayments"}

class SecureShopBot:
    def __init__(self, application: Application):
        self.application = application
        self.loop = application.loop

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        ensure_user_exists(user)
        welcome_text = f"""
👋 Привіт, {user.first_name}!

🤖 Ласкаво просимо до SecureShopBot!

🛒 Тут ви можете:
- Переглянути наш асортимент
- Зробити замовлення
- Оплатити його криптовалютою
- Отримати підтримку

Для початку натисніть кнопку нижче 👇
"""
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
            [InlineKeyboardButton("📢 Наш канал", callback_data='channel')],
            [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(welcome_text.strip(), reply_markup=reply_markup)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_help(update.message)

    async def show_help(self, message):
        help_text = """
ℹ️ Довідка SecureShopBot:

/start - Головне меню
/help - Ця довідка
/channel - Наш Telegram канал
/stats - Статистика (для операторів)

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
"""
        await update.message.reply_text(message_text.strip(), reply_markup=reply_markup)

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in OWNER_IDS:
            await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
            return

        await self.show_stats(update, context)

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in OWNER_IDS:
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

    async def pay_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)

        if not context.args:
            await update.message.reply_text("❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ...")
            return

        order_id = context.args[0]
        items_str = " ".join(context.args[1:])

        pattern = r'(\w{2,4})-(\w{2,4})-([\w$]+?)-(\d+)'
        items = re.findall(pattern, items_str)

        if not items:
            await update.message.reply_text("❌ Не вдалося розпізнати товари у замовленні. Перевірте формат.")
            return

        order_details = []
        total = 0
        has_digital = False

        for item in items:
            service_abbr, plan_abbr, period_abbr, price_str = item
            try:
                price_num = int(price_str)
                total += price_num

                service_name = self.get_service_name(service_abbr)
                plan_name = self.get_plan_name(plan_abbr)
                
                if service_abbr in ["Rob", "PSNT", "PSNI"]:
                     has_digital = True

                display_period = self.format_period(period_abbr)

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

        order_text = f"🧾 Ваше замовлення #{order_id}:\n"
        for item in order_details:
             order_text += f"▫️ {item['service']} {item['plan']} ({item['period']}) - {item['price']} UAH\n"
        order_text += f"💳 Всього: {total} UAH\n\n"

        context.user_data['pending_payment'] = {
            'order_id': order_id,
            'items': order_details,
            'total_uah': total,
            'from_website': True
        }

        keyboard = [
            [InlineKeyboardButton("💳 Оплата по карті", callback_data=f'pay_card_{total}')],
            [InlineKeyboardButton("₿ Оплата криптовалютою", callback_data=f'pay_crypto_{total}')],
            [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"{order_text}Оберіть спосіб оплати:",
            reply_markup=reply_markup
        )

    def get_service_name(self, abbr: str) -> str:
        mapping = {
            "Cha": "ChatGPT",
            "Dis": "Discord",
            "DisU": "Discord Украшення",
            "Duo": "Duolingo",
            "Pic": "PicsArt",
            "Can": "Canva",
            "Net": "Netflix",
            "Rob": "Roblox",
            "PSNT": "PSN (TRY)",
            "PSNI": "PSN (INR)",
        }
        return mapping.get(abbr, abbr)

    def get_plan_name(self, abbr: str) -> str:
        mapping = {
            "Plu": "Plus",
            "Bas": "Basic",
            "Ful": "Full",
            "Ind": "Individual",
            "Fam": "Family",
            "Pro": "Pro",
            "Pre": "Premium",
            "BzN": "Без Nitro",
            "ZN": "З Nitro",
            "GC": "Gift Card",
        }
        return mapping.get(abbr, abbr)

    def format_period(self, abbr: str) -> str:
        formatted = abbr
        if "TRU" in abbr:
            formatted = abbr.replace("TRU", " турецьких лір")
        elif "INR" in abbr:
            formatted = abbr.replace("INR", " індійських рупій")
        return formatted

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

        elif query.data == 'order_subscriptions':
            keyboard = [
                [InlineKeyboardButton("💬 ChatGPT", callback_data='category_chatgpt')],
                [InlineKeyboardButton("🎮 Discord", callback_data='category_discord')],
                [InlineKeyboardButton("🦉 Duolingo", callback_data='category_duolingo')],
                [InlineKeyboardButton("🎨 PicsArt", callback_data='category_picsart')],
                [InlineKeyboardButton("📊 Canva", callback_data='category_canva')],
                [InlineKeyboardButton("📺 Netflix", callback_data='category_netflix')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text("💳 Оберіть підписку:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'order_digital':
            keyboard = [
                [InlineKeyboardButton(" Roblox", callback_data='category_roblox')],
                [InlineKeyboardButton(" PSN (TRY)", callback_data='category_psn_tru')],
                [InlineKeyboardButton(" PSN (INR)", callback_data='category_psn_inr')],
                [InlineKeyboardButton("🎨 Discord Украшення", callback_data='category_discord_decor')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text("🎮 Оберіть цифровий товар:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data == 'question':
            context.user_data['conversation_type'] = 'question'
            await query.edit_message_text("❓ Напишіть ваше запитання. Оператор зв'яжеться з вами найближчим часом.")

        elif query.data == 'channel':
            await self.channel_command(update, context)

        elif query.data == 'help':
            await self.show_help(query.message)

        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]
            await query.edit_message_text("Головне меню:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data.startswith('category_'):
            category = query.data.split('_')[1]
            if category == 'chatgpt':
                keyboard = [
                    [InlineKeyboardButton("Plus", callback_data='chatgpt_plus')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
                ]
                await query.edit_message_text("💬 Оберіть тариф ChatGPT:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'discord':
                keyboard = [
                    [InlineKeyboardButton("Nitro Basic", callback_data='discord_basic')],
                    [InlineKeyboardButton("Nitro Full", callback_data='discord_full')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
                ]
                await query.edit_message_text("🎮 Оберіть тип Discord Nitro:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'duolingo':
                keyboard = [
                    [InlineKeyboardButton("Individual", callback_data='duolingo_individual')],
                    [InlineKeyboardButton("Family", callback_data='duolingo_family')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
                ]
                await query.edit_message_text("🦉 Оберіть тариф Duolingo:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'picsart':
                keyboard = [
                    [InlineKeyboardButton("Plus", callback_data='picsart_plus')],
                    [InlineKeyboardButton("Pro", callback_data='picsart_pro')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
                ]
                await query.edit_message_text("🎨 Оберіть тариф PicsArt:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'canva':
                keyboard = [
                    [InlineKeyboardButton("Individual", callback_data='canva_individual')],
                    [InlineKeyboardButton("Family", callback_data='canva_family')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
                ]
                await query.edit_message_text("📊 Оберіть тариф Canva:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'netflix':
                keyboard = [
                    [InlineKeyboardButton("Premium", callback_data='netflix_premium')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_subscriptions')]
                ]
                await query.edit_message_text("📺 Оберіть тариф Netflix:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'roblox':
                keyboard = [
                    [InlineKeyboardButton("10$ - 459 UAH", callback_data='roblox_10')],
                    [InlineKeyboardButton("25$ - 1149 UAH", callback_data='roblox_25')],
                    [InlineKeyboardButton("50$ - 2299 UAH", callback_data='roblox_50')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')]
                ]
                await query.edit_message_text("🎁 Оберіть Roblox Gift Card:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'psn_tru':
                keyboard = [
                    [InlineKeyboardButton("250 TRU - 349 UAH", callback_data='psn_tru_250')],
                    [InlineKeyboardButton("500 TRU - 699 UAH", callback_data='psn_tru_500')],
                    [InlineKeyboardButton("750 TRU - 1049 UAH", callback_data='psn_tru_750')],
                    [InlineKeyboardButton("1000 TRU - 1350 UAH", callback_data='psn_tru_1000')],
                    [InlineKeyboardButton("1500 TRU - 2000 UAH", callback_data='psn_tru_1500')],
                    [InlineKeyboardButton("2000 TRU - 2700 UAH", callback_data='psn_tru_2000')],
                    [InlineKeyboardButton("2500 TRU - 3400 UAH", callback_data='psn_tru_2500')],
                    [InlineKeyboardButton("3000 TRU - 4100 UAH", callback_data='psn_tru_3000')],
                    [InlineKeyboardButton("4000 TRU - 5300 UAH", callback_data='psn_tru_4000')],
                    [InlineKeyboardButton("5000 TRU - 6600 UAH", callback_data='psn_tru_5000')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')]
                ]
                message_text = "🎁 Оберіть PSN Gift Card (TRY):\n\n"
                message_text += "❗️ВАЖЛИВО: Для активації цієї картки потрібно створити НОВИЙ акаунт PlayStation регіону Туреччина або змінити регіон існуючого акаунта.\n"
                message_text += "TRU - турецькі ліри."
                await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'psn_inr':
                keyboard = [
                    [InlineKeyboardButton("1000 INR - 725 UAH", callback_data='psn_inr_1000')],
                    [InlineKeyboardButton("2000 INR - 1400 UAH", callback_data='psn_inr_2000')],
                    [InlineKeyboardButton("3000 INR - 2100 UAH", callback_data='psn_inr_3000')],
                    [InlineKeyboardButton("4000 INR - 2750 UAH", callback_data='psn_inr_4000')],
                    [InlineKeyboardButton("5000 INR - 3400 UAH", callback_data='psn_inr_5000')],
                    [InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')]
                ]
                message_text = "🎁 Оберіть PSN Gift Card (INR):\n\n"
                message_text += "❗️ВАЖЛИВО: Для активації цієї картки потрібно створити НОВИЙ акаунт PlayStation регіону Індія або змінити регіон існуючого акаунта.\n"
                message_text += "INR - індійські рупії."
                await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
            elif category == 'discord_decor':
                 keyboard = []
                 for item_id, item_data in products.SUBSCRIPTION_PRODUCTS.items():
                     if item_id.startswith('discord_decor_bzn_') or item_id.startswith('discord_decor_zn_'):
                         keyboard.append([InlineKeyboardButton(f"{item_data['name']} - {item_data['price']} UAH", callback_data=item_id)])
                 keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='order_digital')])
                 await query.edit_message_text("🎨 Оберіть Discord Украшення:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif query.data in products.ALL_PRODUCTS:
            product = products.ALL_PRODUCTS[query.data]
            context.user_data['pending_payment'] = {
                'product_id': query.data,
                'product_name': product['name'],
                'price': product['price']
            }
            
            order_text = f"🧾 Ваше замовлення:\n▫️ {product['name']}\n💳 Вартість: {product['price']} UAH\n\n"
            
            keyboard = [
                [InlineKeyboardButton("💳 Оплата по карті", callback_data=f'pay_card_{product["price"]}')],
                [InlineKeyboardButton("₿ Оплата криптовалютою", callback_data=f'pay_crypto_{product["price"]}')],
                [InlineKeyboardButton("❌ Скасувати", callback_data='cancel_payment')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(f"{order_text}Оберіть спосіб оплати:", reply_markup=reply_markup)

        elif query.data.startswith('pay_card_') or query.data.startswith('pay_crypto_'):
            if 'pending_payment' not in context.user_
                 await query.edit_message_text("❌ Помилка: інформація про замовлення відсутня.")
                 return

            pending_payment = context.user_data['pending_payment']
            is_card_payment = query.data.startswith('pay_card_')
            
            try:
                parts = query.data.split('_')
                if len(parts) < 3:
                    raise ValueError("Некорректный формат callback_data для оплаты")
                amount = int(parts[2])
            except (ValueError, IndexError):
                await query.edit_message_text("❌ Помилка: некоректна сума оплати.")
                return

            order_id_suffix = "unknown"
            if 'order_id' in pending_payment:
                order_id_suffix = pending_payment['order_id']
            elif 'product_id' in pending_payment:
                order_id_suffix = pending_payment['product_id']

            if is_card_payment:
                 await query.edit_message_text("💳 Оплата карткою поки що не реалізована через сайт. Скористайтеся криптооплатою або зв'яжіться з оператором.")
            else:
                invoice_data = self.create_invoice(amount=amount, pay_currency="usdtsol", currency="uah", order_id_suffix=order_id_suffix)
                
                if "error" in invoice_
                    await query.edit_message_text(f"❌ Помилка створення платежу: {invoice_data['error']}")
                    return

                pay_url = invoice_data.get("invoice_url")
                if pay_url:
                    keyboard = [
                        [InlineKeyboardButton(name, callback_data=f'invoice_{amount}_{code}')]
                        for name, code in products.AVAILABLE_CURRENCIES.items()
                    ]
                    keyboard.append([InlineKeyboardButton("✅ Оплачено", callback_data='paid_crypto')])
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await query.edit_message_text(
                        f"🔗 Ваш рахунок для оплати:\n"
                        f"Сума: {amount} UAH\n"
                        f"Посилання: {pay_url}\n\n"
                        f"Оберіть криптовалюту або натисніть '✅ Оплачено' після оплати:",
                        reply_markup=reply_markup
                    )
                else:
                    await query.edit_message_text("❌ Не вдалося отримати посилання для оплати. Спробуйте пізніше.")

        elif query.data.startswith('invoice_'):
             if 'pending_payment' not in context.user_data:
                 await query.edit_message_text("❌ Помилка: інформація про замовлення відсутня.")
                 return

             pending_payment = context.user_data['pending_payment']
             try:
                parts = query.data.split('_')
                if len(parts) != 3:
                    raise ValueError("Некорректный формат callback_data для инвойса")
                amount = int(parts[1])
                pay_currency_code = parts[2]
             except (ValueError, IndexError):
                await query.edit_message_text("❌ Помилка: некоректні дані для створення інвойсу.")
                return

             order_id_suffix = "unknown"
             if 'order_id' in pending_payment:
                order_id_suffix = pending_payment['order_id']
             elif 'product_id' in pending_payment:
                order_id_suffix = pending_payment['product_id']

             invoice_data = self.create_invoice(amount=amount, pay_currency=pay_currency_code, currency="uah", order_id_suffix=order_id_suffix)

             if "error" in invoice_data:
                await query.edit_message_text(f"❌ Ошибка создания платежа: {invoice_data['error']}")
                return

             pay_url = invoice_data.get("invoice_url")
             if pay_url:
                keyboard = [
                    [InlineKeyboardButton(name, callback_data=f'invoice_{amount}_{code}')]
                    for name, code in products.AVAILABLE_CURRENCIES.items()
                ]
                keyboard.append([InlineKeyboardButton("✅ Оплачено", callback_data='paid_crypto')])
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    f"🔗 Ваш оновлений рахунок для оплати:\n"
                    f"Сума: {amount} UAH\n"
                    f"Криптовалюта: {pay_currency_code.upper()}\n"
                    f"Посилання: {pay_url}\n\n"
                    f"Оберіть іншу криптовалюту або натисніть '✅ Оплачено' після оплати:",
                    reply_markup=reply_markup
                )
             else:
                 await query.edit_message_text("❌ Не вдалося отримати посилання для оплати. Спробуйте пізніше.")

        elif query.data == 'paid_crypto':
            if 'pending_payment' in context.user_data:
                pending_payment = context.user_data['pending_payment']
                order_text = "🔔 Нове замовлення (очікує оплату):\n"
                if 'order_id' in pending_payment:
                    order_text += f"🧾 Номер замовлення: #{pending_payment['order_id']}\n"
                    bot_statistics['total_orders'] += 1
                    save_statistics()
                if 'items' in pending_payment:
                    for item in pending_payment['items']:
                        order_text += f"▫️ {item['service']} {item['plan']} ({item['period']}) - {item['price']} UAH\n"
                    order_text += f"💳 Всього: {pending_payment['total_uah']} UAH\n"
                    order_text += f"🌐 Джерело: {'Сайт' if pending_payment.get('from_website') else 'Бот'}\n"
                elif 'product_name' in pending_payment:
                    order_text += f"▫️ {pending_payment['product_name']} - {pending_payment['price']} UAH\n"
                    order_text += f"💳 Всього: {pending_payment['price']} UAH\n"
                    order_text += f"🌐 Джерело: Бот\n"
                
                order_text += f"👤 Користувач: @{user.username or 'Невідомий'} (ID: {user_id})\n"
                order_text += f"Ім'я: {user.first_name} {user.last_name or ''}\n"

                await self.forward_order_to_owners(context, user_id, user, order_text)
                
                del context.user_data['pending_payment']
                
                await query.edit_message_text("✅ Дякуємо! Ваш платіж обробляється. Оператор зв'яжеться з вами найближчим часом.")
            else:
                await query.edit_message_text("❌ Помилка: інформація про замовлення відсутня.")

        elif query.data == 'cancel_payment':
            if 'pending_payment' in context.user_data:
                del context.user_data['pending_payment']
            await query.edit_message_text("❌ Оплата скасована. Повертаємося в головне меню.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')],
                [InlineKeyboardButton("ℹ️ Допомога", callback_data='help')]
            ]))

    async def message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        ensure_user_exists(user)
        message_text = update.message.text

        conversation_type = context.user_data.get('conversation_type')

        if conversation_type == 'question':
            bot_statistics['total_questions'] += 1
            save_statistics()
            question_text = f"❓ Нове запитання:\n\n"
            question_text += f"Питання: {message_text}\n"
            question_text += f"Від: @{user.username or 'Невідомий'} (ID: {user_id})\n"
            question_text += f"Ім'я: {user.first_name} {user.last_name or ''}\n"
            
            await update.message.reply_text("✅ Дякуємо за ваше запитання! Оператор зв'яжеться з вами найближчим часом.")
            
            await self.forward_question_to_owners(context, user_id, user, question_text)
            
            if 'conversation_type' in context.user_
                del context.user_data['conversation_type']
        else:
            await self.start(update, context)

    async def forward_order_to_owners(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, user: User, order_text: str):
        logger.info(f"🔔 Надсилання замовлення операторам: {order_text}")
        for owner_id in OWNER_IDS:
            try:
                await context.bot.send_message(chat_id=owner_id, text=order_text)
                logger.info(f"✅ Замовлення надіслано оператору {owner_id}")
            except Exception as e:
                logger.error(f"❌ Не вдалося надіслати замовлення оператору {owner_id}: {e}")

    async def forward_question_to_owners(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, user: User, question_text: str):
        logger.info(f"❓ Надсилання запитання операторам: {question_text}")
        for owner_id in OWNER_IDS:
            try:
                await context.bot.send_message(chat_id=owner_id, text=question_text)
                logger.info(f"✅ Запитання надіслано оператору {owner_id}")
            except Exception as e:
                logger.error(f"❌ Не вдалося надіслати запитання оператору {owner_id}: {e}")

app = Flask(__name__)
CORS(app)

bot_instance = None

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    logger.info(f"📥 Отримано вебхук NOWPayments: {json.dumps(data, indent=2)}")
    return jsonify({'status': 'ok'})

async def start_bot():
    global bot_instance
    try:
        application = Application.builder().token(TOKEN).build()
        bot_instance = SecureShopBot(application)
        
        application.add_handler(CommandHandler("start", bot_instance.start))
        application.add_handler(CommandHandler("help", bot_instance.help_command))
        application.add_handler(CommandHandler("channel", bot_instance.channel_command))
        application.add_handler(CommandHandler("stats", bot_instance.stats_command))
        application.add_handler(CommandHandler("pay", bot_instance.pay_command))
        application.add_handler(CallbackQueryHandler(bot_instance.button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_instance.message_handler))
        
        await application.bot.set_my_commands([
            BotCommandScopeChat("start", "Головне меню"),
            BotCommandScopeChat("help", "Довідка"),
            BotCommandScopeChat("channel", "Наш канал"),
            BotCommandScopeChat("stats", "Статистика (для операторів)"),
            BotCommandScopeChat("pay", "Оплата замовлення з сайту"),
        ])
        
        logger.info("🤖 Бот ініціалізований.")
        
        if USE_POLLING:
            await application.initialize()
            await application.start()
            logger.info("🔄 Бот запущений в режимі polling.")
            return application
        else:
            pass
            
    except Exception as e:
        logger.error(f"❌ Критична помилка запуску бота: {e}")
        raise

def bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_instance.loop = loop
    try:
        application = loop.run_until_complete(start_bot())
        if USE_POLLING and application:
            loop.run_until_complete(application.run_polling())
    except Conflict as e:
        logger.error(f"🚨 Конфликт: {e}")
        time.sleep(30)
        bot_thread()
    except Exception as e:
        logger.error(f"❌ Критична ошибка в bot_thread: {e}")
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
    logger.info("🚀 Запуск SecureShopBot...")
    
    flask_thread = threading.Thread(target=lambda: app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()
    logger.info(f"🌐 Flask сервер запущено на http://{FLASK_HOST}:{FLASK_PORT}")
    
    bot_thread()

if __name__ == '__main__':
    main()
