from telebot import TeleBot, types
from db import save_message
from kb import generate_contact_keyboard

def setup_bot_handlers(bot):
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(message, "Добро пожаловать в онлайн клуб VIP 🎉🥂", reply_markup=generate_contact_keyboard())

    @bot.message_handler(func=lambda message: True)
    def echo_all(message):
        save_message(message.chat.id, message.text)  # Сохраняем сообщение в БД
        bot.send_message(message.chat.id, "Ваше сообщение было отправлено", reply_markup=generate_contact_keyboard())
