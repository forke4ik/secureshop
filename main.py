import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN', '8181378677:AAFullvwrNhPJMi_HxgC75qSEKWdKOtCpbw')  # Токен бота
OWNER_ID = 7106925462  # ID основателя @HiGki2pYYY
PORT = int(os.getenv('PORT', 8443))  # Порт для webhook

# Словарь для хранения активных диалогов
active_conversations = {}

class TelegramBot:
    def __init__(self):
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
    
    def setup_handlers(self):
        """Настройка обработчиков команд и сообщений"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        
        # Проверяем, является ли пользователь основателем
        if user.id == OWNER_ID:
            await update.message.reply_text(
                f"Добро пожаловать, {user.first_name}! Вы вошли как основатель магазина."
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
            # Инициализируем диалог с клиентом
            active_conversations[user_id] = {
                'type': 'order' if query.data == 'order' else 'question',
                'user_info': query.from_user
            }
            
            action_text = "оформить заказ" if query.data == 'order' else "задать вопрос"
            
            await query.edit_message_text(
                f"Отлично! Напишите ваше сообщение, чтобы {action_text}. "
                f"Я передам его основателю магазина."
            )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user_id = update.effective_user.id
        message_text = update.message.text
        
        # Если сообщение от основателя - это ответ клиенту
        if user_id == OWNER_ID:
            await self.handle_owner_message(update, context)
            return
        
        # Если сообщение от клиента
        if user_id in active_conversations:
            await self.forward_to_owner(update, context)
        else:
            # Если диалог не активен, предлагаем начать
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
        
        # Формируем сообщение для основателя
        type_emoji = "🛒" if conversation_type == 'order' else "❓"
        type_text = "ЗАКАЗ" if conversation_type == 'order' else "ВОПРОС"
        
        forward_message = f"""
{type_emoji} {type_text} от клиента:

👤 Пользователь: {user_info.first_name}
📱 Username: @{user_info.username if user_info.username else 'не указан'}
🆔 ID: {user_info.id}

💬 Сообщение:
{update.message.text}

---
Для ответа просто напишите сообщение в этот чат.
        """
        
        # Отправляем основателю
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=forward_message.strip()
        )
        
        # Подтверждаем клиенту
        await update.message.reply_text(
            "✅ Ваше сообщение передано основателю магазина. "
            "Ожидайте ответа в ближайшее время."
        )
    
    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка сообщений от основателя (пока что заглушка)"""
        # TODO: Реализовать логику ответа клиенту
        # Пока что просто уведомляем, что функция в разработке
        await update.message.reply_text(
            "Функция ответа клиентам пока в разработке. "
            "Будет добавлена в следующей версии."
        )
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.warning(f'Update {update} caused error {context.error}')
    
    def run_webhook(self):
        """Запуск бота с webhook для Render.com"""
        self.application.add_error_handler(self.error_handler)
        
        # Получаем URL для webhook
        webhook_url = os.getenv('WEBHOOK_URL', 'https://secureshop-3obw.onrender.com')  # Ваш URL
        
        if not webhook_url:
            logger.error("WEBHOOK_URL не задан в переменных окружения")
            return
        
        # Настраиваем webhook
        self.application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}"
        )

def main():
    """Основная функция"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан в переменных окружения")
        return
    
    bot = TelegramBot()
    logger.info("Бот запущен")
    bot.run_webhook()

if __name__ == '__main__':
    main()
