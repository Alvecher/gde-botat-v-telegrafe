"""Восстанавливает контуры помещений с векторного плана ЦТ.

Для каждой подписи (плашки) на этаже: рендер этажа -> маска стен ->
закрытие дверных проёмов -> заливка от точки рядом с плашкой -> контур.
Размер ядра закрытия подбирается минимальный, при котором заливка
не протекает к чужим подписям.
"""
import json, os, re, sys
import cv2, numpy as np, pymupdf

# Запуск: python extract_rooms.py "ЦТ_план.pdf" rooms.json
# Зависимости (в отдельном venv): pymupdf, opencv-python-headless, numpy
PDF = sys.argv[1]
OUT = sys.argv[2]
Z = 3  # пикселей на pt
FLOOR_GREY = (0.902, 0.902, 0.902)  # заливка пола на плане

# Виртуальные стены (pt) там, где на плане проёмы слишком широкие и заливка протекает.
CUTS = {
    '2': [((580.0, 558.9), (715.3, 545.8))],            # верх S203, ниша
    '3': [((280.8, 274.2), (375.6, 274.2)),              # низ Work in progress, коридор
          ((887.8, 441.0), (874.7, 513.0)),              # E303 / соседняя комната
          ((928.4, 272.0), (898.7, 408.2))],             # левая стена E301
}

ROOM_RE = re.compile(r'^[SNWE]\d{3}(\.\d)?$')
SPECIAL = {'˜': 'П', '˚': 'О', 'ˆ': 'К', '˛': 'Н', '˝': 'М', '˘': 'И', '˙': 'Л'}


def dec(s):
    """Шрифт плана: кириллица сдвинута на 0x400 вниз, часть заглавных заменена спецзнаками.
    Цифры и строчные кириллические буквы пересекаются, поэтому латинские строки
    (номера аудиторий, Work in progress) не трогаем, а остальное сдвигаем целиком."""
    if ROOM_RE.match(s) or any('a' <= ch <= 'z' for ch in s):
        return s
    out = []
    for ch in s:
        o = ord(ch)
        if ch in SPECIAL:
            out.append(SPECIAL[ch])
        elif ch == ' ':
            out.append(' ')
        elif 0x10 <= o <= 0x4F:
            out.append(chr(o + 0x400))
        else:
            out.append(ch)
    return ''.join(out)


COLORS = {
    'common': (0.08, 0.08, 0.08),
    'food': (0.0, 0.65, 0.32),
    'staff': (0.47, 0.35, 1.0),
    'wip': (0.44, 0.76, 0.8),
}


def color_kind(fill):
    for kind, c in COLORS.items():
        if all(abs(a - b) < 0.03 for a, b in zip(fill, c)):
            return kind
    return None


def label_boxes(page):
    """Плашки: небольшие залитые прямоугольники фирменных цветов."""
    boxes = []
    for dr in page.get_drawings():
        fill = dr.get('fill')
        if not fill:
            continue
        kind = color_kind(fill)
        r = dr['rect']
        if kind and 8 < r.width < 160 and 6 < r.height < 120 and len(dr['items']) <= 6:
            boxes.append((kind, r))
    return boxes


def spans(page):
    res = []
    for b in page.get_text('dict')['blocks']:
        for l in b.get('lines', []):
            for s in l['spans']:
                t = s['text'].strip()
                if t:
                    res.append((t, pymupdf.Rect(s['bbox'])))
    return res


def build_labels(page):
    boxes = label_boxes(page)
    sp = spans(page)
    building = pymupdf.Rect(200, 120, 1080, 700)
    labels = []
    for t, r in sp:
        if not r.intersects(building):
            continue
        c = (r.tl + r.br) / 2
        box = None
        for i, (kind, br) in enumerate(boxes):
            if br.contains(c):
                box = (i, kind, br)
                break
        if box is None:
            continue
        i, kind, br = box
        text = t if ROOM_RE.match(t) else dec(t)
        labels.append({'text': text, 'kind': kind, 'box': br, 'span': r, 'box_id': i})
    # группируем «Аудитория» + код: код берём, слово «Аудитория» выкидываем
    out = {}
    for lb in labels:
        key = lb['box_id']
        # «Аудитория» и номер иногда в разных плашках-соседях: привяжем по близости позже
        out.setdefault(key, []).append(lb)
    merged = []
    for key, items in out.items():
        texts = [x['text'] for x in items]
        box = items[0]['box']
        merged.append({'texts': texts, 'kind': items[0]['kind'], 'box': box})
    # плашка «Аудитория» стоит над плашкой с номером: объединяем соседние
    rooms, others = [], []
    for m in merged:
        code = next((t for t in m['texts'] if ROOM_RE.match(t)), None)
        if code:
            rooms.append({'id': code, 'kind': 'room', 'box': m['box'], 'name': 'Аудитория ' + code})
        elif any(t.startswith('Аудитори') for t in m['texts']):
            continue
        else:
            others.append({'id': None, 'kind': m['kind'], 'box': m['box'], 'name': ' '.join(m['texts'])})
    # расширяем бокс аудитории плашкой «Аудитория» над ним
    for r in rooms:
        for m in merged:
            if any(t.startswith('Аудитори') for t in m['texts']):
                b = m['box']
                if abs(b.x0 - r['box'].x0) < 4 and 0 <= r['box'].y0 - b.y1 < 4:
                    r['box'] = r['box'] | b
    return rooms, others, sp


