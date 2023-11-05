import sqlite3

def save_message(user_id, text):
    conn = sqlite3.connect('db/messages.db')
    c = conn.cursor()
    # Предполагается, что таблица уже создана с полями id, user_id, text
    c.execute("INSERT INTO messages (user_id, text) VALUES (?, ?)", (user_id, text))
    conn.commit()
    conn.close()
