import logging
import os
import asyncio
import threading
import time
import requests
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_TOKEN_HERE')
OWNER_ID_1 = 7106925462  # @HiGki2pYYY
OWNER_ID_2 = 6279578957  # @oc33t
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://yourdomain.com')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840))  # сек
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'

# Хранилище состояний
active_conversations = {}
owner_client_map = {}

# Flask App
flask_app = Flask(__name__)
flask_app.start_time = time.time()
bot_running = False
bot_lock = threading.Lock()

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.initialized = False

    def setup_handlers(self):
        self.application.add_handler(CommandHandler('start', self.start))
        self.application.add_handler(CommandHandler('stop', self.stop_conversation))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)

    async def initialize(self):
        await self.application.initialize()
        self.initialized = True
        logger.info('✨ Bot initialized')

    async def start_polling(self):
        try:
            await self.application.start()
            await self.application.updater.start_polling()
            logger.info('🚀 Polling started')
        except Conflict as e:
            logger.error(f'❗ Conflict: {e}')
            await asyncio.sleep(15)
            await self.start_polling()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        # Вход владельцев
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            name = '@HiGki2pYYY' if user.id == OWNER_ID_1 else '@oc33t'
            return await update.message.reply_text(f'Ласкаво просимо, {user.first_name}! ({name}) Ви ввійшли як засновник магазину.')

        # Главное меню
        keyboard = [
            [InlineKeyboardButton('🛒 Зробити замовлення', callback_data='menu_order')],
            [InlineKeyboardButton('❓ Поставити запитання', callback_data='question')]
        ]
        await update.message.reply_text(
            f'Ласкаво просимо, {user.first_name}! 👋\nБудь ласка, оберіть, що вас цікавить:',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return
        if owner_id not in owner_client_map:
            return await update.message.reply_text('У вас немає активного діалогу.')
        client_id = owner_client_map.pop(owner_id)
        active_conversations.pop(client_id, None)
        await context.bot.send_message(client_id, 'Діалог завершено. Використайте /start для нових запитів.')
        await update.message.reply_text('✅ Діалог завершено.')

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        user = query.from_user
        user_id = user.id

        # Назад
        if data == 'back_main':
            return await self.start(update, context)

        # Поставити запитання
        if data == 'question':
            active_conversations[user_id] = {'type': 'question'}
            return await query.edit_message_text('💬 Напишіть ваше питання, і я передам його адміністраторам.')

        # Меню замовлень
        if data == 'menu_order':
            kb = [
                [InlineKeyboardButton('▶️ YouTube', callback_data='svc_youtube')],
                [InlineKeyboardButton('🤖 ChatGPT', callback_data='svc_chatgpt')],
                [InlineKeyboardButton('🎧 Spotify', callback_data='svc_spotify')],
                [InlineKeyboardButton('💬 Discord', callback_data='svc_discord')],
                [InlineKeyboardButton('🔙 Назад', callback_data='back_main')]
            ]
            return await query.edit_message_text('🛒 Оберіть сервіс для замовлення:', reply_markup=InlineKeyboardMarkup(kb))

        # YouTube
        if data == 'svc_youtube':
            kb = [
                [InlineKeyboardButton('6 місяців – 450 UAH', callback_data='opt_YouTube_6місяців_450UAH')],
                [InlineKeyboardButton('12 місяців – 750 UAH', callback_data='opt_YouTube_12місяців_750UAH')],
                [InlineKeyboardButton('🔙 Назад', callback_data='menu_order')]
            ]
            return await query.edit_message_text('📺 YouTube – оберіть термін:', reply_markup=InlineKeyboardMarkup(kb))

        # ChatGPT
        if data == 'svc_chatgpt':
            kb = [
                [InlineKeyboardButton('1 місяць – 650 UAH', callback_data='opt_ChatGPT_1місяць_650UAH')],
                [InlineKeyboardButton('🔙 Назад', callback_data='menu_order')]
            ]
            return await query.edit_message_text('🤖 ChatGPT – оберіть термін:', reply_markup=InlineKeyboardMarkup(kb))

        # Spotify
        if data == 'svc_spotify':
            kb = []
            # Individual
            for term, price in [('1 місяць', '125 UAH'), ('3 місяці', '350 UAH'), ('6 місяців', '550 UAH'), ('12 місяців', '900 UAH')]:
                kb.append([InlineKeyboardButton(f'Premium individual – {term} – {price}', callback_data=f'opt_Spotify_ind_{term}_{price}')])
            # Family
            for term, price in [('1 місяць', '200 UAH'), ('3 місяці', '569 UAH'), ('6 місяців', '1100 UAH'), ('12 місяців', '2100 UAH')]:
                kb.append([InlineKeyboardButton(f'Premium family – {term} – {price}', callback_data=f'opt_Spotify_fam_{term}_{price}')])
            kb.append([InlineKeyboardButton('🔙 Назад', callback_data='menu_order')])
            return await query.edit_message_text('🎧 Spotify – оберіть тариф:', reply_markup=InlineKeyboardMarkup(kb))

        # Discord
        if data == 'svc_discord':
            kb = []
            for plan_key, term, price in [('basic', '1 місяць', '100 UAH'), ('basic', '12 місяців', '900 UAH'), ('full', '1 місяць', '170 UAH'), ('full', '12 місяців', '1700 UAH')]:
                name = 'Nitro basic' if plan_key == 'basic' else 'Nitro full'
                kb.append([InlineKeyboardButton(f'{name} – {term} – {price}', callback_data=f'opt_Discord_{plan_key}_{term}_{price}')])
            kb.append([InlineKeyboardButton('🔙 Назад', callback_data='menu_order')])
            return await query.edit_message_text('💬 Discord – оберіть тариф:', reply_markup=InlineKeyboardMarkup(kb))

        # Выбор опции
        if data.startswith('opt_'):
            _, svc, *rest = data.split('_')
            if svc in ['YouTube', 'ChatGPT']:
                term, price = rest
n            else:
                plan_key = rest[0]
                term, price = rest[1], rest[2]
                svc = 'Spotify' if 'Spotify' in data else 'Discord'
            # Сохранение
            entry = {'type': 'order', 'service': svc, 'plan': plan_key if svc not in ['YouTube','ChatGPT'] else '', 'term': term, 'price': price}
            active_conversations[user.id] = entry
            return await query.edit_message_text(
                f"✅ Замовити {svc} {entry.get('plan','')} {term} – {price}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 Замовити', callback_data='order_confirm')]])
            )

        # Подтверждение заказа
        if data == 'order_confirm':
            order = active_conversations.get(user.id, {})
            # Клиенту
            await query.edit_message_text(
                f"📝 Ви обрали {order.get('service')} {order.get('plan','')} {order.get('term')} – {order.get('price')}\n"
                "Напишіть свої контактні дані для оформлення замовлення, і ми зв’яжемося!"
            )
            # Админам
            summary = (
                f"📦 Нове замовлення!\n"
                f"Користувач: {user.full_name} (@{user.username or 'немає'}) [ID: {user.id}]\n"
                f"Сервіс: {order.get('service')} {order.get('plan','')} {order.get('term')} – {order.get('price')}"
            )
            for owner in [OWNER_ID_1, OWNER_ID_2]:
                await context.bot.send_message(chat_id=owner, text=summary)
            return

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id not in active_conversations:
            return await self.start(update, context)
        conv = active_conversations.pop(user.id)
        text = update.message.text.strip()
        if conv['type'] == 'question':
            summary = f"❓ Запитання від {user.full_name} (@{user.username or 'немає'}) [ID: {user.id}]: {text}"
            for owner in [OWNER_ID_1, OWNER_ID_2]:
                await context.bot.send_message(chat_id=owner, text=summary)
            return await update.message.reply_text('Дякуємо! Ваше питання надіслано адміністрації.')
        if conv['type'] == 'order':
            summary = (
                f"✅ Контакти для замовлення від {user.full_name} (@{user.username or 'немає'}) [ID: {user.id}]:\n"
                f"Сервіс: {conv.get('service')} {conv.get('plan','')} {conv.get('term')} – {conv.get('price')}\n"
                f"Контакти: {text}"
            )
            for owner in [OWNER_ID_1, OWNER_ID_2]:
                await context.bot.send_message(chat_id=owner, text=summary)
            return await update.message.reply_text("Дякуємо! Ми зв'яжемося з вами найближчим часом.")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Error: {context.error}")

    def start_ping_service(self):
        def ping_loop():
            while bot_running:
                try:
                    requests.get(f"{WEBHOOK_URL}/ping")
                except:
                    pass
                time.sleep(PING_INTERVAL)
        threading.Thread(target=ping_loop, daemon=True).start()

async def start_bot():
    global bot_running
    bot = TelegramBot()
    await bot.initialize()
    if USE_POLLING:
        await bot.start_polling()
        bot_running = True
        bot.start_ping_service()
        asyncio.get_event_loop().run_forever()
    else:
        await bot.application.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}")
        bot_running = True
        bot.start_ping_service()
        flask_app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)

@flask_app.route('/ping')
def ping():
    return jsonify(status='alive', uptime=time.time() - flask_app.start_time)

@flask_app.route('/')
def index():
    return jsonify(status='running')

if __name__ == '__main__':
    asyncio.run(start_bot())
