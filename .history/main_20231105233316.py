import telebot
import os
from bot import setup_bot_handlers

def main():
    token = os.getenv('TELEGRAM_API_TOKEN')
    bot = telebot.TeleBot(token)
    setup_bot_handlers(bot)
    bot.polling()

if __name__ == '__main__':
    main()
