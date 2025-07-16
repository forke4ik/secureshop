import logging
import os
import asyncio
import threading
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict
from flask import Flask, request, jsonify

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

# Словари для хранения данных
active_conversations = {}
owner_client_map = {}

# Глобальные переменные для приложения
telegram_app = None
flask_app = Flask(__name__)
bot_running = False

class TelegramBot:
    # --- Карта деталей заказа для отправки администратору ---
    ORDER_DETAILS_MAP = {
        'youtube_6m_450': "▶️ YouTube Premium | 6 місяців - 450 UAH",
        'youtube_12m_750': "▶️ YouTube Premium | 12 місяців - 750 UAH",
        'chatgpt_1m_650': "💬 ChatGPT Plus | 1 місяць - 650 UAH",
        'spotify_ind_1m_125': "🎧 Spotify Premium Individual | 1 місяць - 125 UAH",
        'spotify_ind_3m_350': "🎧 Spotify Premium Individual | 3 місяці - 350 UAH",
        'spotify_ind_6m_550': "🎧 Spotify Premium Individual | 6 місяців - 550 UAH",
        'spotify_ind_12m_900': "🎧 Spotify Premium Individual | 12 місяців - 900 UAH",
        'spotify_fam_1m_200': "🎧 Spotify Premium Family | 1 місяць - 200 UAH",
        'spotify_fam_3m_569': "🎧 Spotify Premium Family | 3 місяці - 569 UAH",
        'spotify_fam_6m_1100': "🎧 Spotify Premium Family | 6 місяців - 1100 UAH",
        'spotify_fam_12m_2100': "🎧 Spotify Premium Family | 12 місяців - 2100 UAH",
        'discord_basic_1m_100': "🎮 Discord Nitro Basic | 1 місяць - 100 UAH",
        'discord_basic_12m_900': "🎮 Discord Nitro Basic | 12 місяців - 900 UAH",
        'discord_full_1m_170': "🎮 Discord Nitro Full | 1 місяць - 170 UAH",
        'discord_full_12m_1700': "🎮 Discord Nitro Full | 12 місяців - 1700 UAH",
    }

    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.ping_running = False
        self.initialized = False

    # --- Генераторы клавиатур ---
    def get_start_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='show_services')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_services_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("▶️ YouTube Premium", callback_data='service_youtube'),
             InlineKeyboardButton("💬 ChatGPT Plus", callback_data='service_chatgpt')],
            [InlineKeyboardButton("🎧 Spotify Premium", callback_data='service_spotify'),
             InlineKeyboardButton("🎮 Discord Nitro", callback_data='service_discord')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_start')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_youtube_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("6 місяців - 450 UAH", callback_data='order_youtube_6m_450')],
            [InlineKeyboardButton("12 місяців - 750 UAH", callback_data='order_youtube_12m_750')],
            [InlineKeyboardButton("⬅️ Назад до послуг", callback_data='show_services')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_chatgpt_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='order_chatgpt_1m_650')],
            [InlineKeyboardButton("⬅️ Назад до послуг", callback_data='show_services')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_spotify_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("👤 Premium Individual", callback_data='spotify_individual')],
            [InlineKeyboardButton("👥 Premium Family", callback_data='spotify_family')],
            [InlineKeyboardButton("⬅️ Назад до послуг", callback_data='show_services')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_spotify_individual_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("1 місяць - 125 UAH", callback_data='order_spotify_ind_1m_125')],
            [InlineKeyboardButton("3 місяці - 350 UAH", callback_data='order_spotify_ind_3m_350')],
            [InlineKeyboardButton("6 місяців - 550 UAH", callback_data='order_spotify_ind_6m_550')],
            [InlineKeyboardButton("12 місяців - 900 UAH", callback_data='order_spotify_ind_12m_900')],
            [InlineKeyboardButton("⬅️ Назад до Spotify", callback_data='service_spotify')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_spotify_family_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("1 місяць - 200 UAH", callback_data='order_spotify_fam_1m_200')],
            [InlineKeyboardButton("3 місяці - 569 UAH", callback_data='order_spotify_fam_3m_569')],
            [InlineKeyboardButton("6 місяців - 1100 UAH", callback_data='order_spotify_fam_6m_1100')],
            [InlineKeyboardButton("12 місяців - 2100 UAH", callback_data='order_spotify_fam_12m_2100')],
            [InlineKeyboardButton("⬅️ Назад до Spotify", callback_data='service_spotify')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_discord_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("⭐ Nitro Basic", callback_data='discord_basic')],
            [InlineKeyboardButton("✨ Nitro Full", callback_data='discord_full')],
            [InlineKeyboardButton("⬅️ Назад до послуг", callback_data='show_services')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_discord_basic_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("1 місяць - 100 UAH", callback_data='order_discord_basic_1m_100')],
            [InlineKeyboardButton("12 місяців - 900 UAH", callback_data='order_discord_basic_12m_900')],
            [InlineKeyboardButton("⬅️ Назад до Discord", callback_data='service_discord')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_discord_full_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("1 місяць - 170 UAH", callback_data='order_discord_full_1m_170')],
            [InlineKeyboardButton("12 місяців - 1700 UAH", callback_data='order_discord_full_12m_1700')],
            [InlineKeyboardButton("⬅️ Назад до Discord", callback_data='service_discord')]
        ]
        return InlineKeyboardMarkup(keyboard)

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)

    async def initialize(self):
        if self.initialized:
            return
        try:
            await self.application.initialize()
            self.initialized = True
            logger.info("✅ Telegram Application инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации Telegram Application: {e}")
            raise

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            owner_name = "@HiGki2pYYY" if user.id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(f"Добро пожаловать, {user.first_name}! ({owner_name})\nВы вошли как основатель магазина.")
            return

        welcome_message = f"Ласкаво просимо, {user.first_name}! 👋\n\nЯ бот-помічник нашого магазину. Будь ласка, оберіть, що вас цікавить:"
        await update.message.reply_text(welcome_message.strip(), reply_markup=self.get_start_keyboard())

    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in [OWNER_ID_1, OWNER_ID_2] or owner_id not in owner_client_map:
            if owner_id in [OWNER_ID_1, OWNER_ID_2]:
                await update.message.reply_text("У вас нет активного диалога для завершения.")
            return

        client_id = owner_client_map.pop(owner_id)
        client_info = active_conversations.pop(client_id, {}).get('user_info')

        try:
            await context.bot.send_message(
                chat_id=client_id,
                text="Діалог завершено представником магазину. Якщо у вас є нові питання, будь ласка, скористайтеся командою /start."
            )
            if client_info:
                await update.message.reply_text(f"✅ Вы успешно завершили диалог с клиентом {client_info.first_name}.")
            else:
                await update.message.reply_text(f"✅ Вы успешно завершили диалог с клиентом ID {client_id}.")
        except Exception as e:
            logger.error(f"Ошибка при уведомлении клиента {client_id}: {e}")
            await update.message.reply_text("Не удалось уведомить клиента (возможно, он заблокировал бота), но диалог был завершен на вашей стороне.")
    
    async def setup_question_conversation(self, query: CallbackQuery):
        user_id = query.from_user.id
        active_conversations[user_id] = {
            'type': 'question',
            'user_info': query.from_user,
            'assigned_owner': None
        }
        await query.edit_message_text(
            "Будь ласка, напишіть ваше запитання. Я передам його засновнику магазину, і він незабаром вам відповість."
        )

    async def initiate_order_flow(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        order_key = query.data.replace('order_', '')
        order_details = self.ORDER_DETAILS_MAP.get(order_key)

        if not order_details:
            await query.edit_message_text("Помилка. Будь ласка, спробуйте ще раз.", reply_markup=self.get_services_keyboard())
            logger.warning(f"Не найден ключ заказа: {order_key}")
            return

        user_id = query.from_user.id
        user_info = query.from_user

        active_conversations[user_id] = {
            'type': 'order',
            'user_info': user_info,
            'assigned_owner': None
        }

        assigned_owner = OWNER_ID_1
        active_conversations[user_id]['assigned_owner'] = assigned_owner
        owner_client_map[assigned_owner] = user_id
        owner_name = "@HiGki2pYYY" if assigned_owner == OWNER_ID_1 else "@oc33t"

        forward_message = f"""
🛒 **НОВЕ ЗАМОВЛЕННЯ**

**📦 Товар:** {order_details}

**👤 Користувач:** {user_info.first_name}
**📱 Username:** @{user_info.username if user_info.username else 'не вказано'}
**🆔 ID:** {user_info.id}

---
*Клієнт очікує на інструкції для оплати. Для відповіді просто напишіть повідомлення в цей чат.*
*Для завершення діалогу використовуйте /stop.*
*Призначено: {owner_name}*
        """
        
        keyboard = [[InlineKeyboardButton("🔄 Передати іншому засновнику", callback_data=f'transfer_{user_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(chat_id=assigned_owner, text=forward_message.strip(), reply_markup=reply_markup, parse_mode='Markdown')
        await query.edit_message_text(
            "✅ **Ваше замовлення прийнято!**\n\n"
            "Менеджер вже отримав ваш запит і незабаром зв'яжеться з вами для уточнення деталей оплати та активації.\n\n"
            "Якщо у вас є додаткові питання, можете написати їх прямо сюди."
        , parse_mode='Markdown')

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = query.from_user.id

        # --- Навигация по меню ---
        if data == 'show_services':
            await query.edit_message_text('🛒 Оберіть сервіс, який вас цікавить:', reply_markup=self.get_services_keyboard())
        elif data == 'back_to_start':
            user = query.from_user
            welcome_message = f"Ласкаво просимо, {user.first_name}! 👋\n\nЯ бот-помічник нашого магазину. Будь ласка, оберіть, що вас цікавить:"
            await query.edit_message_text(welcome_message.strip(), reply_markup=self.get_start_keyboard())
        elif data == 'service_youtube':
            await query.edit_message_text('▶️ **YouTube Premium**\nОберіть бажаний термін підписки:', reply_markup=self.get_youtube_keyboard(), parse_mode='Markdown')
        elif data == 'service_chatgpt':
            await query.edit_message_text('💬 **ChatGPT Plus**\nОберіть бажаний термін підписки:', reply_markup=self.get_chatgpt_keyboard(), parse_mode='Markdown')
        elif data == 'service_spotify':
            await query.edit_message_text('🎧 **Spotify Premium**\nОберіть тип підписки:', reply_markup=self.get_spotify_keyboard(), parse_mode='Markdown')
        elif data == 'service_discord':
            await query.edit_message_text('🎮 **Discord Nitro**\nОберіть тип підписки:', reply_markup=self.get_discord_keyboard(), parse_mode='Markdown')
        elif data == 'spotify_individual':
            await query.edit_message_text('👤 **Spotify Premium Individual**\nОберіть термін:', reply_markup=self.get_spotify_individual_keyboard(), parse_mode='Markdown')
        elif data == 'spotify_family':
            await query.edit_message_text('👥 **Spotify Premium Family**\nОберіть термін:', reply_markup=self.get_spotify_family_keyboard(), parse_mode='Markdown')
        elif data == 'discord_basic':
            await query.edit_message_text('⭐ **Discord Nitro Basic**\nОберіть термін:', reply_markup=self.get_discord_basic_keyboard(), parse_mode='Markdown')
        elif data == 'discord_full':
            await query.edit_message_text('✨ **Discord Nitro Full**\nОберіть термін:', reply_markup=self.get_discord_full_keyboard(), parse_mode='Markdown')

        # --- Обработка действий ---
        elif data == 'question':
            await self.setup_question_conversation(query)
        elif data.startswith('order_'):
            await self.initiate_order_flow(query, context)
        elif data.startswith('transfer_'):
            client_id = int(data.split('_')[1])
            current_owner = user_id
            other_owner = OWNER_ID_2 if current_owner == OWNER_ID_1 else OWNER_ID_1
            other_owner_name = "@oc33t" if other_owner == OWNER_ID_2 else "@HiGki2pYYY"
            
            if client_id in active_conversations:
                active_conversations[client_id]['assigned_owner'] = other_owner
                owner_client_map[other_owner] = client_id
                if current_owner in owner_client_map:
                    del owner_client_map[current_owner]
                
                client_info = active_conversations[client_id]['user_info']
                await query.edit_message_text(f"✅ Чат с клиентом {client_info.first_name} передан {other_owner_name}")
                await context.bot.send_message(
                    chat_id=other_owner,
                    text=f"📨 Вам передан чат с клиентом:\n\n"
                         f"👤 {client_info.first_name} (@{client_info.username or 'не указан'})\n"
                         f"🆔 ID: {client_info.id}\n\n"
                         f"Для ответа просто напишите сообщение. Для завершения диалога используйте /stop"
                )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await self.handle_owner_message(update, context)
            return
        
        if user_id in active_conversations:
            await self.forward_to_owner(update, context)
        else:
            await update.message.reply_text(
                "Будь ласка, використайте /start, щоб розпочати.",
                reply_markup=self.get_start_keyboard()
            )

    async def forward_to_owner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_info = active_conversations[user_id]['user_info']
        conversation_type = active_conversations[user_id]['type']
        
        assigned_owner = active_conversations[user_id].get('assigned_owner')
        if not assigned_owner:
            assigned_owner = OWNER_ID_1
            active_conversations[user_id]['assigned_owner'] = assigned_owner
        
        owner_client_map[assigned_owner] = user_id
        type_emoji = "🛒" if conversation_type == 'order' else "❓"
        type_text = "ЗАМОВЛЕННЯ" if conversation_type == 'order' else "ЗАПИТАННЯ"
        
        forward_message = f"""
{type_emoji} Повідомлення від клієнта ({type_text}):

👤 **Користувач:** {user_info.first_name}
**📱 Username:** @{user_info.username if user_info.username else 'не вказано'}
**🆔 ID:** {user_info.id}

**💬 Повідомлення:**
{update.message.text}
        """
        await context.bot.send_message(chat_id=assigned_owner, text=forward_message.strip(), parse_mode='Markdown')
        await update.message.reply_text("✅ Ваше повідомлення передано. Очікуйте на відповідь.")

    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = update.effective_user.id
        if owner_id not in owner_client_map:
            await update.message.reply_text("У вас нет активного клиента для ответа. Дождитесь нового сообщения.")
            return

        client_id = owner_client_map[owner_id]
        if client_id not in active_conversations:
            del owner_client_map[owner_id]
            await update.message.reply_text("Диалог с клиентом завершен или не найден.")
            return
        
        try:
            await context.bot.send_message(
                chat_id=client_id,
                text=f"📩 **Відповідь від магазину:**\n\n{update.message.text}",
                parse_mode='Markdown'
            )
            client_info = active_conversations[client_id]['user_info']
            await update.message.reply_text(f"✅ Сообщение отправлено клиенту {client_info.first_name}")
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения клиенту {client_id}: {e}")
            await update.message.reply_text("❌ Ошибка при отправке сообщения клиенту. Возможно, он заблокировал бота.")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.warning(f'Update {update} caused error {context.error}')

    def start_ping_service(self):
        if not self.ping_running:
            self.ping_running = True
            ping_thread = threading.Thread(target=self.ping_loop)
            ping_thread.daemon = True
            ping_thread.start()
            logger.info("🔄 Пинговалка запущена")

    def ping_loop(self):
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
            time.sleep(PING_INTERVAL)

bot_instance = TelegramBot()

@flask_app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'status': 'alive', 'message': 'Bot is running'}), 200

@flask_app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'active_conversations': len(active_conversations),
        'owner_client_map': len(owner_client_map),
        'initialized': bot_instance.initialized if bot_instance else False,
        'bot_running': bot_running,
        'mode': 'polling' if USE_POLLING else 'webhook'
    }), 200

