import pandas as pd
import numpy as np
from datetime import timedelta

def detect_regular_payments(df):
    print("Поиск регулярных платежей...")
    df_reg = df[df['counterparty_inn'].notna() & (df['counterparty_inn'] != '')].copy()
    
    groups = df_reg.groupby(['company_id', 'counterparty_inn', 'direction', 'category'])
    regular_events = []
    
    for name, group in groups:
        if len(group) < 3: continue
            
        group = group.sort_values('operation_date')
        diffs = group['operation_date'].diff().dt.days.dropna()
        
        if len(diffs) == 0: continue
        
        median_diff = diffs.median()
        
        if 25 <= median_diff <= 35 or 12 <= median_diff <= 16:
            if diffs.std() < 5:
                last_op = group.iloc[-1]
                next_date = last_op['operation_date'] + timedelta(days=int(median_diff))
                
                regular_events.append({
                    'company_id': name[0],
                    'counterparty_inn': name[1],
                    'direction': name[2],
                    'category': name[3],
                    'median_amount': group['amount'].median(),
                    'interval_days': median_diff,
                    'last_date': last_op['operation_date'],
                    'predicted_next_date': next_date,
                    'source': 'detected_regular'
                })
                
    return pd.DataFrame(regular_events)

def generate_tax_calendar(companies, start_date='2026-10-01', months=2):
    print("Генерация налогового календаря...")
    tax_events = []
    start = pd.to_datetime(start_date)
    
    for _, comp in companies.iterrows():
        company_id = comp['company_id']
        for m in range(months):
            tax_date = (start + pd.DateOffset(months=m)).replace(day=28)
            if tax_date.weekday() == 5: tax_date += timedelta(days=2)
            elif tax_date.weekday() == 6: tax_date += timedelta(days=1)
            
            tax_events.append({
                'company_id': company_id,
                'counterparty_inn': '0000000000',
                'direction': 'Списание',
                'category': 'Налоги',
                'median_amount': -1,
                'interval_days': 30,
                'last_date': pd.NaT,
                'predicted_next_date': tax_date,
                'source': 'tax_calendar'
            })
            
    return pd.DataFrame(tax_events)

if __name__ == "__main__":
    df = pd.read_pickle('categorized_operations.pkl')
    companies = pd.read_csv('companies.csv', sep=';', encoding='utf-8-sig')
    
    regular_df = detect_regular_payments(df)
    print(f"Найдено {len(regular_df)} регулярных событий.")
    
    tax_df = generate_tax_calendar(companies)
    print(f"Сгенерировано {len(tax_df)} налоговых событий.")
    
    future_events = pd.concat([regular_df, tax_df], ignore_index=True)
    
    future_events.to_pickle('future_events.pkl')
    # Оставим CSV на всякий случай, чтобы ты мог открыть в Excel и посмотреть глазами
    future_events.to_csv('future_events.csv', index=False) 
    
    print("События сохранены в future_events.pkl и future_events.csv")
    print(future_events.head())