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
OWNER_ID_1 = 7106925462  # ID первого основателя @HiGki2pYYY
OWNER_ID_2 = 6279578957  # ID второго основателя @oc33t
PORT = int(os.getenv('PORT', 8443))  # Порт для webhook

# Словари для хранения данных
active_conversations = {}  # {client_id: {'type': 'order/question', 'user_info': user, 'assigned_owner': owner_id}}
owner_client_map = {}  # {owner_id: client_id} - текущий клиент для каждого основателя

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
        
        # Кнопки для клиентов
        if query.data in ['order', 'question']:
            # Инициализируем диалог с клиентом
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
        
        # Кнопка передачи чата другому основателю
        elif query.data.startswith('transfer_'):
            client_id = int(query.data.split('_')[1])
            current_owner = user_id
            
            # Определяем другого основателя
            other_owner = OWNER_ID_2 if current_owner == OWNER_ID_1 else OWNER_ID_1
            other_owner_name = "@oc33t" if other_owner == OWNER_ID_2 else "@HiGki2pYYY"
            
            # Обновляем назначение
            if client_id in active_conversations:
                active_conversations[client_id]['assigned_owner'] = other_owner
                owner_client_map[other_owner] = client_id
                if current_owner in owner_client_map:
                    del owner_client_map[current_owner]
                
                client_info = active_conversations[client_id]['user_info']
                
                # Уведомляем текущего основателя
                await query.edit_message_text(
                    f"✅ Чат с клиентом {client_info.first_name} передан {other_owner_name}"
                )
                
                # Уведомляем другого основателя
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
        message_text = update.message.text
        
        # Если сообщение от основателя - это ответ клиенту
        if user_id in [OWNER_ID_1, OWNER_ID_2]:
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
        
        # Выбираем основателя (приоритет первому, если не занят)
        assigned_owner = active_conversations[user_id]['assigned_owner']
        if not assigned_owner:
            # Логика выбора основателя (можно улучшить)
            assigned_owner = OWNER_ID_1  # По умолчанию первый
            active_conversations[user_id]['assigned_owner'] = assigned_owner
        
        owner_client_map[assigned_owner] = user_id
        
        # Формируем сообщение для основателя
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
        
        # Создаем кнопку для передачи другому основателю
        keyboard = [
            [InlineKeyboardButton("🔄 Передать другому основателю", callback_data=f'transfer_{user_id}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Отправляем основателю
        await context.bot.send_message(
            chat_id=assigned_owner,
            text=forward_message.strip(),
            reply_markup=reply_markup
        )
        
        # Подтверждаем клиенту
        await update.message.reply_text(
            "✅ Ваше сообщение передано основателю магазина. "
            "Ожидайте ответа в ближайшее время."
        )
    
    async def handle_owner_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка сообщений от основателя"""
        owner_id = update.effective_user.id
        
        # Проверяем, есть ли у основателя активный клиент
        if owner_id not in owner_client_map:
            owner_name = "@HiGki2pYYY" if owner_id == OWNER_ID_1 else "@oc33t"
            await update.message.reply_text(
                f"У вас нет активного клиента для ответа. ({owner_name})\n"
                f"Дождитесь нового сообщения от клиента."
            )
            return
        
        client_id = owner_client_map[owner_id]
        
        # Проверяем, что клиент еще в активных диалогах
        if client_id not in active_conversations:
            del owner_client_map[owner_id]
            await update.message.reply_text(
                "Диалог с клиентом завершен или не найден."
            )
            return
        
        # Отправляем ответ клиенту
        try:
            await context.bot.send_message(
                chat_id=client_id,
                text=f"📩 Ответ от магазина:\n\n{update.message.text}"
            )
            
            # Подтверждаем основателю
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
    
    def run_webhook(self):
        """Запуск бота с webhook для Render.com"""
        self.application.add_error_handler(self.error_handler)
        
        # Получаем URL для webhook
        webhook_url = os.getenv('WEBHOOK_URL', 'https://secureshop-3obw.onrender.com')
        
        logger.info(f"Запускаем webhook на {webhook_url}")
        
        # Настраиваем webhook
        self.application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}"
        )

def main():
    """Основная функция"""
    logger.info("Запуск бота...")
    logger.info(f"BOT_TOKEN: {BOT_TOKEN[:10]}...")
    logger.info(f"PORT: {PORT}")
    logger.info(f"Основатель 1: {OWNER_ID_1}")
    logger.info(f"Основатель 2: {OWNER_ID_2}")
    
    bot = TelegramBot()
    logger.info("Бот запущен")
    bot.run_webhook()

if __name__ == '__main__':
    main()
