import pandas as pd

from step2_categorize import categorize_operations
from step3_regularity import detect_regular_payments, generate_tax_calendar

REQUIRED = {'company_id', 'operation_date', 'amount', 'direction',
            'payment_purpose', 'counterparty_inn'}


def _parse_dates(s):
    s = s.astype(str).str.strip()
    if s.str.match(r'^\d{4}-\d{2}-\d{2}').all():
        return pd.to_datetime(s)
    return pd.to_datetime(s, dayfirst=True)  # дд.мм.гггг


def run_pipeline(path):
    notes = []
    df = pd.read_csv(path, sep=None, engine='python', encoding='utf-8-sig',
                     dtype={'counterparty_inn': str})

    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError('В файле не хватает колонок: ' + ', '.join(sorted(missing)))

    # очистка
    df['operation_date'] = _parse_dates(df['operation_date']).dt.normalize()
    df = df.drop_duplicates()
    df['payment_purpose'] = df['payment_purpose'].fillna('')

    # чтобы последний остаток дня был настоящим последним
    sort_cols = ['company_id', 'operation_date'] + (['operation_id'] if 'operation_id' in df.columns else [])
    df = df.sort_values(sort_cols, kind='stable').reset_index(drop=True)
    df['amount'] = (df['amount'].astype(str)
                    .str.replace('\xa0', '', regex=False)
                    .str.replace(' ', '', regex=False)
                    .str.replace(',', '.', regex=False)
                    .astype(float))

    # если остатка нет, считаем от нуля
    if 'balance_eod' not in df.columns:
        df = df.sort_values(['company_id', 'operation_date'], kind='stable')
        signed = df['amount'].where(df['direction'] == 'Поступление', -df['amount'])
        df['balance_eod'] = signed.groupby(df['company_id']).cumsum()
        notes.append('В файле нет колонки balance_eod, остаток посчитан от нуля '
                     'и может отличаться от реального.')

    # категории
    df = categorize_operations(df)

    # регулярные платежи и налоги
    companies = pd.DataFrame({'company_id': df['company_id'].unique()})
    regular = detect_regular_payments(df)

    # налоги от последней даты в данных
    tax_start = df['operation_date'].max().replace(day=1).strftime('%Y-%m-%d')
    taxes = generate_tax_calendar(companies, start_date=tax_start, months=3)

    events = pd.concat([regular, taxes], ignore_index=True)
    return df, events, notes
