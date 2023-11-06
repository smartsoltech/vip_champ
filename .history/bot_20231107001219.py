from telebot import TeleBot, types
from db import set_setting, get_setting, get_admin, add_admin, remove_admin, authenticate_admin, authenticate_super_admin
from kb import generate_contact_keyboard, generate_admin_keyboard
import os
from icecream import ic

MASTERADMIN_LOGIN = os.getenv('MASTERADMIN_LOGIN')
MASTERADMIN_PASSWORD = os.getenv('MASTERADMIN_PASSWORD')

def setup_bot_handlers(bot):
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(message, "Добро пожаловать в онлайн клуб VIP 🎉🥂", reply_markup=generate_contact_keyboard())

    @bot.message_handler(commands=['settings'])
    def settings_command(message):
        msg = bot.reply_to(message, "Введите логин и пароль через пробел:")
        bot.register_next_step_handler(msg, process_login_step)

    def process_login_step(message):
        try:
            parts = message.text.split()
            if len(parts) == 2:
                login, password = parts
                ic(login, password)
                if login == MASTERADMIN_LOGIN and password == MASTERADMIN_PASSWORD:
                    bot.send_message(message.chat.id, "Аутентификация успешна!", reply_markup=generate_admin_keyboard(True))
                else:
                    bot.send_message(message.chat.id, "Неверный логин или пароль.")
            else:
                raise ValueError("Неверный формат. Нужно ввести логин и пароль, разделенные пробелом.")
        except Exception as e:
            bot.reply_to(message, "Произошла ошибка при вводе. Пожалуйста, введите логин и пароль через пробел.")
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

    @bot.message_handler(commands=['add_superadmin'])
    def add_superadmin_command(message):
        if authenticate_super_admin(message.from_user.username):
            msg = bot.reply_to(message, "Введите логин нового супер-админа:")
            bot.register_next_step_handler(msg, process_add_superadmin)
        else:
            bot.reply_to(message, "У вас нет прав для выполнения этой команды.")

    def process_add_superadmin(message):
        username = message.text.strip()
        if add_admin(username, is_superadmin=True):
            bot.reply_to(message, f"Супер-админ {username} успешно добавлен.")
        else:
            bot.reply_to(message, "Не удалось добавить супер-админа.")

    # Обработчик для удаления админа
    @bot.message_handler(commands=['remove_admin'])
    def remove_admin_command(message):
        if authenticate_super_admin(message.from_user.username):
            msg = bot.reply_to(message, "Введите логин админа для удаления:")
            bot.register_next_step_handler(msg, process_remove_admin)
        else:
            bot.reply_to(message, "У вас нет прав для выполнения этой команды.")

    def process_remove_admin(message):
        username = message.text.strip()
        if remove_admin(username):
            bot.reply_to(message, f"Админ {username} успешно удален.")
        else:
            bot.reply_to(message, "Не удалось удалить админа.")

    # Обработчик для проверки статуса админа
    @bot.message_handler(commands=['check_admin'])
    def check_admin_command(message):
        username = message.from_user.username
        if authenticate_admin(username):
            if authenticate_super_admin(username):
                status = "супер-админ"
            else:
                status = "админ"
            bot.reply_to(message, f"Вы являетесь {status}.")
        else:
            bot.reply_to(message, "Вы не являетесь администратором.")
                
    # стандартный ответ на неизвестные запросы
    @bot.message_handler(func=lambda message: True)
    def echo_all(message):
        bot.send_message(message.chat.id, "Введите /help для просмотра информации")

