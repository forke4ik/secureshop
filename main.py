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
USE_POLLING = os.getenv('USE_POLLING', 'false').lower() == 'true'

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
            # Проверка на случай конфликта
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
            await self.start_polling()  # Рекурсивный перезапуск
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
        
        # Проверяем, является ли пользователь основателем
        if user.id in [OWNER_ID_1, OWNER_ID_2]:
            owner_name = "@HiGki2pYYY" if user.id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(
                f"Добро пожаловать, {user.first_name}! ({owner_name})\n"
                f"Вы вошли как основатель магазина."
            )
            return
        
        # Приветствие для клиентов
        keyboard = [
            [InlineKeyboardButton("🛒 Зробити замовлення", callback_data='order')],
            [InlineKeyboardButton("❓ Задати питання", callback_data='question')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_message = f"""
Добро пожаловать, {user.first_name}! 👋

Я бот-помощник нашего магазина. Выберите, что вас интересует:
        """
        
        await update.message.reply_text(
            welcome_message.strip(),
            reply_markup=reply_markup
        )
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        
        if query.data in ['order', 'question']:
            active_conversations[user_id] = {
                'type': 'order' if query.data == 'order' else 'question',
                'user_info': query.from_user,
                'assigned_owner': None
            }
            
            action_text = "оформить заказ" if query.data == 'order' else "задать вопрос"
            
            await query.edit_message_text(
                f"Отлично! Напишите ваше сообщение, чтобы {action_text}. "
                f"Я передам его основателю магазина."
            )
        
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
                         f"Для ответа просто напишите сообщение."
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
                [InlineKeyboardButton("❓ Задати питання", callback_data='question')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "Выберите действие:",
                reply_markup=reply_markup
            )
    
    async def forward_to_owner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Пересылка сообщения клиента основателю"""
        user_id = update.effective_user.id
        user_info = active_conversations[user_id]['user_info']
        conversation_type = active_conversations[user_id]['type']
        
        assigned_owner = active_conversations[user_id]['assigned_owner']
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
            "✅ Ваше сообщение передано основателю магазина. "
            "Ожидайте ответа в ближайшее время."
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
                text=f"📩 Ответ от магазина:\n\n{update.message.text}"
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

# Создаем экземпляр бота
bot_instance = TelegramBot()

# Flask маршруты
@flask_app.route('/ping', methods=['GET'])
def ping():
    """Эндпоинт для поддержания активности"""
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
    """Эндпоинт для проверки состояния"""
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

# Webhook обработчик (если используется webhook режим)
@flask_app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Обработчик webhook для Telegram (только для webhook режима)"""
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
            # В webhook режиме нужно обрабатывать синхронно
            # Это требует более сложной настройки, поэтому используем polling
            pass
        return '', 200
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/', methods=['GET'])
def index():
    """Главная страница"""
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
    """Настройка webhook (только для webhook режима)"""
    if USE_POLLING:
        # Удаляем webhook если используем polling
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
    """Запуск бота"""
    global telegram_app, bot_running
    
    with bot_lock:
        if bot_running:
            logger.warning("🛑 Бот уже запущен! Пропускаем повторный запуск")
            return
        
        try:
            # Инициализируем приложение
            await bot_instance.initialize()
            telegram_app = bot_instance.application
            
            if USE_POLLING:
                # Polling режим
                await setup_webhook()  # Удаляем webhook
                await bot_instance.start_polling()
                bot_running = True
                logger.info("✅ Бот запущен в polling режиме")
            else:
                # Webhook режим
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
    """Поток для запуска бота"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_instance.loop = loop
    
    try:
        loop.run_until_complete(start_bot())
        if USE_POLLING:
            # Держим loop живым для polling
            loop.run_forever()
    except Conflict as e:
        logger.error(f"🚨 Критический конфликт: {e}")
        logger.warning("🕒 Ожидаем 30 секунд перед повторным запуском...")
        time.sleep(30)
        bot_thread()  # Рекурсивный перезапуск
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в bot_thread: {e}")
        logger.warning("🕒 Ожидаем 15 секунд перед повторным запуском...")
        time.sleep(15)
        bot_thread()  # Рекурсивный перезапуск
    finally:
        try:
            if not loop.is_closed():
                loop.close()
        except:
            pass
        logger.warning("🔁 Перезапускаем поток бота...")
        time.sleep(5)
        bot_thread()  # Гарантированный перезапуск

def main():
    """Основная функция"""
    flask_app.start_time = time.time()
    
    logger.info("🚀 Запуск SecureShop Telegram Bot...")
    logger.info(f"🔑 BOT_TOKEN: {BOT_TOKEN[:10]}...")
    logger.info(f"🌐 PORT: {PORT}")
    logger.info(f"📡 WEBHOOK_URL: {WEBHOOK_URL}")
    logger.info(f"⏰ PING_INTERVAL: {PING_INTERVAL} секунд")
    logger.info(f"🔄 РЕЖИМ: {'Polling' if USE_POLLING else 'Webhook'}")
    logger.info(f"👤 Основатель 1: {OWNER_ID_1} (@HiGki2pYYY)")
    logger.info(f"👤 Основатель 2: {OWNER_ID_2} (@oc33t)")
    
    # Запускаем бота в отдельном потоке
    bot_thread_instance = threading.Thread(target=bot_thread)
    bot_thread_instance.daemon = True
    bot_thread_instance.start()
    
    # Ждем немного для инициализации
    time.sleep(3)
    
    # Запускаем пинговалку
    bot_instance.start_ping_service()
    
    # Запускаем Flask сервер
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
