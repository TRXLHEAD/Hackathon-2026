const $ = id => document.getElementById(id);
const rub = n => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0}).format(n) + ' ₽';
const state = {company: null, page: 1, charts: {}};
const PALETTE = ['#2347d6','#c2412d','#1f8a5b','#d99a00','#7a4fd1','#0f8da8','#8a8f98','#d6558a','#5b7a2f'];

function go(i){ $('track').style.transform = `translateX(-${i * 25}%)`; }
document.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(+b.dataset.go));

async function api(url){
  const r = await fetch(url);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || 'Ошибка запроса');
  return j;
}

/* ---------- загрузка ---------- */
$('pick').onclick = () => $('file').click();
$('file').onchange = async () => {
  const file = $('file').files[0];
  if (!file) return;
  const msg = $('upload-msg');
  msg.className = 'hint'; msg.textContent = 'Обрабатываем файл…';
  $('pick').disabled = true;

  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/api/upload', {method: 'POST', body: fd});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error);

    const sel = $('company');
    sel.innerHTML = j.companies.map(c => `<option>${c}</option>`).join('');
    state.company = sel.value;
    $('file-info').textContent =
      `${j.operations.toLocaleString('ru-RU')} операций, ${j.date_from} — ${j.date_to}. ` + j.notes.join(' ');
    msg.textContent = '';
    go(1);
  } catch (e) {
    msg.className = 'hint err';
    msg.textContent = e instanceof TypeError
      ? 'Нет связи с сервером. Проверьте, что он запущен и страница открыта по адресу http://127.0.0.1:5000'
      : e.message;
  } finally {
    $('pick').disabled = false; $('file').value = '';
  }
};
$('company').onchange = e => state.company = e.target.value;

/* ---------- история списаний ---------- */
async function loadHistory(){
  const p = new URLSearchParams({
    company: state.company, page: state.page,
    q: $('q').value, category: $('cat').value
  });
  const j = await api('/api/history?' + p);
  state.page = j.page;
  document.querySelector('#s-history h2').textContent = 'История списаний, ' + state.company;

  const cat = $('cat'), cur = cat.value;
  cat.innerHTML = '<option value="">Все категории</option>' +
    j.categories.map(c => `<option>${c}</option>`).join('');
  cat.value = cur;

  $('h-sum').textContent = `${j.total.toLocaleString('ru-RU')} операций на ${rub(j.sum)}`;
  $('h-body').innerHTML = j.rows.length ? j.rows.map(r => `
    <tr><td>${r.date}</td><td>${esc(r.name) || '—'}</td><td><span class="tag">${r.category}</span></td>
    <td class="purpose">${esc(r.purpose) || '—'}</td><td>${r.inn || '—'}</td>
    <td class="num">−${rub(r.amount)}</td></tr>`).join('')
    : '<tr><td colspan="6">Ничего не найдено</td></tr>';
  $('page-info').textContent = `Страница ${j.page} из ${j.pages}`;
  $('prev').disabled = j.page <= 1;
  $('next').disabled = j.page >= j.pages;
}
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

$('go-history').onclick = () => {
  state.page = 1; $('q').value = ''; $('cat').value = '';
  loadHistory().then(() => go(2)).catch(e => alert(e.message));
};
$('prev').onclick = () => { state.page--; loadHistory(); };
$('next').onclick = () => { state.page++; loadHistory(); };
$('cat').onchange = () => { state.page = 1; loadHistory(); };
let t; $('q').oninput = () => { clearTimeout(t); t = setTimeout(() => { state.page = 1; loadHistory(); }, 300); };

/* ---------- статистика ---------- */
function draw(id, config){
  if (state.charts[id]) state.charts[id].destroy();
  state.charts[id] = new Chart($(id), config);
}
const money = {callbacks: {label: c => ` ${c.dataset.label || c.label}: ${rub(c.parsed.y ?? c.parsed)}`}};
const axisY = {ticks: {callback: v => new Intl.NumberFormat('ru-RU', {notation: 'compact'}).format(v)}};

function donut(id, d){
  draw(id, {type: 'doughnut',
    data: {labels: d.labels, datasets: [{data: d.values, backgroundColor: PALETTE, borderWidth: 0}]},
    options: {plugins: {legend: {position: 'right'},
      tooltip: {callbacks: {label: c => ` ${c.label}: ${rub(c.parsed)}`}}}}});
}

async function loadStats(){
  const j = await api('/api/stats?company=' + encodeURIComponent(state.company));
  const k = j.kpi;
  document.querySelector('#s-stats h2').textContent = 'Полная статистика, ' + state.company;
  $('kpis').innerHTML = [
    ['Поступления', rub(k.income)], ['Списания', rub(k.outcome)],
    ['Остаток на конец периода', rub(k.balance)], ['Операций', k.operations.toLocaleString('ru-RU')]
  ].map(([a, b]) => `<div class="kpi"><small>${a}</small><div>${b}</div></div>`).join('');

  draw('ch-monthly', {type: 'bar',
    data: {labels: j.monthly.labels, datasets: [
      {label: 'Поступления', data: j.monthly.income, backgroundColor: '#1f8a5b'},
      {label: 'Списания', data: j.monthly.outcome, backgroundColor: '#c2412d'}]},
    options: {plugins: {tooltip: money}, scales: {y: axisY}}});

  donut('ch-out', j.outcome_cat);
  donut('ch-in', j.income_cat);

  draw('ch-balance', {type: 'line',
    data: {labels: j.balance.labels, datasets: [{label: 'Остаток', data: j.balance.values,
      borderColor: '#2347d6', backgroundColor: 'rgba(35,71,214,.08)', fill: true, pointRadius: 0, tension: .2}]},
    options: {plugins: {legend: {display: false}, tooltip: money},
      scales: {y: axisY, x: {ticks: {maxTicksLimit: 8}}}}});

  runForecast();
}

