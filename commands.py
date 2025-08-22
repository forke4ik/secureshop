# commands.py
import logging
import json
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import OWNER_ID_1, OWNER_ID_2
import db  # Предполагается, что у вас будет файл db.py для работы с БД

logger = logging.getLogger(__name__)

# --- Вспомогательные функции ---

def is_owner(user_id: int) -> bool:
    """Проверяет, является ли пользователь владельцем."""
    return user_id in [OWNER_ID_1, OWNER_ID_2]

# --- Команды для владельцев ---

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статистику (для владельцев)."""
    logger.info(f"📈 Вызов /stats пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        return # Игнорируем команду от не-владельцев

    try:
        # Получаем данные из БД
        total_users_db = db.get_total_users_count()
        total_orders_db = db.get_total_orders_count()
        total_questions_db = db.get_total_questions_count()
        active_chats_db = db.get_active_conversations_count()
        
        # Формируем сообщение
        stats_message = (
            f"📊 Статистика бота:\n"
            f"👤 Усього користувачів (БД): {total_users_db}\n"
            f"🛒 Усього замовлень (БД): {total_orders_db}\n"
            f"❓ Усього запитаннь (БД): {total_questions_db}\n"
            f"👥 Активних чатів (БД): {active_chats_db}"
        )
        await update.message.reply_text(stats_message)
    except Exception as e:
        logger.error(f"Ошибка получения статистики из БД: {e}")
        await update.message.reply_text("❌ Помилка при отриманні статистики з бази даних.")

async def export_users_json(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экспортирует список пользователей в JSON файл (для владельцев)."""
    logger.info(f"📁 Вызов /json пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
        return

    try:
        # Получаем данные пользователей из БД
        users_data_db = db.get_all_users_data()
        if not users_data_db:
             await update.message.reply_text("ℹ️ В базі даних немає користувачів для експорту.")
             return

        # Подготавливаем данные для JSON
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

        # Создаем JSON файл
        json_data = json.dumps(users_data, ensure_ascii=False, indent=2).encode('utf-8')
        file = io.BytesIO(json_data)
        file.seek(0)
        file.name = 'users_export.json'
        await update.message.reply_document(document=file, caption="📊 Експорт усіх користувачів у JSON")
    except Exception as e:
        logger.error(f"Ошибка экспорта пользователей в JSON: {e}")
        await update.message.reply_text("❌ Помилка при експорті користувачів у JSON.")

async def show_active_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список активных чатов (для владельцев)."""
    logger.info(f"👥 Вызов /chats пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        return

    try:
        # Получаем активные диалоги из БД
        active_chats = db.get_all_active_conversations()
        if not active_chats:
            await update.message.reply_text("ℹ️ Немає активних чатів.")
            return

        message = "👥 Активні чати:\n"
        for chat in active_chats:
            user_info = chat.get('user_info', {})
            user_name = user_info.get('first_name', 'Невідомий')
            username = user_info.get('username', '')
            user_display = f"@{username}" if username else user_name
            assigned_owner_id = chat.get('assigned_owner')
            owner_display = "Ніхто" if not assigned_owner_id else ("Власник 1" if assigned_owner_id == OWNER_ID_1 else "Власник 2")
            
            # Проверяем, является ли текущий владелец тем, кто ведет диалог
            is_current_owner = assigned_owner_id == owner_id
            
            message += f"▫️ {user_display} (ID: {chat['user_id']}) - {chat['type']} - Веде: {owner_display}\n"
            
            # Добавляем кнопку "Продолжить" только если диалог не ведет текущий владелец
            if not is_current_owner and assigned_owner_id is not None:
                 keyboard = [[InlineKeyboardButton("➡️ Перейти", callback_data=f'continue_chat_{chat["user_id"]}')]]
                 reply_markup = InlineKeyboardMarkup(keyboard)
                 # Отправляем отдельное сообщение с кнопкой
                 await update.message.reply_text(
                     f"Перейти до чату з {user_display} (ID: {chat['user_id']})?",
                     reply_markup=reply_markup
                 )
            elif assigned_owner_id is None:
                # Если диалог не назначен, показываем кнопку "Взять"
                keyboard = [[InlineKeyboardButton("✅ Взяти", callback_data=f'take_question_{chat["user_id"]}' if chat['type'] == 'question' else f'take_order_{chat["user_id"]}')]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                     f"Взяти чат з {user_display} (ID: {chat['user_id']})?",
                     reply_markup=reply_markup
                )
            # Если диалог ведет текущий владелец, просто показываем информацию без кнопки
            
        # Отправляем основное сообщение со списком
        # await update.message.reply_text(message.strip())
        # (Кнопки уже отправлены выше)
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения активных чатов: {e}")
        await update.message.reply_text("❌ Произошла ошибка при получении активных чатов.")

async def show_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список активных вопросов (для владельцев)."""
    logger.info(f"❓ Вызов /questions пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        return

    try:
        # Получаем активные вопросы из БД
        active_questions = db.get_active_questions()
        if not active_questions:
            await update.message.reply_text("ℹ️ Немає активних запитань.")
            return

        message = "❓ Активні запитання:\n"
        for question in active_questions:
            user_info = question.get('user_info', {})
            user_name = user_info.get('first_name', 'Невідомий')
            username = user_info.get('username', '')
            user_display = f"@{username}" if username else user_name
            assigned_owner_id = question.get('assigned_owner')
            owner_display = "Ніхто" if not assigned_owner_id else ("Власник 1" if assigned_owner_id == OWNER_ID_1 else "Власник 2")
            
            # Проверяем, является ли текущий владелец тем, кто ведет вопрос
            is_current_owner = assigned_owner_id == owner_id
            
            message += f"▫️ {user_display} (ID: {question['user_id']}) - Веде: {owner_display}\n"
            
            # Добавляем кнопку "Продолжить" или "Взять"
            if not is_current_owner and assigned_owner_id is not None:
                 keyboard = [[InlineKeyboardButton("➡️ Перейти", callback_data=f'continue_chat_{question["user_id"]}')]]
                 reply_markup = InlineKeyboardMarkup(keyboard)
                 await update.message.reply_text(
                     f"Перейти до питання від {user_display} (ID: {question['user_id']})?",
                     reply_markup=reply_markup
                 )
            elif assigned_owner_id is None:
                keyboard = [[InlineKeyboardButton("✅ Відповісти", callback_data=f'take_question_{question["user_id"]}')]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                     f"Відповісти на питання від {user_display} (ID: {question['user_id']})?",
                     reply_markup=reply_markup
                )
            # Если диалог ведет текущий владелец, просто показываем информацию без кнопки
            
        # await update.message.reply_text(message.strip())
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения активных вопросов: {e}")
        await update.message.reply_text("❌ Произошла ошибка при получении активных вопросов.")

async def show_conversation_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает историю переписки с пользователем (для владельцев)."""
    logger.info(f"📜 Вызов /history пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
        return

    if not context.args:
        await update.message.reply_text("ℹ️ Використання: /history <user_id>")
        return

    try:
        user_id = int(context.args[0])
        # Получаем историю из БД
        history = db.get_conversation_history(user_id)
        if not history:
            await update.message.reply_text(f"ℹ️ Немає історії повідомлень для користувача {user_id}.")
            return

        # Получаем информацию о пользователе
        user_info = db.get_user_info(user_id)
        if not user_info:
            user_info = {'first_name': 'Невідомий', 'username': 'N/A'}
            
        message = (
            f"📜 Історія переписки з користувачем:\n"
            f"👤 Ім'я: {user_info['first_name']}\n"
            f"🆔 ID: {user_id}\n"
            f"🪪 Username: @{user_info['username'] if user_info['username'] else 'не вказано'}\n\n"
            f"📨 Повідомлення:\n"
        )
        
        # Формируем историю (от старых к новым)
        for msg in reversed(history): # reversed чтобы последние сообщения были внизу
            sender = "👤 Клієнт" if msg['is_from_user'] else "👨‍💼 Магазин"
            timestamp = msg['created_at'].strftime('%d.%m.%Y %H:%M')
            message += f"{sender} [{timestamp}]:\n{msg['message']}\n\n"

        # Отправляем историю (возможно, потребуется разбить на части)
        if len(message) <= 4096:
            await update.message.reply_text(message)
        else:
            parts = [message[i:i+4096] for i in range(0, len(message), 4096)]
            for part in parts:
                await update.message.reply_text(part)
                
    except ValueError:
        await update.message.reply_text("❌ Невірний формат ID користувача. Має бути числом.")
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории переписки: {e}")
        await update.message.reply_text("❌ Произошла ошибка при получении истории переписки.")

async def clear_active_conversations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очищает все активные диалоги из БД (для владельцев)."""
    logger.info(f"🧹 Вызов /clear пользователем {update.effective_user.id}")
    owner_id = update.effective_user.id
    if not is_owner(owner_id):
        await update.message.reply_text("❌ У вас немає доступу до цієї команди.")
        return

    try:
        deleted_count = db.clear_all_active_conversations()
        # Также нужно очистить локальные переменные в main.py, если они используются
        # Это можно сделать через callback или напрямую в main.py
        await update.message.reply_text(f"✅ Усі активні діалоги очищено з бази даних. Видалено записів: {deleted_count}")
    except Exception as e:
        logger.error(f"❌ Ошибка очистки активных диалогов: {e}")
        await update.message.reply_text("❌ Произошла ошибка при очистке активных диалогов.")

# --- Команды общего назначения ---

# Если какие-либо не-владельские команды нужно перенести, они тоже будут здесь.
# Например, если бы у команды /start была сложная логика, её можно было бы вынести.
# Но в данном случае /start, /help, /order, /question, /channel, /stop, /pay, /dialog
# лучше оставить в main.py или соответствующих модулях для простоты.
