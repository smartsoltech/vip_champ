# db.py
import csv
from io import StringIO
import sqlalchemy
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, joinedload
from sqlalchemy.exc import IntegrityError
from contextlib import contextmanager
import os
from werkzeug.security import generate_password_hash, check_password_hash
from icecream import ic
import shutil
import datetime

DATABASE_URL = "sqlite:///./db/settings.db" # Используйте ваш путь к файлу базы данных
engine = create_engine(DATABASE_URL)
Base = declarative_base()
session_factory = sessionmaker(bind=engine)
Session = scoped_session(session_factory)

def setup_icecream():
    ic.configureOutput(includeContext=True)
    
class Client(Base):
    __tablename__ = 'clients'
    id = Column(Integer, primary_key=True)
    first_name = Column(String)
    last_name = Column(String)
    chat_id = Column(Integer, unique=True)
    is_bot = Column(Boolean, default=False)

@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()

def init_db():
    Base.metadata.create_all(engine)
    
def get_all_clients():
    with session_scope() as session:
        clients = session.query(Client.first_name, Client.last_name, Client.chat_id).filter_by(is_bot=False).all()
        return [{'first_name': client.first_name, 'last_name':client.last_name, 'chat_id': client.chat_id} for client in clients]
    
def get_or_create_client(chat_id, first_name, last_name, is_bot):
    with session_scope() as session:
        client = session.query(Client).filter_by(chat_id=chat_id).first()
        if not client and not is_bot:
            client = Client(chat_id=chat_id, first_name=first_name, last_name=last_name, is_bot=is_bot)
            session.add(client)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()  # Rollback the failed transaction
                client = session.query(Client).filter_by(chat_id=chat_id).first()  # Try to fetch again
        return client
class Setting(Base):
    __tablename__ = 'settings'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    value = Column(String)

# Создаем движок базы данных
engine = create_engine(DATABASE_URL)
# Создаем все таблицы в базе данных
Base.metadata.create_all(engine)
# Создаем сессию для работы с базой данных
Session = sessionmaker(bind=engine)
session = Session()

# Функции для работы с настройками
def get_setting(name):
    instance = session.query(Setting).filter_by(name=name).first()
    return instance.value if instance else None

def get_settings():
    settings = session.query(Setting).all()
    return [{'name': setting.name, 'value': setting.value} for setting in settings] if settings else None

def set_setting(name, value):
    instance = session.query(Setting).filter_by(name=name).first()
    if instance:
        instance.value = value
    else:
        new_setting = Setting(name=name, value=value)
        session.add(new_setting)
    session.commit()

class Admin(Base):
    __tablename__ = 'admins'
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    is_superadmin = Column(Boolean, default=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
# Создаем все таблицы в базе данных
Base.metadata.create_all(engine)

# Функции для работы с администраторами
# def add_admin(username, password, is_superadmin=False):
#     new_admin = Admin(username=username, is_superadmin=is_superadmin)
#     new_admin.set_password(password)
#     session.add(new_admin)
#     session.commit()
    
def get_admin(usename):
    admins = session.query(Admin).all()
    return [{'id': admin.id, 'username': admin.username, 'is_superadmin': admin.is_superadmin} for admin in admins]


def get_all_admin():
    admins = session.query(Admin).all()
    return [{'id': admin.id, 'username': admin.username, 'is_superadmin': admin.is_superadmin} for admin in admins]

def add_admin(username, password_hash, is_superadmin=False):
    session = Session()
    # Проверяем, существует ли уже админ с таким именем пользователя
    existing_admin = session.query(Admin).filter_by(username=username).first()
    if existing_admin is not None:
        # Администратор с таким именем пользователя уже существует
        session.close()
        return False

    # Если администратора с таким именем пользователя нет, добавляем нового
    new_admin = Admin(username=username, password_hash=generate_password_hash(password_hash), is_superadmin=is_superadmin)
    session.add(new_admin)
    try:
        session.commit()
        return True
    except sqlalchemy.exc.IntegrityError as e:
        # Обрабатываем возможные ошибки целостности базы данных
        session.rollback()
        return False
    finally:
        session.close()

def authenticate_admin(username, password):
    admin = session.query(Admin).filter_by(username=username).first()
    if admin and admin.check_password(password):
        return True
    ic(password, username)
    return False

def authenticate_super_admin(username):
    admin = get_admin()
    return admin and admin.is_superadmin


def remove_admin(username):
    session = Session()
    # Получаем экземпляр администратора по имени пользователя
    admin = session.query(Admin).filter_by(username=username).first()
    if admin:
        try:
            session.delete(admin)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            print(f"Ошибка при удалении администратора: {e}")
            return False
    else:
        # Администратор с таким именем пользователя не найден
        return False


def export_clients_to_csv(file_path='clients.csv'):
    clients = session.query(Client).all()
    with open(file_path, mode='w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['ID', 'First Name', 'Last Name', 'Chat ID'])
        for client in clients:
            writer.writerow([client.id, client.first_name, client.last_name, client.chat_id])

# Функция для проверки, является ли пользователь администратором
def is_admin(user_id):
    # Замените следующую строку на проверку, является ли user_id администратором в вашей системе
    return session.query(Admin).filter(Admin.chat_id == user_id).first() is not None

def process_csv(file_path):
    with open(file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            # Очистка и обработка данных
            chat_id = int(row['id'])
            nickname = row['nickname']
            first_name, last_name = parse_nickname(nickname)

            # Проверка на дубликаты и добавление в БД
            if not session.query(Client).filter(Client.chat_id == chat_id).first():
                new_client = Client(chat_id=chat_id, first_name=first_name, last_name=last_name)
                session.add(new_client)

        session.commit()

def parse_nickname(nickname):
    # Разбивка никнейма на имя и фамилию, если это возможно
    parts = nickname.split()
    first_name = parts[0] if parts else ''
    last_name = parts[1] if len(parts) > 1 else ''
    return first_name, last_name

def add_client_from_csv_row(row):
    chat_id = int(row['id'])
    nickname = row['nickname']
    first_name, last_name = parse_nickname(nickname)

    existing_client = session.query(Client).filter(Client.chat_id == chat_id).first()
    if not existing_client:
        new_client = Client(chat_id=chat_id, first_name=first_name, last_name=last_name)
        session.add(new_client)
        session.commit()
        return f"Клиент {first_name} {last_name} (ID: {chat_id}) добавлен."
    else:
        return f"Клиент {first_name} {last_name} (ID: {chat_id}) уже существует."

def backup_database():
    current_time = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_file_name = f"backup_{current_time}.db"
    try:
        shutil.copyfile('your_database.db', backup_file_name)
        print(f"Бэкап базы данных создан: {backup_file_name}")
    except Exception as e:
        print(f"Ошибка при создании бэкапа: {e}")