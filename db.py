# db.py
import logging
import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL

logger = logging.getLogger(__name__)

def init_db():
    """Инициализирует базу данных."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGINT PRIMARY KEY,
                        username VARCHAR(255),
                        first_name VARCHAR(255),
                        last_name VARCHAR(255),
                        language_code VARCHAR(10),
                        is_bot BOOLEAN,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS active_conversations (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        type VARCHAR(20),
                        assigned_owner BIGINT,
                        last_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(id),
                        message TEXT,
                        is_from_user BOOLEAN,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка инициализации базы данных: {e}")

def get_total_users_count():
    """Получает общее количество пользователей."""
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
    """Получает данные всех пользователей."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC")
                users = cur.fetchall()
                return users
    except Exception as e:
        logger.error(f"Ошибка получения данных всех пользователей: {e}")
        return []

def get_active_conversations():
    """Получает все активные диалоги."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM active_conversations")
                conversations = cur.fetchall()
                return conversations
    except Exception as e:
        logger.error(f"Ошибка получения активных диалогов: {e}")
        return []

def get_active_questions():
    """Получает все активные вопросы."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM active_conversations WHERE type = 'question'")
                questions = cur.fetchall()
                return questions
    except Exception as e:
        logger.error(f"Ошибка получения активных вопросов: {e}")
        return []

def get_conversation_history(user_id, limit=50):
    """Получает историю переписки с пользователем."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT * FROM messages
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (user_id, limit))
                history = cur.fetchall()
                return history
    except Exception as e:
        logger.error(f"Ошибка получения истории переписки для {user_id}: {e}")
        return []

def clear_all_active_conversations():
    """Очищает все активные диалоги."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_conversations")
                deleted_count = cur.rowcount
                return deleted_count
    except Exception as e:
        logger.error(f"Ошибка очистки активных диалогов: {e}")
        return 0

def save_new_question(user_id, user_info, message_text):
    """Сохраняет новое вопрос."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Сохраняем пользователя
                cur.execute("""
                    INSERT INTO users (id, username, first_name, last_name, language_code, is_bot, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        language_code = EXCLUDED.language_code,
                        updated_at = NOW()
                """, (user_info.id, user_info.username, user_info.first_name, user_info.last_name, user_info.language_code, user_info.is_bot))
                
                # Сохраняем активный диалог
                cur.execute("""
                    INSERT INTO active_conversations (user_id, type, assigned_owner, last_message, created_at, updated_at)
                    VALUES (%s, 'question', NULL, %s, NOW(), NOW())
                """, (user_id, message_text))
                
                # Сохраняем сообщение
                cur.execute("""
                    INSERT INTO messages (user_id, message, is_from_user, created_at)
                    VALUES (%s, %s, TRUE, NOW())
                """, (user_id, message_text))
                
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения нового вопроса от {user_id}: {e}")

def is_user_in_active_conversation(user_id):
    """Проверяет, находится ли пользователь в активном диалоге."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM active_conversations WHERE user_id = %s", (user_id,))
                return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"Ошибка проверки активного диалога для {user_id}: {e}")
        return False

def get_assigned_owner(user_id):
    """Получает ID владельца, который ведет диалог с пользователем."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT assigned_owner FROM active_conversations WHERE user_id = %s", (user_id,))
                result = cur.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Ошибка получения назначенного владельца для {user_id}: {e}")
        return None
