# from telebot import TeleBot, types
# from db import save_message
# from kb import generate_contact_keyboard

# def setup_bot_handlers(bot):
#     @bot.message_handler(commands=['start', 'help'])
#     def send_welcome(message):
#         bot.reply_to(message, "Добро пожаловать в онлайн клуб VIP 🎉🥂", reply_markup=generate_contact_keyboard())

#     @bot.message_handler(func=lambda message: True)
#     def echo_all(message):
#         bot.send_message(message.chat.id, "Ваше сообщение было отправлено", reply_markup=generate_contact_keyboard())

import os
from telebot import types
import telebot
from db import authenticate_admin, set_setting, get_setting
from kb import generate_main_keyboard, generate_admin_keyboard, generate_contact_keyboard
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
bot = telebot.TeleBot(TOKEN)

def setup_bot_handlers(bot):
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(message, "Добро пожаловать в онлайн клуб VIP 🎉🥂", reply_markup=generate_contact_keyboard())


SUPERADMIN_LOGIN = os.getenv('SUPERADMIN_LOGIN')
SUPERADMIN_PASSWORD = os.getenv('SUPERADMIN_PASSWORD')

@bot.message_handler(commands=['settings'])
def settings_command(message):
    msg = bot.reply_to(message, "Введите логин и пароль через пробел:")
    bot.register_next_step_handler(msg, process_login_step)

def process_login_step(message):
    try:
        login, password = message.text.split()
        if login == SUPERADMIN_LOGIN and password == SUPERADMIN_PASSWORD:
            bot.send_message(message.chat.id, "Аутентификация успешна!", reply_markup=generate_admin_keyboard(True))
        else:
            bot.send_message(message.chat.id, "Неверный логин или пароль.")
    except Exception as e:
        bot.reply_to(message, "Ошибка ввода. Пожалуйста, введите логин и пароль через пробел.")
        
@bot.message_handler(func=lambda message: message.text == 'Ссылка на канал')
def channel_link(message):
    link = get_setting('channel_link')
    bot.reply_to(message, link or "Ссылка на канал еще не установлена.")

@bot.message_handler(func=lambda message: message.text == 'Ссылка на бота')
def bot_link(message):
    link = get_setting('bot_link')
    bot.reply_to(message, link or "Ссылка на бота еще не установлена.")

@bot.message_handler(func=lambda message: message.text == 'Контакт менеджера')
def manager_contact(message):
    contact = get_setting('manager_contact')
    bot.reply_to(message, contact or "Контакт менеджера еще не установлен.")