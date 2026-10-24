import os
import traceback

import pandas as pd
from flask import Flask, render_template, request, jsonify, Response
from werkzeug.exceptions import HTTPException

from pipeline import run_pipeline
from forecast import forecast_company, load_external

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 МБ
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Данные последнего загруженного файла (для одного пользователя этого достаточно)
STATE = {'ops': None, 'events': None, 'external': None}


def name_of(row):
    n = getattr(row, 'counterparty_name', '')
    return n if isinstance(n, str) else ''


def need_data():
    return STATE['ops'] is None


def company_ops(company_id):
    ops = STATE['ops']
    return ops[ops['company_id'].astype(str) == str(company_id)]


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.csv'):
        return jsonify(error='Загрузите файл в формате .csv'), 400

    path = os.path.join(UPLOAD_DIR, 'current.csv')
    f.save(path)

    try:
        ops, events, notes = run_pipeline(path)
    except Exception as e:
        return jsonify(error=str(e)), 400

    STATE['ops'], STATE['events'], STATE['external'] = ops, events, None

    return jsonify(
        companies=[str(c) for c in sorted(ops['company_id'].unique())],
        notes=notes,
        operations=int(len(ops)),
        date_from=ops['operation_date'].min().strftime('%d.%m.%Y'),
        date_to=ops['operation_date'].max().strftime('%d.%m.%Y'),
    )


@app.route('/api/history')
def history():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400

    df = company_ops(request.args.get('company'))
    df = df[df['direction'] == 'Списание']

    category = request.args.get('category')
    if category:
        df = df[df['category'] == category]

    q = (request.args.get('q') or '').strip().lower()
    if q:
        names = df['counterparty_name'].fillna('') if 'counterparty_name' in df else ''
        df = df[df['payment_purpose'].str.lower().str.contains(q, regex=False)
                | df['counterparty_inn'].fillna('').str.contains(q, regex=False)
                | pd.Series(names, index=df.index).astype(str).str.lower().str.contains(q, regex=False)]

    categories = sorted(company_ops(request.args.get('company'))
                        .query("direction == 'Списание'")['category'].unique())

    df = df.sort_values('operation_date', ascending=False)
    per_page = 25
    page = max(int(request.args.get('page', 1)), 1)
    total = len(df)
    pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, pages)
    chunk = df.iloc[(page - 1) * per_page: page * per_page]

    rows = [{
        'date': r.operation_date.strftime('%d.%m.%Y'),
        'inn': r.counterparty_inn if isinstance(r.counterparty_inn, str) else '',
        'name': name_of(r),
        'category': r.category,
        'purpose': r.payment_purpose,
        'amount': float(r.amount),
    } for r in chunk.itertuples()]

    return jsonify(rows=rows, total=total, page=page, pages=pages,
                   sum=float(df['amount'].sum()), categories=categories)


@app.route('/api/stats')
def stats():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400

    df = company_ops(request.args.get('company')).copy()
    if df.empty:
        return jsonify(error='Нет данных по этой компании'), 404

    inc = df[df['direction'] == 'Поступление']
    out = df[df['direction'] == 'Списание']

    df['month'] = df['operation_date'].dt.strftime('%Y-%m')
    monthly = (df.pivot_table(index='month', columns='direction', values='amount',
                              aggfunc='sum', fill_value=0).sort_index())
    for col in ('Поступление', 'Списание'):
        if col not in monthly:
            monthly[col] = 0

    bal = (df.sort_values('operation_date', kind='stable')
             .groupby('operation_date')['balance_eod'].last())

    def by_cat(x):
        s = x.groupby('category')['amount'].sum().sort_values(ascending=False)
        return {'labels': list(s.index), 'values': [float(v) for v in s.values]}

    return jsonify(
        kpi=dict(income=float(inc['amount'].sum()),
                 outcome=float(out['amount'].sum()),
                 balance=float(bal.iloc[-1]),
                 operations=int(len(df))),
        monthly=dict(labels=list(monthly.index),
                     income=[float(v) for v in monthly['Поступление']],
                     outcome=[float(v) for v in monthly['Списание']]),
        balance=dict(labels=[d.strftime('%Y-%m-%d') for d in bal.index],
                     values=[float(v) for v in bal.values]),
        income_cat=by_cat(inc),
        outcome_cat=by_cat(out),
    )


TEMPLATE_CSV = (
    "company_id;due_date;amount;direction;type;description;probability\n"
    "C001;2026-10-05;150000;Поступление;счет;Оплата счета № 123 от ООО Ромашка;80\n"
    "C001;2026-10-10;45000;Списание;счет;Счет поставщика за запчасти;100\n"
    "C001;2026-10-28;256000;Списание;налог;Единый налоговый платеж;100\n"
)


@app.route('/api/external/template')
def external_template():
    return Response('\ufeff' + TEMPLATE_CSV, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=schet_i_nalogi_shablon.csv'})


@app.route('/api/external', methods=['POST'])
def external_upload():
    if need_data():
        return jsonify(error='Сначала загрузите файл с операциями'), 400
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.csv'):
        return jsonify(error='Загрузите файл в формате .csv'), 400
    path = os.path.join(UPLOAD_DIR, 'external.csv')
    f.save(path)
    try:
        STATE['external'] = load_external(path)
    except Exception as e:
        return jsonify(error=str(e)), 400
    ext = STATE['external']
    return jsonify(rows=int(len(ext)), taxes=int((ext['kind'] == 'налог').sum()),
                   invoices=int((ext['kind'] == 'счет').sum()))


@app.route('/api/forecast')
def forecast():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400
    res = forecast_company(
        STATE['ops'], STATE['events'], STATE['external'],
        request.args.get('company'),
        delay_days=max(int(request.args.get('delay', 0) or 0), 0),
        extra_expense=max(float(request.args.get('extra', 0) or 0), 0),
        extra_day=max(int(request.args.get('extra_day', 1) or 1), 1),
    )
    if res is None:
        return jsonify(error='Нет данных по этой компании'), 404
    return jsonify(res)


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify(error=f'{type(e).__name__}: {e}'), 500


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
