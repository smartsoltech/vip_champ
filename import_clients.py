#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import csv
import os

# Путь к входному CSV-файлу и к файлу SQLite
CSV_FILE = "clients.csv"
DB_FILE  = "./db/settings.db"
TABLE    = "clients"


def csv_to_sqlite(csv_path: str, db_path: str, table_name: str = TABLE):
    # Создаём директорию под БД, если её нет
    db_dir = os.path.dirname(db_path) or "."
    os.makedirs(db_dir, exist_ok=True)

    # Подключаемся к SQLite
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    # Создаём таблицу, если ещё не существует
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            first_name TEXT,
            last_name  TEXT,
            chat_id    INTEGER PRIMARY KEY
        );
    """ )

    rows = []
    seen = set()  # для проверки дубликатов в CSV

    # Открываем CSV с utf-8-sig, чтобы убрать BOM, если он есть
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Диагностика заголовков (можно убрать после проверки)
        print("DEBUG: detected columns:", reader.fieldnames)

        for row in reader:
            first = row.get("First Name", "").strip() or None
            last  = row.get("Last Name", "").strip()  or None
            chat  = row.get("Chat ID", "").strip()

            # Пробуем преобразовать chat_id к целому, допускаем отрицательные числа
            try:
                chat_id = int(chat)
            except (ValueError, TypeError):
                print(f"Skipping invalid chat_id: {chat!r}")
                continue

            # Проверка дубликатов внутри CSV
            if chat_id in seen:
                print(f"Skipping duplicate chat_id in CSV: {chat_id}")
                continue
            seen.add(chat_id)

            rows.append((first, last, chat_id))

    # Вставляем все строки, игнорируя дубликаты в БД по chat_id
    cur.executemany(
        f"INSERT OR IGNORE INTO {table_name} (first_name, last_name, chat_id) VALUES (?, ?, ?);",
        rows
    )

    conn.commit()
    conn.close()
    print(f"Imported {len(rows)} unique records into '{db_path}', table '{table_name}'.")


if __name__ == "__main__":
    if not os.path.exists(CSV_FILE):
        print(f"Error: CSV file '{CSV_FILE}' not found.")
        exit(1)
    csv_to_sqlite(CSV_FILE, DB_FILE)
