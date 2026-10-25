# прогноз остатка методом монте-карло: 1000 вариантов следующих 30 дней,
# вероятность разрыва = доля вариантов, где остаток ушёл ниже нуля
import numpy as np
import pandas as pd

from step3_regularity import detect_regular_payments

IN, OUT = 'Поступление', 'Списание'
LOOKBACK_DAYS = 180
OVERDUE_DAYS = 3
PLAN_MATCH = 0.10
SKIP_CATEGORIES = {'Перевод собственных средств'}  # переводы себе не считаем
COST_CATEGORIES = {'Аренда', 'Закупки/Поставщики'}
QUARTER_MONTHS = (1, 4, 7, 10)


def _key(df):
    return (df['counterparty_inn'].fillna('').astype(str) + '|'
            + df['direction'] + '|' + df['category'])


def _tax_dates(start, end):
    out = []
    for p in pd.period_range(start - pd.DateOffset(months=1), end + pd.DateOffset(months=1), freq='M'):
        d = pd.Timestamp(p.year, p.month, 28)
        if d.weekday() == 5:
            d += pd.Timedelta(days=2)
        elif d.weekday() == 6:
            d += pd.Timedelta(days=1)
        if start <= d <= end:
            out.append(d)
    return out


def _was_paid(comp, r):
    lo = r.date - pd.Timedelta(days=10)
    m = comp[(comp['direction'] == r.direction) & (comp['operation_date'] >= lo)
             & ((comp['amount'] - r.amount).abs() <= PLAN_MATCH * r.amount)]
    return len(m) > 0