@flask_app.route(f'/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    if USE_POLLING:
        return jsonify({'error': 'Webhook disabled in polling mode'}), 400
    if not bot_instance.initialized:
        return jsonify({'error': 'Bot not initialized'}), 500
    try:
        update = Update.de_json(request.get_json(force=True), bot_instance.application.bot)
        await bot_instance.application.process_update(update)
        return '', 200
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/', methods=['GET'])
def index():
    return jsonify({'message': 'Telegram Bot SecureShop активен'}), 200

async def setup_webhook_or_polling():
    global telegram_app, bot_running
    
    await bot_instance.initialize()
    telegram_app = bot_instance.application
    
    if USE_POLLING:
        await telegram_app.bot.delete_webhook()
        logger.info("🗑️ Webhook удален - используется polling режим")
        bot_running = True
        logger.info("✅ Бот готов к запуску в режиме polling...")
        # Запуск polling будет в отдельной функции, которая блокирует поток
        await telegram_app.run_polling()
    else:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await telegram_app.bot.set_webhook(webhook_url)
        bot_running = True
        logger.info(f"✅ Webhook установлен: {webhook_url}")
        logger.info("✅ Бот запущен в режиме webhook")

def run_bot_in_thread():
    """Эта функция запускает бота в отдельном потоке, чтобы не блокировать Flask."""
    logger.info("Запуск потока для Telegram бота...")
    asyncio.run(setup_webhook_or_polling())

def main():
    logger.info("🚀 Запуск SecureShop Telegram Bot...")
    logger.info(f"🔑 BOT_TOKEN: {BOT_TOKEN[:10]}...")
    logger.info(f"🔄 РЕЖИМ: {'Polling' if USE_POLLING else 'Webhook'}")

    # Запускаем бота в отдельном потоке, если используется Polling
    if USE_POLLING:
        bot_thread = threading.Thread(target=run_bot_in_thread)
        bot_thread.daemon = True
        bot_thread.start()
    
    # Запускаем пинговалку, если URL задан
    if WEBHOOK_URL and not WEBHOOK_URL.isspace():
        bot_instance.start_ping_service()
    
    # Запускаем Flask сервер в основном потоке
    # Для webhook режима, бот будет обрабатывать запросы через Flask
    if not USE_POLLING:
        # В режиме webhook, инициализация происходит синхронно перед запуском Flask
        asyncio.run(setup_webhook_or_polling())
    
    logger.info("🌐 Запуск Flask сервера...")
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
