import os
import traceback

import pandas as pd
from datetime import timedelta

from flask import (Flask, render_template, request, jsonify, Response, send_file,
                   redirect, session)
from werkzeug.exceptions import HTTPException

from pipeline import run_pipeline
import auth
import db
import plan_events
from forecast import forecast_company, compare_scenario, plan_fact, advice, advice

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.secret_key = auth.secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))
auth.init()

# что можно открыть без входа
PUBLIC_ENDPOINTS = {'login_page', 'auth_login', 'auth_register', 'static'}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if session.get('user'):
        return None
    if request.path.startswith('/api/'):
        return jsonify(error='Нужно войти в систему', auth=True), 401
    return redirect('/login')
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# операции лежат в sqlite (business_forecast.db), тут только кэш, чтобы не читать базу каждый раз
CACHE = {'ops': None}


def get_ops():
    if CACHE['ops'] is None:
        CACHE['ops'] = db.load_operations()
    return CACHE['ops']


def name_of(row):
    n = getattr(row, 'counterparty_name', '')
    return n if isinstance(n, str) else ''


def log_error():
    with open('error.log', 'a', encoding='utf-8') as f:
        traceback.print_exc(file=f)
    # то же самое — в журнал, чтобы ошибки были видны на сайте
    last = traceback.format_exc().strip().splitlines()[-1]
    auth.log('error', f'{request.method} {request.path}: {last}', 'error')


def need_data():
    return get_ops() is None


def company_ops(company_id):
    ops = get_ops()
    return ops[ops['company_id'].astype(str) == str(company_id)]


@app.route('/')
def index():
    return render_template('index.html', user=auth.current_user())


# ---------- вход, регистрация, выход ----------

@app.route('/login')
def login_page():
    if session.get('user'):
        return redirect('/')
    return render_template('login.html', first_user=auth.user_count() == 0)


def _creds():
    data = request.get_json(silent=True) or {}
    return data.get('username', ''), data.get('password', '')


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    username, password = _creds()
    try:
        name, role = auth.login(username, password)
    except ValueError as e:
        return jsonify(error=str(e)), 401
    auth.start_session(name, role)
    auth.log('login', 'Вход в систему')
    return jsonify(ok=True, username=name, role=role)


@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    username, password = _creds()
    try:
        name, role = auth.register(username, password)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    auth.log('register', 'Создан аккаунт (владелец)', username=name)
    return jsonify(ok=True, username=name, role=role)


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    auth.log('logout', 'Выход из системы')
    session.clear()
    return jsonify(ok=True)


@app.route('/api/auth/me')
def auth_me():
    return jsonify(**auth.current_user())


@app.route('/api/db/download')
def db_download():
    if need_data():
        return jsonify(error='База пока пустая'), 400
    auth.log('db_download', 'Скачана база .db')
    return send_file(db.DB_PATH, as_attachment=True, download_name='business_forecast.db')


@app.route('/api/db/dump')
def db_dump():
    if need_data():
        return jsonify(error='База пока пустая'), 400
    auth.log('db_dump', 'Скачан дамп .sql')
    return Response(db.dump_sql(), mimetype='text/plain; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=business_forecast.sql'})


TEMPLATE_OPS_CSV = (
    "company_id;operation_date;amount;direction;payment_purpose;counterparty_inn;balance_eod\n"
    "C001;2026-09-01;250000;Поступление;Оплата по счету № 101 за услуги;7701234567;1250000\n"
    "C001;2026-09-03;85000;Списание;Оплата по договору аренды помещения за сентябрь;7709876543;1165000\n"
    "C001;2026-09-05;120000;Списание;Выплата заработной платы за август;;1045000\n"
    "C001;2026-09-10;60000;Списание;Оплата за товар по счету поставщика № 55;5001112223;985000\n"
    "C001;2026-09-15;180000;Поступление;Поступление по картам (эквайринг);;1165000\n"
    "C001;2026-09-28;40000;Списание;Единый налоговый платеж;7727406020;1125000\n"
)