const fmtDay = d => new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'long'}).format(new Date(d + 'T00:00:00'));
const fmtShort = d => d.slice(8, 10) + '.' + d.slice(5, 7);
const fmtPct = p => p > 0.99 ? 'более 99%' : p < 0.01 ? 'менее 1%' : Math.round(p * 100) + '%';

async function runForecast(){
  const msg = $('f-msg'), box = $('verdict');
  msg.textContent = 'Считаем прогноз…'; msg.className = 'hint';
  try {
    const p = new URLSearchParams({company: state.company, delay: $('f-delay').value || 0,
      extra: $('f-extra').value || 0, extra_day: $('f-extra-day').value || 1});
    const j = await api('/api/forecast?' + p);
    msg.textContent = '';

    const r = j.risk;
    let title, body;
    if (j.level === 'low') {
      title = 'Кассовый разрыв в ближайшие 30 дней маловероятен';
      body = `<p>Вероятность нехватки денег: ${fmtPct(r.probability)}.</p>
              <p>Самый низкий ожидаемый остаток — ${rub(j.lowest.value)} (${fmtDay(j.lowest.date)}).</p>`;
    } else {
      title = j.level === 'high' ? 'Высокий риск: денег может не хватить' : 'Есть заметный риск нехватки денег';
      body = `<p>Вероятность разрыва: <b style="display:inline;font-size:inherit">${fmtPct(r.probability)}</b>.</p>
              <p>Скорее всего это случится <b style="display:inline;font-size:inherit">${fmtDay(r.date)}</b>`
           + (r.date_early && r.date_early !== r.date ? ` (в плохом случае уже ${fmtDay(r.date_early)})` : '') + `.</p>
              <p>Не хватит около <b style="display:inline;font-size:inherit">${rub(r.shortfall)}</b>, в неблагоприятном случае до ${rub(r.shortfall_worst)}.</p>
              <p>Что можно сделать: ускорить оплату выставленных счетов, перенести необязательные платежи, заранее договориться об овердрафте.</p>`;
    }
    box.className = 'verdict ' + j.level;
    box.innerHTML = `<b>${title}</b>${body}`;
    box.hidden = false;

    const labels = j.days.map(d => fmtShort(d.date));
    draw('ch-forecast', {type: 'line',
      data: {labels, datasets: [
        {label: 'нижняя граница', data: j.days.map(d => d.p10), borderWidth: 0, pointRadius: 0, fill: false},
        {label: 'Диапазон 8 из 10', data: j.days.map(d => d.p90), borderWidth: 0, pointRadius: 0,
          backgroundColor: 'rgba(35,71,214,.14)', fill: '-1'},
        {label: 'Ожидаемый остаток', data: j.days.map(d => d.p50), borderColor: '#2347d6', borderWidth: 3, pointRadius: 0, fill: false},
        {label: 'Ноль', data: j.days.map(() => 0), borderColor: '#c2412d', borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, fill: false}]},
      options: {interaction: {mode: 'index', intersect: false},
        plugins: {legend: {labels: {filter: i => i.text !== 'нижняя граница'}},
          tooltip: {filter: i => i.dataset.label !== 'нижняя граница',
            callbacks: {label: c => ` ${c.dataset.label}: ${rub(c.parsed.y)}`}}},
        scales: {y: axisY, x: {ticks: {maxTicksLimit: 10}}}}});

    $('f-upcoming').innerHTML = j.upcoming.length ? j.upcoming.map(u => {
      const inn = u.direction === 'Поступление';
      return `<tr><td>${fmtDay(u.date)}</td><td>${esc(u.title)}</td>
        <td class="num ${inn ? 'pos' : 'neg'}" style="color:inherit"><span class="${inn ? 'pos' : 'neg'}" style="color:${inn ? '#1f8a5b' : '#c2412d'}">${inn ? '+' : '−'}${rub(u.amount)}</span></td></tr>`;
    }).join('') : '<tr><td colspan="3">Известных платежей на ближайшие 30 дней не найдено</td></tr>';

    $('f-notes').innerHTML = j.notes.map(n => `<li>${esc(n)}</li>`).join('');
  } catch (e) {
    msg.className = 'hint err'; msg.textContent = e.message;
    box.hidden = true;
    if (state.charts['ch-forecast']) { state.charts['ch-forecast'].destroy(); delete state.charts['ch-forecast']; }
  }
}
$('f-run').onclick = runForecast;

/* ---------- счета и налоги из внешних источников ---------- */
$('ext-pick').onclick = () => $('ext-file').click();
$('ext-file').onchange = async () => {
  const f = $('ext-file').files[0];
  if (!f) return;
  const m = $('ext-msg'); m.className = 'hint'; m.textContent = 'Загружаем…';
  const fd = new FormData(); fd.append('file', f);
  try {
    const r = await fetch('/api/external', {method: 'POST', body: fd});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error);
    m.textContent = `Добавлено: счетов ${j.invoices}, налогов ${j.taxes}`;
  } catch (e) {
    m.className = 'hint err'; m.textContent = e.message;
  } finally { $('ext-file').value = ''; }
};

$('go-stats').onclick = () => loadStats().then(() => go(3)).catch(e => alert(e.message));
