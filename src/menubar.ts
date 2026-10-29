// Menu bar popover: sessions and spend stats, in the office's palette.
// Lives on the server like the office itself, so a style change shows up the
// next time the popover opens, without rebuilding the app.

type Session = {
  id: string
  title: string
  project: string
  at: number
  isLive: boolean
  working: number
  agents: number
  cost: number
  today: number
}

type Day = {
  day: string
  usd: number
  models: Record<string, number>
  projects: Record<string, number>
  parts: number[]
  tokens: number
  sessions: number
  started: number
}

type Stats = { since: string; last24: number; days: Day[]; hours: [string, number, number][] }

type Bridge = { postMessage(msg: unknown): void }
const bridge: Bridge | undefined = (window as any).webkit?.messageHandlers?.ao

const $ = (id: string) => document.getElementById(id) as HTMLElement
const esc = (s: string) => s.replace(/[&<>"]/g, c => `&${{ '&': 'amp', '<': 'lt', '>': 'gt', '"': 'quot' }[c]};`)

let sessions: Session[] = []
let stats: Stats | null = null
let online = false
let tab = load('tab', 'sessions')
let range = load('range', '30')
let autostart = false
let active = true

function load(key: string, fallback: string): string {
  try { return localStorage.getItem('ao.' + key) ?? fallback } catch { return fallback }
}
function keep(key: string, value: string) {
  try { localStorage.setItem('ao.' + key, value) } catch { /* private mode */ }
}

// ── Numbers: whole dollars, cents only below a dollar ──
function money(usd: number): string {
  if (usd > 0 && usd < 1) return '$' + usd.toFixed(2)
  return '$' + Math.round(usd).toLocaleString('ru-RU').replace(/\s/g, ' ')
}
const pct = (part: number, all: number) => all ? Math.round((part / all) * 100) + '%' : '0%'
function tokens(n: number): string {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + ' млрд'
  if (n >= 1e6) return Math.round(n / 1e6) + ' млн'
  return Math.round(n / 1e3) + ' тыс'
}
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
const parse = (day: string) => new Date(day + 'T00:00:00')
const short = (day: string) => { const d = parse(day); return `${d.getDate()} ${MONTHS[d.getMonth()]}` }
const weekday = (day: string) => (parse(day).getDay() + 6) % 7
const clock = (t: number) => { const d = new Date(t); return `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}` }
function ago(t: number): string {
  const m = Math.round((Date.now() - t) / 60000)
  return m < 60 ? `${m} мин назад` : `${Math.round(m / 60)} ч назад`
}

// ── Models: fixed order, fixed colors (validated against the dark panel) ──
const MODELS = ['fable', 'opus', 'sonnet', 'haiku', 'other'] as const
type Family = typeof MODELS[number]
const family = (model: string): Family => MODELS.find(f => model.includes(f)) ?? 'other'
const color = (f: Family) => `var(--m-${f})`
function modelName(model: string): string {
  const m = model.replace(/^claude-/, '').split('-')
  const name = m[0] ?? model
  return name[0]?.toUpperCase() + name.slice(1) + (m.length > 1 ? ' ' + m.slice(1).join('.') : '')
}
function byFamily(days: Day[]): Map<Family, { usd: number; names: Set<string> }> {
  const out = new Map<Family, { usd: number; names: Set<string> }>()
  for (const d of days) {
    for (const [model, usd] of Object.entries(d.models)) {
      const f = family(model)
      const slot = out.get(f) ?? { usd: 0, names: new Set<string>() }
      slot.usd += usd
      slot.names.add(modelName(model))
      out.set(f, slot)
    }
  }
  return out
}

// ── Pixel building: the same office as the menu bar and app icons ──
const LOGO = [
  '................',
  '.....GGGGGG.....',
  '.....GGGGGG.....',
  '..RRRRRRRRRRRR..',
  '..BBBBBBBBBBBB..',
  '..BBCCBCCBYYBB..',
  '..BBCCBCCBYYBB..',
  '..BBBBBBBBBBBB..',
  '..BBDDBCCBCCBB..',
  '..BBDDBCCBCCBB..',
  '..BBBBBBBBBBBB..',
  '..BBCCBOOBCCBB..',
  '..BBCCBOOBCCBB..',
  '..BBBBBOOBBBBB..',
  'LLLLLLLLLLLLLLLL',
  '................',
]
const LOGO_COLORS: Record<string, string> = {
  G: '#f0c040', R: '#8a4a5a', B: '#4b5a8a', C: '#6ee7ff', Y: '#f0c040', D: '#1b1e2b', O: '#a0673d', L: '#3a3e63',
}
function logo(): string {
  let s = ''
  LOGO.forEach((row, y) => [...row].forEach((ch, x) => {
    const fill = LOGO_COLORS[ch]
    if (fill) s += `<rect x="${x}" y="${y}" width="1" height="1" fill="${fill}"/>`
  }))
  return s
}

// ── Talking to the app ──
function send(cmd: string, arg?: unknown) {
  if (bridge) bridge.postMessage({ cmd, arg })
}
function openPath(path: string) {
  if (bridge) send('open', path)
  else window.open(path, '_blank')
}

async function get<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(path, { cache: 'no-store' })
    return res.ok ? await res.json() as T : null
  } catch {
    return null
  }
}

