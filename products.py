# products.py

# Словарь для подписок
SUBSCRIPTION_PRODUCTS = {
    'chatgpt_1': {'name': "ChatGPT Plus (1 місяць)", 'price': 650},
    'discord_basic_1': {'name': "Discord Nitro Basic (1 місяць)", 'price': 100},
    'discord_basic_12': {'name': "Discord Nitro Basic (12 місяців)", 'price': 900},
    'discord_full_1': {'name': "Discord Nitro Full (1 місяць)", 'price': 170},
    'discord_full_12': {'name': "Discord Nitro Full (12 місяців)", 'price': 1700},
    'duolingo_ind_1': {'name': "Duolingo Individual (1 місяць)", 'price': 200},
    'duolingo_ind_12': {'name': "Duolingo Individual (12 місяців)", 'price': 1500},
    'duolingo_fam_12': {'name': "Duolingo Family (12 місяців)", 'price': 380},
    'picsart_plus_1': {'name': "PicsArt Plus (1 місяць)", 'price': 130},
    'picsart_plus_12': {'name': "PicsArt Plus (12 місяців)", 'price': 800},
    'picsart_pro_1': {'name': "PicsArt Pro (1 місяць)", 'price': 180},
    'picsart_pro_12': {'name': "PicsArt Pro (12 місяців)", 'price': 1000},
    'canva_ind_1': {'name': "Canva Individual (1 місяць)", 'price': 350},
    'canva_ind_12': {'name': "Canva Individual (12 місяців)", 'price': 3000},
    'canva_fam_1': {'name': "Canva Family (1 місяць)", 'price': 850},
    'canva_fam_12': {'name': "Canva Family (12 місяців)", 'price': 7500},
    'netflix_1': {'name': "Netflix Premium (1 місяць)", 'price': 350},
    # Discord Украшення
    'discord_decor_bzn_6': {'name': "Discord Украшення Без Nitro (6$)", 'price': 180},
    'discord_decor_bzn_8': {'name': "Discord Украшення Без Nitro (8$)", 'price': 235},
    'discord_decor_bzn_10': {'name': "Discord Украшення Без Nitro (10$)", 'price': 295},
    'discord_decor_bzn_11': {'name': "Discord Украшення Без Nitro (11$)", 'price': 325},
    'discord_decor_bzn_12': {'name': "Discord Украшення Без Nitro (12$)", 'price': 355},
    'discord_decor_bzn_13': {'name': "Discord Украшення Без Nitro (13$)", 'price': 385},
    'discord_decor_bzn_15': {'name': "Discord Украшення Без Nitro (15$)", 'price': 440},
    'discord_decor_bzn_16': {'name': "Discord Украшення Без Nitro (16$)", 'price': 470},
    'discord_decor_bzn_18': {'name': "Discord Украшення Без Nitro (18$)", 'price': 530},
    'discord_decor_bzn_24': {'name': "Discord Украшення Без Nitro (24$)", 'price': 705},
    'discord_decor_bzn_29': {'name': "Discord Украшення Без Nitro (29$)", 'price': 855},
    'discord_decor_zn_5': {'name': "Discord Украшення З Nitro (5$)", 'price': 145},
    'discord_decor_zn_7': {'name': "Discord Украшення З Nitro (7$)", 'price': 205},
    'discord_decor_zn_8_5': {'name': "Discord Украшення З Nitro (8.5$)", 'price': 250},
    'discord_decor_zn_9': {'name': "Discord Украшення З Nitro (9$)", 'price': 265},
    'discord_decor_zn_14': {'name': "Discord Украшення З Nitro (14$)", 'price': 410},
    'discord_decor_zn_22': {'name': "Discord Украшення З Nitro (22$)", 'price': 650},
}

# --- НОВІ ЦИФРОВІ ТОВАРИ ---
DIGITAL_PRODUCTS = {
    # Roblox
    'roblox_10': {'name': "Roblox Gift Card (10$)", 'price': 459},
    'roblox_25': {'name': "Roblox Gift Card (25$)", 'price': 1149},
    'roblox_50': {'name': "Roblox Gift Card (50$)", 'price': 2299},
    # PSN TRY
    'psn_tru_250': {'name': "PSN Gift Card (250 TRU)", 'price': 349},
    'psn_tru_500': {'name': "PSN Gift Card (500 TRU)", 'price': 699},
    'psn_tru_750': {'name': "PSN Gift Card (750 TRU)", 'price': 1049},
    'psn_tru_1000': {'name': "PSN Gift Card (1000 TRU)", 'price': 1350},
    'psn_tru_1500': {'name': "PSN Gift Card (1500 TRU)", 'price': 2000},
    'psn_tru_2000': {'name': "PSN Gift Card (2000 TRU)", 'price': 2700},
    'psn_tru_2500': {'name': "PSN Gift Card (2500 TRU)", 'price': 3400},
    'psn_tru_3000': {'name': "PSN Gift Card (3000 TRU)", 'price': 4100},
    'psn_tru_4000': {'name': "PSN Gift Card (4000 TRU)", 'price': 5300},
    'psn_tru_5000': {'name': "PSN Gift Card (5000 TRU)", 'price': 6600},
    # PSN INR
    'psn_inr_1000': {'name': "PSN Gift Card (1000 INR)", 'price': 725},
    'psn_inr_2000': {'name': "PSN Gift Card (2000 INR)", 'price': 1400},
    'psn_inr_3000': {'name': "PSN Gift Card (3000 INR)", 'price': 2100},
    'psn_inr_4000': {'name': "PSN Gift Card (4000 INR)", 'price': 2750},
    'psn_inr_5000': {'name': "PSN Gift Card (5000 INR)", 'price': 3400},
}
# --- КІНЕЦЬ НОВИХ ЦИФРОВИХ ТОВАРІВ ---

# Объединяем все продукты для удобства поиска по ID из команды
ALL_PRODUCTS = {**SUBSCRIPTION_PRODUCTS, **DIGITAL_PRODUCTS}

# Словарь для навигации назад (пример, можно расширить)
SUBSCRIPTION_BACK_MAP = {
    # ... (можна залишити як є або розширити)
}

# Валюты для криптооплаты
AVAILABLE_CURRENCIES = {
    "Bitcoin (BTC)": "btc",
    "Ethereum (ETH)": "eth",
    "Tether (USDT)": "usdt",
    "Litecoin (LTC)": "ltc",
    "Bitcoin Cash (BCH)": "bch",
    "TRON (TRX)": "trx",
    "Dash (DASH)": "dash",
    "USDC": "usdc",
    "Dogecoin (DOGE)": "doge",
    "Toncoin (TON)": "ton",
    "Binance Coin (BNB)": "bnb",
    "Solana (SOL)": "sol",
    "XRP (XRP)": "xrp"
}

# API ключ для NOWPayments
NOWPAYMENTS_API_KEY = "YOUR_NOWPAYMENTS_API_KEY_HERE" # Замініть на ваш справжній ключ