def simulate(comp, plan, as_of, days=30, n_sims=1000, seed=7, scenario=None):
    sc = {'client_delay': 0, 'revenue_drop': 0.0, 'cost_growth': 0.0,
          'extra_expense': 0.0, 'extra_day': 1, 'cost_delay': 0}
    sc.update(scenario or {})
    rng = np.random.default_rng(seed)

    comp = comp[comp['operation_date'] <= as_of].sort_values('operation_date', kind='stable')
    cid = str(comp['company_id'].iloc[0])
    start_balance = float(comp['balance_eod'].iloc[-1])
    start = as_of + pd.Timedelta(days=1)
    dates = pd.date_range(start, periods=days)
    end = dates[-1]

    names = {}
    if 'counterparty_name' in comp:
        n = comp.dropna(subset=['counterparty_name'])
        names = dict(zip(n['counterparty_inn'], n['counterparty_name']))

    lookback_start = max(comp['operation_date'].min(), as_of - pd.Timedelta(days=LOOKBACK_DAYS))
    history_days = (as_of - lookback_start).days + 1

    # самый крупный клиент за последние месяцы
    big_inn, big_name = None, None
    recent_in = comp[(comp['operation_date'] >= lookback_start) & (comp['direction'] == IN)
                     & (~comp['category'].isin(SKIP_CATEGORIES))
                     & comp['counterparty_inn'].notna() & (comp['counterparty_inn'] != '')]
    if len(recent_in):
        big_inn = recent_in.groupby('counterparty_inn')['amount'].sum().idxmax()
        big_name = names.get(big_inn, big_inn)

    in_m = np.zeros((n_sims, days))    # по истории
    out_m = np.zeros((n_sims, days))
    in_x = np.zeros((n_sims, days))    # плановые события
    out_x = np.zeros((n_sims, days))
    upcoming, overdue, notes = [], [], []

    def add(flow, base_day, amounts, sd=0.0, prob=1.0, late_only=False, mult=1.0):
        jit = rng.normal(0, sd, n_sims) if sd > 0 else np.zeros(n_sims)
        if late_only:
            jit = np.abs(jit)
        day = np.maximum(np.rint(base_day + jit).astype(int), 0)
        amt = rng.choice(amounts, n_sims) if len(amounts) > 1 else np.full(n_sims, float(amounts[0]))
        ok = (day < days) & (rng.random(n_sims) < prob)
        flow[np.arange(n_sims)[ok], day[ok]] += amt[ok] * mult

    def note_upcoming(day, title, amount, direction, kind, source):
        if 0 <= day < days:
            upcoming.append({'date': dates[day].strftime('%Y-%m-%d'), 'title': title,
                             'amount': float(amount), 'direction': direction,
                             'kind': kind, 'source': source})

    # регулярные платежи
    used_keys, used, stale = set(), 0, 0
    reg = detect_regular_payments(comp)
    if len(reg):
        reg = reg[(reg['category'] != 'Налоги') & (~reg['category'].isin(SKIP_CATEGORIES))]
        for r in reg.itertuples():
            interval = max(int(round(r.interval_days)), 1)
            grp = comp[(comp['counterparty_inn'] == r.counterparty_inn)
                       & (comp['direction'] == r.direction) & (comp['category'] == r.category)]
            if grp.empty:
                continue
            last = grp['operation_date'].max()
            if (start - last).days > 2.5 * interval:  # давно не было платежа, значит закончился
                stale += 1
                continue

            amounts = grp['amount'].tail(12).to_numpy()
            median = float(np.median(amounts))
            is_in = r.direction == IN
            flow = in_m if is_in else out_m
            mult = 1 + sc['cost_growth'] if (not is_in and r.category in COST_CATEGORIES) else 1.0
            shift = 0
            if is_in and r.counterparty_inn == big_inn:
                shift = int(sc['client_delay'])
            elif not is_in and r.category in COST_CATEGORIES:
                shift = int(sc['cost_delay'])
            title = f"{r.category}: {names.get(r.counterparty_inn, r.counterparty_inn)}"
            sd = 2.0 if is_in else 1.0
            prob = 0.9 if is_in else 1.0

            exp = last + pd.Timedelta(days=interval)
            if exp < start:  # ожидали уже, а платежа нет
                late = (as_of - exp).days
                flagged = late > OVERDUE_DAYS
                if flagged:
                    overdue.append({'expected': exp.strftime('%Y-%m-%d'), 'title': title,
                                    'amount': median, 'direction': r.direction,
                                    'days_late': int(late), 'source': 'История операций'})
                add(flow, shift, amounts, sd=4.0 if is_in else 0.0,
                    prob=(0.6 if flagged else prob) if is_in else 1.0, late_only=True, mult=mult)
                note_upcoming(shift, title + ' (с опозданием)' if flagged else title,
                              median * mult, r.direction, 'regular', 'История операций')
                exp += pd.Timedelta(days=interval)
            while exp < start:
                exp += pd.Timedelta(days=interval)

            d = (exp - start).days
            while d < days:
                add(flow, d + shift, amounts, sd=sd, prob=prob, mult=mult)
                note_upcoming(d + shift, title, median * mult, r.direction, 'regular', 'История операций')
                d += interval
            used_keys.add(f"{r.counterparty_inn}|{r.direction}|{r.category}")
            used += 1

    # плановые события: счета, договоры, налоги, ручной ввод
    ext = pd.DataFrame()
    if plan is not None and len(plan):
        ext = plan[(plan['company_id'].isna() | (plan['company_id'].astype(str) == cid))
                   & (plan['date'] >= as_of - pd.Timedelta(days=60)) & (plan['date'] <= end)]
    tax_plan_dates, paid, n_plan = [], 0, 0
    for r in ext.itertuples():
        is_in = r.direction == IN
        if r.kind == 'налог':
            tax_plan_dates.append(r.date)
        if r.date < start:
            if _was_paid(comp, r):
                paid += 1
                continue
            late = (as_of - r.date).days
            d = 0
            if late > OVERDUE_DAYS:
                overdue.append({'expected': r.date.strftime('%Y-%m-%d'), 'title': r.description,
                                'amount': float(r.amount), 'direction': r.direction,
                                'days_late': int(late), 'source': r.source})
        else:
            d = (r.date - start).days
        prob = float(r.probability) if pd.notna(r.probability) else (0.8 if is_in else 1.0)
        if r.date < start and is_in:
            prob = min(prob, 0.6)
        add(in_x if is_in else out_x, d, np.array([float(r.amount)]),
            sd=3.0 if is_in else 0.0, prob=prob, late_only=is_in)
        note_upcoming(d, r.description, r.amount, r.direction, r.kind, r.source)
        n_plan += 1

    # налоговый календарь (ЕНП до 28-го), если налог не добавлен вручную
    t = comp[comp['category'] == 'Налоги']
    monthly = t.groupby(t['operation_date'].dt.to_period('M'))['amount'].sum()
    if len(monthly) > 1:
        monthly = monthly[monthly.index != as_of.to_period('M')]  # текущий месяц может быть неполным
    monthly = monthly.tail(12)
    all_vals = monthly.to_numpy()
    q_vals = monthly[np.array([p.month in QUARTER_MONTHS for p in monthly.index], dtype=bool)].to_numpy()
    if len(all_vals):
        for d_tax in _tax_dates(start, end):
            if any(abs((d_tax - x).days) <= 5 for x in tax_plan_dates):
                continue
            quarter = d_tax.month in QUARTER_MONTHS
            vals = q_vals if (quarter and len(q_vals)) else all_vals
            d = (d_tax - start).days
            add(out_x, d, vals)
            title = ('ЕНП до 28-го (в т.ч. квартальные: НДС, аванс по УСН)' if quarter
                     else 'ЕНП до 28-го (налоги и взносы)')
            note_upcoming(d, title, np.median(vals), OUT, 'налог', 'Налоговый календарь')
        notes.append('Налоги: по календарю (ЕНП до 28-го), сумма по прошлым месяцам.')
    elif not tax_plan_dates:
        notes.append('Налоги не учтены: в истории нет налоговых платежей. '
                     'Добавьте предстоящие налоги вручную или файлом.')

    # внеплановый расход из сценария (вызывается всегда, чтобы случайные числа не сдвигались)
    extra_d = int(min(max(sc['extra_day'] - 1, 0), days - 1))
    add(out_x, extra_d, np.array([float(sc['extra_expense'])]))
    if sc['extra_expense'] > 0:
        note_upcoming(extra_d, 'Внеплановый расход (сценарий)', sc['extra_expense'], OUT, 'extra', 'Сценарий')

    # остальные операции, которые не по расписанию
    lb = comp[(comp['operation_date'] >= lookback_start)
              & (~comp['category'].isin(SKIP_CATEGORIES | {'Налоги'}))]
    lb = lb[~_key(lb).isin(used_keys)]
    idx = pd.date_range(lookback_start, as_of)
    is_weekend = idx.weekday >= 5

    def daily(mask):
        return lb[mask].groupby('operation_date')['amount'].sum().reindex(idx, fill_value=0).to_numpy()

    d_in = lb['direction'] == IN
    d_out = lb['direction'] == OUT
    big = (lb['counterparty_inn'] == big_inn) if big_inn else pd.Series(False, index=lb.index)
    cost = lb['category'].isin(COST_CATEGORIES)
    pools = {'in_big': daily(d_in & big), 'in_oth': daily(d_in & ~big),
             'out_cost': daily(d_out & cost), 'out_oth': daily(d_out & ~cost)}
    irr = {k: np.zeros((n_sims, days)) for k in pools}
    for d, date in enumerate(dates):
        pool = np.where(is_weekend == (date.weekday() >= 5))[0]
        if len(pool) == 0:
            pool = np.arange(len(idx))
        pick = pool[rng.integers(0, len(pool), n_sims)]
        for k, series in pools.items():
            irr[k][:, d] = series[pick]

    def shifted(a, k):
        # k > 0: платежи приходят позже, k < 0: раньше
        k = int(k)
        if k == 0:
            return a
        out = np.zeros_like(a)
        if 0 < k < days:
            out[:, k:] = a[:, :days - k]
        elif -days < k < 0:
            out[:, :days + k] = a[:, -k:]
            out[:, days + k:] = a[:, days + k:]
        return out

    irr['in_big'] = shifted(irr['in_big'], sc['client_delay'])
    irr['out_cost'] = shifted(irr['out_cost'], sc['cost_delay'])

    in_m += irr['in_oth'] + irr['in_big']
    out_m += irr['out_oth'] + irr['out_cost'] * (1 + sc['cost_growth'])
    in_m *= (1 - sc['revenue_drop'])

    inflow = in_m + in_x
    outflow = out_m + out_x

    if history_days < 60:
        notes.append(f'Истории мало ({history_days} дн.), прогноз может быть неточным.')
    notes.insert(0, f'Прогноз построен по операциям за последние {min(history_days, LOOKBACK_DAYS)} дней.')
    notes.insert(1, f'Регулярных платежей и поступлений учтено: {used}'
                    + (f' (ещё {stale} давно не повторялись и не учтены).' if stale else '.'))
    if n_plan or paid:
        notes.append(f'Плановых событий учтено: {n_plan}'
                     + (f', уже оплачено по выписке: {paid}.' if paid else '.'))
    notes.append('Переводы собственных средств не учитываются.')

    return {'dates': dates, 'inflow': inflow, 'outflow': outflow,
            'bal': start_balance + np.cumsum(inflow - outflow, axis=1),
            'start_balance': start_balance, 'as_of': as_of, 'upcoming': upcoming,
            'overdue': overdue, 'notes': notes,
            'big_client': {'name': big_name, 'inn': big_inn} if big_inn else None}