def walls_page(page, color=(0, 0, 0)):
    """Чистая страница, на которой перерисованы только стены плана."""
    tmp = pymupdf.open()
    wp = tmp.new_page(width=page.rect.width, height=page.rect.height)
    for dr in page.get_drawings():
        f = dr.get('fill'); r = dr['rect']
        if not (f and max(f) < 0.1 and r.width > 60 and r.height > 60 and r.x0 > 200 and r.y1 < 760):
            continue
        sh = wp.new_shape()
        for it in dr['items']:
            if it[0] == 'l':
                sh.draw_line(it[1], it[2])
            elif it[0] == 'c':
                sh.draw_bezier(it[1], it[2], it[3], it[4])
            elif it[0] == 're':
                sh.draw_rect(it[1])
            elif it[0] == 'qu':
                sh.draw_quad(it[1])
        sh.finish(fill=color, color=None, even_odd=bool(dr.get('even_odd')), closePath=bool(dr.get('closePath')))
        sh.commit()
    return tmp, wp


def render_mask(page, floor):
    """Маска только из стен плюс виртуальные стены CUTS."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(Z, Z), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3).copy()
    tmp, wp = walls_page(page)
    wpix = wp.get_pixmap(matrix=pymupdf.Matrix(Z, Z), alpha=False)
    walls = np.frombuffer(wpix.samples, dtype=np.uint8).reshape(wpix.h, wpix.w, 3)
    mask = (walls[:, :, 0] < 128).astype(np.uint8)
    for (x0, y0), (x1, y1) in CUTS.get(floor, []):
        cv2.line(mask, (int(x0 * Z), int(y0 * Z)), (int(x1 * Z), int(y1 * Z)), 1, int(1.5 * Z))
    return img, mask


def redact_rects(page):
    """Плашки, которые на нашей карте рисуем сами: «Аудитория», номера,
    недоступные зоны, столовая. Возвращает их прямоугольники."""
    boxes = label_boxes(page)
    sp = spans(page)
    rects = []
    for kind, br in boxes:
        texts = [dec(t) for t, r in sp if br.contains((r.tl + r.br) / 2)]
        if (kind in ('staff', 'wip')
                or any(ROOM_RE.match(t) or t.startswith('Аудитори') or t.startswith('Обеденн') for t in texts)):
            rects.append(br)
    return rects


def region_for(mask, seed_pts, other_boxes, k_pt):
    k = max(1, int(round(k_pt * Z)))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    free = (1 - closed).astype(np.uint8)
    h, w = free.shape
    for (x, y) in seed_pts:
        px, py = int(x * Z), int(y * Z)
        if not (0 <= py < h and 0 <= px < w and free[py, px]):
            continue
        ff = free.copy()
        fmask = np.zeros((h + 2, w + 2), np.uint8)
        cv2.floodFill(ff, fmask, (px, py), 2)
        reg = (ff == 2).astype(np.uint8)
        # протекли наружу здания
        if reg[0, :].any() or reg[-1, :].any() or reg[:, 0].any() or reg[:, -1].any():
            return reg, ['exterior']
        # протекли к чужой подписи: центр чужой плашки оказался внутри области
        leaked = []
        for name, b in other_boxes:
            cx, cy = int((b.x0 + b.x1) / 2 * Z), int((b.y0 + b.y1) / 2 * Z)
            if 0 <= cy < h and 0 <= cx < w and reg[cy, cx]:
                leaked.append(name)
        return reg, leaked
    return None, None


def polygon(reg):
    # возвращаем дверные «щели» обратно: дилатация на полстены, затем внешний контур
    cnts, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cnts, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, 1.2 * Z, True)
    return [[round(float(p[0][0]) / Z, 1), round(float(p[0][1]) / Z, 1)] for p in c], cv2.contourArea(c) / Z / Z


def seeds_around(box):
    m = 5
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    return [(cx, cy), (cx, box.y1 + m), (box.x1 + m, cy), (box.x0 - m, cy), (cx, box.y0 - m),
            (box.x1 + m, box.y1 + m), (box.x0 - m, box.y1 + m), (cx, box.y1 + 12), (box.x1 + 14, cy)]


def main():
    doc = pymupdf.open(PDF)
    result = {'floors': {}}
    for floor, pi in (('2', 2), ('3', 3)):
        page = doc[pi]
        rooms, others, sp = build_labels(page)
        img, mask = render_mask(page, floor)
        items = rooms + others
        # «сторожа»: все прочие плашки этажа, включая коридорные (лестницы, атриум)
        corridor_boxes = []
        for t, r in sp:
            d = dec(t)
            if d.startswith(('с ', 'со ', 'Атри', 'Ресеп')):
                corridor_boxes.append((d, r))
        out = []
        for it in [x for x in items if x['kind'] in ('room', 'staff', 'wip') or x['name'].startswith('Обеденн')]:
            others_b = [(o['name'], o['box']) for o in items if o is not it] + corridor_boxes
            best = None
            for k_pt in (1, 2, 3, 4, 5, 6, 8, 10, 12, 14, 17, 20, 24, 28, 32):
                reg, leaked = region_for(mask, seeds_around(it['box']), others_b, k_pt)
                if reg is None:
                    continue
                if not leaked:
                    best = (reg, k_pt)
                    break
            kind = 'study' if it['name'].startswith('Обеденн') else it['kind']
            entry = {'id': it['id'], 'name': it['name'], 'kind': kind,
                     'label': [round(it['box'].x0, 1), round(it['box'].y0, 1), round(it['box'].x1, 1), round(it['box'].y1, 1)]}
            if best:
                poly, area = polygon(best[0])
                dist = cv2.distanceTransform(best[0], cv2.DIST_L2, 5)
                _, rmax, _, (mx, my) = cv2.minMaxLoc(dist)
                entry.update(poly=poly, area=round(area), kernel=best[1],
                             center=[round(mx / Z, 1), round(my / Z, 1)], r=round(rmax / Z, 1))
            else:
                entry.update(poly=None)
            out.append(entry)
            print(floor, entry['id'] or entry['name'], entry['kind'], 'k=', entry.get('kernel'), 'area=', entry.get('area'), 'pts=', len(entry['poly'] or []))
        result['floors'][floor] = out
        # отладочная картинка
        dbg = img.copy()
        for e in out:
            if e['poly']:
                pts = (np.array(e['poly']) * Z).astype(np.int32)
                col = {'room': (255, 0, 0), 'staff': (120, 90, 255), 'wip': (0, 160, 200), 'study': (0, 170, 80)}[e['kind']]
                ov = dbg.copy()
                cv2.fillPoly(ov, [pts], col)
                dbg = cv2.addWeighted(ov, 0.35, dbg, 0.65, 0)
                cv2.polylines(dbg, [pts], True, col, 2)
        outdir = os.path.dirname(OUT) or '.'
        # подложка: копия страницы в памяти, PDF на диске не меняется
        base_doc = pymupdf.open(PDF)
        bp = base_doc[pi]
        for r in redact_rects(bp):
            # графику плашек редакция в этом PDF не удаляет, поэтому закрашиваем
            # цветом пола; стены поверх восстанавливает отдельный слой walls*.svg
            bp.add_redact_annot(r + (-1, -1, 1, 1), fill=FLOOR_GREY, cross_out=False)
        bp.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                            text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        open(os.path.join(outdir, f'floor{floor}.svg'), 'w').write(bp.get_svg_image(text_as_path=True))
        if os.environ.get('DEBUG_PNG'):
            bp.get_pixmap(dpi=110, clip=pymupdf.Rect(210, 122, 1080, 702)).save(OUT.replace('.json', f'_base{floor}.png'))
        _, wp = walls_page(page, color=(0.08, 0.08, 0.08))
        open(os.path.join(outdir, f'walls{floor}.svg'), 'w').write(wp.get_svg_image())
        if os.environ.get('DEBUG_PNG'):
          cv2.imwrite(OUT.replace('.json', f'_floor{floor}.png'), cv2.cvtColor(dbg, cv2.COLOR_RGB2BGR)[int(120 * Z):int(700 * Z), int(200 * Z):int(1090 * Z)])
    json.dump(result, open(OUT, 'w'), ensure_ascii=False, indent=1)


main()
