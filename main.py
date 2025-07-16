import logging
import os
import asyncio
import threading
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
bot_lock = threading.Lock()  # Блокировка для управления доступом к боту

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        self.ping_running = False
        self.initialized = False
        self.polling_task = None
        self.loop = None
    
    def setup_handlers(self):
        """Настройка обработчиков команд и сообщений"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)
    
    async def initialize(self):
        """Асинхронная инициализация приложения"""
        try:
            await self.application.initialize()
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
        """Обработчик команды /start"""
        user = update.effective_user
        
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            owner_name = "@HiGki2pYYY" if user.id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(
                f"Добро пожаловать, {user.first_name}! ({owner_name})\n"
                f"Вы вошли как основатель магазина."
            )
            return
        
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')]
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
    
    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /stop для основателей"""
        owner_id = update.effective_user.id

        if owner_id not in [OWNER_ID_1, OWNER_ID_2]:
            return

        if owner_id not in owner_client_map:
            await update.message.reply_text("У вас нет активного диалога для завершения.")
            return

        client_id = owner_client_map[owner_id]
        client_info = active_conversations.get(client_id, {}).get('user_info')

        try:
            await context.bot.send_message(
                chat_id=client_id,
                text="Діалог завершено представником магазину. Якщо у вас є нові питання, будь ласка, скористайтесь командою /start."
            )
            if client_info:
                await update.message.reply_text(f"✅ Вы успешно завершили диалог с клиентом {client_info.first_name}.")
            else:
                await update.message.reply_text(f"✅ Вы успешно завершили диалог с клиентом ID {client_id}.")

        except Exception as e:
            logger.error(f"Ошибка при уведомлении клиента {client_id} о завершении диалога: {e}")
            await update.message.reply_text("Не удалось уведомить клиента (возможно, он заблокировал бота), но диалог был завершен на вашей стороне.")

        if client_id in active_conversations:
            del active_conversations[client_id]
        if owner_id in owner_client_map:
            del owner_client_map[owner_id]

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        
        # Главное меню
        if query.data == 'order':
            keyboard = [
                [InlineKeyboardButton("📺 YouTube", callback_data='category_youtube')],
                [InlineKeyboardButton("💬 ChatGPT", callback_data='category_chatgpt')],
                [InlineKeyboardButton("🎵 Spotify", callback_data='category_spotify')],
                [InlineKeyboardButton("🎮 Discord", callback_data='category_discord')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
            ]
            await query.edit_message_text(
                "📦 Оберіть категорію товару:",
                reply_markup=InlineKeyboardMarkup(keyboard)
        
        # Кнопка "Назад" в главное меню
        elif query.data == 'back_to_main':
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')]
            ]
            await query.edit_message_text(
                "Головне меню:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Меню YouTube
        elif query.data == 'category_youtube':
            keyboard = [
                [InlineKeyboardButton("6 місяців - 450 UAH", callback_data='youtube_6')],
                [InlineKeyboardButton("12 місяців - 750 UAH", callback_data='youtube_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "📺 Оберіть варіант YouTube Premium:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Меню ChatGPT
        elif query.data == 'category_chatgpt':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 650 UAH", callback_data='chatgpt_1')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "💬 Оберіть варіант ChatGPT Plus:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Меню Spotify
        elif query.data == 'category_spotify':
            keyboard = [
                [InlineKeyboardButton("Premium Individual", callback_data='spotify_individual')],
                [InlineKeyboardButton("Premium Family", callback_data='spotify_family')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "🎵 Оберіть тип Spotify Premium:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Подменю Spotify Individual
        elif query.data == 'spotify_individual':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 125 UAH", callback_data='spotify_ind_1')],
                [InlineKeyboardButton("3 місяці - 350 UAH", callback_data='spotify_ind_3')],
                [InlineKeyboardButton("6 місяців - 550 UAH", callback_data='spotify_ind_6')],
                [InlineKeyboardButton("12 місяців - 900 UAH", callback_data='spotify_ind_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_spotify')]
            ]
            await query.edit_message_text(
                "👤 Spotify Premium Individual:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Подменю Spotify Family
        elif query.data == 'spotify_family':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 200 UAH", callback_data='spotify_fam_1')],
                [InlineKeyboardButton("3 місяці - 569 UAH", callback_data='spotify_fam_3')],
                [InlineKeyboardButton("6 місяців - 1100 UAH", callback_data='spotify_fam_6')],
                [InlineKeyboardButton("12 місяців - 2100 UAH", callback_data='spotify_fam_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_spotify')]
            ]
            await query.edit_message_text(
                "👨‍👩‍👧‍👦 Spotify Premium Family:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Меню Discord
        elif query.data == 'category_discord':
            keyboard = [
                [InlineKeyboardButton("Nitro Basic", callback_data='discord_basic')],
                [InlineKeyboardButton("Nitro Full", callback_data='discord_full')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='order')]
            ]
            await query.edit_message_text(
                "🎮 Оберіть тип Discord Nitro:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Подменю Discord Basic
        elif query.data == 'discord_basic':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 100 UAH", callback_data='discord_basic_1')],
                [InlineKeyboardButton("12 місяців - 900 UAH", callback_data='discord_basic_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text(
                "🔹 Discord Nitro Basic:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Подменю Discord Full
        elif query.data == 'discord_full':
            keyboard = [
                [InlineKeyboardButton("1 місяць - 170 UAH", callback_data='discord_full_1')],
                [InlineKeyboardButton("12 місяців - 1700 UAH", callback_data='discord_full_12')],
                [InlineKeyboardButton("⬅️ Назад", callback_data='category_discord')]
            ]
            await query.edit_message_text(
                "✨ Discord Nitro Full:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Обработка выбора товара
        elif query.data in [
            'youtube_6', 'youtube_12',
            'chatgpt_1',
            'spotify_ind_1', 'spotify_ind_3', 'spotify_ind_6', 'spotify_ind_12',
            'spotify_fam_1', 'spotify_fam_3', 'spotify_fam_6', 'spotify_fam_12',
            'discord_basic_1', 'discord_basic_12',
            'discord_full_1', 'discord_full_12'
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
                reply_markup=InlineKeyboardMarkup(keyboard))
        
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
                'user_info': query.from_user,
                'assigned_owner': None,
                'order_details': order_text
            }
            
            await query.edit_message_text(
                "✅ Ваше замовлення прийнято! Засновник магазину зв'яжеться з вами найближчим часом.\n\n"
                "Ви можете продовжити з іншим запитанням або замовленням.",
                reply_markup=None
            )
            
            # Пересылаем заказ владельцу
            await self.forward_order_to_owner(
                context, 
                user_id, 
                query.from_user, 
                order_text
            )
        
        # Обработка кнопки "question"
        elif query.data == 'question':
            active_conversations[user_id] = {
                'type': 'question',
                'user_info': query.from_user,
                'assigned_owner': None
            }
            await query.edit_message_text(
                "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
            )
        
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
                
                client_info = active_conversations[client_id]['user_info']
                
                await query.edit_message_text(
                    f"✅ Чат с клиентом {client_info.first_name} передан {other_owner_name}"
                )
                
                await context.bot.send_message(
                    chat_id=other_owner,
                    text=f"📨 Вам передан чат с клиентом:\n\n"
                         f"👤 {client_info.first_name} (@{client_info.username or 'не указан'})\n"
                         f"🆔 ID: {client_info.id}\n\n"
                         f"Для ответа просто напишите сообщение. Для завершения диалога используйте /stop"
                )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user_id = update.effective_user.id
        
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
            await self.handle_owner_message(update, context)
            return
        
        if user_id in active_conversations:
            await self.forward_to_owner(update, context)
        else:
            keyboard = [
                [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
                [InlineKeyboardButton("❓ Поставити запитання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "Будь ласка, оберіть дію або використайте /start, щоб розпочати.",
                reply_markup=reply_markup
            )
    
    async def forward_to_owner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Пересылка сообщения клиента основателю"""
        user_id = update.effective_user.id
        user_info = active_conversations[user_id]['user_info']
        conversation_type = active_conversations[user_id]['type']
        
        assigned_owner = active_conversations[user_id].get('assigned_owner')
        if not assigned_owner:
            assigned_owner = OWNER_ID_1
            active_conversations[user_id]['assigned_owner'] = assigned_owner
        
        owner_client_map[assigned_owner] = user_id
        
        type_emoji = "🛒" if conversation_type == 'order' else "❓"
        type_text = "ЗАКАЗ" if conversation_type == 'order' else "ВОПРОС"
        owner_name = "@HiGki2pYYY" if assigned_owner == OWNER_ID_1 else "@oc33t"
        
        forward_message = f"""
{type_emoji} {type_text} от клиента:

👤 Пользователь: {user_info.first_name}
📱 Username: @{user_info.username if user_info.username else 'не указан'}
🆔 ID: {user_info.id}

💬 Сообщение:
{update.message.text}

---
Для ответа просто напишите сообщение в этот чат.
Для завершения диалога используйте /stop.
Назначен: {owner_name}
        """
        
        keyboard = [
            [InlineKeyboardButton("🔄 Передать другому основателю", callback_data=f'transfer_{user_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=assigned_owner,
            text=forward_message.strip(),
            reply_markup=reply_markup
        )
        
        await update.message.reply_text(
            "✅ Ваше повідомлення передано засновнику магазину. "
            "Очікуйте на відповідь найближчим часом."
        )
    
    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка сообщений от основателя"""
        owner_id = update.effective_user.id
        
        if owner_id not in owner_client_map:
            owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(
                f"У вас нет активного клиента для ответа. ({owner_name})\n"
                f"Дождитесь нового сообщения от клиента."
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
            await context.bot.send_message(
                chat_id=client_id,
                text=f"📩 Відповідь від магазину:\n\n{update.message.text}"
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
            'youtube_6': {'name': "YouTube Premium (6 місяців)", 'price': 450},
            'youtube_12': {'name': "YouTube Premium (12 місяців)", 'price': 750},
            'chatgpt_1': {'name': "ChatGPT Plus (1 місяць)", 'price': 650},
            'spotify_ind_1': {'name': "Spotify Premium Individual (1 місяць)", 'price': 125},
            'spotify_ind_3': {'name': "Spotify Premium Individual (3 місяці)", 'price': 350},
            'spotify_ind_6': {'name': "Spotify Premium Individual (6 місяців)", 'price': 550},
            'spotify_ind_12': {'name': "Spotify Premium Individual (12 місяців)", 'price': 900},
            'spotify_fam_1': {'name': "Spotify Premium Family (1 місяць)", 'price': 200},
            'spotify_fam_3': {'name': "Spotify Premium Family (3 місяці)", 'price': 569},
            'spotify_fam_6': {'name': "Spotify Premium Family (6 місяців)", 'price': 1100},
            'spotify_fam_12': {'name': "Spotify Premium Family (12 місяців)", 'price': 2100},
            'discord_basic_1': {'name': "Discord Nitro Basic (1 місяць)", 'price': 100},
            'discord_basic_12': {'name': "Discord Nitro Basic (12 місяців)", 'price': 900},
            'discord_full_1': {'name': "Discord Nitro Full (1 місяць)", 'price': 170},
            'discord_full_12': {'name': "Discord Nitro Full (12 місяців)", 'price': 1700},
        }
        return products.get(product_code, {'name': "Невідомий товар", 'price': 0})
    
    def get_back_action(self, product_code):
        """Возвращает действие для кнопки 'Назад' в зависимости от товара"""
        category_map = {
            'youtube_6': 'category_youtube',
            'youtube_12': 'category_youtube',
            'chatgpt_1': 'category_chatgpt',
            'spotify_ind_1': 'spotify_individual',
            'spotify_ind_3': 'spotify_individual',
            'spotify_ind_6': 'spotify_individual',
            'spotify_ind_12': 'spotify_individual',
            'spotify_fam_1': 'spotify_family',
            'spotify_fam_3': 'spotify_family',
            'spotify_fam_6': 'spotify_family',
            'spotify_fam_12': 'spotify_family',
            'discord_basic_1': 'discord_basic',
            'discord_basic_12': 'discord_basic',
            'discord_full_1': 'discord_full',
            'discord_full_12': 'discord_full',
        }
        return category_map.get(product_code, 'order')
    
    async def forward_order_to_owner(self, context, client_id, client_info, order_text):
        """Пересылает заказ владельцу"""
        assigned_owner = OWNER_ID_1  # Первый владелец по умолчанию
        active_conversations[client_id]['assigned_owner'] = assigned_owner
        owner_client_map[assigned_owner] = client_id
        
        owner_name = "@HiGki2pYYY" if assigned_owner == OWNER_ID_1 else "@oc33t"
        
        forward_message = f"""
🛒 НОВЕ ЗАМОВЛЕННЯ!

👤 Клієнт: {client_info.first_name}
📱 Username: @{client_info.username if client_info.username else 'не вказано'}
🆔 ID: {client_info.id}

📋 Деталі замовлення:
{order_text}

---
Для відповіді напишіть повідомлення.
Для завершення діалогу використайте /stop.
Призначено: {owner_name}
        """
        
        keyboard = [
            [InlineKeyboardButton("🔄 Передати іншому засновнику", callback_data=f'transfer_{client_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=assigned_owner,
            text=forward_message.strip(),
            reply_markup=reply_markup
        )

bot_instance = TelegramBot()

@flask_app.route('/ping', methods=['GET'])
def ping():
    return jsonify({
        'status': 'alive',
        'message': 'Bot is running',
        'timestamp': time.time(),
        'uptime': time.time() - flask_app.start_time if hasattr(flask_app, 'start_time') else 0,
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
        'mode': 'polling' if USE_POLLING else 'webhook'
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
        'bot_running': bot_running
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

def main():
    flask_app.start_time = time.time()
    
    logger.info("🚀 Запуск SecureShop Telegram Bot...")
    logger.info(f"🔑 BOT_TOKEN: {BOT_TOKEN[:10]}...")
    logger.info(f"🌐 PORT: {PORT}")
    logger.info(f"📡 WEBHOOK_URL: {WEBHOOK_URL}")
    logger.info(f"⏰ PING_INTERVAL: {PING_INTERVAL} секунд")
    logger.info(f"🔄 РЕЖИМ: {'Polling' if USE_POLLING else 'Webhook'}")
    logger.info(f"👤 Основатель 1: {OWNER_ID_1} (@HiGki2pYYY)")
    logger.info(f"👤 Основатель 2: {OWNER_ID_2} (@oc33t)")
    
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
