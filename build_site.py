#!/usr/bin/env python3
"""Собирает карту ЦТ со слайдером времени и занятостью аудиторий:
site/index.html (всё на одном экране, главная) и site/floors.html (этажи по отдельности).

Запуск:
  python3 build_site.py [путь/к/raspisanie-spiskom.xlsx]

Без аргумента берёт самый свежий raspisanie-spiskom*.xlsx из ~/Downloads и ~/Desktop.
xlsx: выгрузка cu-schedule.ru «Скачать Excel → Списком», листы по неделям.
Колонки ищутся по заголовкам, поэтому порядок колонок может меняться.

Зависимость: openpyxl. Геометрия карты берётся из map/ (строится один раз
скриптом map/extract_rooms.py из PDF-плана).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
MAP_DIR = ROOT / "map"
# Два варианта страницы. Главная (index.html): всё на одном экране, 3 этаж + врезка 2 этажа.
# floors.html: этажи по отдельности с переключателем.
OUTPUTS = {"combined": ROOT / "site" / "index.html", "floors": ROOT / "site" / "floors.html"}

REQUIRED = {"Дата": "date", "Начало": "start", "Конец": "end", "Аудитория": "room"}
OPTIONAL = {"Название": "title", "Тип": "type", "Курс": "course"}
# Номера на карте: S202.1; в таблице бывает S202-1 (приводится в room_tokens).
SNWE_LIKE_RE = re.compile(r"^[SNWE]\d")

# Сетка пар ЦУ
SLOTS = [("08:30", "09:50"), ("10:00", "11:20"), ("11:30", "12:50"), ("13:00", "14:20"),
         ("14:30", "15:50"), ("16:00", "17:20"), ("17:30", "18:50"), ("19:00", "20:20"),
         ("20:35", "21:55")]

# Кадр карты в координатах PDF (pt): только здание, без заголовков и легенды.
# В «одном экране» кадр ниже: в пустом правом нижнем углу 3 этажа стоит врезка 2 этажа.
# В «одном экране» справа пустое поле обрезано: восточная стена здания проходит около x=1015.
VIEWBOX = {"floors": (210, 122, 870, 580), "combined": (210, 122, 812, 600)}
INSET = (806, 630, 208, 86)  # x, y, ширина, высота врезки


def find_xlsx(arg: str | None) -> Path:
    if arg:
        path = Path(arg).expanduser()
        if not path.exists():
            sys.exit(f"Нет файла: {path}")
        return path
    candidates = []
    for folder in (Path.home() / "Downloads", Path.home() / "Desktop"):
        candidates += folder.glob("raspisanie-spiskom*.xlsx")
    if not candidates:
        sys.exit("Не нашёл raspisanie-spiskom*.xlsx в Загрузках и на Рабочем столе. Передай путь аргументом.")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def to_minutes(value) -> int | None:
    if isinstance(value, dt.datetime):
        value = value.time()
    if isinstance(value, dt.time):
        return value.hour * 60 + value.minute
    if isinstance(value, str):
        m = re.match(r"^\s*(\d{1,2}):(\d{2})", value)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2))
    return None


def to_date(value) -> str | None:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        m = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            return "-".join(m.groups())
        m = re.match(r"^\s*(\d{2})\.(\d{2})\.(\d{4})", value)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def room_tokens(raw) -> list[str]:
    if not raw:
        return []
    tokens = []
    for part in re.split(r"[+,;/]| и ", str(raw)):
        t = part.strip().upper().replace("Е", "E").replace("-", ".")
        if t:
            tokens.append(t)
    return tokens


def load_events(xlsx: Path, known_rooms: set[str]):
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    events = set()
    unknown_snwe = Counter()
    rows_total = rows_bad = 0
    all_days = set()  # период таблицы целиком, а не только дни с парами в S/N/W/E
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            continue
        index = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
        missing = [name for name in REQUIRED if name not in index]
        if missing:
            print(f"  лист «{ws.title}» пропущен: нет колонок {missing}", file=sys.stderr)
            continue
        col = {key: index[name] for name, key in {**REQUIRED, **OPTIONAL}.items() if name in index}
        for row in rows:
            if not row or all(v is None for v in row):
                continue
            rows_total += 1
            get = lambda key: row[col[key]] if key in col and col[key] < len(row) else None
            day, start, end = to_date(get("date")), to_minutes(get("start")), to_minutes(get("end"))
            if not day or start is None or end is None:
                rows_bad += 1
                continue
            all_days.add(day)
            if end <= start:
                end = start + 60
            title = str(get("title") or get("course") or "Занятие").strip()
            kind = str(get("type") or "").strip()
            for room in room_tokens(get("room")):
                if room in known_rooms:
                    events.add((room, day, start, end, title, kind))
                elif SNWE_LIKE_RE.match(room):
                    unknown_snwe[room] += 1
    return sorted(events, key=lambda e: (e[1], e[0], e[2])), unknown_snwe, rows_total, rows_bad, all_days


def svg_data_uri(path: Path) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def load_geometry():
    """Контуры с карты: фигуры по этажам и множество номеров аудиторий."""
    geometry = json.loads((MAP_DIR / "rooms.json").read_text("utf-8"))
    floors = {}
    known_rooms = set()
    for floor, items in geometry["floors"].items():
        shapes = []
        for it in items:
            if not it.get("poly"):
                continue
            kind = it["kind"] if it["kind"] in ("room", "study") else "blocked"
            shapes.append({"kind": kind, "id": it["id"], "name": it["name"], "poly": it["poly"],
                           "center": it["center"], "r": it["r"]})
            if it["kind"] == "room":
                known_rooms.add(it["id"])
        floors[floor] = {"shapes": shapes,
                         "background": svg_data_uri(MAP_DIR / f"floor{floor}.svg"),
                         "walls": svg_data_uri(MAP_DIR / f"walls{floor}.svg")}
    return floors, known_rooms


def known_rooms() -> set[str]:
    return load_geometry()[1]


def write_atomic(path: Path, text: str) -> None:
    """Пишем во временный файл рядом и подменяем: страница никогда не бывает недописанной."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def build(xlsx: Path) -> dict:
    """Собирает обе версии страницы из xlsx. Возвращает статистику для проверок и отчёта."""
    floors, rooms = load_geometry()
    events, unknown, rows_total, rows_bad, all_days = load_events(xlsx, rooms)

    # Компактная форма: {date: {room: [[start, end, titleIdx, typeIdx], ...]}}
    titles, kinds = [], []
    title_idx, kind_idx = {}, {}
    by_day: dict[str, dict[str, list]] = {}
    for room, day, start, end, title, kind in events:
        ti = title_idx.setdefault(title, len(titles))
        if ti == len(titles):
            titles.append(title)
        ki = kind_idx.setdefault(kind, len(kinds))
        if ki == len(kinds):
            kinds.append(kind)
        by_day.setdefault(day, {}).setdefault(room, []).append([start, end, ti, ki])

    days = sorted(all_days)
    template = (ROOT / "site" / "template.html").read_text("utf-8")
    for layout, out in OUTPUTS.items():
        data = {
            "layout": layout,
            "generatedAt": dt.datetime.now().strftime("%d.%m.%Y %H:%M"),
            "source": xlsx.name,
            "range": [days[0], days[-1]] if days else None,
            "viewBox": VIEWBOX[layout],
            "inset": INSET,
            "slots": SLOTS,
            # в «одном экране» подложка 2 этажа не нужна: от него только аудитории и столовая
            "floors": floors if layout == "floors" else {
                "3": floors["3"], "2": {"shapes": floors["2"]["shapes"]}},
            "titles": titles,
            "kinds": kinds,
            "days": by_day,
        }
        write_atomic(out, template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":"))))

    return {
        "rows": rows_total, "rows_bad": rows_bad, "events": len(events),
        "range": [days[0], days[-1]] if days else None,
        "rooms_on_map": len(rooms), "rooms_used": len({e[0] for e in events}),
        "unknown_rooms": dict(unknown),
    }


def print_report(xlsx: Path, st: dict) -> None:
    print(f"Таблица: {xlsx}")
    print(f"Строк {st['rows']}, не разобрано {st['rows_bad']}; занятий в аудиториях карты: {st['events']}")
    print(f"Период: {st['range'][0]} .. {st['range'][1]}" if st["range"] else "Период: нет данных")
    print(f"Аудиторий на карте {st['rooms_on_map']}, из них встречаются в расписании {st['rooms_used']}")
    if st["unknown_rooms"]:
        print(f"ВНИМАНИЕ: в таблице есть аудитории S/N/W/E, которых нет на карте: {st['unknown_rooms']}")
    for out in OUTPUTS.values():
        print(f"Готово: {out} ({out.stat().st_size // 1024} КБ)")


def main() -> int:
    xlsx = find_xlsx(sys.argv[1] if len(sys.argv) > 1 else None)
    print_report(xlsx, build(xlsx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
