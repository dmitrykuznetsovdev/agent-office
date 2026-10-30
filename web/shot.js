// The «Скриншот» file: its funny name and its PNG.
// by how many are busy: nobody, one, a crowd
const NAMES = [
    ['Все на кофе', 'Тихий час', 'Ушли на обед и не вернулись', 'Офис притворяется спящим', 'Здесь могла быть ваша работа'],
    ['Один за всех', 'Кто-то же должен работать', 'Последний герой', 'Остальные на совещании', 'Сам себе отдел'],
    ['Кипит работа', 'Застукали за работой', 'Фото для инвесторов', 'Аврал по плану', 'Деньги горят красиво'],
];
export function funnyName(working) {
    const list = NAMES[Math.min(working, 2)];
    return list[Math.floor(Math.random() * list.length)];
}
export function pngOf(canvas) {
    return new Promise((ok, no) => canvas.toBlob(b => b ? ok(b) : no(new Error('картинка не собралась')), 'image/png'));
}
