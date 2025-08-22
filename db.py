# db.py
import logging
import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL

logger = logging.getLogger(__name__)

# --- Инициализация БД ---
# (Эта функция, вероятно, уже есть в вашем коде)
def init_db():
    # ... ваш код инициализации ...
    pass

# --- Функции для работы с пользователями ---
def get_total_users_count():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                count = cur.fetchone()[0]
                return count
    except Exception as e:
        logger.error(f"Ошибка получения количества пользователей: {e}")
        return 0

def get_all_users_data():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC")
                users = cur.fetchall()
                return users
    except Exception as e:
        logger.error(f"Ошибка получения данных всех пользователей: {e}")
        return []

def get_user_info(user_id):
     try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                user = cur.fetchone()
                return user
     except Exception as e:
        logger.error(f"Ошибка получения информации о пользователе {user_id}: {e}")
        return None

# --- Функции для работы с активными диалогами ---
def get_active_conversations_count():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM active_conversations")
                count = cur.fetchone()[0]
                return count
    except Exception as e:
        logger.error(f"Ошибка получения количества активных диалогов: {e}")
        return 0

def get_all_active_conversations():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Предполагается, что в таблице active_conversations есть поля: user_id, type, assigned_owner
                cur.execute("SELECT * FROM active_conversations")
                conversations = cur.fetchall()
                # Здесь может потребоваться дополнительная логика для получения user_info
                # Например, сделать отдельный запрос или join с таблицей users
                # Для простоты предположим, что user_info хранится как JSON или отдельно обрабатывается
                return conversations
    except Exception as e:
        logger.error(f"Ошибка получения всех активных диалогов: {e}")
        return []

def get_active_questions():
    # Можно использовать get_all_active_conversations и фильтровать по type='question'
    # Или сделать отдельный запрос
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM active_conversations WHERE type = 'question'")
                questions = cur.fetchall()
                return questions
    except Exception as e:
        logger.error(f"Ошибка получения активных вопросов: {e}")
        return []

def clear_all_active_conversations():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations")
                deleted_count = cur.rowcount
                # Также очищаем таблицу messages или удаляем сообщения по user_id из active_conversations
                # cur.execute("DELETE FROM messages") # Или более точечно
                return deleted_count
    except Exception as e:
        logger.error(f"Ошибка очистки активных диалогов: {e}")
        return 0

# --- Функции для работы с заказами ---
def get_total_orders_count():
    # Это может быть количество записей в таблице заказов или подсчет по типу в active_conversations
    # Предположим, что заказы тоже хранятся в active_conversations с типом 'order'
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM active_conversations WHERE type IN ('order', 'subscription_order', 'digital_order')")
                count = cur.fetchone()[0]
                return count
    except Exception as e:
        logger.error(f"Ошибка получения количества заказов: {e}")
        return 0

# --- Функции для работы с вопросами ---
def get_total_questions_count():
    # Предполагаем, что вопросы хранятся с типом 'question'
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM active_conversations WHERE type = 'question'")
                count = cur.fetchone()[0]
                return count
    except Exception as e:
        logger.error(f"Ошибка получения количества вопросов: {e}")
        return 0

# --- Функции для работы с сообщениями ---
def get_conversation_history(user_id, limit=50):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT * FROM messages
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (user_id, limit))
                # Используем DESC и reversed в commands.py
                history = cur.fetchall()
                return history
    except Exception as e:
        logger.error(f"Ошибка получения истории переписки для {user_id}: {e}")
        return []

# --- Функции для сохранения данных ---
# (save_user, save_active_conversation, save_message и т.д. должны быть здесь)
# ... (ваш код сохранения данных) ...
