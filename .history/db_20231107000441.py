# db.py

from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:///./db/settings.db"  # Используйте ваш путь к файлу базы данных
Base = declarative_base()

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