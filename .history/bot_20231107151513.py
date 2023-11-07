from telebot import TeleBot, types
from db import set_setting, get_setting, get_admin, add_admin, remove_admin, authenticate_admin, authenticate_super_admin
from db import get_or_create_client, get_settings, get_all_clients, export_clients_to_csv, is_admin
from kb import generate_contact_keyboard, generate_admin_keyboard
from db import Admin, session
import os, csv, io
from dotenv import load_dotenv
from icecream import ic
load_dotenv()

MASTERADMIN_LOGIN = os.getenv('MASTERADMIN_LOGIN')
MASTERADMIN_PASSWORD = os.getenv('MASTERADMIN_PASSWORD')

def authenticate_admin(username, password):
    admin = session.query(Admin).filter_by(username=username).first()
    if admin and admin.check_password(password):
        return True
    return False

def setup_bot_handlers(bot):
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        if message.from_user.is_bot:
            bot.reply_to(message, "Мы не общаемся с ботами.")
            return

        client = get_or_create_client(
            chat_id=message.chat.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            is_bot=message.from_user.is_bot
        )
        if client:
            bot.reply_to(message, "Выберите опцию:", reply_markup=generate_contact_keyboard())
        else:
            bot.reply_to(message, "Произошла ошибка при сохранении данных.")
            
    @bot.message_handler(commands=['settings'])
    def settings_command(message):
        msg = bot.reply_to(message, "Введите логин и пароль через пробел:")
        bot.register_next_step_handler(msg, process_login_step)
        ic(msg)
        
    def process_login_step(message):
        try:
            username, password = message.text.split()
            if authenticate_admin(username, password):
                bot.send_message(message.chat.id, "Authentication successful!")
                bot.reply_to(message, 'choose operation', reply_markup = generate_admin_keyboard(is_su=True))
                ic(username, password)
                # Here you can call the function to generate the admin keyboard
            else:
                bot.send_message(message.chat.id, "Invalid login or password.")
        except Exception as e:
            bot.reply_to(message, "Error in input. Please enter login and password separated by space.")
            ic(e)
                   
    @bot.message_handler(func=lambda message: message.text == 'Изменить контакт менеджера')
    def change_manager_contact(message):
        msg = bot.reply_to(message, "Отправьте новый контакт менеджера.")
        bot.register_next_step_handler(msg, process_manager_contact)

    def process_manager_contact(message):
        set_setting('manager_contact', message.text)
        bot.send_message(message.chat.id, "Контакт менеджера успешно обновлен.")

    # Обработчик для команды "Изменить ссылку на канал"
    @bot.message_handler(func=lambda message: message.text == 'Изменить ссылку на канал')
    def change_channel_link(message):
        msg = bot.reply_to(message, "Отправьте новую ссылку на канал.")
        bot.register_next_step_handler(msg, process_channel_link)

    def process_channel_link(message):
        set_setting('channel_link', message.text)
        bot.send_message(message.chat.id, "Ссылка на канал успешно обновлена.")

    # Обработчик для изменения ссылки на бота
    @bot.message_handler(func=lambda message: message.text == 'Изменить ссылку на бота')
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
          
    @bot.message_handler(func=lambda message: message.text.startswith('/send_all'))
    def send_all(message):
        msg = bot.reply_to(message, "Введите логин и пароль через пробел для аутентификации:")
        bot.register_next_step_handler(msg, process_send_all_login, message.text)

    def process_send_all_login(message, command_text):
        try:
            username, password = message.text.split(' ', 1)
            if authenticate_admin(username, password):
                _, text = command_text.split(' ', 1)
                clients = get_all_clients()
                for client_data in clients:
                    personalized_message = f"Привет, {client_data['first_name']}!\n{text}"
                    bot.send_message(client_data['chat_id'], personalized_message)
                bot.reply_to(message, "Сообщение отправлено всем клиентам.")
            else:
                bot.reply_to(message, "Неверный логин или пароль.")
        except ValueError:
            bot.reply_to(message, "Введите логин и пароль через пробел.")
            
    # @bot.message_handler(func=lambda message: message.text.startswith('/send_all'))
    # def send_all(message):
    #     try:
    #         parts = message.text.split(' ', 3)
    #         if len(parts) < 4:
    #             bot.reply_to(message, "Неправильный формат команды. Нужно: /send_all [login] [password] [message]")
    #             return

    #         _, login, password, text = parts

    #         if login == MASTERADMIN_LOGIN and password == MASTERADMIN_PASSWORD:
    #             clients = get_all_clients()
    #             for client_data in clients:
    #                 personalized_message = f"Привет, {client_data['first_name']}!\n{text}"
    #                 bot.send_message(client_data['chat_id'], personalized_message)
    #             bot.reply_to(message, "Сообщение отправлено всем клиентам.")
    #         else:
    #             bot.reply_to(message, "У вас нет прав для выполнения этой команды.")
    #     except Exception as e:
    #         bot.reply_to(message, f"Произошла ошибка: {e}")

    def check_masteradmin_credentials(login, password):
        return login == MASTERADMIN_LOGIN and password == MASTERADMIN_PASSWORD

    @bot.message_handler(commands=['export_clients'])
    def handle_export_clients_command(message):
        msg = bot.send_message(message.chat.id, "Введите логин и пароль мастер-админа:")
        bot.register_next_step_handler(msg, process_export_clients)

    def process_export_clients(message):
        # Создаем объект StringIO для хранения данных CSV
        clients_csv = io.StringIO()
        writer = csv.writer(clients_csv)

        # Записываем заголовки столбцов
        writer.writerow(['First Name', 'Last Name', 'Chat ID'])

        # Получаем данные клиентов из базы данных
        clients = get_all_clients()  # Эта функция должна возвращать список словарей клиентов

        # Записываем данные клиентов
        for client in clients:
            # Используем ключи словаря для доступа к данным
            writer.writerow([client['first_name'], client['last_name'], client['chat_id']])

        # Перемещаем указатель в начало файла
        clients_csv.seek(0)

        # Отправляем файл
        bot.send_document(message.chat.id, ('clients.csv', clients_csv.getvalue().encode('utf-8-sig')), caption='Вот список клиентов!')

        # Не забудьте закрыть StringIO объект после использования
        clients_csv.close()
        
        
    # Обработчик команды для получения списка админов
    
    @bot.message_handler(commands=['get_admins'])
    def handle_get_admins_command(message):
        msg = bot.send_message(message.chat.id, "Введите логин и пароль через пробел для аутентификации:")
        bot.register_next_step_handler(msg, process_get_admins_login)

    def process_get_admins_login(message):
        try:
            username, password = message.text.split(' ', 1)
            if authenticate_admin(username, password):
                admins_list = get_admin()
                ic(admins_list)
                response = '\n'.join([f"{admin.username} (Суперадмин: {'Да' if admin.is_superadmin else 'Нет'})" for admin in admins_list])
                bot.send_message(message.chat.id, f'Список админов:\n{response}')
            else:
                bot.send_message(message.chat.id, "Неверный логин или пароль.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль через пробел.")
            ic(ValueError)

    # @bot.message_handler(commands=['get_admins'])
    # def handle_get_admins_command(message):
    #     msg = bot.send_message(message.chat.id, "Введите логин и пароль мастер-админа:")
    #     bot.register_next_step_handler(msg, process_get_admins)

    # def process_get_admins(message):
    #     try:
    #         login, password = message.text.split()
    #         if check_masteradmin_credentials(login, password):
    #             admins_list = get_admin()
    #             # Отправка списка админов пользователю
    #             bot.send_message(message.chat.id, f'Список админов: {admins_list}')
    #         else:
    #             bot.send_message(message.chat.id, "Неверный логин или пароль.")
    #     except ValueError:
    #         bot.send_message(message.chat.id, "Введите логин и пароль через пробел.")

    # Обработчик команды для получения настроек
    @bot.message_handler(commands=['get_settings'])
    def handle_get_settings_command(message):
        msg = bot.send_message(message.chat.id, "Введите логин и пароль через пробел для аутентификации:")
        bot.register_next_step_handler(msg, process_get_settings_login)

    def process_get_settings_login(message):
        try:
            username, password = message.text.split(' ', 1)
            if authenticate_admin(username, password):
                settings = get_settings()
                for setting in settings:
                    bot.send_message(message.chat.id, f"{setting['name']}: {setting['value']}")
            else:
                bot.send_message(message.chat.id, "Неверный логин или пароль.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль через пробел.")

    
    # @bot.message_handler(commands=['get_settings'])
    # def handle_get_settings_command(message):
    #     msg = bot.send_message(message.chat.id, "Введите логин и пароль мастер-админа:")
    #     bot.register_next_step_handler(msg, process_get_settings)

    # def process_get_settings(message):
    #     try:
    #         login, password = message.text.split()
    #         if check_masteradmin_credentials(login, password):
    #             settings = get_settings()
    #             for setting in settings:
    #                 # Отправка настроек пользователю
    #                 bot.send_message(message.chat.id, f"{setting['name']}: {setting['value']}")
    #         else:
    #             bot.send_message(message.chat.id, "Неверный логин или пароль.")
    #     except ValueError:
    #         bot.send_message(message.chat.id, "Введите логин и пароль через пробел.")
            
            
    SUPERADMIN_USERNAME = os.getenv('MASTERADMIN_USERNAME')
    SUPERADMIN_PASSWORD = os.getenv('MASTERADMIN_PASSWORD')

    def is_superadmin(username, password):
        return username == SUPERADMIN_USERNAME and password == SUPERADMIN_PASSWORD
    ic(MASTERADMIN_LOGIN, MASTERADMIN_PASSWORD)
    
    @bot.message_handler(commands=['add_admin'])
    def add_admin_command(message):
        msg = bot.reply_to(message, "Введите логин и пароль через пробел для аутентификации:")
        bot.register_next_step_handler(msg, process_add_admin_login)

    def process_add_admin_login(message):
        try:
            username, password = message.text.split(' ', 1)
            if authenticate_admin(username, password):
                msg = bot.reply_to(message, "Введите логин и пароль нового админа через пробел:")
                bot.register_next_step_handler(msg, process_add_admin_data)
            else:
                bot.send_message(message.chat.id, "Неверный логин или пароль.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль через пробел.")

    def process_add_admin_data(message):
        try:
            new_username, new_password = message.text.split(' ', 1)
            if add_admin(new_username, new_password):
                bot.send_message(message.chat.id, f"Админ {new_username} успешно добавлен.")
            else:
                bot.send_message(message.chat.id, "Не удалось добавить админа.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль нового админа через пробел.")

    # Обработчик для команды добавления суперадмина
    @bot.message_handler(commands=['add_superadmin'])
    def add_superadmin_command(message):
        msg = bot.reply_to(message, "Введите логин и пароль через пробел для аутентификации:")
        bot.register_next_step_handler(msg, process_add_superadmin_login)

    def process_add_superadmin_login(message):
        try:
            username, password = message.text.split(' ', 1)
            if authenticate_super_admin(username, password):
                msg = bot.reply_to(message, "Введите логин и пароль нового суперадмина через пробел:")
                bot.register_next_step_handler(msg, process_add_superadmin_data)
            else:
                bot.send_message(message.chat.id, "Неверный логин или пароль.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль через пробел.")

    def process_add_superadmin_data(message):
        try:
            new_username, new_password = message.text.split(' ', 1)
            if add_admin(new_username, new_password, is_superadmin=True):
                bot.send_message(message.chat.id, f"Суперадмин {new_username} успешно добавлен.")
            else:
                bot.send_message(message.chat.id, "Не удалось добавить суперадмина.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль нового суперадмина через пробел.")
        
    # стандартный ответ на неизвестные запросы - это самый посследний хэндлер. все хэндлеры ниже него работать не будут!!!!
    @bot.message_handler(func=lambda message: True)
    def handle_message(message):
        if message.from_user.is_bot:
            bot.reply_to(message, "Мы не общаемся с ботами.")
            return

        client = get_or_create_client(
            chat_id=message.chat.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            is_bot=message.from_user.is_bot
        )
        if client:
            bot.reply_to(message, "Выберите опцию:", reply_markup=generate_contact_keyboard())
        else:
            bot.reply_to(message, "Произошла ошибка при сохранении данных.")

