import argparse
import os
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Определение модели базы данных
Base = declarative_base()

class Admin(Base):
    __tablename__ = 'admins'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True)
    is_superadmin = Column(Boolean, default=False)

# Настройка аргументов командной строки
parser = argparse.ArgumentParser(description='Добавление админа в базу данных.')
parser.add_argument('-sa', '--superadmin', action='store_true', help='Добавить супер-админа')
args = parser.parse_args()

# Установка соединения с базой данных
DATABASE_URL = os.getenv('DATABASE_URL')
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
session = Session()

Base.metadata.create_all(engine)

def add_superadmin(username):
    # Проверка, существует ли уже супер-админ
    existing_admin = session.query(Admin).filter_by(username=username).first()
    if existing_admin:
        print(f"Супер-админ с именем {username} уже существует.")
        return False

    # Добавление нового супер-админа
    new_admin = Admin(username=username, is_superadmin=True)
    session.add(new_admin)
    session.commit()
    print(f"Супер-админ {username} успешно добавлен.")
    return True

if __name__ == '__main__':
    if args.superadmin:
        superadmin_username = os.getenv('MASTERADMIN_LOGIN')
        if superadmin_username:
            add_superadmin(superadmin_username)
        else:
            print("Необходимо установить переменную окружения MASTERADMIN_LOGIN.")
    else:
        print("Этот скрипт предназначен только для добавления супер-админа.")
