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
    'netflix_1': {'name': "Netflix Premium (1 місяць)", 'price': 350}
}

# Словарь для цифровых товаров
DIGITAL_PRODUCTS = {
    'discord_decor_bzn_6': {'name': "Discord Прикраси (Без Nitro) 6$", 'price': 180},
    'discord_decor_bzn_8': {'name': "Discord Прикраси (Без Nitro) 8$", 'price': 235},
    'discord_decor_bzn_10': {'name': "Discord Прикраси (Без Nitro) 10$", 'price': 295},
    'discord_decor_bzn_11': {'name': "Discord Прикраси (Без Nitro) 11$", 'price': 325},
    'discord_decor_bzn_12': {'name': "Discord Прикраси (Без Nitro) 12$", 'price': 355},
    'discord_decor_bzn_13': {'name': "Discord Прикраси (Без Nitro) 13$", 'price': 385},
    'discord_decor_bzn_15': {'name': "Discord Прикраси (Без Nitro) 15$", 'price': 440},
    'discord_decor_bzn_16': {'name': "Discord Прикраси (Без Nitro) 16$", 'price': 470},
    'discord_decor_bzn_18': {'name': "Discord Прикраси (Без Nitro) 18$", 'price': 530},
    'discord_decor_bzn_24': {'name': "Discord Прикраси (Без Nitro) 24$", 'price': 705},
    'discord_decor_bzn_29': {'name': "Discord Прикраси (Без Nitro) 29$", 'price': 855},
    'discord_decor_zn_5': {'name': "Discord Прикраси (З Nitro) 5$", 'price': 145},
    'discord_decor_zn_7': {'name': "Discord Прикраси (З Nitro) 7$", 'price': 205},
    'discord_decor_zn_8_5': {'name': "Discord Прикраси (З Nitro) 8.5$", 'price': 250},
    'discord_decor_zn_9': {'name': "Discord Прикраси (З Nitro) 9$", 'price': 265},
    'discord_decor_zn_14': {'name': "Discord Прикраси (З Nitro) 14$", 'price': 410},
    'discord_decor_zn_22': {'name': "Discord Прикраси (З Nitro) 22$", 'price': 650},
}

# Словарь для определения кнопки "Назад" для подписок
SUBSCRIPTION_BACK_MAP = {
    'chatgpt_1': 'category_chatgpt',
    'discord_basic_1': 'discord_basic',
    'discord_basic_12': 'discord_basic',
    'discord_full_1': 'discord_full',
    'discord_full_12': 'discord_full',
    'duolingo_ind_1': 'duolingo_individual',
    'duolingo_ind_12': 'duolingo_individual',
    'duolingo_fam_12': 'duolingo_family',
    'picsart_plus_1': 'picsart_plus',
    'picsart_plus_12': 'picsart_plus',
    'picsart_pro_1': 'picsart_pro',
    'picsart_pro_12': 'picsart_pro',
    'canva_ind_1': 'canva_individual',
    'canva_ind_12': 'canva_individual',
    'canva_fam_1': 'canva_family',
    'canva_fam_12': 'canva_family',
    'netflix_1': 'category_netflix'
}

# Словарь для определения кнопки "Назад" для цифровых товаров
DIGITAL_BACK_MAP = {
    'discord_decor_bzn_6': 'discord_decor_without_nitro',
    'discord_decor_bzn_8': 'discord_decor_without_nitro',
    'discord_decor_bzn_10': 'discord_decor_without_nitro',
    'discord_decor_bzn_11': 'discord_decor_without_nitro',
    'discord_decor_bzn_12': 'discord_decor_without_nitro',
    'discord_decor_bzn_13': 'discord_decor_without_nitro',
    'discord_decor_bzn_15': 'discord_decor_without_nitro',
    'discord_decor_bzn_16': 'discord_decor_without_nitro',
    'discord_decor_bzn_18': 'discord_decor_without_nitro',
    'discord_decor_bzn_24': 'discord_decor_without_nitro',
    'discord_decor_bzn_29': 'discord_decor_without_nitro',
    'discord_decor_zn_5': 'discord_decor_with_nitro',
    'discord_decor_zn_7': 'discord_decor_with_nitro',
    'discord_decor_zn_8_5': 'discord_decor_with_nitro',
    'discord_decor_zn_9': 'discord_decor_with_nitro',
    'discord_decor_zn_14': 'discord_decor_with_nitro',
    'discord_decor_zn_22': 'discord_decor_with_nitro',
}

# Словарь для сопоставления аббревиатур из команды /pay с полными названиями (сервисы)
SERVICE_MAP = {
    "Cha": "ChatGPT", "Dis": "Discord", "Duo": "Duolingo",
    "Pic": "PicsArt", "Can": "Canva", "Net": "Netflix",
    "DisU": "Discord Прикраси"
}

# Словарь для сопоставления аббревиатур из команды /pay с полными названиями (планы)
PLAN_MAP = {
    "Bas": "Basic", "Ful": "Full", "Ind": "Individual",
    "Fam": "Family", "Plu": "Plus", "Pro": "Pro",
    "Pre": "Premium", "BzN": "Без Nitro", "ZN": "З Nitro"
}
