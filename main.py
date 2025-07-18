import logging
import os
import asyncio
import threading
import time
import json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import Conflict, RetryAfter
from flask import Flask, request, jsonify

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_DEFAULT_BOT_TOKEN')  # Измените на реальный токен
OWNER_ID_1 = 7106925462  # @HiGki2pYYY
OWNER_ID_2 = 6279578957  # @oc33t
PORT = int(os.getenv('PORT', 10000))  # Render.com использует порт 10000
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://your-render-app.onrender.com')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', 840))  # 14 минут
USE_POLLING = os.getenv('USE_POLLING', 'true').lower() == 'true'

# Путь к файлу с данными
STATS_FILE = "bot_stats.json"

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
bot_instance = None
flask_app = Flask(__name__)
bot_running = False
bot_lock = threading.Lock()

# Добавленная функция автосохранения
def auto_save_loop():
    while True:
        time.sleep(60)  # Сохраняем каждую минуту
        save_stats()
        logger.info("Автосохранение статистики")

# Функция для запуска бота в потоке
def bot_thread():
    global bot_running, bot_instance
    with bot_lock:
        if bot_running:
            return
        bot_running = True
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(bot_instance.initialize())
        if USE_POLLING:
            loop.run_until_complete(bot_instance.start_polling())
        else:
            # Режим вебхука
            pass
        loop.run_forever()
    except Exception as e:
        logger.error(f"Ошибка в потоке бота: {e}")
    finally:
        loop.close()
        bot_running = False

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
            ("channel", "Наш головний канал")
        ]
        await self.application.bot.set_my_commands(commands)
    
    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("order", self.order_command))
        self.application.add_handler(CommandHandler("question", self.question_command))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)
    
    # Заглушки для обработчиков
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Привет! Я бот SecureShop.")
    
    async def stop_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Диалог остановлен.")
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Статистика будет здесь.")
    
    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Помощь: /start, /order, /question")
    
    async def channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Наш канал: https://t.me/your_channel")
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer()
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Сообщение получено.")
    
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Ошибка: {context.error}")
    
    # Обработчики заказов и вопросов
    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("📺 YouTube", callback_data='category_youtube')],
            [InlineKeyboardButton("💬 ChatGPT", callback_data='category_chatgpt')],
            [InlineKeyboardButton("🎵 Spotify", callback_data='category_spotify')],
            [InlineKeyboardButton("🎮 Discord", callback_data='category_discord')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        await update.message.reply_text(
            "📦 Оберіть категорію товару:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def question_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        active_conversations[user_id] = {
            'type': 'question',
            'user_info': update.effective_user,
            'assigned_owner': None,
            'last_message': "Нове запитання"
        }
        bot_statistics['total_questions'] += 1
        save_stats()
        await update.message.reply_text("📝 Напишіть ваше запитання.")
    
    # Инициализация и запуск
    async def initialize(self):
        try:
            await self.application.bot.delete_webhook()
            logger.info("🗑️ Вебхук удален")
            await self.application.initialize()
            await self.set_commands_menu()
            self.initialized = True
            logger.info("✅ Telegram Application инициализирован")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации: {e}")
            raise
    
    async def start_polling(self):
        try:
            await self.application.start()
            await self.application.updater.start_polling()
            logger.info("✅ Polling запущен")
        except Conflict as e:
            logger.error(f"🚨 Конфликт: {e}")
            await asyncio.sleep(30)
            await self.start_polling()
        except RetryAfter as e:
            wait_time = e.retry_after + 5
            logger.warning(f"🕒 Ожидаем {wait_time} секунд...")
            await asyncio.sleep(wait_time)
            await self.start_polling()
        except Exception as e:
            logger.error(f"❌ Ошибка запуска polling: {e}")
            raise
    
    # Пинг-сервис для Render.com
    def start_ping_service(self):
        if not self.ping_running:
            self.ping_running = True
            threading.Thread(target=self.ping_loop, daemon=True).start()
            logger.info("🔄 Служба пинга запущена")
    
    def ping_loop(self):
        while self.ping_running:
            time.sleep(PING_INTERVAL)
            logger.info("🔄 Отправка пинга для поддержания активности...")
            if not USE_POLLING and WEBHOOK_URL:
                try:
                    import requests
                    response = requests.get(WEBHOOK_URL)
                    logger.info(f"Пинг отправлен, статус: {response.status_code}")
                except Exception as e:
                    logger.error(f"Ошибка при пинге: {e}")

def main():
    global bot_instance
    
    # Увеличиваем задержку для Render.com
    if os.environ.get('RENDER'):
        logger.info("⏳ Ожидаем 15 секунд для предотвращения конфликтов...")
        time.sleep(15)
    
    # Запускаем автосохранение
    auto_save_thread = threading.Thread(target=auto_save_loop)
    auto_save_thread.daemon = True
    auto_save_thread.start()
    
    # Инициализация бота
    bot_instance = TelegramBot()
    
    logger.info("🚀 Запуск SecureShop Telegram Bot...")
    logger.info(f"🔑 BOT_TOKEN: {BOT_TOKEN[:10]}...")
    logger.info(f"🌐 PORT: {PORT}")
    logger.info(f"📡 WEBHOOK_URL: {WEBHOOK_URL}")
    logger.info(f"⏰ PING_INTERVAL: {PING_INTERVAL} секунд")
    logger.info(f"🔄 РЕЖИМ: {'Polling' if USE_POLLING else 'Webhook'}")
    
    # Запуск бота в отдельном потоке
    bot_thread_instance = threading.Thread(target=bot_thread)
    bot_thread_instance.daemon = True
    bot_thread_instance.start()
    
    time.sleep(3)
    
    # Запуск пинг-сервиса
    bot_instance.start_ping_service()
    
    # Запуск Flask сервера
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
