from telebot import TeleBot, types
from db import set_setting, get_setting
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
 
    @bot.message_handler(func=lambda message: message.text == 'Изменить контакт менеджера')
    def change_manager_contact(message):
        msg = bot.reply_to(message, "Отправьте новый контакт менеджера.")
        bot.register_next_step_handler(msg, process_manager_contact)

    def process_manager_contact(message):
        set_setting('manager_contact', message.text)
        bot.send_message(message.chat.id, "Контакт менеджера успешно обновлен.")

    # Обработчик для команды "Изменить ссылку на канал"
    def change_channel_link(message):
        msg = bot.reply_to(message, "Отправьте новую ссылку на канал.")
        bot.register_next_step_handler(msg, process_channel_link)

    def process_channel_link(message):
        set_setting('channel_link', message.text)
        bot.send_message(message.chat.id, "Ссылка на канал успешно обновлена.")

    # Обработчик для изменения ссылки на бота
    @bot.message_handler(func=lambda message: message.text == 'Изменить ссылку на бота' and authenticate_admin(message.from_user.id))
    def change_bot_link(message):
        msg = bot.reply_to(message, "Отправьте новую ссылку на бота.")
        bot.register_next_step_handler(msg, process_bot_link)

    def process_bot_link(message):
        set_setting('bot_link', message.text)
        bot.send_message(message.chat.id, "Ссылка на бота успешно обновлена.")

    # Обработчик для добавления супер-админа
    @bot.message_handler(func=lambda message: message.text == 'Добавить админа' and authenticate_super_admin(message.from_user.id))
    def add_admin(message):
        msg = bot.reply_to(message, "Отправьте логин нового админа.")
        bot.register_next_step_handler(msg, process_new_admin)

    def process_new_admin(message):
        # Здесь должна быть логика для добавления нового админа
        # Например, добавление в базу данных или в список админов в файле
        new_admin_username = message.text
        # add_new_admin(new_admin_username)  # Эта функция должна быть реализована вами
        bot.send_message(message.chat.id, f"Админ {new_admin_username} успешно добавлен.") 
                
    # стандартный ответ на неизвестные запросы
    @bot.message_handler(func=lambda message: True)
    def echo_all(message):
        bot.send_message(message.chat.id, "Введите /help для просмотра информации")

