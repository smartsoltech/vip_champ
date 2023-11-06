import telebot
import os
# from bot import setup_bot_handlers
from dotenv import load_dotenv

load_dotenv()

def main():
    token = os.getenv('TELEGRAM_API_TOKEN')
    bot = telebot.TeleBot(token)
    # setup_bot_handlers(bot)
    bot.polling(non_stop=True)

if __name__ == '__main__':
    main()
