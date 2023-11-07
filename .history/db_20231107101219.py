# db.py
import csv
from io import StringIO
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, joinedload
from sqlalchemy.exc import IntegrityError
from contextlib import contextmanager
import os

DATABASE_URL = "sqlite:///./db/settings.db" # Используйте ваш путь к файлу базы данных
engine = create_engine(DATABASE_URL)
Base = declarative_base()
session_factory = sessionmaker(bind=engine)
Session = scoped_session(session_factory)


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
        clients = session.query(Client.first_name, Client.chat_id).filter_by(is_bot=False).all()
        return [{'first_name': client.first_name, 'chat_id': client.chat_id} for client in clients]
    
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
    username = Column(String, unique=True)
    is_superadmin = Column(Boolean, default=False)

# Создаем все таблицы в базе данных
Base.metadata.create_all(engine)

# Функции для работы с администраторами
def add_admin(username, is_superadmin=False):
    new_admin = Admin(username=username, is_superadmin=is_superadmin)
    session.add(new_admin)
    try:
        session.commit()
        return True
    except:
        session.rollback()
        return False

def get_admin():
    admins = session.query(Admin).all()
    return [{'id': admin.id, 'username': admin.username, 'is_superadmin': admin.is_superadmin} for admin in admins]

def authenticate_admin(username):
    admin = get_admin(username)
    return admin is not None

def authenticate_super_admin(username):
    admin = get_admin(username)
    return admin and admin.is_superadmin

def remove_admin(username):
    admin = get_admin(username)
    if admin:
        session.delete(admin)
        session.commit()
        return True
    return False

# def export_clients_to_csv():
#     # Замените следующую строку на запрос к вашей базе данных для получения данных клиентов
#     clients = session.query(Client).all()  # Это пример, используйте вашу сессию и модель
#     output = StringIO()
#     writer = csv.writer(output)

#     # Запись заголовков CSV
#     writer.writerow(['ID', 'First Name', 'Last Name', 'Chat ID'])

#     # Запись данных клиентов
#     for client in clients:
#         writer.writerow([client.id, client.first_name, client.last_name, client.chat_id])

#     output.seek(0)
#     return output

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
