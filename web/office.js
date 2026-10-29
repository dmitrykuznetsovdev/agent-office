// Pixel office drawn as one SVG string. Pure: (studio, now) -> markup.
// Motion is SMIL; every animation's `begin` is offset by the time already
// elapsed, so a redraw continues an animation instead of restarting it.
const GUESTS = {
    'general-purpose': { name: 'Фрилансер', prop: 'cap', shirt: '#6a6f7a' },
    Explore: { name: 'Разведчик', prop: 'binoculars', shirt: '#4a6a5a' },
    Plan: { name: 'Архитектор', prop: 'paper', shirt: '#4a4a7a' },
    claude: { name: 'Фрилансер', prop: 'cap', shirt: '#6a6f7a' },
};
const SKIN = ['#f1c27d', '#e0ac69', '#c68642', '#ffdbac', '#8d5524'];
export const W = 96;
export const H = 76;
const TOP = 22;
// strip at the top of each row for the department's name
const BAND = 10;
const COLS = 5;
export const WIDTH = W * COLS;
const WALK_MS = 1000;
const FLY_MS = 900;
const CHECK_MS = 15000;
const AWAY_MS = 30 * 60000;
export function roleKey(type) {
    const i = type.lastIndexOf(':');
    return i === -1 ? type : type.slice(i + 1);
}
// Who sits where: office desks by full type, then by the bare key.
function deskOf(office, type) {
    const all = office.zones.flatMap(z => z.desks);
    return all.find(d => d.type === type) ?? all.find(d => d.type !== 'main' && roleKey(d.type) === roleKey(type));
}
export function roleName(type, office) {
    if (type === 'producer' || type === 'main')
        return office ? deskOf(office, 'main')?.name ?? 'Главный' : 'Главный';
    const desk = office && deskOf(office, type);
    if (desk)
        return desk.name;
    const guest = office?.guests[type] ?? GUESTS[type];
    if (guest)
        return guest.name;
    const key = roleKey(type);
    return key.length > 12 ? key.slice(0, 11) + '…' : key;
}
// Small zones share a row when they fit whole; big ones wrap at five desks.
// The free-lance row closes the office.
function rowsOf(office) {
    const rows = [];
    for (const z of office.zones) {
        const last = rows[rows.length - 1];
        if (last && z.desks.length > 0 && last.desks.length + z.desks.length <= COLS) {
            last.labels.push({ col: last.desks.length, text: z.name });
            last.desks.push(...z.desks);
            continue;
        }
        for (let i = 0; i < Math.max(1, z.desks.length); i += COLS) {
            rows.push({ labels: i ? [] : [{ col: 0, text: z.name }], desks: z.desks.slice(i, i + COLS) });
        }
    }
    return rows;
}
function esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function r(x, y, w, h, fill, extra = '') {
    return `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}"${extra}/>`;
}
function sec(ms) {
    return (ms / 1000).toFixed(2) + 's';
}
function hash(s) {
    let n = 0;
    for (const c of s)
        n = (n * 31 + c.charCodeAt(0)) >>> 0;
    return n;
}
function deskOrigin(col, row) {
    return [col * W, TOP + row * H + BAND];
}
function furniture(d, isProducer) {
    const { cx, cy } = d;
    const wood = isProducer ? '#c08a3e' : '#a0673d';
    const woodDark = isProducer ? '#8a5f22' : '#6e4427';
    let s = '';
    // chair
    s += r(cx + 20, cy + 27, 4, 16, '#3a3f55') + r(cx + 20, cy + 41, 14, 3, '#3a3f55') + r(cx + 26, cy + 44, 2, 7, '#2a2e40');
    // desk
    s += r(cx + 30, cy + 40, 52, 5, wood) + r(cx + 30, cy + 45, 52, 1, woodDark);
    s += r(cx + 33, cy + 46, 3, 9, woodDark) + r(cx + 76, cy + 46, 3, 9, woodDark);
    // monitor
    s += r(cx + 52, cy + 24, 22, 14, '#1b1e2b') + r(cx + 61, cy + 38, 4, 2, '#1b1e2b');
    // keyboard
    s += r(cx + 44, cy + 38, 12, 2, '#8a8fa8');
    return s;
}
function screen(d) {
    const x = d.cx + 54;
    const y = d.cy + 26;
    if (!d.isWorking)
        return r(x, y, 18, 10, '#2c3245');
    let s = r(x, y, 18, 10, '#2a6f8f');
    s += `<rect x="${x}" y="${y}" width="18" height="10" fill="#6ee7ff"><animate attributeName="opacity" values=".55;.85;.6;.9;.55" dur="1.6s" repeatCount="indefinite"/></rect>`;
    if (d.role.prop === 'chart') {
        s += r(x + 3, y + 5, 2, 4, '#0b3a4a') + r(x + 7, y + 3, 2, 6, '#0b3a4a') + r(x + 11, y + 6, 2, 3, '#0b3a4a') + r(x + 14, y + 2, 2, 7, '#0b3a4a');
        return s;
    }
    s += `<g><animateTransform attributeName="transform" type="translate" values="0 0;0 -4" dur="1.2s" repeatCount="indefinite"/>`;
    for (let i = 0; i < 4; i++) {
        const w = [12, 8, 14, 6][i] ?? 8;
        s += r(x + 2, y + 2 + i * 3, w, 1, '#0b3a4a');
    }
    s += `</g>`;
    return s;
}
function person(d, isProducer) {
    if (!d.isOccupied)
        return '';
    const ox = d.cx + 24;
    const oy = d.cy + 18;
    const skin = SKIN[d.role.skin % SKIN.length] ?? '#f1c27d';
    const { shirt, hair } = d.role;
    let body = '';
    // legs under the desk, body, head, hair, eye
    body += r(ox + 4, oy + 22, 14, 4, '#2a2e44') + r(ox + 16, oy + 26, 3, 8, '#2a2e44');
    body += r(ox + 1, oy + 9, 11, 13, shirt);
    if (isProducer)
        body += r(ox + 8, oy + 10, 2, 8, '#e0b030');
    body += r(ox + 2, oy, 9, 9, skin);
    body += r(ox + 2, oy - 1, 9, 3, hair) + r(ox + 2, oy + 2, 2, 4, hair);
    body += r(ox + 8, oy + 4, 1, 2, '#1a1a22');
    // arm toward the keyboard; it types while working
    const arm = r(ox + 8, oy + 13, 10, 3, shirt) + r(ox + 18, oy + 13, 2, 3, skin);
    body += d.isWorking
        ? `<g>${arm}<animateTransform attributeName="transform" type="translate" values="0 0;0 -1;0 0;0 0" dur=".35s" repeatCount="indefinite"/></g>`
        : arm;
    body += wearable(d.role.prop, ox, oy);
    const fade = d.isWorking ? '' : ' opacity=".8"';
    if (d.walkElapsed < WALK_MS) {
        // walks in from the left, legs ticking
        return `<g${fade}><animateTransform attributeName="transform" type="translate" from="-40 0" to="0 0" dur="${sec(WALK_MS)}" begin="${sec(-d.walkElapsed)}" fill="freeze"/>${body}</g>`;
    }
    return `<g${fade}>${body}</g>`;
}
// Props worn on the person (drawn with them, so they walk in too).
function wearable(prop, ox, oy) {
    switch (prop) {
        case 'crown':
            return r(ox + 2, oy - 3, 9, 2, '#f0c040') + r(ox + 2, oy - 5, 1, 2, '#f0c040') + r(ox + 6, oy - 5, 1, 2, '#f0c040') + r(ox + 10, oy - 5, 1, 2, '#f0c040');
        case 'headphones':
            return r(ox + 1, oy - 2, 11, 2, '#222') + r(ox + 4, oy + 3, 3, 5, '#d04040');
        case 'beret':
            return r(ox + 1, oy - 3, 11, 3, '#111') + r(ox + 6, oy - 4, 1, 1, '#111');
        case 'beretRed':
            return r(ox + 1, oy - 3, 11, 3, '#c0303a') + r(ox + 6, oy - 4, 1, 1, '#c0303a');
        case 'cap':
            return r(ox + 2, oy - 2, 9, 3, '#d06a2a') + r(ox + 9, oy, 4, 1, '#d06a2a');
        case 'glasses':
            return r(ox + 6, oy + 3, 5, 1, '#111') + r(ox + 7, oy + 4, 1, 1, '#111') + r(ox + 10, oy + 4, 1, 1, '#111');
        case 'binoculars':
            return r(ox + 9, oy + 3, 4, 3, '#222') + r(ox + 13, oy + 3, 1, 1, '#9ad');
        case 'magnifier':
            return r(ox + 17, oy + 2, 5, 1, '#ccc') + r(ox + 17, oy + 6, 5, 1, '#ccc') + r(ox + 17, oy + 3, 1, 3, '#ccc') + r(ox + 21, oy + 3, 1, 3, '#ccc') + r(ox + 18, oy + 3, 3, 3, '#9fe6ff', ' opacity=".5"') + r(ox + 18, oy + 7, 2, 5, '#6a4a2a');
        default:
            return '';
    }
}
// Props that stay on the desk.
function deskProp(d) {
    const { cx, cy } = d;
    const x = cx + 76;
    const y = cy + 40;
    switch (d.role.prop) {
        case 'camera':
            return r(cx + 32, y - 7, 9, 6, '#1a1a1a') + r(cx + 41, y - 6, 3, 4, '#777') + r(cx + 33, y - 6, 1, 1, '#e33');
        case 'scissors':
            return r(x - 4, y - 3, 1, 1, '#ccc') + r(x - 3, y - 2, 1, 1, '#ccc') + r(x - 2, y - 3, 1, 1, '#ccc') + r(x - 5, y - 2, 2, 2, '#e04040') + r(x - 1, y - 2, 2, 2, '#e04040');
        case 'books':
            return r(x - 2, y - 3, 6, 3, '#c04040') + r(x - 1, y - 6, 6, 3, '#4060c0') + r(x - 2, y - 9, 6, 3, '#40a060');
        case 'mic':
            return r(x - 1, y - 9, 3, 4, '#888') + r(x, y - 5, 1, 4, '#555') + r(x - 2, y - 1, 5, 1, '#555');
        case 'script':
        case 'paper':
            return r(x - 3, y - 2, 8, 2, '#f0eee6') + r(x - 2, y - 2, 5, 1, '#bbb') + r(x + 4, y - 5, 1, 4, '#e0b030');
        case 'box':
            return r(x - 4, y - 8, 9, 8, '#c8955a') + r(x, y - 8, 1, 8, '#a8753a');
        case 'download':
            return r(x - 3, y - 8, 7, 8, '#2e3a50') + r(x, y - 7, 1, 4, '#5f5') + r(x - 1, y - 4, 3, 1, '#5f5') + r(x, y - 3, 1, 1, '#5f5');
        case 'layers':
            return r(x - 4, y - 3, 7, 2, '#6aa0e0') + r(x - 3, y - 5, 7, 2, '#e0a06a', ' opacity=".9"') + r(x - 2, y - 7, 7, 2, '#a06ae0', ' opacity=".9"');
        case 'frame':
            return r(x - 4, y - 9, 9, 8, '#e0b030') + r(x - 3, y - 8, 7, 6, '#7ac') + r(x - 3, y - 4, 7, 2, '#4a8a3a');
        case 'tablet':
            return r(x - 5, y - 2, 9, 2, '#333') + r(x + 3, y - 6, 1, 4, '#c060e0');
        case 'clipboard':
            return r(x - 3, y - 9, 7, 9, '#a0703a') + r(x - 2, y - 8, 5, 7, '#f0eee6') + r(x - 1, y - 5, 1, 1, '#3a3') + r(x, y - 4, 1, 1, '#3a3') + r(x + 1, y - 6, 1, 1, '#3a3');
        case 'beretRed':
            return r(x - 3, y - 2, 7, 1, '#c0303a');
        default:
            return '';
    }
}
function bubble(d) {
    if (!d.isWorking || !d.activity)
        return '';
    const text = d.activity.length > 24 ? d.activity.slice(0, 23) + '…' : d.activity;
    const w = Math.round(text.length * 4.0 + 9);
    const headX = d.cx + 30;
    const x = Math.max(d.cx - 20, Math.min(headX - 8, WIDTH - w - 2));
    const y = d.cy + 1;
    return (`<g>` +
        `<rect x="${x}" y="${y}" width="${w}" height="11" rx="2" fill="#f4f1e8"/>` +
        r(headX, y + 11, 3, 2, '#f4f1e8') + r(headX + 1, y + 13, 1, 1, '#f4f1e8') +
        `<text x="${x + 4}" y="${y + 8}" font-size="6.5" fill="#1d1f2e" font-family="Menlo, ui-monospace, monospace">${esc(text)}</text>` +
        `</g>`);
}
function check(d) {
    if (d.isWorking || d.checkElapsed >= CHECK_MS)
        return '';
    const x = d.cx + 28;
    const y = d.cy + 4;
    const fadeAt = CHECK_MS - 2000 - d.checkElapsed;
    return (`<g>` +
        `<animate attributeName="opacity" from="1" to="0" dur="2s" begin="${sec(fadeAt)}" fill="freeze"/>` +
        r(x, y + 4, 2, 2, '#4ade80') + r(x + 2, y + 6, 2, 2, '#4ade80') + r(x + 4, y + 4, 2, 2, '#4ade80') + r(x + 6, y + 2, 2, 2, '#4ade80') + r(x + 8, y, 2, 2, '#4ade80') +
        `</g>`);
}
function badge(d) {
    if (d.count < 2)
        return '';
    const x = d.cx + W - 14;
    const y = d.cy + 16;
    return `<rect x="${x}" y="${y}" width="12" height="9" rx="4" fill="#e0b030"/><text x="${x + 6}" y="${y + 7}" font-size="6.5" text-anchor="middle" fill="#1d1f2e" font-family="Menlo, monospace">×${d.count}</text>`;
}
export function money(usd) {
    return '$' + (usd < 10 ? usd.toFixed(2) : usd < 100 ? usd.toFixed(1) : Math.round(usd).toString());
}
// What the desk has earned this session, all its hires together.
function salary(d) {
    if (d.cost < 0.005)
        return '';
    return `<text x="${d.cx + W - 4}" y="${d.cy + 9}" font-size="6.5" text-anchor="end" fill="#6ee7a0" font-family="Menlo, monospace">${money(d.cost)}</text>`;
}
function plate(d) {
    const fill = d.isOccupied ? '#e8e6f0' : '#6a6f90';
    const weight = d.isWorking ? ' font-weight="bold"' : '';
    return `<text x="${d.cx + 52}" y="${d.cy + 63}" font-size="7" text-anchor="middle" fill="${fill}"${weight} font-family="-apple-system, Helvetica, sans-serif">${esc(d.role.name)}</text>`;
}
// A sheet (task) flying producer -> desk, or a folder (result) back.
function flight(from, to, elapsed, color) {
    if (elapsed >= FLY_MS)
        return '';
    const [x1, y1] = from;
    const [x2, y2] = to;
    const mx = (x1 + x2) / 2;
    const my = Math.min(y1, y2) - 30;
    return (`<g opacity="0">` +
        r(0, 0, 6, 5, color) + r(1, 1, 4, 1, '#0003') +
        `<animateMotion path="M${x1},${y1} Q${mx},${my} ${x2},${y2}" dur="${sec(FLY_MS)}" begin="${sec(-elapsed)}" fill="freeze"/>` +
        `<animate attributeName="opacity" values="1;1;0" keyTimes="0;.85;1" dur="${sec(FLY_MS)}" begin="${sec(-elapsed)}" fill="freeze"/>` +
        `</g>`);
}
function deskCenter(col, row) {
    const [cx, cy] = deskOrigin(col, row);
    return [cx + 60, cy + 34];
}
function background(labels, height) {
    let s = r(0, 0, WIDTH, height, '#232538');
    for (let row = 0; row < labels.length; row++) {
        const y = TOP + row * H;
        s += r(0, y, WIDTH, H, row % 2 ? '#2a2d45' : '#2e3150');
        // floor boards
        for (let i = 0; i < WIDTH; i += 24)
            s += r(i, y + H - 6, 1, 6, '#00000022');
        s += r(0, y + H - 1, WIDTH, 1, '#1c1e30');
        s += r(0, y, WIDTH, BAND, '#00000026');
        for (const l of labels[row] ?? []) {
            // a zone that starts mid-row gets a wall to its left
            if (l.col)
                s += r(l.col * W - 1, y, 2, H - 1, '#1c1e30');
            s += `<text x="${l.col * W + 5}" y="${y + 7.5}" font-size="6" letter-spacing=".8" fill="#7a80a8" font-family="-apple-system, Helvetica, sans-serif">${esc(l.text)}</text>`;
        }
    }
    // decor in the freelance row
    const free = labels.length - 1;
    const [px, py] = deskOrigin(3, free);
    s += r(px + 20, py + 40, 10, 12, '#8a5a3a') + r(px + 16, py + 26, 6, 14, '#3f8a4a') + r(px + 22, py + 22, 6, 18, '#4fa05a') + r(px + 28, py + 28, 6, 12, '#3f8a4a');
    s += r(px + 56, py + 22, 12, 30, '#d8dde8') + r(px + 58, py + 16, 8, 8, '#7ac0f0', ' opacity=".85"') + r(px + 59, py + 36, 2, 2, '#e44') + r(px + 63, py + 36, 2, 2, '#44e');
    const [sx, sy] = deskOrigin(4, free);
    s += r(sx + 10, sy + 34, 70, 12, '#7a3a4a') + r(sx + 10, sy + 26, 70, 9, '#8a4a5a') + r(sx + 8, sy + 30, 6, 16, '#6a2a3a') + r(sx + 76, sy + 30, 6, 16, '#6a2a3a');
    s += `<text x="${sx + 45}" y="${sy + 60}" font-size="7" text-anchor="middle" fill="#6a6f90" font-family="-apple-system, Helvetica, sans-serif">кулер и диван</text>`;
    return s;
}
function header(st, now, title) {
    const working = st.agents.filter(a => a.isWorking).length + (st.producer.isBusy ? 1 : 0);
    const mins = st.since ? Math.floor((now - st.since) / 60000) : 0;
    const clock = `${Math.floor(mins / 60)}:${String(mins % 60).padStart(2, '0')}`;
    let s = r(0, 0, WIDTH, TOP, '#191a29');
    s += `<text x="6" y="14" font-size="9" font-weight="bold" letter-spacing="1" fill="#f0c040" font-family="-apple-system, Helvetica, sans-serif">${esc(title)}</text>`;
    s += `<text x="${WIDTH - 6}" y="14" font-size="7.5" text-anchor="end" fill="#b8bcd8" font-family="Menlo, monospace">в работе ${working} · агентов ${st.agents.length} · ${clock} · итого ${money(st.cost)}</text>`;
    return s;
}
export function render(st, now, office) {
    const desks = new Map();
    const flights = [];
    const rows = rowsOf(office);
    const free = rows.length;
    const height = TOP + H * (free + 1);
    const blank = { isOccupied: false, isWorking: false, activity: '', count: 0, cost: 0, walkElapsed: Infinity, checkElapsed: Infinity };
    rows.forEach((row, ri) => row.desks.forEach((od, col) => {
        const [cx, cy] = deskOrigin(col, ri);
        const role = { name: od.name, shirt: od.shirt, hair: od.hair, skin: od.skin, prop: od.prop, col, row: ri };
        desks.set(od.type, { cx, cy, role, ...blank });
    }));
    // an office without a boss desk still has one: first free seat
    if (!desks.has('main')) {
        const [cx, cy] = deskOrigin(0, free);
        desks.set('main', { cx, cy, role: { name: 'Главный', shirt: '#3b3f8f', hair: '#4a3020', skin: 1, prop: 'crown', col: 0, row: free }, ...blank });
    }
    const prod = desks.get('main');
    const producerHand = deskCenter(prod.role.col, prod.role.row);
    prod.isOccupied = true;
    prod.isWorking = st.producer.isBusy;
    prod.activity = st.producer.activity;
    prod.cost = st.producer.cost;
    const seat = (type) => {
        const od = deskOf(office, type);
        return od ? od.type : 'guest:' + type;
    };
    // Guests take the free-lance desks in order of arrival, working ones first.
    const guests = [];
    const firstGuest = desks.get('main').role.row === free ? 1 : 0;
    for (const a of [...st.agents].filter(a => a.isWorking || now - a.endedAt < AWAY_MS).sort((x, y) => Number(y.isWorking) - Number(x.isWorking) || x.startedAt - y.startedAt)) {
        if (deskOf(office, a.type) || guests.includes(a.type))
            continue;
        if (guests.length < 3 - firstGuest)
            guests.push(a.type);
    }
    guests.forEach((type, i) => {
        const g = office.guests[type] ?? GUESTS[type] ?? { name: roleName(type), prop: 'cap' };
        const shirt = GUESTS[type]?.shirt ?? '#5a6a8a';
        const col = i + firstGuest;
        const [cx, cy] = deskOrigin(col, free);
        const role = { name: g.name, shirt, hair: ['#2b1d14', '#c9a227', '#111'][hash(type) % 3] ?? '#111', skin: hash(type) % 5, prop: g.prop, col, row: free };
        desks.set('guest:' + type, { cx, cy, role, ...blank });
    });
    for (const a of st.agents) {
        const d = desks.get(seat(a.type));
        if (d)
            d.cost += a.cost;
    }
    // Who finished long ago has gone home: their desk is free again.
    const present = st.agents.filter(a => a.isWorking || now - a.endedAt < AWAY_MS);
    for (const a of present) {
        const d = desks.get(seat(a.type));
        if (!d)
            continue;
        d.isOccupied = true;
        d.walkElapsed = Math.min(d.walkElapsed, now - a.startedAt);
        const to = deskCenter(d.role.col, d.role.row);
        if (a.isWorking) {
            d.count += 1;
            if (!d.isWorking || a.startedAt > 0)
                d.activity = a.activity || a.desc;
            d.isWorking = true;
            flights.push(flight(producerHand, to, now - a.startedAt, '#f4f1e8'));
        }
        else {
            d.checkElapsed = Math.min(d.checkElapsed, now - a.endedAt);
            flights.push(flight(to, producerHand, now - a.endedAt, '#f0c040'));
        }
    }
    let s = background([...rows.map(r => r.labels), [{ col: 0, text: 'ФРИЛАНС' }]], height) + header(st, now, office.title);
    for (const [key, d] of desks) {
        const isProducer = key === 'main';
        s += furniture(d, isProducer) + screen(d) + person(d, isProducer) + deskProp(d) + plate(d) + badge(d) + salary(d);
    }
    for (const d of desks.values())
        s += check(d) + bubble(d);
    s += flights.join('');
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${WIDTH} ${height}" width="${WIDTH}" height="${height}" shape-rendering="crispEdges">${s}</svg>`;
}
