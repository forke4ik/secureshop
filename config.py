# config.py
import os
from dotenv import load_dotenv
load_dotenv()
# Telegram Bot Token
BOT_TOKEN = os.getenv('BOT_TOKEN')
# Database URL
DATABASE_URL = os.getenv('DATABASE_URL')
# Owner IDs
OWNER_ID_1 = int(os.environ.get('OWNER_ID_1', 0)) # Замініть 0 на реальний ID, якщо потрібно за замовчуванням
OWNER_ID_2 = int(os.environ.get('OWNER_ID_2', 0)) # Замініть 0 на реальний ID, якщо потрібно за замовчуванням
SECURE_SUPPORT_ID = int(os.environ.get('SECURE_SUPPORT_ID', 0)) # Новий ID менеджера
OWNER_IDS = [id for id in [OWNER_ID_1, OWNER_ID_2] if id is not None]
# NOWPayments API
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY')
NOWPAYMENTS_IPN_SECRET = os.getenv('NOWPAYMENTS_IPN_SECRET')
# Payment settings
PAYMENT_CURRENCY = "UAH"  # Изменено с USD на UAH
# Card number for manual payment simulation
CARD_NUMBER = "5355 2800 4715 6045"