def summarize(sim):
    bal, dates = sim['bal'], sim['dates']
    p10, p50, p90 = np.percentile(bal, [10, 50, 90], axis=0)
    p_gap_day = (bal < 0).mean(axis=0)

    min_bal = bal.min(axis=1)
    gap = min_bal < 0
    prob = float(gap.mean())
    risk = {'probability': prob, 'date': None, 'date_early': None,
            'shortfall': None, 'shortfall_worst': None}
    if gap.any():
        first = (bal < 0).argmax(axis=1)[gap]
        risk['date'] = dates[int(np.median(first))].strftime('%Y-%m-%d')
        risk['date_early'] = dates[int(np.percentile(first, 10))].strftime('%Y-%m-%d')
        deficit = -min_bal[gap]
        risk['shortfall'] = float(np.median(deficit))
        risk['shortfall_worst'] = float(np.percentile(deficit, 90))

    level = 'low' if prob < 0.10 else 'medium' if prob < 0.40 else 'high'
    i_min = int(np.argmin(p50))
    upcoming = sorted(sim['upcoming'], key=lambda x: (x['date'], -x['amount']))
    return {
        'start_date': sim['as_of'].strftime('%Y-%m-%d'),
        'start_balance': sim['start_balance'],
        'days': [{'date': dates[i].strftime('%Y-%m-%d'), 'p10': float(p10[i]), 'p50': float(p50[i]),
                  'p90': float(p90[i]), 'p_gap': float(p_gap_day[i])} for i in range(len(dates))],
        'risk': risk, 'level': level,
        'lowest': {'date': dates[i_min].strftime('%Y-%m-%d'), 'value': float(p50[i_min])},
        'upcoming': upcoming[:max(15, len(dates) // 3)],
        'overdue': sorted(sim['overdue'], key=lambda x: x['expected']),
        'big_client': sim['big_client'],
        'notes': sim['notes'],
    }


def _company(ops, company_id):
    comp = ops[ops['company_id'].astype(str) == str(company_id)]
    return comp.sort_values('operation_date', kind='stable')


def forecast_company(ops, plan, company_id, days=30, scenario=None, n_sims=1000, seed=7):
    comp = _company(ops, company_id)
    if comp.empty:
        return None
    sim = simulate(comp, plan, comp['operation_date'].max(), days, n_sims, seed, scenario)
    return summarize(sim)


def compare_scenario(ops, plan, company_id, scenario, days=30):
    base = forecast_company(ops, plan, company_id, days)
    if base is None:
        return None
    return {'base': base, 'scenario': forecast_company(ops, plan, company_id, days, scenario)}


def plan_fact(ops, plan, company_id, window=28, n_sims=500, seed=7):
    comp = _company(ops, company_id)
    if comp.empty:
        return None
    last = comp['operation_date'].max()
    if (last - comp['operation_date'].min()).days < 120:
        return {'error': 'Для плана-факта нужно минимум 4 месяца истории.'}

    cutoff = last - pd.Timedelta(days=window)
    sim = simulate(comp, plan, cutoff, window, n_sims, seed)
    dates = sim['dates']

    fact = comp[(comp['operation_date'] > cutoff) & (~comp['category'].isin(SKIP_CATEGORIES))]

    def f_daily(direction):
        s = fact[fact['direction'] == direction].groupby('operation_date')['amount'].sum()
        return s.reindex(dates, fill_value=0).to_numpy()

    f_in, f_out = f_daily(IN), f_daily(OUT)
    fact_bal = sim['start_balance'] + np.cumsum(f_in - f_out)
    p10, p50, p90 = np.percentile(sim['bal'], [10, 50, 90], axis=0)
    outside = (fact_bal < p10) | (fact_bal > p90)

    weeks = []
    for w in range(window // 7):
        sl = slice(7 * w, 7 * w + 7)
        row = {'from': dates[sl.start].strftime('%Y-%m-%d'), 'to': dates[sl.stop - 1].strftime('%Y-%m-%d')}
        for name, sims_arr, fact_arr in (('in', sim['inflow'], f_in), ('out', sim['outflow'], f_out)):
            w_sims = sims_arr[:, sl].sum(axis=1)
            lo, mid, hi = np.percentile(w_sims, [10, 50, 90])
            fv = float(fact_arr[sl].sum())
            row[f'plan_{name}'] = float(mid)
            row[f'fact_{name}'] = fv
            row[f'flag_{name}'] = bool(fv < lo or fv > hi)
        weeks.append(row)

    return {
        'cutoff': cutoff.strftime('%Y-%m-%d'),
        'days': [{'date': dates[i].strftime('%Y-%m-%d'), 'p10': float(p10[i]), 'p50': float(p50[i]),
                  'p90': float(p90[i]), 'fact': float(fact_bal[i]), 'outside': bool(outside[i])}
                 for i in range(window)],
        'weeks': weeks,
        'inside': int((~outside).sum()), 'total': window,
    }


# ---------- план действий и запас прочности ----------
TARGET_RISK = 0.10   # считаем безопасным, если разрыв возможен не чаще чем в 1 случае из 10


def _gap_prob(sim):
    return float((sim['bal'].min(axis=1) < 0).mean())


def _lever(title, detail, prob, base):
    return {'title': title, 'detail': detail, 'prob_after': prob,
            'delta': base - prob, 'enough': prob <= TARGET_RISK}


def advice(ops, plan, company_id, days=30):
    """Не просто «риск есть», а что с ним делать: каждую меру проверяем той же симуляцией
    (тот же seed, поэтому разница — это эффект меры, а не случайность).
    Плюс запас прочности: какое ухудшение бизнес выдержит, оставаясь в безопасной зоне."""
    comp = _company(ops, company_id)
    if comp.empty:
        return None
    as_of = comp['operation_date'].max()

    def run(sc=None):
        return simulate(comp, plan, as_of, days, 1000, 7, sc)

    base = run()
    p0 = _gap_prob(base)
    min_bal = base['bal'].min(axis=1)
    out = {'probability': p0, 'target': TARGET_RISK, 'safe': p0 <= TARGET_RISK,
           'reserve': None, 'levers': [], 'tolerance': {}, 'big_client': base['big_client']}

    if p0 > TARGET_RISK:
        # подушка: столько денег добавить на счёт, чтобы в 9 случаях из 10 не уйти в минус
        need = float(max(0.0, -np.percentile(min_bal, TARGET_RISK * 100)))
        out['reserve'] = float(np.ceil(need / 10000) * 10000)
        levers = []
        for pct in (10, 20):
            levers.append(_lever(f'Поднять выручку на {pct}%', 'Продажи, допродажи, ускорение оплат от клиентов',
                                 _gap_prob(run({'revenue_drop': -pct / 100})), p0))
        for pct in (10, 20):
            levers.append(_lever(f'Сократить закупки и аренду на {pct}%', 'Торг с поставщиками, пересмотр договоров',
                                 _gap_prob(run({'cost_growth': -pct / 100})), p0))
        levers.append(_lever('Выручка +10% и закупки −10% одновременно', 'Сочетание двух мер',
                             _gap_prob(run({'revenue_drop': -0.10, 'cost_growth': -0.10})), p0))
        reserve_txt = f'{out["reserve"]:,.0f}'.replace(',', ' ')
        levers.append(_lever(f'Подушка {reserve_txt} ₽ на счёте',
                             'Овердрафт, кредитная линия или собственные средства', TARGET_RISK, p0))
        out['levers'] = sorted(levers, key=lambda x: (x['prob_after'], x['title']))
        return out

    # запас прочности: растим стресс по шагам, пока риск остаётся в безопасной зоне
    held = None
    for pct in (10, 20, 30, 40, 50):
        if _gap_prob(run({'revenue_drop': pct / 100})) > TARGET_RISK:
            break
        held = pct
    out['tolerance']['revenue_drop'] = held or 0
    if base['big_client']:
        held = None
        for delay in (7, 14, 21, 28, 42, 56):
            if _gap_prob(run({'client_delay': delay})) > TARGET_RISK:
                break
            held = delay
        out['tolerance']['client_delay'] = held or 0
    return out


SAFE_PROB = 0.10  # безопасной считаем зону, где разрыв возможен не чаще 1 случая из 10


def _gap_prob(sim):
    return float((sim['bal'].min(axis=1) < 0).mean())


def advice(ops, plan, company_id, days=30, n_sims=1000, seed=7):
    """Что делать с риском разрыва. Каждая мера проверяется той же симуляцией
    с теми же случайными числами (seed), поэтому разница — это именно эффект меры."""
    comp = _company(ops, company_id)
    if comp.empty:
        return None
    as_of = comp['operation_date'].max()

    def run(scenario=None):
        return simulate(comp, plan, as_of, days, n_sims, seed, scenario)

    base = run()
    prob = _gap_prob(base)
    big = base['big_client']
    out = {'probability': prob, 'target': SAFE_PROB, 'safe': prob <= SAFE_PROB + 1e-9,
           'big_client': big}

    if out['safe']:
        # запас прочности: насколько можно ухудшить условия, пока риск не выйдет за порог
        def limit(key, steps, scale=1.0):
            best = 0
            for v in steps:
                if _gap_prob(run({key: v * scale})) > SAFE_PROB + 1e-9:
                    break
                best = v
            return best

        out['tolerance'] = {'revenue_drop': limit('revenue_drop', (10, 20, 30, 40, 50), 0.01)}
        if big:
            out['tolerance']['client_delay'] = limit('client_delay', [d for d in (7, 14, 21, 28) if d < days])
        return out

    # резерв: добавка к остатку, при которой разрыв возможен не чаще SAFE_PROB
    deficit = -base['bal'].min(axis=1)
    out['reserve'] = float(max(np.percentile(deficit, 100 * (1 - SAFE_PROB)), 0))

    candidates = []
    if big:
        candidates.append(('Ускорить оплату от клиента «%s» на 7 дней' % big['name'],
                           'Выставить счёт раньше или договориться о предоплате', {'client_delay': -7}))
    candidates += [
        ('Получить отсрочку у поставщиков и арендодателя на 7 дней',
         'Сдвигает закупки и аренду; налоги и зарплата остаются в срок', {'cost_delay': 7}),
        ('Сократить закупки и аренду на 10%',
         'Пересмотреть условия договоров, найти более дешёвых поставщиков', {'cost_growth': -0.10}),
        ('Увеличить выручку на 10%',
         'Рост продаж или повышение цен; считается от поступлений по истории', {'revenue_drop': -0.10}),
    ]
    levers = []
    for title, detail, sc in candidates:
        after = _gap_prob(run(sc))
        levers.append({'title': title, 'detail': detail, 'prob_after': after,
                       'enough': after <= SAFE_PROB + 1e-9})
    out['levers'] = sorted(levers, key=lambda x: x['prob_after'])
    return out
