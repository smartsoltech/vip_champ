import argparse
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from your_model_file import Base, Admin  # Замените на ваш файл модели
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка аргументов командной строки
parser = argparse.ArgumentParser(description='Добавление админа в базу данных.')
parser.add_argument('-sa', '--superadmin', action='store_true', help='Добавить супер-админа')
parser.add_argument('-env', '--fromenv', action='store_true', help='Использовать переменные окружения для имени пользователя и пароля')

args = parser.parse_args()

# Установка соединения с базой данных
DATABASE_URL = os.getenv('DATABASE_URL')
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
session = Session()

Base.metadata.create_all(engine)

def add_admin(username, password, is_superadmin=False):
    # Проверка, существует ли уже админ
    existing_admin = session.query(Admin).filter_by(username=username).first()
    if existing_admin:
        print(f"Админ с именем {username} уже существует.")
        return False

    # Добавление нового админа
    new_admin = Admin(username=username, password=password, is_superadmin=is_superadmin)
    session.add(new_admin)
    session.commit()
    print(f"Админ {username} успешно добавлен.")
    return True

if __name__ == '__main__':
    if args.fromenv:
        username = os.getenv('MASTERADMIN_USERNAME')
        password = os.getenv('MASTERADMIN_PASSWORD')
        if not username or not password:
            print("Необходимо установить переменные окружения MASTERADMIN_USERNAME и MASTERADMIN_PASSWORD.")
            exit(1)
    else:
        username = input("Введите имя пользователя: ")
        password = input("Введите пароль: ")

    if args.superadmin:
        add_admin(username, password, is_superadmin=True)
    else:
        add_admin(username, password)
