# products_config.py - Конфігурація продуктів

# Подписки
SUBSCRIPTIONS = {
    'chatgpt': {
        'name': 'ChatGPT',
        'plans': {
            'basic': {
                'name': 'Basic',
                'description': 'Доступ к базовым функциям ChatGPT',
                'options': [
                    {"period": "1 місяць", "price": 100},
                    {"period": "3 місяці", "price": 280},
                    {"period": "6 місяців", "price": 520},
                    {"period": "12 місяців", "price": 950}
                ]
            },
            'full': {
                'name': 'Full',
                'description': 'Полный доступ ко всем возможностям ChatGPT',
                'options': [
                    {"period": "1 місяць", "price": 150},
                    {"period": "3 місяці", "price": 400},
                    {"period": "6 місяців", "price": 750},
                    {"period": "12 місяців", "price": 1350}
                ]
            }
        }
    },
    'discord': {
        'name': 'Discord',
        'plans': {
            'basic': {
                'name': 'Nitro Basic',
                'description': 'Базові переваги Discord Nitro',
                'options': [
                    {"period": "1 місяць", "price": 100},
                    {"period": "12 місяців", "price": 900}
                ]
            },
            'full': {
                'name': 'Nitro Full',
                'description': 'Повний пакет переваг Discord Nitro',
                'options': [
                    {"period": "1 місяць", "price": 170},
                    {"period": "12 місяців", "price": 1700}
                ]
            }
        }
    },
    'duolingo': {
        'name': 'Duolingo',
        'plans': {
            'ind': {
                'name': 'Individual',
                'description': 'Преміум підписка на один акаунт',
                'options': [
                    {"period": "1 місяць", "price": 200},
                    {"period": "12 місяців", "price": 1500}
                ]
            },
            'fam': {
                'name': 'Family',
                'description': 'Сімейна підписка',
                'options': [
                    {"period": "12 місяців", "price": 2000}
                ]
            }
        }
    },
    'picsart': {
        'name': 'PicsArt',
        'plans': {
            'plus': {
                'name': 'Plus',
                'description': 'Розширені інструменти',
                'options': [
                    {"period": "1 місяць", "price": 130},
                    {"period": "12 місяців", "price": 800}
                ]
            },
            'pro': {
                'name': 'Pro',
                'description': 'Професійні можливості',
                'options': [
                    {"period": "1 місяць", "price": 180},
                    {"period": "12 місяців", "price": 1100}
                ]
            }
        }
    },
    'canva': {
        'name': 'Canva',
        'plans': {
            'ind': {
                'name': 'Individual',
                'description': 'Профессиональные инструменты для одного пользователя',
                'options': [
                    {"period": "1 місяць", "price": 150},
                    {"period": "12 місяців", "price": 1000}
                ]
            },
            'fam': {
                'name': 'Family',
                'description': 'Доступ для всей семьи',
                'options': [
                    {"period": "1 місяць", "price": 200},
                    {"period": "12 місяців", "price": 1300}
                ]
            }
        }
    },
    'spotify': {
        'name': 'Spotify',
        'plans': {
            'ind': {
                'name': 'Individual',
                'description': 'Преміум підписка на один акаунт',
                'options': [
                    {"period": "1 місяць", "price": 130},
                    {"period": "12 місяців", "price": 900}
                ]
            },
            'fam': {
                'name': 'Family',
                'description': 'Сімейна підписка',
                'options': [
                    {"period": "1 місяць", "price": 180},
                    {"period": "12 місяців", "price": 1300}
                ]
            }
        }
    },
    'netflix': {
        'name': 'Netflix',
        'plans': {
            'stand': {
                'name': 'Standard',
                'description': 'Доступ на 2 екрани у Full HD якості',
                'options': [
                    {"period": "1 місяць", "price": 250},
                    {"period": "3 місяці", "price": 700},
                    {"period": "6 місяців", "price": 1300},
                    {"period": "12 місяців", "price": 2400}
                ]
            },
            'pre': {
                'name': 'Premium',
                'description': 'Доступ на 4 екрани у Ultra HD (4K) якості',
                'options': [
                    {"period": "1 місяць", "price": 320},
                    {"period": "3 місяці", "price": 900},
                    {"period": "6 місяців", "price": 1650},
                    {"period": "12 місяців", "price": 3100}
                ]
            }
        }
    }
}

