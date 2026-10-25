import os
import sqlite3

import pandas as pd

from plan_events import COLUMNS

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'business_forecast.db')


def connect():
    return sqlite3.connect(DB_PATH)


def _has_table(conn, name):
    q = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    return conn.execute(q, (name,)).fetchone() is not None


def init_plan_table():
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS plan_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT, kind TEXT, direction TEXT, date TEXT,
            amount REAL, description TEXT, probability REAL, source TEXT)""")


def save_operations(ops, events):
    o = ops.copy()
    o['operation_date'] = o['operation_date'].dt.strftime('%Y-%m-%d')

    ev = events.copy()
    for col in ('last_date', 'predicted_next_date'):
        if col in ev:
            ev[col] = pd.to_datetime(ev[col]).dt.strftime('%Y-%m-%d')

    companies = pd.DataFrame({'company_id': sorted(ops['company_id'].astype(str).unique())})

    with connect() as conn:
        o.to_sql('operations', conn, index=False, if_exists='replace', chunksize=20000)
        ev.to_sql('future_events', conn, index=False, if_exists='replace')
        companies.to_sql('companies', conn, index=False, if_exists='replace')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_ops_company_date ON operations(company_id, operation_date)')
    init_plan_table()


def load_operations():
    if not os.path.exists(DB_PATH):
        return None
    with connect() as conn:
        if not _has_table(conn, 'operations'):
            return None
        return pd.read_sql('SELECT * FROM operations', conn, parse_dates=['operation_date'])


def load_plan():
    init_plan_table()
    with connect() as conn:
        df = pd.read_sql('SELECT * FROM plan_events', conn, parse_dates=['date'])
    return df[COLUMNS]


def add_plan(rows):
    r = rows.copy()
    r['date'] = r['date'].dt.strftime('%Y-%m-%d')
    r = r.drop(columns=['id'])
    r = r.astype(object).where(r.notna(), None)
    init_plan_table()
    with connect() as conn:
        conn.executemany(
            """INSERT INTO plan_events
               (company_id, kind, direction, date, amount, description, probability, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            r[['company_id', 'kind', 'direction', 'date', 'amount',
               'description', 'probability', 'source']].values.tolist())


def delete_plan(event_id):
    init_plan_table()
    with connect() as conn:
        conn.execute('DELETE FROM plan_events WHERE id = ?', (event_id,))


def dump_sql():
    with connect() as conn:
        for line in conn.iterdump():
            yield line + '\n'
