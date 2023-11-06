from models import Admin, Setting, Session
from werkzeug.security import generate_password_hash, check_password_hash

def add_admin(username, password, role='admin'):
    session = Session()
    hashed_password = generate_password_hash(password)
    new_admin = Admin(username=username, role=role, password=hashed_password)
    session.add(new_admin)
    session.commit()
    session.close()

def authenticate_admin(username, password):
    session = Session()
    admin = session.query(Admin).filter_by(username=username).first()
    if admin and check_password_hash(admin.password, password):
        session.close()
        return admin
    session.close()
    return None

def change_admin(username, new_username, new_password):
    session = Session()
    admin = session.query(Admin).filter_by(username=username).first()
    if admin:
        admin.username = new_username
        admin.password = generate_password_hash(new_password)
        session.commit()
    session.close()

def get_setting(key):
    session = Session()
    setting = session.query(Setting).filter_by(key=key).first()
    session.close()
    return setting.value if setting else None

def set_setting(key, value):
    session = Session()
    setting = session.query(Setting).filter_by(key=key).first()
    if setting:
        setting.value = value
    else:
        setting = Setting(key=key, value=value)
        session.add(setting)
    session.commit()
    session.close()
