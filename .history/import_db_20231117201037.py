import sqlite3
import csv
import sys

def import_data(db_name, table_name, data_file):
    # Подключение к базе данных
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    # Чтение данных из файла
    with open(data_file, 'r') as file:
        dr = csv.DictReader(file)  # предполагается, что первая строка содержит заголовки
        to_db = [(i[field] for field in dr.fieldnames) for i in dr]

    # Вставка данных в таблицу
    columns = ', '.join(dr.fieldnames)
    placeholders = ', '.join('?' * len(dr.fieldnames))
    sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
    cursor.executemany(sql, to_db)

    # Фиксация изменений и закрытие соединения
    conn.commit()
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) == 4:
        db_name, table_name, data_file = sys.argv[1], sys.argv[2], sys.argv[3]
        import_data(db_name, table_name, data_file)
        print("Данные успешно импортированы.")
    else:
        print("Использование: script.py <имя_базы_данных> <имя_таблицы> <путь_к_файлу_данных>")
