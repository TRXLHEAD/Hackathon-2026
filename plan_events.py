import re

import numpy as np
import pandas as pd

IN, OUT = 'Поступление', 'Списание'
COLUMNS = ['id', 'company_id', 'kind', 'direction', 'date', 'amount',
           'description', 'probability', 'source']
SOURCES = ['Банк', '1С', 'ЭДО', 'Онлайн-касса', 'Маркетплейс', 'Другое']

TEMPLATE_CSV = (
    "company_id;type;direction;amount;issue_date;due_date;payment_term_days;description;probability;"
    "parts;interval_days;prepay_percent;deferral_days\n"
    "C001;счет;Поступление;150000;2026-09-25;2026-10-05;;Оплата счета № 123;80;;;;\n"
    "C001;счет;Списание;45000;2026-09-28;;14;Счет поставщика за запчасти;100;;;;\n"
    "C001;налог;Списание;256000;;2026-10-28;;Единый налоговый платеж;100;;;;\n"
    "C001;договор;Поступление;900000;;2026-10-10;;Договор с ООО Ромашка;85;3;30;30;14\n"
    "C001;кредит;Списание;120000;;2026-10-15;;Платеж по кредиту (график банка);100;;;;\n"
)


def _date(v):
    if v is None or (isinstance(v, float) and np.isnan(v)) or str(v).strip() in ('', 'nan', 'NaT'):
        return pd.NaT
    v = str(v).strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}', v):
        return pd.Timestamp(v).normalize()
    return pd.to_datetime(v, dayfirst=True).normalize()


def _num(v, default=None):
    if v is None or str(v).strip() in ('', 'nan'):
        return default
    return float(str(v).replace('\xa0', '').replace(' ', '').replace(',', '.'))


def _kind(text):
    t = str(text).lower()
    for key, name in (('налог', 'налог'), ('договор', 'договор'), ('кредит', 'кредит'),
                      ('зарплат', 'зарплата'), ('поруч', 'платёжное поручение')):
        if key in t:
            return name
    return 'счёт'


def _prob(v):
    p = _num(v)
    if p is None:
        return np.nan
    return p / 100 if p > 1 else p


def parse_file(path, source):
    df = pd.read_csv(path, sep=None, engine='python', encoding='utf-8-sig', dtype=str)
    missing = {'amount', 'direction'} - set(df.columns)
    if missing:
        raise ValueError('В файле не хватает колонок: ' + ', '.join(sorted(missing)))
    if 'due_date' not in df and not {'issue_date', 'payment_term_days'} <= set(df.columns):
        raise ValueError('Нужна колонка due_date (срок оплаты) или пара issue_date + payment_term_days')

    rows = []
    for n, r in enumerate(df.to_dict('records'), start=2):
        try:
            direction = str(r['direction']).strip()
            if direction not in (IN, OUT):
                raise ValueError('direction должен быть «Поступление» или «Списание»')
            amount = _num(r['amount'])
            if amount is None or amount <= 0:
                raise ValueError('не указана сумма')

            due = _date(r.get('due_date'))
            if pd.isna(due):
                issue = _date(r.get('issue_date'))
                term = _num(r.get('payment_term_days'), 0)
                if pd.isna(issue):
                    raise ValueError('не указан срок оплаты')
                due = issue + pd.Timedelta(days=int(term))

            kind = _kind(r.get('type', ''))
            company = r.get('company_id')
            company = None if company is None or str(company).strip() in ('', 'nan') else str(company).strip()
            desc = r.get('description')
            desc = '' if desc is None or str(desc) == 'nan' else str(desc)
            prob = _prob(r.get('probability'))

            base = dict(company_id=company, direction=direction, probability=prob, source=source)

            if kind == 'договор':
                parts = int(_num(r.get('parts'), 1))
                interval = int(_num(r.get('interval_days'), 30))
                prepay = (_num(r.get('prepay_percent'), 0) or 0) / 100
                deferral = int(_num(r.get('deferral_days'), 0))
                title = desc or 'Договор'
                if prepay > 0:
                    rows.append({**base, 'kind': kind, 'date': due, 'amount': amount * prepay,
                                 'description': f'{title}: предоплата'})
                rest = amount * (1 - prepay)
                first = due + pd.Timedelta(days=deferral)
                for i in range(parts):
                    rows.append({**base, 'kind': kind, 'date': first + pd.Timedelta(days=interval * i),
                                 'amount': rest / parts,
                                 'description': f'{title}: платёж {i + 1} из {parts}'})
            else:
                rows.append({**base, 'kind': kind, 'date': due, 'amount': amount,
                             'description': desc or kind.capitalize()})
        except Exception as e:
            raise ValueError(f'Строка {n}: {e}')

    out = pd.DataFrame(rows)
    out['id'] = np.nan
    return out[COLUMNS]


def manual_row(data):
    direction = data.get('direction')
    if direction not in (IN, OUT):
        raise ValueError('Выберите, это поступление или списание')
    amount = _num(data.get('amount'))
    if amount is None or amount <= 0:
        raise ValueError('Введите сумму больше нуля')
    date = _date(data.get('date'))
    if pd.isna(date):
        raise ValueError('Укажите дату')
    company = data.get('company_id')
    return pd.DataFrame([{
        'id': np.nan, 'company_id': str(company) if company else None,
        'kind': 'другое', 'direction': direction, 'date': date, 'amount': amount,
        'description': (data.get('description') or '').strip() or 'Планируемое событие',
        'probability': _prob(data.get('probability')), 'source': 'Ручной ввод',
    }])[COLUMNS]
