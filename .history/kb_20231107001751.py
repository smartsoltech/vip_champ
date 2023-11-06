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
    channel_button = types.InlineKeyboardButton(text="📢 Перейти в канал", url='fhttps://t.me/{channel_link}')
    chat_button = types.InlineKeyboardButton(text="💬 Перейти в чат розыгрыша", url='https://t.me/{bot_link}')

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