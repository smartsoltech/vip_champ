# db.py

from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import IntegrityError
from contextlib import contextmanager
import os

DATABASE_URL = "sqlite:///./db/settings.db"  # Используйте ваш путь к файлу базы данных
Base = declarative_base()

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

def get_admin(username):
    return session.query(Admin).filter_by(username=username).first()

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