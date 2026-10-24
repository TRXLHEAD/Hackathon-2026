import pandas as pd
import numpy as np
from datetime import timedelta
from prophet import Prophet
import warnings
warnings.filterwarnings('ignore')

df_ops = pd.read_pickle('categorized_operations.pkl')
df_events = pd.read_pickle('future_events.pkl')

def get_ml_forecast(comp_id):
    try:
        df_in = df_ops[(df_ops['company_id'] == comp_id) & (df_ops['direction'] == 'Поступление')]
        if df_in['operation_date'].nunique() < 14: 
            return pd.DataFrame({'ds': pd.date_range(start=pd.to_datetime('2026-10-01'), periods=60), 'yhat': 0, 'yhat_lower': 0, 'yhat_upper': 0})
        daily_in = df_in.groupby('operation_date')['amount'].sum().reset_index()
        daily_in.columns = ['ds', 'y']
        all_dates = pd.date_range(start=daily_in['ds'].min(), end=daily_in['ds'].max())
        daily_in = daily_in.set_index('ds').reindex(all_dates, fill_value=0).reset_index()
        daily_in.columns = ['ds', 'y']
        m = Prophet(yearly_seasonality=False, weekly_seasonality=True, daily_seasonality=False)
        m.fit(daily_in)
        future = m.make_future_dataframe(periods=60)
        forecast = m.predict(future)
        last_hist = daily_in['ds'].max()
        fc = forecast[forecast['ds'] > last_hist][['ds', 'yhat', 'yhat_lower', 'yhat_upper']].copy()
        for col in ['yhat', 'yhat_lower', 'yhat_upper']: fc[col] = fc[col].clip(lower=0)
        return fc
    except Exception:
        return pd.DataFrame({'ds': pd.date_range(start=pd.to_datetime('2026-10-01'), periods=60), 'yhat': 0, 'yhat_lower': 0, 'yhat_upper': 0})

def generate_forecast(company_id, delay_days, extra_expense):
    comp_ops = df_ops[df_ops['company_id'] == company_id].sort_values('operation_date', kind='stable')
    if comp_ops.empty: return None
    
    last_date = comp_ops['operation_date'].max()
    last_balance = comp_ops[comp_ops['operation_date'] == last_date]['balance_eod'].iloc[-1]
    ml_fc = get_ml_forecast(company_id).copy()
    comp_events = df_events[df_events['company_id'] == company_id].copy()
    
    three_m = last_date - pd.DateOffset(months=3)
    recent_taxes = comp_ops[(comp_ops['category'] == 'Налоги') & (comp_ops['operation_date'] >= three_m)]
    avg_tax = recent_taxes['amount'].median() if not recent_taxes.empty else 0
    comp_events.loc[comp_events['median_amount'] == -1, 'median_amount'] = avg_tax

    if delay_days > 0:
        ml_fc['ds'] = ml_fc['ds'] + timedelta(days=delay_days)
        ml_fc = ml_fc.groupby('ds').sum().reset_index()
        
    if extra_expense > 0:
        comp_events = pd.concat([comp_events, pd.DataFrame([{
            'direction': 'Списание', 'category': 'Внеплановый расход',
            'median_amount': extra_expense, 'predicted_next_date': last_date + timedelta(days=1)
        }])], ignore_index=True)

    forecast_dates = pd.date_range(start=last_date + timedelta(days=1), periods=30)
    bal_mid, bal_low, bal_high = last_balance, last_balance, last_balance
    forecast_data = []
    
    for date in forecast_dates:
        daily_out = comp_events[(comp_events['direction'] == 'Списание') & (comp_events['predicted_next_date'] == date)]['median_amount'].sum()
        ml_row = ml_fc[ml_fc['ds'] == date]
        
        in_mid = ml_row['yhat'].values[0] if not ml_row.empty else 0
        in_low = ml_row['yhat_lower'].values[0] if not ml_row.empty else 0
        in_high = ml_row['yhat_upper'].values[0] if not ml_row.empty else 0
            
        daily_in_manual = comp_events[(comp_events['direction'] == 'Поступление') & (comp_events['predicted_next_date'] == date)]['median_amount'].sum()
        
        bal_mid = bal_mid + in_mid + daily_in_manual - daily_out
        bal_low = bal_low + in_low + daily_in_manual - daily_out
        bal_high = bal_high + in_high + daily_in_manual - daily_out
        
        forecast_data.append({
            'date': date.strftime('%Y-%m-%d'),
            'balance_mid': bal_mid,
            'balance_low': bal_low,
            'balance_high': bal_high
        })
        
    prob = 0
    if any(d['balance_high'] < 0 for d in forecast_data): prob = 99
    elif any(d['balance_mid'] < 0 for d in forecast_data): prob = 85
    elif any(d['balance_low'] < 0 for d in forecast_data): prob = 50
        
    return {'data': forecast_data, 'prob': prob, 'last_balance': last_balance}