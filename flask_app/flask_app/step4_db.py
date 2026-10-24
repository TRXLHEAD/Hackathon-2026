import pandas as pd
import sqlite3
import os

def create_database():
    print("Создание базы данных SQLite...")
    db_path = 'business_forecast.db'
    
    # Если база есть, удаляем её для чистоты эксперимента
    if os.path.exists(db_path):
        os.remove(db_path)
        
    conn = sqlite3.connect(db_path)
    
    # 1. Загружаем исторические операции
    print("Загрузка операций...")
    df_ops = pd.read_pickle('categorized_operations.pkl')
    # Переводим даты в строку для SQLite
    df_ops['operation_date'] = df_ops['operation_date'].dt.strftime('%Y-%m-%d')
    df_ops.to_sql('operations', conn, index=False, if_exists='replace')
    
    # 2. Загружаем будущие события
    print("Загрузка будущих событий...")
    df_events = pd.read_pickle('future_events.pkl')
    df_events['predicted_next_date'] = df_events['predicted_next_date'].dt.strftime('%Y-%m-%d')
    if 'last_date' in df_events.columns:
        # Обрабатываем NaT (пустые даты)
        df_events['last_date'] = df_events['last_date'].dt.strftime('%Y-%m-%d').fillna('')
    df_events.to_sql('future_events', conn, index=False, if_exists='replace')
    
    # 3. Загружаем справочник компаний
    print("Загрузка справочника компаний...")
    df_comp = pd.read_csv('companies.csv', sep=';', encoding='utf-8-sig')
    df_comp.to_sql('companies', conn, index=False, if_exists='replace')
    
    # Проверка
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM operations")
    print(f"В БД загружено операций: {cursor.fetchone()[0]}")
    
    conn.close()
    print("База данных успешно создана: business_forecast.db")

if __name__ == "__main__":
    create_database()