async function refresh(withStats = false) {
  const list = await get<Session[]>('api/sessions')
  online = list !== null
  if (list) sessions = list
  if (withStats || !stats || tab === 'stats') stats = await get<Stats>('api/stats') ?? stats
  render()
}

// ── Header ──
function renderState() {
  const busy = sessions.filter(s => s.working > 0).length
  $('state').innerHTML = !online
    ? '<span class="lamp idle"></span>сервер не отвечает'
    : busy
      ? `<span class="lamp"></span>в работе: ${busy}`
      : '<span class="lamp idle"></span>все отдыхают'
}

// ── Sessions tab ──
function sessionRow(s: Session, lamp: boolean): string {
  const state = s.working > 0 ? `в работе: ${s.working}` : s.isLive ? 'затихла' : ago(s.at)
  // главное — сегодняшнее; полная цена сессии, если она шла и раньше
  const money2 = !s.today
    ? `<div class="a">${money(s.cost)}</div><div class="b">до сегодня</div>`
    : money(s.today) === money(s.cost)
      ? `<div class="a">${money(s.today)}</div><div class="b">сегодня</div>`
      : `<div class="a">${money(s.today)}</div><div class="b">сегодня · всего ${money(s.cost)}</div>`
  return `<button class="row" data-open="/#${esc(s.id)}" title="Открыть офис сессии">
    ${lamp ? `<span class="lamp${s.working > 0 ? '' : ' idle'}"></span>` : ''}
    <span class="body"><div class="t">${esc(s.title)}</div><div class="p">${esc(s.project)} · ${state}</div></span>
    <span class="money">${money2}</span></button>`
}

function renderSessions(): string {
  const today = sessions.reduce((a, s) => a + (s.today ?? 0), 0)
  const busy = sessions.filter(s => s.working > 0)
  const hands = busy.reduce((a, s) => a + s.working, 0)
  const last24 = stats?.last24 ?? sessions.reduce((a, s) => a + s.cost, 0)
  const from = new Date(Date.now() - 23 * 3600e3)
  from.setMinutes(0, 0, 0)
  let html = `<div class="tiles">
    <div class="tile pix"><div class="k">Сегодня</div><div class="v">${money(today)}</div><div class="s">с 00:00</div></div>
    <div class="tile pix"><div class="k">За 24 часа</div><div class="v">${money(last24)}</div><div class="s">с ${clock(from.getTime())} вчера</div></div>
    <div class="tile pix"><div class="k">В работе</div><div class="v">${busy.length ? '<span class="lamp big"></span>' : ''}${busy.length}</div><div class="s">${busy.length ? `исполнителей: ${hands}` : 'сессий'}</div></div>
  </div>`
  if (last24 - today >= 1) {
    html += `<p class="note"><b>Сегодня</b> — с полуночи. <b>24 часа</b> — скользящие сутки: туда входит и вчерашний вечер (${money(last24 - today)}).</p>`
  }
  const live = sessions.filter(s => s.isLive).sort((a, b) => b.working - a.working || b.at - a.at)
  const rest = sessions.filter(s => !s.isLive)
  html += `<section><h3>ОТКРЫТЫЕ <span class="hint">${live.length || ''}</span></h3>`
  html += live.length ? live.map(s => sessionRow(s, true)).join('') : `<div class="empty">${online ? 'Открытых сессий нет' : 'Сервер не отвечает'}</div>`
  html += '</section>'
  if (rest.length) {
    html += `<details${load('rest', '') ? ' open' : ''} id="rest"><summary><h3><span>ЗАТИХШИЕ ЗА СУТКИ</span> <span class="hint">${rest.length} · ${money(rest.reduce((a, s) => a + s.cost, 0))}</span></h3></summary>`
    html += rest.map(s => sessionRow(s, false)).join('') + '</details>'
  }
  return html
}

