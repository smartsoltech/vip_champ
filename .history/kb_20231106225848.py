from telebot import types
import os

def generate_contact_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    manager_username = os.getenv('MANAGER_USERNAME')
    deposit_button = types.InlineKeyboardButton(text="💰 Внести депозит", url="https://t.me/VIPchampKR")
    contact_button = types.InlineKeyboardButton(text="🤝 Связаться с менеджером", url="https://t.me/VIPchampKR")
    channel_button = types.InlineKeyboardButton(text="📢 Перейти в канал", url="https://t.me/+E6Xo5rKob8k4MzIy")
    chat_button = types.InlineKeyboardButton(text="💬 Перейти в чат розыгрыша", url="https://t.me/HappyWeekendVip_bot")

    # Добавляем каждую кнопку в отдельный ряд
    keyboard.row(deposit_button)
    keyboard.row(contact_button)
    keyboard.row(channel_button)
    keyboard.row(chat_button)
    return keyboard

def generate_admin_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton('Изменить контакт менеджера', callback_data='change_manager'))
    keyboard.add(types.InlineKeyboardButton('Изменить ссылку на канал', callback_data='change_channel'))
    keyboard.add(types.InlineKeyboardButton('Изменить ссылку на бота', callback_data='change_bot'))
    return keyboard
bot.py
Добавим обработчики команд и интегрируем клавиатуры:

python
Copy code
import os
from telebot import types
import telebot
from db_functions import authenticate_admin, set_setting, get_setting
from kb import generate_main_keyboard, generate_admin_keyboard
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Добро пожаловать в онлайн клуб VIP!", reply_markup=generate_main_keyboard())

@bot.message_handler(func=lambda message: message.text == 'Контакт менеджера')
def manager_contact(message):
    contact = get_setting('manager_contact')
    bot.reply_to(message, contact or "Контакт менеджера еще не установлен.")

@bot.message_handler(func=lambda message: message.text == 'Ссылка на канал')
def channel_link(message):
    link = get_setting('channel_link')
    bot.reply_to(message, link or "Ссылка на канал еще не установлена.")

@bot.message_handler(func=lambda message: message.text == 'Ссылка на бота')
def bot_link(message):
    link = get_setting('bot_link')
    bot.reply_to(message, link or "Ссылка на бота еще не установлена.")

@bot.message_handler(commands=['settings'])
def settings(message):
    admin = authenticate_admin(message.from_user.username, 'password')  # Здесь должна быть ваша логика аутентификации
    if admin and admin.role == 'superadmin':
        bot.send_message(message.chat.id, "Настройки админа:", reply_markup=generate_admin_keyboard())
    else:
        bot.send_message(message.chat.id, "У вас нет доступа к настройкам.")

# Здесь добавьте другие обработчики команд и колбэков
# ...

if __name__ == '__main__':
    bot.polling(none_stop=True)
Обратите внимание, что в обработчике /settings используется заглушка для аутентификации ('password'). Вам нужно будет реализовать надежный механизм аутентификации, возможно, с использованием временных токенов или другого метода верификации.

Также вам нужно будет добавить обработчики для колбэк-данных, чтобы обрабатывать нажатия кнопок в инлайн-клавиатуре, и реализовать логику для изменения настроек через команды /change_manager, /change_channel, /change_bot.

Не забудьте также обеспечить безопасность ваших данных, особенно при работе с паролями и аутентификацией пользователей.





