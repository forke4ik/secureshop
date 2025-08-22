# products_config.py - Конфигурация продуктов

SUBSCRIPTIONS = {
    'chatgpt': {
        'name': 'ChatGPT',
        'plans': {
            'plus': {
                'name': 'Plus',
                'description': 'Доступ к ChatGPT Plus',
                'options': [
                    {"period": "1 місяць", "price": 650},
                ]
            },
        }
    },
    'discord': {
        'name': 'Discord',
        'plans': {
            'basic': {
                'name': 'Nitro Basic',
                'description': 'Discord Nitro Basic',
                'options': [
                    {"period": "1 місяць", "price": 100},
                    {"period": "12 місяців", "price": 900}
                ]
            },
            'full': {
                'name': 'Nitro Full',
                'description': 'Discord Nitro Full',
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
                    {"period": "12 місяців", "price": 380}
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
            'ind': {
                'name': 'Individual',
                'description': 'Індивідуальна підписка',
                'options': [
                    {"period": "1 місяць", "price": 350},
                    {"period": "12 місяців", "price": 3000}
                ]
            },
            'fam': {
                'name': 'Family',
                'description': 'Сімейна підписка',
                'options': [
                    {"period": "1 місяць", "price": 850},
                    {"period": "12 місяців", "price": 7500}
                ]
            }
        }
    },
    'netflix': {
        'name': 'Netflix',
        'plans': {
            'pre': {
                'name': 'Premium',
                'description': 'Доступ на 4 екрани у Ultra HD (4K) якості',
                'options': [
                    {"period": "1 місяць", "price": 350},
                ]
            }
        }
    }
}

DIGITAL_PRODUCTS = {
    'discord_bzn_6': {'name': 'Discord Украшення (Без Nitro) - 6$', 'price': 180, 'category': 'bzn'},
    'discord_bzn_8': {'name': 'Discord Украшення (Без Nitro) - 8$', 'price': 235, 'category': 'bzn'},
    'discord_bzn_10': {'name': 'Discord Украшення (Без Nitro) - 10$', 'price': 295, 'category': 'bzn'},
    'discord_bzn_11': {'name': 'Discord Украшення (Без Nitro) - 11$', 'price': 325, 'category': 'bzn'},
    'discord_bzn_12': {'name': 'Discord Украшення (Без Nitro) - 12$', 'price': 355, 'category': 'bzn'},
    'discord_bzn_13': {'name': 'Discord Украшення (Без Nitro) - 13$', 'price': 385, 'category': 'bzn'},
    'discord_bzn_15': {'name': 'Discord Украшення (Без Nitro) - 15$', 'price': 440, 'category': 'bzn'},
    'discord_bzn_16': {'name': 'Discord Украшення (Без Nitro) - 16$', 'price': 470, 'category': 'bzn'},
    'discord_bzn_18': {'name': 'Discord Украшення (Без Nitro) - 18$', 'price': 530, 'category': 'bzn'},
    'discord_bzn_24': {'name': 'Discord Украшення (Без Nitro) - 24$', 'price': 705, 'category': 'bzn'},
    'discord_bzn_29': {'name': 'Discord Украшення (Без Nitro) - 29$', 'price': 855, 'category': 'bzn'},
    'discord_zn_5': {'name': 'Discord Украшення (З Nitro) - 5$', 'price': 145, 'category': 'zn'},
    'discord_zn_7': {'name': 'Discord Украшення (З Nitro) - 7$', 'price': 205, 'category': 'zn'},
    'discord_zn_8_5': {'name': 'Discord Украшення (З Nitro) - 8.5$', 'price': 250, 'category': 'zn'},
    'discord_zn_9': {'name': 'Discord Украшення (З Nitro) - 9$', 'price': 265, 'category': 'zn'},
    'discord_zn_14': {'name': 'Discord Украшення (З Nitro) - 14$', 'price': 410, 'category': 'zn'},
    'discord_zn_22': {'name': 'Discord Украшення (З Nitro) - 22$', 'price': 650, 'category': 'zn'},
    'psn_inr_1000': {'name': 'PSN Gift Card 1000 INR', 'price': 725, 'category': 'psn'},
    'psn_inr_2000': {'name': 'PSN Gift Card 2000 INR', 'price': 1400, 'category': 'psn'},
    'psn_inr_3000': {'name': 'PSN Gift Card 3000 INR', 'price': 2100, 'category': 'psn'},
    'psn_inr_4000': {'name': 'PSN Gift Card 4000 INR', 'price': 2750, 'category': 'psn'},
    'psn_inr_5000': {'name': 'PSN Gift Card 5000 INR', 'price': 3400, 'category': 'psn'}
}

DIGITAL_PRODUCT_MAP = {
    f'digital_{key}': key for key in DIGITAL_PRODUCTS.keys()
}
