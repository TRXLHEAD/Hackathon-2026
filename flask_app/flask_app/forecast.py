"""
Прогноз остатка по дням и оценка риска кассового разрыва.

Как это работает (метод Монте-Карло):
  1. Берём остаток на последнюю дату в выписке.
  2. Собираем будущие движения денег трёх видов:
       - регулярные платежи и поступления (найдены в step3, повторяются по расписанию);
       - налоги (из внешнего списка или по налоговому календарю) и выставленные счета
         (из внешнего CSV);
       - «обычная жизнь»: всё остальное, что не повторяется по расписанию. Берём реальные
         дни из последних 6 месяцев и случайно подставляем их на будущие дни.
  3. Прогоняем 1000 возможных вариантов следующих 30 дней (сроки и суммы слегка «плавают»).
  4. Вероятность разрыва = доля вариантов, где остаток уходит ниже нуля.
"""
import numpy as np
import pandas as pd

IN, OUT = 'Поступление', 'Списание'
LOOKBACK_DAYS = 180
SKIP_CATEGORIES = {'Перевод собственных средств'}   # не считаем ни приходом, ни расходом


def _key(df):
    return (df['counterparty_inn'].fillna('').astype(str) + '|'
            + df['direction'] + '|' + df['category'])


def load_external(path):
    """Читает CSV с выставленными счетами и налогами из внешних источников."""
    from pipeline import _parse_dates

    df = pd.read_csv(path, sep=None, engine='python', encoding='utf-8-sig',
                     dtype={'company_id': str})
    need = {'due_date', 'amount', 'direction'}
    missing = need - set(df.columns)
    if missing:
        raise ValueError('В файле счетов не хватает колонок: ' + ', '.join(sorted(missing)))

    out = pd.DataFrame(index=df.index)
    out['company_id'] = df['company_id'] if 'company_id' in df else None
    out['date'] = _parse_dates(df['due_date'])
    out['amount'] = (df['amount'].astype(str)
                     .str.replace('\xa0', '', regex=False)
                     .str.replace(' ', '', regex=False)
                     .str.replace(',', '.', regex=False).astype(float))
    out['direction'] = df['direction'].astype(str).str.strip()
    bad = ~out['direction'].isin([IN, OUT])
    if bad.any():
        raise ValueError('В колонке direction допустимы только значения «Поступление» и «Списание».')

    kind = df['type'].astype(str).str.lower() if 'type' in df else pd.Series('счет', index=df.index)
    out['kind'] = np.where(kind.str.contains('налог'), 'налог', 'счет')
    out['description'] = df['description'].fillna('') if 'description' in df else ''
    if 'probability' in df:
        p = pd.to_numeric(df['probability'], errors='coerce')
        out['probability'] = np.where(p > 1, p / 100, p)
    else:
        out['probability'] = np.nan
    return out