@app.route('/api/template')
def ops_template():
    return Response('\ufeff' + TEMPLATE_OPS_CSV, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=shablon_operacij.csv'})


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
        log_error()
        auth.log('upload_failed', f'Файл «{f.filename}»: {type(e).__name__}: {e}', 'error')
        return jsonify(error=f'{type(e).__name__}: {e}'), 400

    db.save_operations(ops, events)
    CACHE['ops'] = ops
    auth.log('upload', f'Загружен файл «{f.filename}»: {len(ops)} операций, '
                       f'компаний: {ops["company_id"].nunique()}')

    return jsonify(
        companies=[str(c) for c in sorted(ops['company_id'].unique())],
        notes=notes,
        operations=int(len(ops)),
        date_from=ops['operation_date'].min().strftime('%d.%m.%Y'),
        date_to=ops['operation_date'].max().strftime('%d.%m.%Y'),
    )


@app.route('/api/state')
def state():
    ops = get_ops()
    if ops is None:
        return jsonify(has_data=False)
    return jsonify(has_data=True,
                   companies=[str(c) for c in sorted(ops['company_id'].unique())],
                   operations=int(len(ops)),
                   date_from=ops['operation_date'].min().strftime('%d.%m.%Y'),
                   date_to=ops['operation_date'].max().strftime('%d.%m.%Y'))


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


@app.route('/api/overview')
def overview():
    """Краткая сводка по компании для экрана выбора: цифры и последние списания."""
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400
    df = company_ops(request.args.get('company'))
    if df.empty:
        return jsonify(error='Нет данных по этой компании'), 404

    inc = df[df['direction'] == 'Поступление']
    out = df[df['direction'] == 'Списание']
    bal = (df.sort_values('operation_date', kind='stable')
             .groupby('operation_date')['balance_eod'].last())
    step = max(len(bal) // 60, 1)
    spark = [float(v) for v in bal.values[::step]]
    if spark[-1] != float(bal.iloc[-1]):
        spark.append(float(bal.iloc[-1]))

    last = out.sort_values('operation_date', ascending=False, kind='stable').head(4)
    recent = []
    for r in last.itertuples():
        who = name_of(r).strip() or str(r.payment_purpose or '').strip() or 'Без названия'
        recent.append({'date': r.operation_date.strftime('%d.%m'), 'who': who,
                       'category': r.category, 'amount': float(r.amount)})

    return jsonify(
        income=float(inc['amount'].sum()),
        outcome=float(out['amount'].sum()),
        balance=float(bal.iloc[-1]),
        out_count=int(len(out)),
        spark=spark,
        date_from=df['operation_date'].min().strftime('%d.%m.%Y'),
        date_to=df['operation_date'].max().strftime('%d.%m.%Y'),
        recent=recent,
    )


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

    def top_payers(x, n=5):
        if x.empty:
            return []
        names = x['counterparty_name'] if 'counterparty_name' in x else None
        inn = x['counterparty_inn'].fillna('').astype(str) if 'counterparty_inn' in x else ''
        if names is not None:
            key = names.where(names.notna() & (names.astype(str).str.strip() != ''), inn)
        else:
            key = inn
        key = key.astype(str).replace('', 'Без названия')
        g = (x.assign(_k=key).groupby('_k')['amount']
              .agg(['sum', 'count']).sort_values('sum', ascending=False))
        total = float(g['sum'].sum()) or 1.0
        return [dict(name=str(k), sum=float(r['sum']), count=int(r['count']),
                     share=float(r['sum']) / total * 100) for k, r in g.head(n).iterrows()]

    return jsonify(
        income_top=top_payers(inc),
        income_clients=int(inc['counterparty_inn'].nunique()) if 'counterparty_inn' in inc else 0,
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


def days_from_args():
    try:
        d = int(request.args.get('days', 30) or 30)
    except ValueError:
        d = 30
    return min(max(d, 30), 365)


def scenario_from_args():
    a = request.args
    return {
        'client_delay': max(int(a.get('client_delay', 0) or 0), 0),
        'revenue_drop': min(max(float(a.get('revenue_drop', 0) or 0), 0), 90) / 100,
        'cost_growth': max(float(a.get('cost_growth', 0) or 0), 0) / 100,
        'extra_expense': max(float(a.get('extra', 0) or 0), 0),
        'extra_day': max(int(a.get('extra_day', 1) or 1), 1),
    }


@app.route('/api/plan/template')
def plan_template():
    return Response('\ufeff' + plan_events.TEMPLATE_CSV, mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=plan_shablon.csv'})


@app.route('/api/plan')
def plan_list():
    plan = db.load_plan()
    cid = request.args.get('company')
    plan = plan[plan['company_id'].isna() | (plan['company_id'].astype(str) == str(cid))]
    plan = plan.sort_values('date')
    return jsonify(sources=plan_events.SOURCES, items=[{
        'id': int(r.id), 'date': r.date.strftime('%Y-%m-%d'), 'title': r.description,
        'kind': r.kind, 'direction': r.direction, 'amount': float(r.amount),
        'source': r.source, 'probability': None if pd.isna(r.probability) else float(r.probability),
    } for r in plan.itertuples()])


@app.route('/api/plan/upload', methods=['POST'])
def plan_upload():
    if need_data():
        return jsonify(error='Сначала загрузите файл с операциями'), 400
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.csv'):
        return jsonify(error='Загрузите файл в формате .csv'), 400
    source = request.form.get('source') or 'Другое'
    path = os.path.join(UPLOAD_DIR, 'plan.csv')
    f.save(path)
    try:
        rows = plan_events.parse_file(path, source)
    except Exception as e:
        auth.log('plan_upload_failed', f'Файл «{f.filename}»: {e}', 'warning')
        return jsonify(error=str(e)), 400
    db.add_plan(rows)
    auth.log('plan_upload', f'Файл «{f.filename}» ({source}): добавлено событий — {len(rows)}')
    return jsonify(added=int(len(rows)), source=source)


@app.route('/api/plan/add', methods=['POST'])
def plan_add():
    if need_data():
        return jsonify(error='Сначала загрузите файл с операциями'), 400
    try:
        rows = plan_events.manual_row(request.get_json(force=True))
    except Exception as e:
        return jsonify(error=str(e)), 400
    db.add_plan(rows)
    r = rows.iloc[0]
    auth.log('plan_add', f'{r["direction"]} {r["amount"]:.0f} ₽ на {r["date"]:%d.%m.%Y}: {r["description"]}')
    return jsonify(added=1)


@app.route('/api/plan/<int:event_id>', methods=['DELETE'])
def plan_delete(event_id):
    db.delete_plan(event_id)
    auth.log('plan_delete', f'Удалено событие плана № {event_id}')
    return jsonify(ok=True)


@app.route('/api/forecast')
def forecast():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400
    res = forecast_company(get_ops(), db.load_plan(), request.args.get('company'), days_from_args())
    if res is None:
        return jsonify(error='Нет данных по этой компании'), 404
    return jsonify(res)


@app.route('/api/scenario')
def scenario():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400
    days = days_from_args()
    res = compare_scenario(get_ops(), db.load_plan(), request.args.get('company'), scenario_from_args(), days)
    if res is None:
        return jsonify(error='Нет данных по этой компании'), 404
    return jsonify(res)


@app.route('/api/advice')
def advice_view():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400
    res = advice(get_ops(), db.load_plan(), request.args.get('company'), days_from_args())
    if res is None:
        return jsonify(error='Нет данных по этой компании'), 404
    return jsonify(res)


@app.route('/api/advice')
def advice_route():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400
    res = advice(get_ops(), db.load_plan(), request.args.get('company'), days_from_args())
    if res is None:
        return jsonify(error='Нет данных по этой компании'), 404
    return jsonify(res)


@app.route('/api/planfact')
def planfact():
    if need_data():
        return jsonify(error='Сначала загрузите файл'), 400
    res = plan_fact(get_ops(), db.load_plan(), request.args.get('company'))
    if res is None:
        return jsonify(error='Нет данных по этой компании'), 404
    return jsonify(res)


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    log_error()
    return jsonify(error=f'{type(e).__name__}: {e}'), 500


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
