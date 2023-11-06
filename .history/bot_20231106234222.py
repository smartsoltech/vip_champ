from telebot import TeleBot, types
from db import save_message
from kb import generate_contact_keyboard, generate_admin_keyboard
import os

def setup_bot_handlers(bot):
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(message, "Добро пожаловать в онлайн клуб VIP 🎉🥂", reply_markup=generate_contact_keyboard())

    @bot.message_handler(commands=['settings'])
    def settings_command(message):
        msg = bot.reply_to(message, "Введите логин и пароль через пробел:")
        bot.register_next_step_handler(msg, process_login_step)

    def process_login_step(message):
        login, password = message.text.split()
        if login == os.getenv('SUPERADMIN_LOGIN') and password == os.getenv('SUPERADMIN_PASSWORD'):
            bot.send_message(message.chat.id, "Аутентификация успешна!", reply_markup=generate_admin_keyboard())
        else:
            bot.send_message(message.chat.id, "Неверный логин или пароль.")
            
    @bot.message_handler(func=lambda message: True)
    def echo_all(message):
        bot.send_message(message.chat.id, "Ваше сообщение было отправлено", reply_markup=generate_contact_keyboard())