def forecast_company(ops, events, external, company_id, days=30, delay_days=0,
                     extra_expense=0.0, extra_day=1, n_sims=1000, seed=7):
    rng = np.random.default_rng(seed)
    cid = str(company_id)

    comp = ops[ops['company_id'].astype(str) == cid].sort_values('operation_date', kind='stable')
    if comp.empty:
        return None

    last_date = comp['operation_date'].max()
    start_balance = float(comp[comp['operation_date'] == last_date]['balance_eod'].iloc[-1])
    start = last_date + pd.Timedelta(days=1)
    dates = pd.date_range(start, periods=days)

    names = {}
    if 'counterparty_name' in comp:
        n = comp.dropna(subset=['counterparty_name'])
        names = dict(zip(n['counterparty_inn'], n['counterparty_name']))

    inflow = np.zeros((n_sims, days))
    outflow = np.zeros((n_sims, days))
    upcoming, notes = [], []

    def add(flow, base_day, amounts, sd=0.0, prob=1.0, late_only=False):
        """Добавляет платёж во все сценарии: срок и сумма немного «плавают»."""
        jit = rng.normal(0, sd, n_sims) if sd > 0 else np.zeros(n_sims)
        if late_only:
            jit = np.abs(jit)
        day = np.maximum(np.rint(base_day + jit).astype(int), 0)
        amt = rng.choice(amounts, n_sims) if len(amounts) > 1 else np.full(n_sims, float(amounts[0]))
        ok = (day < days) & (rng.random(n_sims) < prob)
        flow[np.arange(n_sims)[ok], day[ok]] += amt[ok]

    def note_upcoming(day, title, amount, direction, kind):
        if 0 <= day < days:
            upcoming.append({'date': dates[day].strftime('%Y-%m-%d'), 'title': title,
                             'amount': float(amount), 'direction': direction, 'kind': kind})

    lookback_start = max(comp['operation_date'].min(), last_date - pd.Timedelta(days=LOOKBACK_DAYS))
    history_days = (last_date - lookback_start).days + 1

    ev = events[events['company_id'].astype(str) == cid] if len(events) else events

    # ---------- 1. Регулярные платежи ----------
    used_keys, used, stale = set(), 0, 0
    if len(ev) and 'source' in ev:
        reg = ev[(ev['source'] == 'detected_regular')
                 & (ev['category'] != 'Налоги')
                 & (~ev['category'].isin(SKIP_CATEGORIES))]
        for r in reg.itertuples():
            interval = max(int(round(r.interval_days)), 1)
            grp = comp[(comp['counterparty_inn'] == r.counterparty_inn)
                       & (comp['direction'] == r.direction) & (comp['category'] == r.category)]
            if grp.empty:
                continue
            last = grp['operation_date'].max()
            if (start - last).days > 2.5 * interval:      # платёж давно не приходил — считаем, что закончился
                stale += 1
                continue
            nxt = last + pd.Timedelta(days=interval)
            while nxt < start:
                nxt += pd.Timedelta(days=interval)

            amounts = grp['amount'].tail(12).to_numpy()
            median = float(np.median(amounts))
            is_in = r.direction == IN
            title = f"{r.category}: {names.get(r.counterparty_inn, r.counterparty_inn)}"

            d = (nxt - start).days
            while d < days:
                shift = delay_days if is_in else 0
                add(inflow if is_in else outflow, d + shift, amounts,
                    sd=2.0 if is_in else 1.0, prob=0.9 if is_in else 1.0)
                note_upcoming(d + shift, title, median, r.direction, 'regular')
                d += interval
            used_keys.add(f"{r.counterparty_inn}|{r.direction}|{r.category}")
            used += 1

    # ---------- 2. Налоги ----------
    ext = None
    if external is not None and len(external):
        ext = external[external['company_id'].isna() | (external['company_id'].astype(str) == cid)]

    ext_taxes = ext[ext['kind'] == 'налог'] if ext is not None else None
    if ext_taxes is not None and len(ext_taxes):
        for r in ext_taxes.itertuples():
            d = max((r.date - start).days, 0)
            add(outflow, d, np.array([r.amount]), sd=0, prob=1.0)
            note_upcoming(d, r.description or 'Налоговый платёж', r.amount, OUT, 'tax')
        notes.append(f'Налоги: взяты из загруженного списка ({len(ext_taxes)} шт.).')
    else:
        t = comp[(comp['category'] == 'Налоги') & (comp['operation_date'] >= lookback_start)]
        monthly = t.groupby(t['operation_date'].dt.to_period('M'))['amount'].sum().to_numpy()
        cal = ev[ev['source'] == 'tax_calendar'] if len(ev) and 'source' in ev else []
        if len(monthly) and len(cal):
            for r in cal.itertuples():
                d = (pd.Timestamp(r.predicted_next_date) - start).days
                if 0 <= d < days:
                    add(outflow, d, monthly)
                    note_upcoming(d, 'Налоги (оценка по прошлым месяцам)', np.median(monthly), OUT, 'tax')
            notes.append('Налоги: по налоговому календарю (28-е число), сумма по прошлым месяцам.')
        else:
            notes.append('Налоги не учтены: в истории нет налоговых платежей. '
                         'Загрузите список предстоящих налогов, чтобы прогноз был точнее.')

    # ---------- 3. Выставленные счета ----------
    if ext is not None:
        inv = ext[ext['kind'] == 'счет']
        for r in inv.itertuples():
            is_in = r.direction == IN
            d = max((r.date - start).days, 0)
            prob = r.probability if pd.notna(r.probability) else (0.8 if is_in else 1.0)
            shift = delay_days if is_in else 0
            add(inflow if is_in else outflow, d + shift, np.array([r.amount]),
                sd=3.0 if is_in else 0.0, prob=prob, late_only=is_in)
            note_upcoming(d + shift, r.description or ('Ожидаемая оплата счёта' if is_in else 'Счёт к оплате'),
                          r.amount, r.direction, 'invoice')
        if len(inv):
            notes.append(f'Счета из загруженного списка учтены: {len(inv)} шт.')

    # ---------- 4. Внеплановый расход (сценарий) ----------
    if extra_expense > 0:
        d = int(min(max(extra_day - 1, 0), days - 1))
        add(outflow, d, np.array([float(extra_expense)]))
        note_upcoming(d, 'Внеплановый расход (ваш сценарий)', extra_expense, OUT, 'extra')

    # ---------- 5. «Обычная жизнь»: всё, что не повторяется по расписанию ----------
    lb = comp[(comp['operation_date'] >= lookback_start)
              & (~comp['category'].isin(SKIP_CATEGORIES | {'Налоги'}))]
    lb = lb[~_key(lb).isin(used_keys)]
    idx = pd.date_range(lookback_start, last_date)
    in_daily = lb[lb['direction'] == IN].groupby('operation_date')['amount'].sum().reindex(idx, fill_value=0).to_numpy()
    out_daily = lb[lb['direction'] == OUT].groupby('operation_date')['amount'].sum().reindex(idx, fill_value=0).to_numpy()
    is_weekend = idx.weekday >= 5

    irr_in = np.zeros((n_sims, days))
    irr_out = np.zeros((n_sims, days))
    for d, date in enumerate(dates):
        pool = np.where(is_weekend == (date.weekday() >= 5))[0]
        if len(pool) == 0:
            pool = np.arange(len(idx))
        pick = pool[rng.integers(0, len(pool), n_sims)]
        irr_in[:, d] = in_daily[pick]
        irr_out[:, d] = out_daily[pick]
    if delay_days > 0:
        shifted = np.zeros_like(irr_in)
        if delay_days < days:
            shifted[:, delay_days:] = irr_in[:, :days - delay_days]
        irr_in = shifted
    inflow += irr_in
    outflow += irr_out

    if history_days < 60:
        notes.append(f'Истории мало ({history_days} дн.), прогноз может быть неточным.')
    notes.insert(0, f'Прогноз построен по операциям за последние {min(history_days, LOOKBACK_DAYS)} дней.')
    notes.insert(1, f'Регулярных платежей и поступлений учтено: {used}'
                    + (f' (ещё {stale} давно не повторялись и не учтены).' if stale else '.'))
    notes.append('Переводы собственных средств не учитываются.')

    # ---------- Итоги ----------
    bal = start_balance + np.cumsum(inflow - outflow, axis=1)
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
    upcoming.sort(key=lambda x: (x['date'], -x['amount']))

    return {
        'start_date': last_date.strftime('%Y-%m-%d'),
        'start_balance': start_balance,
        'days': [{'date': dates[i].strftime('%Y-%m-%d'), 'p10': float(p10[i]), 'p50': float(p50[i]),
                  'p90': float(p90[i]), 'p_gap': float(p_gap_day[i])} for i in range(days)],
        'risk': risk,
        'level': level,
        'lowest': {'date': dates[i_min].strftime('%Y-%m-%d'), 'value': float(p50[i_min])},
        'upcoming': upcoming[:15],
        'notes': notes,
    }
