from telebot import TeleBot, types
from db import set_setting, get_setting, get_admin, add_admin, remove_admin, authenticate_admin, authenticate_super_admin
from db import get_or_create_client, get_settings, get_all_clients, export_clients_to_csv, is_admin
from kb import generate_contact_keyboard, generate_admin_keyboard, generate_admin_inline_keyboard
from db import Admin, session
import os, csv, io
from dotenv import load_dotenv
from icecream import ic
load_dotenv()

MASTERADMIN_LOGIN = os.getenv('MASTERADMIN_LOGIN')
MASTERADMIN_PASSWORD = os.getenv('MASTERADMIN_PASSWORD')


def setup_icecream():
    ic.configureOutput(includeContext=True)
    
    
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
                bot.reply_to(message, 'choose operation', reply_markup = generate_admin_inline_keyboard())
                ic(username, password, authenticate_admin(username, password))
                # Here you can call the function to generate the admin keyboard
            else:
                bot.send_message(message.chat.id, "Invalid login or password.")
                ic(username, password, authenticate_admin(username, password))
                ic(get_admin())
        except Exception as e:
            bot.reply_to(message, "Error in input. Please enter login and password separated by space.")
            ic(e)
                   
  ##### обработкам callback админских кнопок
  
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback_query(call):
        if call.data == "change_manager_contact":
            msg = bot.send_message(call.message.chat.id, "Отправьте новый контакт менеджера.")
            bot.register_next_step_handler(msg, process_manager_contact)
            ic(call.data)
        elif call.data == "change_channel_link":
            msg = bot.send_message(call.message.chat.id, "Отправьте новую ссылку на канал.")
            bot.register_next_step_handler(msg, process_channel_link)
            ic(call.data)
        elif call.data == "change_bot_link":
            msg = bot.send_message(call.message.chat.id, "Отправьте новую ссылку на бота.")
            bot.register_next_step_handler(msg, process_bot_link)
            ic(call.data)
        elif call.data == "remove_admin":
            msg = bot.send_message(call.message.chat.id, "Введите логин админа для удаления:")
            bot.register_next_step_handler(msg, process_remove_admin)
        elif call.data == "check_admin":
            process_check_admin(call.message)
            ic(call.data)
        elif call.data == 'get_admins':
            def handle_get_admins_command(message):
                msg = bot.send_message(message.chat.id, "Введите логин и пароль через пробел для аутентификации:")
                bot.register_next_step_handler(msg, process_get_admins_login)
        elif call.data == 'export_clients':
            def handle_export_clients_command(message):
                msg = bot.send_message(message.chat.id, "Формирование списка клиентов")
                bot.register_next_step_handler(msg, process_export_clients)
        elif call.data == "send_all":
            msg = bot.send_message(call.message.chat.id, "Введите сообщение для отправки всем клиентам:")
            bot.register_next_step_handler(msg, process_send_all_message)
        bot.answer_callback_query(call.id)
        
    # Не забудьте всегда отвечать на callback, чтобы убрать часы загрузки на кнопке
        bot.answer_callback_query(call.id)
  # обработка действий по inline клаве
  # смена контакта менеджера
    def process_manager_contact(message):
        set_setting('manager_contact', message.text)
        bot.send_message(message.chat.id, "Контакт менеджера успешно обновлен.", reply_markup = generate_admin_inline_keyboard())
  # смена ссылки на канал
    def process_channel_link(message):
        set_setting('channel_link', message.text)
        bot.send_message(message.chat.id, "Ссылка на канал успешно обновлена.", reply_markup = generate_admin_inline_keyboard())
  # смена ссылки на бота
    def process_bot_link(message):
        set_setting('bot_link', message.text)
        bot.send_message(message.chat.id, "Ссылка на бота успешно обновлена.", reply_markup = generate_admin_inline_keyboard())
  # удаление админа
    def process_remove_admin(message):
        username = message.text.strip()
        if remove_admin(username):
            bot.send_message(message.chat.id, f"Админ {username} успешно удален.", reply_markup = generate_admin_inline_keyboard())
        else:
            bot.send_message(message.chat.id, "Не удалось удалить админа.")
  #  проверка админа
    def process_check_admin(message):
        username = message.from_user.username
        if authenticate_admin(username):
            if authenticate_super_admin(username):
                status = "супер-админ"
            else:
                status = "админ"
            bot.send_message(message.chat.id, f"Вы являетесь {status}.")
        else:
            bot.send_message(message.chat.id, "Вы не являетесь администратором.")
  # рассылка сообщения клиентам
    def process_send_all_message(message):
        text = message.text
        clients = get_all_clients()
        for client_data in clients:
            personalized_message = f"Привет, {client_data['first_name']}!\n{text}"
            bot.send_message(client_data['chat_id'], personalized_message)
        bot.reply_to(message, "Сообщение отправлено всем клиентам.")
  # получение списка админов   
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
   # экспорт клиентов в csv           
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
            
  
    def check_masteradmin_credentials(login, password):
        return login == MASTERADMIN_LOGIN and password == MASTERADMIN_PASSWORD

 
  
        
        
    # Обработчик команды для получения списка админов
    

    

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
            if username == MASTERADMIN_LOGIN and password == MASTERADMIN_PASSWORD:
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
                ic(add_admin(new_username, new_password))
            else:
                bot.send_message(message.chat.id, "Не удалось добавить админа.")
                ic(new_username, new_password)
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль нового админа через пробел.")
            ic(ValueError)

    # Обработчик для команды добавления суперадмина
    @bot.message_handler(commands=['add_superadmin'])
    def add_superadmin_command(message):
        msg = bot.reply_to(message, "Введите логин и пароль через пробел для аутентификации:")
        bot.register_next_step_handler(msg, process_add_superadmin_login)
        
    def process_add_superadmin_login(message):
        try:
            username, password = message.text.split(' ', 1)
            
            if username == MASTERADMIN_LOGIN and password == MASTERADMIN_PASSWORD:
                ic(authenticate_admin(username, password))
                msg = bot.reply_to(message, "Введите логин и пароль нового суперадмина через пробел:")
                bot.register_next_step_handler(msg, process_add_superadmin_data)
            else:
                bot.send_message(message.chat.id, "Неверный логин или пароль.")
                ic(username, password, authenticate_admin(username, password))
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль через пробел.")
            ic(ValueError)

    def process_add_superadmin_data(message):
        try:
            new_username, new_password = message.text.split(' ', 1)
            if add_admin(new_username, new_password, is_superadmin=True):
                bot.send_message(message.chat.id, f"Суперадмин {new_username} успешно добавлен.")
            else:
                bot.send_message(message.chat.id, "Не удалось добавить суперадмина.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите логин и пароль нового суперадмина через пробел.")
            ic(ValueError)
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