// ── Stats tab ──
const W = 348

function rangeDays(all: Day[]): Day[] {
  return range === 'all' ? all : all.slice(-Number(range))
}

function weeks(days: Day[]): Day[] {
  const out: Day[] = []
  for (const d of days) {
    const monday = new Date(parse(d.day).getTime() - weekday(d.day) * 86400e3)
    const key = `${monday.getFullYear()}-${String(monday.getMonth() + 1).padStart(2, '0')}-${String(monday.getDate()).padStart(2, '0')}`
    let w = out[out.length - 1]
    if (!w || w.day !== key) {
      w = { day: key, usd: 0, models: {}, projects: {}, parts: [0, 0, 0, 0], tokens: 0, sessions: 0, started: 0 }
      out.push(w)
    }
    w.usd += d.usd
    w.started += d.started
    for (const [m, usd] of Object.entries(d.models)) w.models[m] = (w.models[m] ?? 0) + usd
  }
  return out
}

function niceMax(v: number): number {
  if (v <= 0) return 1
  const p = 10 ** Math.floor(Math.log10(v))
  return ([1, 2, 2.5, 5, 10].find(k => k * p >= v) ?? 10) * p
}

function tipOf(title: string, d: Day, extra = ''): string {
  const lines = MODELS.map(f => [f, [...Object.entries(d.models)].filter(([m]) => family(m) === f)] as const)
    .flatMap(([, ms]) => ms.map(([m, usd]) => `${modelName(m)}: ${money(usd)}`))
  return `<b>${esc(title)}</b><br>итого ${money(d.usd)}${lines.length ? '<br>' + lines.join('<br>') : ''}${extra}`
}

// stacked bars, one per day or week, models in fixed order bottom-up
function barChart(rows: Day[], label: (d: Day) => string, tip: (d: Day, i: number) => string, h = 104): string {
  const left = 40, top = 6, bottom = 14
  const plot = h - top - bottom
  const max = niceMax(Math.max(...rows.map(r => r.usd), 0))
  const step = (W - left) / Math.max(rows.length, 1)
  const bw = Math.max(1, Math.floor(step) - (step > 6 ? 2 : 1))
  let s = ''
  for (const k of [0, 0.5, 1]) {
    const y = Math.round(top + plot - plot * k)
    s += `<rect x="${left}" y="${y}" width="${W - left}" height="1" fill="var(--line)"/>`
    s += `<text x="${left - 4}" y="${y + 3}" text-anchor="end">${money(max * k)}</text>`
  }
  rows.forEach((r, i) => {
    const x = Math.round(left + i * step + (step - bw) / 2)
    let y = top + plot
    for (const f of MODELS) {
      const usd = Object.entries(r.models).filter(([m]) => family(m) === f).reduce((a, [, v]) => a + v, 0)
      const hh = Math.round((usd / max) * plot)
      if (hh <= 0) continue
      y -= hh
      s += `<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(hh - 1, 1)}" fill="${color(f)}"/>`
    }
    s += `<rect class="hit" x="${Math.round(left + i * step)}" y="0" width="${Math.ceil(step)}" height="${top + plot}" data-tip="${esc(tip(r, i))}"/>`
  })
  const marks = rows.length <= 8 ? rows.map((_, i) => i) : [0, Math.floor((rows.length - 1) / 2), rows.length - 1]
  for (const i of marks) {
    const r = rows[i]
    if (!r) continue
    const x = left + i * step + step / 2
    const anchor = i === 0 && rows.length > 8 ? 'start' : i === rows.length - 1 && rows.length > 8 ? 'end' : 'middle'
    s += `<text x="${anchor === 'start' ? left : anchor === 'end' ? W : x}" y="${h - 3}" text-anchor="${anchor}">${esc(label(r))}</text>`
  }
  return `<svg viewBox="0 0 ${W} ${h}">${s}</svg>`
}