# Цифровые товары (Discord Украшення и PSN Gift Cards)
DIGITAL_PRODUCTS = {
    # Discord Украшення Без Nitro
    'discord_decor_bzn_6': {
        'name': "Discord Украшення Без Nitro (6$)",
        'price': 175,
        'category': 'bzn'
    },
    'discord_decor_bzn_8': {
        'name': "Discord Украшення Без Nitro (8$)",
        'price': 235,
        'category': 'bzn'
    },
    'discord_decor_bzn_10': {
        'name': "Discord Украшення Без Nitro (10$)",
        'price': 295,
        'category': 'bzn'
    },
    'discord_decor_bzn_11': {
        'name': "Discord Украшення Без Nitro (11$)",
        'price': 325,
        'category': 'bzn'
    },
    'discord_decor_bzn_12': {
        'name': "Discord Украшення Без Nitro (12$)",
        'price': 355,
        'category': 'bzn'
    },
    'discord_decor_bzn_13': {
        'name': "Discord Украшення Без Nitro (13$)",
        'price': 385,
        'category': 'bzn'
    },
    'discord_decor_bzn_15': {
        'name': "Discord Украшення Без Nitro (15$)",
        'price': 445,
        'category': 'bzn'
    },
    'discord_decor_bzn_16': {
        'name': "Discord Украшення Без Nitro (16$)",
        'price': 475,
        'category': 'bzn'
    },
    'discord_decor_bzn_18': {
        'name': "Discord Украшення Без Nitro (18$)",
        'price': 535,
        'category': 'bzn'
    },
    'discord_decor_bzn_24': {
        'name': "Discord Украшення Без Nitro (24$)",
        'price': 715,
        'category': 'bzn'
    },
    'discord_decor_bzn_29': {
        'name': "Discord Украшення Без Nitro (29$)",
        'price': 865,
        'category': 'bzn'
    },
    
    # Discord Украшення З Nitro
    'discord_decor_zn_5': {
        'name': "Discord Украшення З Nitro (5$)",
        'price': 145,
        'category': 'zn'
    },
    'discord_decor_zn_7': {
        'name': "Discord Украшення З Nitro (7$)",
        'price': 205,
        'category': 'zn'
    },
    'discord_decor_zn_8_5': {
        'name': "Discord Украшення З Nitro (8.5$)",
        'price': 250,
        'category': 'zn'
    },
    'discord_decor_zn_9': {
        'name': "Discord Украшення З Nitro (9$)",
        'price': 265,
        'category': 'zn'
    },
    'discord_decor_zn_14': {
        'name': "Discord Украшення З Nitro (14$)",
        'price': 410,
        'category': 'zn'
    },
    'discord_decor_zn_22': {
        'name': "Discord Украшення З Nitro (22$)",
        'price': 650,
        'category': 'zn'
    },
    
    # PSN Gift Cards
    'psn_5': {
        'name': "PSN Gift Card (5$)",
        'price': 145,
        'category': 'psn'
    },
    'psn_10': {
        'name': "PSN Gift Card (10$)",
        'price': 295,
        'category': 'psn'
    },
    'psn_15': {
        'name': "PSN Gift Card (15$)",
        'price': 445,
        'category': 'psn'
    },
    'psn_20': {
        'name': "PSN Gift Card (20$)",
        'price': 595,
        'category': 'psn'
    },
    'psn_25': {
        'name': "PSN Gift Card (25$)",
        'price': 745,
        'category': 'psn'
    },
    'psn_30': {
        'name': "PSN Gift Card (30$)",
        'price': 895,
        'category': 'psn'
    },
    'psn_50': {
        'name': "PSN Gift Card (50$)",
        'price': 1495,
        'category': 'psn'
    },
    'psn_100': {
        'name': "PSN Gift Card (100$)",
        'price': 2995,
        'category': 'psn'
    }
}

# Маппинг для кнопок цифровых товаров
DIGITAL_PRODUCT_MAP = {
    # Discord Украшення Без Nitro
    'digital_discord_decor_bzn_6': 'discord_decor_bzn_6',
    'digital_discord_decor_bzn_8': 'discord_decor_bzn_8',
    'digital_discord_decor_bzn_10': 'discord_decor_bzn_10',
    'digital_discord_decor_bzn_11': 'discord_decor_bzn_11',
    'digital_discord_decor_bzn_12': 'discord_decor_bzn_12',
    'digital_discord_decor_bzn_13': 'discord_decor_bzn_13',
    'digital_discord_decor_bzn_15': 'discord_decor_bzn_15',
    'digital_discord_decor_bzn_16': 'discord_decor_bzn_16',
    'digital_discord_decor_bzn_18': 'discord_decor_bzn_18',
    'digital_discord_decor_bzn_24': 'discord_decor_bzn_24',
    'digital_discord_decor_bzn_29': 'discord_decor_bzn_29',
    
    # Discord Украшення З Nitro
    'digital_discord_decor_zn_5': 'discord_decor_zn_5',
    'digital_discord_decor_zn_7': 'discord_decor_zn_7',
    'digital_discord_decor_zn_8_5': 'discord_decor_zn_8_5',
    'digital_discord_decor_zn_9': 'discord_decor_zn_9',
    'digital_discord_decor_zn_14': 'discord_decor_zn_14',
    'digital_discord_decor_zn_22': 'discord_decor_zn_22',
    
    # PSN Gift Cards
    'digital_psn_5': 'psn_5',
    'digital_psn_10': 'psn_10',
    'digital_psn_15': 'psn_15',
    'digital_psn_20': 'psn_20',
    'digital_psn_25': 'psn_25',
    'digital_psn_30': 'psn_30',
    'digital_psn_50': 'psn_50',
    'digital_psn_100': 'psn_100',
}
