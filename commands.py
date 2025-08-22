# commands.py
import logging
import json
import io
from telegram import Update
from telegram.ext import ContextTypes
from config import OWNER_ID_1, OWNER_ID_2
import db

logger = logging.getLogger(__name__)

def is_owner(user_id: int) -> bool:
    return user_id in [OWNER_ID_1, OWNER_ID_2]

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"📈 Вызов /stats пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        return

    try:
        total_users_db = db.get_total_users_count()
        total_orders_db = db.get_total_orders_count() # Предполагается, что это общее количество заказов
        total_questions_db = db.get_total_questions_count() # Предполагается, что это общее количество вопросов
        # active_chats_db = db.get_active_conversations_count() # Удалено
        
        stats_message = (
            f"📊 Статистика бота:\n"
            f"👤 Усього користувачів (БД): {total_users_db}\n"
            f"🛒 Усього замовлень (БД): {total_orders_db}\n"
            f"❓ Усього запитаннь (БД): {total_questions_db}\n"
            # f"👥 Активних чатів (БД): {active_chats_db}" # Удалено
        )
        await update.message.reply_text(stats_message)
    except Exception as e:
        logger.error(f"Ошибка получения статистики из БД: {e}")
        await update.message.reply_text("❌ Помилка при отриманні статистики з бази даних.")

async def export_users_json(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"📁 Вызов /json пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
        return

    try:
        users_data_db = db.get_all_users_data() # Предполагается, что такая функция есть
        if not users_data_db:
             await update.message.reply_text("ℹ️ В базі даних немає користувачів для експорту.")
             return

        users_data = []
        for user in users_data_db:
            users_data.append({
                'id': user['id'],
                'username': user['username'],
                'first_name': user['first_name'],
                'last_name': user['last_name'],
                'language_code': user['language_code'],
                'is_bot': user['is_bot'],
                'created_at': user['created_at'].isoformat() if user['created_at'] else None,
                'updated_at': user['updated_at'].isoformat() if user['updated_at'] else None
            })

        json_data = json.dumps(users_data, ensure_ascii=False, indent=2).encode('utf-8')
        file = io.BytesIO(json_data)
        file.seek(0)
        file.name = 'users_export.json'
        await update.message.reply_document(document=file, caption="📊 Експорт усіх користувачів у JSON")
    except Exception as e:
        logger.error(f"Ошибка экспорта пользователей в JSON: {e}")
        await update.message.reply_text("❌ Помилка при експорті користувачів у JSON.")