function legend(fams: Map<Family, { usd: number; names: Set<string> }>): string {
  return '<div class="legend">' + MODELS.filter(f => fams.has(f))
    .map(f => `<span><i style="background:${color(f)}"></i>${[...fams.get(f)!.names].join(', ')}</span>`).join('') + '</div>'
}

function heatmap(all: Day[]): string {
  const span = range === '7' ? 7 : 28
  const from = all.slice(-span)[0]?.day ?? ''
  const grid: number[][] = WEEKDAYS.map(() => Array(24).fill(0))
  for (const [day, hour, usd] of stats?.hours ?? []) {
    if (day >= from) grid[weekday(day)]![hour]! += usd
  }
  const max = Math.max(...grid.flat(), 0.01)
  const left = 20, cw = Math.floor((W - left) / 24), ch = 11
  let s = ''
  grid.forEach((row, y) => {
    s += `<text x="0" y="${y * (ch + 2) + 9}">${WEEKDAYS[y]}</text>`
    row.forEach((usd, x) => {
      const level = usd <= 0 ? 0 : Math.ceil((usd / max) * 5)
      const fill = level ? `var(--gold)" fill-opacity="${[0, 0.18, 0.34, 0.52, 0.74, 1][level]}` : 'var(--line)'
      s += `<rect x="${left + x * cw}" y="${y * (ch + 2)}" width="${cw - 2}" height="${ch}" fill="${fill}"/>`
      s += `<rect class="hit" x="${left + x * cw}" y="${y * (ch + 2)}" width="${cw}" height="${ch + 2}" data-tip="${esc(`<b>${WEEKDAYS[y]}, ${x}:00–${x + 1}:00</b><br>${money(usd)} за ${span} дн.`)}"/>`
    })
  })
  const hy = 7 * (ch + 2) + 9
  for (const hr of [0, 6, 12, 18, 23]) s += `<text x="${left + hr * cw + (cw - 2) / 2}" y="${hy}" text-anchor="middle">${hr}</text>`
  return `<svg viewBox="0 0 ${W} ${hy + 2}">${s}</svg>`
}

function barList(rows: { name: string; usd: number; fill: string; tip?: string; note?: string }[], total: number): string {
  const max = Math.max(...rows.map(r => r.usd), 0.01)
  return '<div class="bars">' + rows.map(r => `<div class="line"${r.tip ? ` data-tip="${esc(r.tip)}"` : ''}>
    <span class="name">${r.fill.startsWith('var(--m-') ? `<i style="background:${r.fill}"></i>` : ''}${esc(r.name)}</span>
    <span class="track"><span class="fill" style="display:block;width:${(r.usd / max) * 100}%;background:${r.fill}"></span></span>
    <span class="val">${money(r.usd)} <small>${pct(r.usd, total)}</small></span></div>`).join('') + '</div>'
}

