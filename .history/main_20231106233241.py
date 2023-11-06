import telebot
import os
# from bot import setup_bot_handlers
from dotenv import load_dotenv
from kb import generate_contact_keyboard

load_dotenv()

def main():
    token = os.getenv('TELEGRAM_API_TOKEN')
    SUPERADMIN_LOGIN = os.getenv('SUPERADMIN_LOGIN')
    SUPERADMIN_PASSWORD = os.getenv('SUPERADMIN_PASSWORD')
    bot = telebot.TeleBot(token)
    setup_bot_handlers(bot)
    bot.polling(non_stop=True)
    return token, SUPERADMIN_LOGIN, SUPERADMIN_PASSWORD

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
        login, password = message.text.split()
        if login == su_login and password == su_pwd:
            bot.send_message(message.chat.id, "Аутентификация успешна!", reply_markup=generate_admin_keyboard(True))
        else:
            bot.send_message(message.chat.id, "Неверный логин или пароль.")
    except Exception as e:
        bot.reply_to(message, "Ошибка ввода. Пожалуйста, введите логин и пароль через пробел.")
    
if __name__ == '__main__':
    main()
    token, su_login, su_pwd = main()
