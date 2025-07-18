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
BOT_TOKEN = os.getenv('BOT_TOKEN', '8181378677:AAFullvwrNhPJMi_HxgC75qSEKWdKOtCpbw')
OWNER_ID_1 = 7106925462  # @HiGki2pYYY
OWNER_ID_2 = 6279578957  # @oc33t
PORT = int(os.getenv('PORT', 8443))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://secureshop-3obw.onrender.com')
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
            ("channel", "Наш головний канал")
        ]
        await self.application.bot.set_my_commands(commands)
    
    def setup_handlers(self):
        """Настройка обработчиков команд и сообщений"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("stop", self.stop_conversation))
        self.application.add_handler(CommandHandler("stats", self.show_stats))
        self.application.add_handler(CommandHandler("help", self.show_help))
        self.application.add_handler(CommandHandler("channel", self.channel_command))
        self.application.add_handler(CommandHandler("order", self.order_command))
        # Добавлен обработчик команды /question
        self.application.add_handler(CommandHandler("question", self.question_command))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)
    
    async def initialize(self):
        """Асинхронная инициализация приложения"""
        try:
            # Принудительно удаляем вебхук перед инициализацией
            try:
                await self.application.bot.delete_webhook()
                logger.info("🗑️ Вебхук удален перед инициализацией")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось удалить вебхук: {e}")
                
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
            if self.application.updater and self.application.updater.running:
                logger.warning("🛑 Бот уже запущен! Пропускаем повторный запуск")
                return
            
            logger.info("🔄 Запуск polling режима...")
            
            # Убедимся, что вебхук удален
            try:
                await self.application.bot.delete_webhook()
                logger.info("🗑️ Вебхук удален перед запуском polling")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка удаления вебхука: {e}")
            
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
            logger.warning("🕒 Ожидаем 30 секунд перед повторной попыткой...")
            await asyncio.sleep(30)
            await self.start_polling()
        except RetryAfter as e:
            logger.warning(f"🚦 Rate limit: {e}")
            wait_time = e.retry_after + 5
            logger.warning(f"🕒 Ожидаем {wait_time} секунд...")
            await asyncio.sleep(wait_time)
            await self.start_polling()
        except Exception as e:
            logger.error(f"❌ Ошибка запуска polling: {e}")
            raise
    
    # ... (остальные методы класса без изменений, кроме добавленных ниже)

    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /order"""
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
        """Обработчик команды /question"""
        user_id = update.effective_user.id
        
        # Сохраняем вопрос
        active_conversations[user_id] = {
            'type': 'question',
            'user_info': update.effective_user,
            'assigned_owner': None,
            'last_message': "Нове запитання"
        }
        
        # Обновляем статистику
        bot_statistics['total_questions'] += 1
        save_stats()
        
        await update.message.reply_text(
            "📝 Напишіть ваше запитання. Я передам його засновнику магазину."
        )
    
    # ... (остальные методы класса без изменений)

# ... (остальной код без изменений до функции main)

def main():
    # Увеличиваем задержку для Render.com
    if os.environ.get('RENDER'):
        logger.info("⏳ Ожидаем 15 секунд для предотвращения конфликтов...")
        time.sleep(15)  # Увеличено до 15 секунд
    
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