function renderStats(): string {
  if (!stats) return `<div class="empty">${online ? 'Считаю траты по журналам…' : 'Сервер не отвечает'}</div>`
  const all = stats.days
  if (!all.length) return '<div class="empty">Журналов с тратами пока нет</div>'
  const days = rangeDays(all)
  const total = days.reduce((a, d) => a + d.usd, 0)
  const started = days.reduce((a, d) => a + d.started, 0)
  const active = days.filter(d => d.usd > 0).length
  const tokensSum = days.reduce((a, d) => a + d.tokens, 0)
  let html = `<div class="seg">${[['7', '7 дней'], ['30', '30 дней'], ['all', 'Всё время']]
    .map(([k, t]) => `<button data-range="${k}" class="${range === k ? 'on' : ''}">${t}</button>`).join('')}
    <span class="since">${short(days[0]!.day)} — ${short(days[days.length - 1]!.day)}</span></div>`
  html += `<div class="tiles four">
    <div class="tile pix"><div class="k">Потрачено</div><div class="v">${money(total)}</div><div class="s">${tokens(tokensSum)} ток.</div></div>
    <div class="tile pix"><div class="k">В день</div><div class="v">${money(active ? total / active : 0)}</div><div class="s">${active} из ${days.length} дн.</div></div>
    <div class="tile pix"><div class="k">Сессий</div><div class="v">${started}</div><div class="s">начато</div></div>
    <div class="tile pix"><div class="k">Сессия</div><div class="v">${money(started ? total / started : 0)}</div><div class="s">в среднем</div></div>
  </div>`

  const fams = byFamily(days)
  const byWeek = days.length > 45
  const rows = byWeek ? weeks(days) : days
  html += `<section><h3>${byWeek ? 'ПО НЕДЕЛЯМ' : 'ПО ДНЯМ'} <span class="hint">наведите на столбик</span></h3><div class="chart pix">`
  html += barChart(rows, r => short(r.day), r => tipOf(byWeek ? 'неделя с ' + short(r.day) : `${WEEKDAYS[weekday(r.day)]}, ${short(r.day)}`, r,
    `<br>сессий: ${r.started}`))
  html += legend(fams) + '</div></section>'

  // week over week, always the last 8 weeks; this week vs the same days of the last one
  const ws = weeks(all).slice(-8)
  const thisWeek = ws[ws.length - 1]
  if (!byWeek && ws.length > 1 && thisWeek) {
    const n = weekday(all[all.length - 1]!.day) + 1
    const prevDays = all.filter(d => d.day < thisWeek.day).slice(-7).filter(d => weekday(d.day) < n)
    const prev = prevDays.reduce((a, d) => a + d.usd, 0)
    const delta = prev ? Math.round(((thisWeek.usd - prev) / prev) * 100) : 0
    html += `<section><h3>НЕДЕЛЯ К НЕДЕЛЕ <span class="hint">${prev ? `${delta > 0 ? '+' : ''}${delta}% к тем же дням прошлой` : ''}</span></h3><div class="chart pix">`
    html += barChart(ws, r => short(r.day), (r, i) => {
      const before = ws[i - 1]
      const change = before && before.usd ? `<br>к прошлой: ${r.usd >= before.usd ? '+' : ''}${Math.round(((r.usd - before.usd) / before.usd) * 100)}%` : ''
      return tipOf('неделя с ' + short(r.day), r, change + `<br>сессий: ${r.started}`)
    }, 84)
    html += '</div></section>'
  }

  html += '<section><h3>МОДЕЛИ</h3><div class="stack">' + MODELS.filter(f => fams.has(f))
    .map(f => `<span style="flex:${fams.get(f)!.usd};background:${color(f)}"></span>`).join('') + '</div>'
  html += barList(MODELS.filter(f => fams.has(f)).map(f => ({ name: [...fams.get(f)!.names].join(', '), usd: fams.get(f)!.usd, fill: color(f) })), total)
  html += '</section>'

  const parts = [0, 1, 2, 3].map(i => days.reduce((a, d) => a + (d.parts[i] ?? 0), 0))
  const partNames = ['Вход', 'Ответы модели', 'Чтение кэша', 'Запись в кэш']
  const partTips = ['новый текст в контексте', 'то, что пишет модель', 'повторное чтение контекста — растёт с длиной сессии', 'кэширование контекста на 5 мин или час']
  html += '<section><h3>ИЗ ЧЕГО СЧЁТ</h3>'
  html += barList(partNames.map((name, i) => ({ name, usd: parts[i] ?? 0, fill: '#7a80a8', tip: `<b>${name}</b><br>${partTips[i]}` })), total)
  html += '</section>'

  html += `<section><h3>КОГДА ТРАТИМ <span class="hint">последние ${range === '7' ? 7 : 28} дн., по часам</span></h3><div class="chart pix">${heatmap(all)}</div></section>`

  const projects = new Map<string, number>()
  for (const d of days) for (const [p, usd] of Object.entries(d.projects)) projects.set(p, (projects.get(p) ?? 0) + usd)
  const top = [...projects].sort((a, b) => b[1] - a[1])
  const shown = top.slice(0, 6).map(([name, usd]) => ({ name, usd, fill: 'var(--gold)' }))
  const other = top.slice(6).reduce((a, [, usd]) => a + usd, 0)
  if (other > 0) shown.push({ name: `прочие (${top.length - 6})`, usd: other, fill: 'var(--muted)' })
  html += '<section><h3>ПАПКИ</h3>' + barList(shown, total) + '</section>'
  html += `<p class="note">История с ${short(stats.since)}. Сервер копит её в своей папке, поэтому она переживает чистку журналов Claude Code.</p>`
  return html
}

