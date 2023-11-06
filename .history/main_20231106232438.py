import telebot
import os
from bot import setup_bot_handlers
from dotenv import load_dotenv

load_dotenv()

def main():
    token = os.getenv('TELEGRAM_API_TOKEN')
    SUPERADMIN_LOGIN = os.getenv('SUPERADMIN_LOGIN')
    SUPERADMIN_PASSWORD = os.getenv('SUPERADMIN_PASSWORD')
    bot = telebot.TeleBot(token)
    setup_bot_handlers(bot)
    bot.polling(non_stop=True)
    return token, SUPERADMIN_LOGIN, SUPERADMIN_PASSWORD

if __name__ == '__main__':
    main()
    token, su_login, su_pwd = main()
