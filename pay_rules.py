# pay_rules.py - Правила для обработки /pay

import re
import logging
from products_config import SUBSCRIPTIONS, DIGITAL_PRODUCTS

logger = logging.getLogger(__name__)

# --- Правила сопоставления аббревиатур из команды /pay с названиями продуктов ---
SERVICE_ABBR_MAP = {
    "Cha": "chatgpt",
    "Dis": "discord",
    "Duo": "duolingo",
    "Pic": "picsart",
    "Can": "canva",
    "Net": "netflix",
    "DisU": "Цифровий товар (Discord Украшення)",
    "PSN": "Цифровий товар (PSN)",
}

PLAN_ABBR_MAP = {
    "Bas": "basic",
    "Ful": "full",
    "Ind": "ind",
    "Fam": "fam",
    "Plu": "plus",
    "Pro": "pro",
    "Pre": "pre",
    "Std": "std",
    "BzN": "Без Nitro",
    "ZN": "З Nitro",
    "Dec": "Украшення",
    "INR": "Gift Card INR",
    "Prod": "Продукт",
}

# --- Функции для обработки /pay ---

def parse_pay_command(args):
    """
    Парсит аргументы команды /pay.
    Возвращает order_id и список items.
    """
    if not args:
        return None, "❌ Неправильний формат команди. Використовуйте: /pay <order_id> <товар1> <товар2> ..."

    order_id = args[0]
    items_str = " ".join(args[1:])
    
    # Паттерн для парсинга отдельного товара: ServiceAbbr-PlanAbbr-Period-Price
    pattern = r'(\w{2,4})-(\w{2,4})-([\w\s$]+?)-(\d+)'
    items = re.findall(pattern, items_str)

    if not items:
        return None, "❌ Не вдалося розпізнати товари у замовленні. Перевірте формат."

    parsed_items = []
    for service_abbr, plan_abbr, period, price_str in items:
        try:
            price = int(price_str)
            parsed_items.append({
                'service_abbr': service_abbr,
                'plan_abbr': plan_abbr,
                'period': period,
                'price': price
            })
        except ValueError:
            logger.warning(f"Неверный формат цены в элементе: {service_abbr}-{plan_abbr}-{period}-{price_str}")
            continue
            
    if not parsed_items:
        return None, "❌ Не вдалося розпізнати жодного товару у замовленні."

    return order_id, parsed_items

def get_full_product_info(parsed_item):
    """
    Преобразует распарсенный элемент в полную информацию о продукте.
    Возвращает словарь с полной информацией или None, если продукт не найден.
    """
    service_abbr = parsed_item['service_abbr']
    plan_abbr = parsed_item['plan_abbr']
    period = parsed_item['period']
    price = parsed_item['price']
    
    logger.info(f"Поиск информации для: {service_abbr}-{plan_abbr}-{period}-{price}")

    # Обработка цифровых товаров (DisU, PSN)
    if service_abbr in ["DisU", "PSN"]:
        matched_product = None
        for prod_id, prod_info in DIGITAL_PRODUCTS.items():
            if prod_info['price'] == price:
                if (service_abbr == "DisU" and "Украшення" in prod_info['name']) or \
                   (service_abbr == "PSN" and "PSN" in prod_info['name']):
                    matched_product = prod_info
                    matched_product_id = prod_id
                    break
        
        if matched_product:
            return {
                'service_name': "Цифровий товар" if service_abbr == "DisU" else "PSN Gift Card",
                'plan_name': matched_product['name'],
                'period': "1 шт",
                'price': price,
                'type': 'digital'
            }
        else:
            logger.warning(f"Цифровой товар с ценой {price} не найден для сервиса {service_abbr}.")
            return {
                'service_name': f"Цифровий товар ({service_abbr})",
                'plan_name': f"Невідомий товар ({plan_abbr})",
                'period': period,
                'price': price,
                'type': 'digital'
            }

    # Обработка подписок
    else:
        service_key = SERVICE_ABBR_MAP.get(service_abbr)
        plan_key = PLAN_ABBR_MAP.get(plan_abbr)
        
        if not service_key or not plan_key:
            logger.warning(f"Неизвестная аббревиатура: Сервис={service_abbr}, План={plan_abbr}")
            return {
                'service_name': f"Невідомий сервіс ({service_abbr})",
                'plan_name': f"Невідомий план ({plan_abbr})",
                'period': period,
                'price': price,
                'type': 'subscription'
            }
            
        service_data = SUBSCRIPTIONS.get(service_key)
        if not service_data:
            logger.warning(f"Сервис {service_key} не найден в конфигурации.")
            return {
                'service_name': service_key,
                'plan_name': plan_key,
                'period': period,
                'price': price,
                'type': 'subscription'
            }
            
        plan_data = service_data['plans'].get(plan_key)
        if not plan_data:
            logger.warning(f"План {plan_key} не найден для сервиса {service_key}.")
            return {
                'service_name': service_data['name'],
                'plan_name': plan_key,
                'period': period,
                'price': price,
                'type': 'subscription'
            }

        return {
            'service_name': service_data['name'],
            'plan_name': plan_data['name'],
            'period': period,
            'price': price,
            'type': 'subscription'
        }

# --- Функции для генерации команды /pay (обратный процесс) ---

def generate_pay_command_from_selection(user_id, service_key, plan_key, period, price):
    """
    Генерирует команду /pay на основе выбора пользователя в интерфейсе.
    """
    # Определяем аббревиатуры
    service_abbr = service_key[:3].capitalize() # Первые 3 буквы + заглавная
    plan_abbr = plan_key.upper()
    # Упрощенное форматирование периода
    period_abbr = period.replace('місяць', 'м').replace('місяців', 'м')
    
    # Генерируем order_id
    order_id = 'O' + str(user_id)[-4:] + str(price)[-2:]
    
    command = f"/pay {order_id} {service_abbr}-{plan_abbr}-{period_abbr}-{price}"
    return command, order_id

def generate_pay_command_from_digital_product(user_id, product_id, product_info):
    """
    Генерирует команду /pay для цифрового товара.
    """
    # Определяем аббревиатуру сервиса и плана на основе ID продукта или категории
    if "PSN" in product_info['name']:
        service_abbr = "PSN"
        plan_abbr = "INR" # Или можно кодировать номинал, например, "1K", "2K" и т.д.
    else:
        # Для остальных цифровых товаров (Discord Украшення)
        service_abbr = "DisU"
        plan_abbr = "Dec" if "Украшення" in product_info['name'] else "Prod"
        
    price = product_info['price']
    order_id = 'D' + str(user_id)[-4:] + str(price)[-2:]
    command = f"/pay {order_id} {service_abbr}-{plan_abbr}-1шт-{price}"
    return command, order_id