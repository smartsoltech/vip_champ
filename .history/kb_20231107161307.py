from telebot import types
import os
from db import get_setting

def generate_contact_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    # Получаем данные из базы данных
    manager_contact = get_setting('manager_contact')
    channel_link = get_setting('channel_link')
    bot_link = get_setting('bot_link')
    
    # Создаем кнопки с полученными данными
    deposit_button = types.InlineKeyboardButton(text="💰 Внести депозит", url=f'https://t.me/{manager_contact}')
    contact_button = types.InlineKeyboardButton(text="🤝 Связаться с менеджером", url=f'https://t.me/{manager_contact}')
    channel_button = types.InlineKeyboardButton(text="📢 Перейти в канал", url=f'https://t.me/{channel_link}')
    chat_button = types.InlineKeyboardButton(text="💬 Перейти в чат розыгрыша", url=f'https://t.me/{bot_link}')

    # Добавляем каждую кнопку в отдельный ряд
    keyboard.row(deposit_button)
    keyboard.row(contact_button)
    keyboard.row(channel_button)
    keyboard.row(chat_button)
    return keyboard

def generate_admin_keyboard(is_su):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, )
    keyboard.add('Изменить контакт менеджера')
    keyboard.add('Изменить ссылку на канал')
    keyboard.add('Изменить ссылку на бота')
    if os.getenv('IS_SUPERADMIN') == 'true' and is_su:  # Предполагаем, что у вас есть переменная окружения IS_SUPERADMIN
        keyboard.add('Добавить админа')
    return keyboard

def generate_admin_inline_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)  # row_width=1 для одной кнопки в ряду
    # Создаем кнопки
    change_manager_contact_button = types.InlineKeyboardButton(text="Изменить контакт менеджера", callback_data="change_manager_contact")
    change_channel_link_button = types.InlineKeyboardButton(text="Изменить ссылку на канал", callback_data="change_channel_link")
    change_bot_link_button = types.InlineKeyboardButton(text="Изменить ссылку на бота", callback_data="change_bot_link")
    export_clients_button = types.InlineKeyboardButton(text="Экспорт клиентов", callback_data="export_clients")
    get_admins_button = types.InlineKeyboardButton(text="Список админов", callback_data="get_admins")
    add_admin_button = types.InlineKeyboardButton(text="Добавить админа", callback_data="add_admin")
    remove_admin_button = types.InlineKeyboardButton(text="Удалить админа", callback_data="remove_admin")
    send_all_button = types.InlineKeyboardButton(text="Отправить всем", callback_data="send_all")

    # Добавляем кнопки на клавиатуру
    keyboard.add(change_manager_contact_button, change_channel_link_button, change_bot_link_button,
                 export_clients_button, get_admins_button, add_admin_button, remove_admin_button, send_all_button)
    
    return keyboard