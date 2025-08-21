# products_config.py - Конфигурация продуктов

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
                    {"period": "12 місяців", "price": 1400}
                ]
            }
        }
    },
    'discord': {
        'name': 'Discord',
        'plans': {
            'ind': {
                'name': 'Individual',
                'description': 'Підписка на один акаунт',
                'options': [
                    {"period": "1 місяць", "price": 80},
                    {"period": "3 місяці", "price": 220},
                    {"period": "6 місяців", "price": 400},
                    {"period": "12 місяців", "price": 750}
                ]
            },
            'fam': {
                'name': 'Family',
                'description': 'Підписка на вас та ще 5 осіб',
                'options': [
                    {"period": "1 місяць", "price": 120},
                    {"period": "3 місяці", "price": 320},
                    {"period": "6 місяців", "price": 600},
                    {"period": "12 місяців", "price": 1100}
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
                    {"period": "1 місяць", "price": 70},
                    {"period": "12 місяців", "price": 450}
                ]
            },
            'fam': {
                'name': 'Family',
                'description': 'Сімейна підписка',
                'options': [
                    {"period": "1 місяць", "price": 100},
                    {"period": "12 місяців", "price": 650}
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
                    {"period": "12 місяців", "price": 1000}
                ]
            }
        }
    },
    'canva': {
        'name': 'Canva',
        'plans': {
            'pro': {
                'name': 'Pro',
                'description': 'Профессиональные инструменты дизайна',
                'options': [
                    {"period": "1 місяць", "price": 150},
                    {"period": "12 місяців", "price": 900}
                ]
            }
        }
    },
    'netflix': {
        'name': 'Netflix',
        'plans': {
            'bas': {
                'name': 'Basic',
                'description': 'Доступ на 1 екран у HD якості',
                'options': [
                    {"period": "1 місяць", "price": 180},
                    {"period": "3 місяці", "price": 500},
                    {"period": "6 місяців", "price": 950},
                    {"period": "12 місяців", "price": 1800}
                ]
            },
            'std': {
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
    'discord_bzn_6': {'name': 'Discord Украшення (Без Nitro) - 6€', 'price': 220, 'category': 'bzn'},
    'discord_bzn_8': {'name': 'Discord Украшення (Без Nitro) - 8€', 'price': 290, 'category': 'bzn'},
    'discord_bzn_10': {'name': 'Discord Украшення (Без Nitro) - 10€', 'price': 360, 'category': 'bzn'},
    'discord_bzn_11': {'name': 'Discord Украшення (Без Nitro) - 11€', 'price': 400, 'category': 'bzn'},
    'discord_bzn_12': {'name': 'Discord Украшення (Без Nitro) - 12€', 'price': 430, 'category': 'bzn'},
    'discord_bzn_13': {'name': 'Discord Украшення (Без Nitro) - 13€', 'price': 470, 'category': 'bzn'},
    'discord_bzn_15': {'name': 'Discord Украшення (Без Nitro) - 15€', 'price': 540, 'category': 'bzn'},
    'discord_bzn_16': {'name': 'Discord Украшення (Без Nitro) - 16€', 'price': 580, 'category': 'bzn'},
    'discord_bzn_18': {'name': 'Discord Украшення (Без Nitro) - 18€', 'price': 650, 'category': 'bzn'},
    'discord_bzn_24': {'name': 'Discord Украшення (Без Nitro) - 24€', 'price': 870, 'category': 'bzn'},
    'discord_bzn_29': {'name': 'Discord Украшення (Без Nitro) - 29€', 'price': 1050, 'category': 'bzn'},
    
    # Discord Украшення З Nitro
    'discord_zn_5': {'name': 'Discord Украшення (З Nitro) - 5€', 'price': 185, 'category': 'zn'},
    'discord_zn_7': {'name': 'Discord Украшення (З Nitro) - 7€', 'price': 260, 'category': 'zn'},
    'discord_zn_8_5': {'name': 'Discord Украшення (З Nitro) - 8.5€', 'price': 310, 'category': 'zn'},
    'discord_zn_9': {'name': 'Discord Украшення (З Nitro) - 9€', 'price': 330, 'category': 'zn'},
    'discord_zn_14': {'name': 'Discord Украшення (З Nitro) - 14€', 'price': 515, 'category': 'zn'},
    'discord_zn_22': {'name': 'Discord Украшення (З Nitro) - 22€', 'price': 810, 'category': 'zn'},
    
    # PSN Gift Cards
    'psn_inr_1000': {'name': 'PSN Gift Card 1000 INR', 'price': 725, 'category': 'psn'},
    'psn_inr_2000': {'name': 'PSN Gift Card 2000 INR', 'price': 1400, 'category': 'psn'},
    'psn_inr_3000': {'name': 'PSN Gift Card 3000 INR', 'price': 2100, 'category': 'psn'},
    'psn_inr_4000': {'name': 'PSN Gift Card 4000 INR', 'price': 2750, 'category': 'psn'},
    'psn_inr_5000': {'name': 'PSN Gift Card 5000 INR', 'price': 3400, 'category': 'psn'}
}

# Словарь для быстрого поиска продукта по callback_data
DIGITAL_PRODUCT_MAP = {
    f'digital_{key}': key for key in DIGITAL_PRODUCTS.keys()
}