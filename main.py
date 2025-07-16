import logging
import os
import threading
import time
import asyncio
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
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840))
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'

# Состояния
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
        self.loop = None

    def setup_handlers(self):
        self.application.add_handler(CommandHandler('start', self.start))
        self.application.add_handler(CommandHandler('stop', self.stop_conversation))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)

    async def initialize(self):
        await self.application.initialize()
        self.initialized = True
        logger.info('Telegram Application initialized')

    async def start_polling(self):
        try:
            await self.application.start()
            await self.application.updater.start_polling()
            logger.info('Polling started')
        except Conflict as e:
            logger.error(f'Conflict: {e}')
            await asyncio.sleep(15)
            await self.start_polling()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            name = '@HiGki2pYYY' if user.id == OWNER_ID_1 else '@oc33t'
            return await update.message.reply_text(f'Ласкаво просимо, {user.first_name}! ({name}) Ви ввійшли як засновник магазину.')
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
        if owner_id not in [OWNER_ID_1, OWNER_ID_2]: return
        if owner_id not in owner_client_map:
            return await update.message.reply_text('У вас немає активного діалогу.')
        client_id = owner_client_map.pop(owner_id)
        active_conversations.pop(client_id, None)
        await context.bot.send_message(client_id, 'Діалог завершено. Використайте /start для нових запитів.')
        await update.message.reply_text('✅ Діалог завершено.')

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query; await query.answer()
        data = query.data; uid = query.from_user.id
        # Back
        if data == 'back_main': return await self.start(update, context)
        # Question
        if data == 'question':
            active_conversations[uid] = {'type':'question'}
            return await query.edit_message_text('💬 Напишіть ваше питання...')
        # Order menu
        if data == 'menu_order':
            kb = [[InlineKeyboardButton('▶️ YouTube', callback_data='svc_youtube')],
                  [InlineKeyboardButton('🤖 ChatGPT', callback_data='svc_chatgpt')],
                  [InlineKeyboardButton('🎧 Spotify', callback_data='svc_spotify')],
                  [InlineKeyboardButton('💬 Discord', callback_data='svc_discord')],
                  [InlineKeyboardButton('🔙 Назад', callback_data='back_main')]]
            return await query.edit_message_text('🛒 Оберіть сервіс:', reply_markup=InlineKeyboardMarkup(kb))
        # YouTube
        if data=='svc_youtube':
            kb = [[InlineKeyboardButton('6 місяців – 450 UAH', callback_data='opt_YouTube_6м_450')],
                  [InlineKeyboardButton('12 місяців – 750 UAH', callback_data='opt_YouTube_12м_750')],
                  [InlineKeyboardButton('🔙 Назад', callback_data='menu_order')]]
            return await query.edit_message_text('📺 YouTube – оберіть термін:', reply_markup=InlineKeyboardMarkup(kb))
        # ChatGPT
        if data=='svc_chatgpt':
            kb=[[InlineKeyboardButton('1 місяць – 650 UAH', callback_data='opt_ChatGPT_1м_650')],
                [InlineKeyboardButton('🔙 Назад', callback_data='menu_order')]]
            return await query.edit_message_text('🤖 ChatGPT – оберіть термін:', reply_markup=InlineKeyboardMarkup(kb))
        # Spotify
        if data=='svc_spotify':
            kb=[]
            for plan in [('Individual','1 місяць','125'),('Individual','3 місяці','350'),('Individual','6 місяців','550'),('Individual','12 місяців','900'),
                         ('Family','1 місяць','200'),('Family','3 місяці','569'),('Family','6 місяців','1100'),('Family','12 місяців','2100')]:
                kb.append([InlineKeyboardButton(f'Premium {plan[0]} – {plan[1]} – {plan[2]} UAH', callback_data=f'opt_Spotify_{plan[0]}_{plan[1]}_{plan[2]}')])
            kb.append([InlineKeyboardButton('🔙 Назад', callback_data='menu_order')])
            return await query.edit_message_text('🎧 Spotify – оберіть тариф:', reply_markup=InlineKeyboardMarkup(kb))
        # Discord
        if data=='svc_discord':
            kb=[]
            for dn in [('Basic','1 місяць','100'),('Basic','12 місяців','900'),('Full','1 місяць','170'),('Full','12 місяців','1700')]:
                kb.append([InlineKeyboardButton(f'Nitro {dn[0]} – {dn[1]} – {dn[2]} UAH', callback_data=f'opt_Discord_{dn[0]}_{dn[1]}_{dn[2]}')])
            kb.append([InlineKeyboardButton('🔙 Назад', callback_data='menu_order')])
            return await query.edit_message_text('💬 Discord – оберіть тариф:', reply_markup=InlineKeyboardMarkup(kb))
        # Option selected
        if data.startswith('opt_'):
            _, svc, *rest = data.split('_')
            term, price = rest[-2], rest[-1]+' UAH'
            plan = rest[1] if len(rest)==3 else ''
            active_conversations[uid] = {'type':'order','service':svc,'plan':plan,'term':term,'price':price}
            return await query.edit_message_text(f"✅ Замовити {svc} {plan} {term} – {price}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 Замовити', callback_data='order_confirm')]]))
        # Confirm
        if data=='order_confirm':
            order=active_conversations[uid]
            await query.edit_message_text(f"📝 Ви обрали {order['service']} {order['plan']} {order['term']} – {order['price']}\nНапишіть контакти.")
            summary=f"📦 Нове замовлення!\nКористувач: {query.from_user.full_name} [ID: {uid}]\nСервіс: {order['service']} {order['plan']} {order['term']} – {order['price']}"
            for o in [OWNER_ID_1,OWNER_ID_2]: await context.bot.send_message(o, summary)
            return

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid=update.effective_user.id
        if uid in active_conversations:
            conv=active_conversations.pop(uid)
            if conv['type']=='question':
                txt=update.message.text
                sumq=f"❓ Питання від {update.effective_user.full_name}: {txt}"
                for o in [OWNER_ID_1,OWNER_ID_2]: await context.bot.send_message(o,sumq)
                return await update.message.reply_text('Дякуємо!')
            if conv['type']=='order':
                cnt=update.message.text
                sumo=f"✅ Контакти від {update.effective_user.full_name}: {cnt}\nСервіс: {conv['service']} {conv['plan']} {conv['term']} – {conv['price']}"
                for o in [OWNER_ID_1,OWNER_ID_2]: await context.bot.send_message(o,sumo)
                return await update.message.reply_text("Дякуємо! Ми зв'яжемося.")