// ── Render ──
function render() {
  renderState()
  document.querySelectorAll<HTMLButtonElement>('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab))
  const main = $('main')
  const y = window.scrollY
  main.innerHTML = tab === 'stats' ? renderStats() : renderSessions()
  window.scrollTo(0, y)
  $('autostart').classList.toggle('on', autostart)
  send('size', document.documentElement.scrollHeight)
}

// ── Events ──
document.addEventListener('click', e => {
  const t = e.target as HTMLElement
  const open = t.closest<HTMLElement>('[data-open]')
  if (open) return openPath(open.dataset.open!)
  const tabBtn = t.closest<HTMLElement>('[data-tab]')
  if (tabBtn) {
    tab = tabBtn.dataset.tab!
    keep('tab', tab)
    window.scrollTo(0, 0)
    render()
    if (tab === 'stats') refresh(true)
    return
  }
  const r = t.closest<HTMLElement>('[data-range]')
  if (r) {
    range = r.dataset.range!
    keep('range', range)
    return render()
  }
})
document.addEventListener('toggle', e => {
  const d = e.target as HTMLDetailsElement
  if (d.id === 'rest') {
    keep('rest', d.open ? '1' : '')
    send('size', document.documentElement.scrollHeight)
  }
}, true)

const tip = $('tip')
document.addEventListener('mousemove', e => {
  const t = (e.target as HTMLElement).closest?.('[data-tip]') as HTMLElement | SVGElement | null
  const html = t?.getAttribute('data-tip')
  if (!html) { tip.style.display = 'none'; return }
  tip.innerHTML = html
  tip.style.display = 'block'
  const r = tip.getBoundingClientRect()
  const x = Math.min(e.clientX + 12, window.innerWidth - r.width - 6)
  const yy = e.clientY + 14 + r.height > window.innerHeight ? e.clientY - r.height - 10 : e.clientY + 14
  tip.style.left = Math.max(6, x) + 'px'
  tip.style.top = yy + 'px'
})
document.addEventListener('mouseleave', () => { tip.style.display = 'none' })

$('office').onclick = () => openPath('/')
$('restart').onclick = () => send('restart')
$('log').onclick = () => send('log')
$('quit').onclick = () => send('quit')
$('autostart').onclick = () => send('autostart')

// the app calls these when the popover opens and closes
const w = window as any
w.aoShow = (state: { autostart?: boolean }) => {
  autostart = !!state?.autostart
  active = true
  refresh(true)
}
w.aoHide = () => { active = false; tip.style.display = 'none' }

if (bridge) document.body.classList.add('app')
send('ready')
$('logo').innerHTML = logo()
render()
refresh(true)
setInterval(() => { if (active) refresh() }, 5000)
