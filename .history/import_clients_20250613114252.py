#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import csv
import os

def csv_to_sqlite(csv_path: str, db_path: str, table_name: str = "clients"):
    # Создаём папку под БД, если нужно
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Создаём таблицу (если ещё нет). Подберите типы под ваши данные.
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            first_name TEXT,
            last_name  TEXT,
            chat_id    INTEGER PRIMARY KEY
        );
    """)

    # Открываем CSV и собираем данные
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            first = row.get("First Name", "").strip() or None
            last  = row.get("Last Name", "").strip() or None
            chat  = row.get("Chat ID", "").strip()
            if not chat:
                continue
            rows.append((first, last, int(chat)))

    # Вставляем данные, пропуская дубликаты по chat_id
    cur.executemany(
        f"INSERT OR IGNORE INTO {table_name} (first_name, last_name, chat_id) VALUES (?, ?, ?);",
        rows
    )

    conn.commit()
    conn.close()
    print(f"Импортировано {len(rows)} записей в {db_path} таблицу {table_name}.")

if __name__ == "__main__":
    csv_file = "clients.csv"     # путь к вашему CSV
    sqlite_db = "data/clients.db"  # куда сохранить БД
    csv_to_sqlite(csv_file, sqlite_db)
