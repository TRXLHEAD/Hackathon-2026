import pandas as pd
import numpy as np

def load_and_clean_data(filepath='all_operations.csv', comp_path='companies.csv'):
    print("Загрузка данных...")
    # Читаем ИНН как строку, чтобы не потерять нули
    df = pd.read_csv(filepath, sep=';', encoding='utf-8-sig', dtype={'counterparty_inn': str})
    companies = pd.read_csv(comp_path, sep=';', encoding='utf-8-sig')
    
    # Конвертация дат
    df['operation_date'] = pd.to_datetime(df['operation_date'])
    
    # Очистка от полных дублей
    initial_len = len(df)
    df = df.drop_duplicates()
    print(f"Удалено дублей: {initial_len - len(df)}")
    
    # Заполняем пустые назначения платежа
    df['payment_purpose'] = df['payment_purpose'].fillna('')
    
    # Базовый парсинг: приводим сумму к float
    df['amount'] = df['amount'].astype(float)
    
    return df, companies

if __name__ == "__main__":
    df, companies = load_and_clean_data()
    print(f"Загружено {len(df)} операций по {df['company_id'].nunique()} компаниям.")
    # Сохраняем промежуточный результат в pickle
    df.to_pickle('clean_operations.pkl') 
    print("Данные сохранены в clean_operations.pkl")