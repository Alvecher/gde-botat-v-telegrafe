#!/usr/bin/env python3
"""Сбор занятий ЦУ для поиска свободных аудиторий.

Источники:
  lms          https://my.centraluniversity.ru/api/micro-lms/calendar-events/single-date
               нужна cookie bff.cookie в переменной CU_LMS_BFF_COOKIE
  cu-schedule  https://cu-schedule.ru/api/timetable, без авторизации,
               но зависит от чужого бэкенда (в сентябре 2026 отдавал 401 от LMS)

Результат:
  data/raw/<source>/<дата>.json   сырые ответы (кэш только для прошедших дней)
  data/occupancy.json             нормализованные занятия + список всех аудиторий

Свободные аудитории считаются на клиенте: все аудитории минус занятые в момент t.
Только стандартная библиотека Python.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

LMS_URL = "https://my.centraluniversity.ru/api/micro-lms/calendar-events/single-date?date={day}"
CU_SCHEDULE_URL = "https://cu-schedule.ru/api/timetable?weekStart={day}"

SEMESTER_START = date(2026, 9, 1)
SEMESTER_END = date(2026, 12, 31)
USER_AGENT = "cu-free-rooms/0.1 (student project)"
# Время ЦУ московское: UTC+3 круглый год.
MSK = timezone(timedelta(hours=3))


class AuthError(Exception):
    pass


# ---------- HTTP ----------

def http_get_json(url: str, cookie: str | None = None) -> object:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if cookie:
        headers["Cookie"] = f"bff.cookie={cookie}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AuthError(f"HTTP {error.code}: cookie недействительна или истекла") from error
        body = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {error.code} для {url}: {body}") from error


def normalize_cookie(raw: str) -> str:
    value = raw.strip().strip('"').strip("'")
    if value.startswith("bff.cookie="):
        value = value[len("bff.cookie="):]
    return value


# ---------- разбор ответа ----------

def extract_events(payload: object) -> list[dict]:
    """Достаёт список событий из ответа неизвестной формы."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("events", "items", "data", "content", "result", "calendarEvents"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = extract_events(value)
                if nested:
                    return nested
        for value in payload.values():
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                return value
    return []


def entity_label(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("title", "name", "label", "value"):
            label = value.get(key)
            if isinstance(label, str) and label.strip():
                return label.strip()
    return ""


def normalize_time(value: object) -> str | None:
    """'10:00', '10:00:00', '2026-09-23T10:00:00+03:00' -> '10:00'."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "T" in text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(MSK)
        return parsed.strftime("%H:%M")
    parts = text.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1][:2].isdigit():
        return f"{int(parts[0]):02d}:{parts[1][:2]}"
    return None


def normalize_event(item: dict, requested_day: str, source: str) -> dict | None:
    day = item.get("date") if isinstance(item.get("date"), str) else requested_day
    day = day[:10]
    start = normalize_time(item.get("startTime") or item.get("start") or item.get("startDate"))
    end = normalize_time(item.get("endTime") or item.get("end") or item.get("endDate"))
    if not start or not end:
        return None

    room = entity_label(item.get("locationLabel")) or entity_label(item.get("location"))
    course = item.get("slim") if isinstance(item.get("slim"), dict) else {}

    return {
        "id": str(item.get("id") or ""),
        "date": day,
        "start": start,
        "end": end,
        "room": room,
        "campus": entity_label(item.get("campus")),
        "title": entity_label(item.get("title")) or "Без названия",
        "course": entity_label(course.get("name")) or entity_label(item.get("courseName")),
        "type": entity_label(item.get("eventType")) or "other",
        "calendar": entity_label(item.get("calendar")),
        "source": source,
    }


# ---------- источники ----------

def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def fetch_lms(start: date, end: date, cookie: str, refresh: bool, delay: float) -> dict[str, object]:
    raw_dir = DATA_DIR / "raw" / "lms"
    raw_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, object] = {}
    days = list(daterange(start, end))
    for index, day in enumerate(days, 1):
        key = day.isoformat()
        cache = raw_dir / f"{key}.json"
        # Прошедшие дни берём из кэша, сегодня и будущее всегда качаем заново:
        # расписание на них ещё могут поменять.
        if cache.exists() and not refresh and day < date.today():
            payloads[key] = json.loads(cache.read_text("utf-8"))
            continue
        payload = http_get_json(LMS_URL.format(day=key), cookie)
        cache.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        payloads[key] = payload
        print(f"  [{index}/{len(days)}] {key}: {len(extract_events(payload))} событий", file=sys.stderr)
        time.sleep(delay)
    return payloads


def fetch_cu_schedule(start: date, end: date, refresh: bool, delay: float) -> dict[str, object]:
    raw_dir = DATA_DIR / "raw" / "cu-schedule"
    raw_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, object] = {}
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        key = monday.isoformat()
        cache = raw_dir / f"week-{key}.json"
        week_is_past = monday + timedelta(days=6) < date.today()
        if cache.exists() and not refresh and week_is_past:
            payload = json.loads(cache.read_text("utf-8"))
        else:
            payload = http_get_json(CU_SCHEDULE_URL.format(day=key))
            time.sleep(delay)
        failures = payload.get("failures") if isinstance(payload, dict) else None
        count = len(extract_events(payload))
        if count:  # пустой ответ сломанного бэкенда не кэшируем
            cache.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        print(f"  неделя {key}: {count} событий, ошибок бэкенда: {len(failures or [])}", file=sys.stderr)
        if failures and not count:
            print(f"    пример ошибки: {failures[0]}", file=sys.stderr)
        payloads[key] = payload
        monday += timedelta(days=7)
    return payloads


# ---------- сборка ----------

def build_occupancy(payloads: dict[str, object], source: str, start: date, end: date) -> dict:
    events: list[dict] = []
    skipped = 0
    for key, payload in payloads.items():
        for item in extract_events(payload):
            event = normalize_event(item, key, source)
            if event is None:
                skipped += 1
                continue
            if start.isoformat() <= event["date"] <= end.isoformat():
                events.append(event)

    # Дубли возможны на стыке недель и у повторяющихся событий.
    unique: dict[tuple, dict] = {}
    for event in events:
        unique[(event["id"], event["date"], event["start"], event["room"])] = event
    events = sorted(unique.values(), key=lambda e: (e["date"], e["start"], e["room"]))

    rooms = sorted({e["room"] for e in events if e["room"]})
    return {
        "generatedAt": datetime.now(MSK).isoformat(timespec="seconds"),
        "source": source,
        "range": [start.isoformat(), end.isoformat()],
        "rooms": rooms,
        "stats": {
            "events": len(events),
            "withoutRoom": sum(1 for e in events if not e["room"]),
            "skippedUnparsed": skipped,
        },
        "events": events,
    }


def print_probe(payload: object, day: str) -> None:
    events = extract_events(payload)
    print(f"\nДень {day}: форма ответа {type(payload).__name__}", end="")
    if isinstance(payload, dict):
        print(f", ключи верхнего уровня: {list(payload.keys())[:15]}")
    else:
        print()
    print(f"Событий: {len(events)}")
    if not events:
        print("Сырой ответ (начало):", json.dumps(payload, ensure_ascii=False)[:800])
        return
    print(f"Ключи события: {sorted(events[0].keys())}")
    normalized = [normalize_event(e, day, "probe") for e in events]
    parsed = [e for e in normalized if e]
    print(f"Разобрано: {len(parsed)} из {len(events)}")
    print("Аудитории:", Counter(e["room"] or "(пусто)" for e in parsed).most_common(15))
    print("Потоки/календари:", len({e["calendar"] for e in parsed}), "шт.")
    print("Курсы:", len({e["course"] or e["title"] for e in parsed}), "шт.")
    print("Пример:")
    print(json.dumps(parsed[0] if parsed else {}, ensure_ascii=False, indent=1))
    if len({e["course"] or e["title"] for e in parsed}) <= 6:
        print("\n!!! Курсов мало: похоже, эндпоинт отдаёт только ЛИЧНОЕ расписание владельца cookie.\n    Проверь на будний день: в выходной курсов мало и так.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=["lms", "cu-schedule"], default="lms")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=SEMESTER_START)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=SEMESTER_END)
    parser.add_argument("--probe", nargs="?", const=date.today().isoformat(), metavar="ДАТА",
                        help="запросить один день и показать структуру ответа, ничего не сохраняя")
    parser.add_argument("--refresh", action="store_true", help="перекачать дни, которые уже в кэше")
    parser.add_argument("--delay", type=float, default=0.4, help="пауза между запросами, с")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cookie = normalize_cookie(os.environ.get("CU_LMS_BFF_COOKIE", ""))
    if args.source == "lms" and not cookie:
        print("Не задана CU_LMS_BFF_COOKIE. Запусти «Собрать расписание.command».", file=sys.stderr)
        return 2

    try:
        if args.probe:
            if args.source == "lms":
                payload = http_get_json(LMS_URL.format(day=args.probe), cookie)
            else:
                monday = date.fromisoformat(args.probe)
                payload = http_get_json(CU_SCHEDULE_URL.format(day=(monday - timedelta(days=monday.weekday())).isoformat()))
            print_probe(payload, args.probe)
            return 0

        print(f"Источник {args.source}, период {args.start} .. {args.end}", file=sys.stderr)
        if args.source == "lms":
            payloads = fetch_lms(args.start, args.end, cookie, args.refresh, args.delay)
        else:
            payloads = fetch_cu_schedule(args.start, args.end, args.refresh, args.delay)
    except AuthError as error:
        print(f"Ошибка авторизации: {error}. Скопируй свежую bff.cookie из LMS.", file=sys.stderr)
        return 1

    occupancy = build_occupancy(payloads, args.source, args.start, args.end)
    out = DATA_DIR / "occupancy.json"
    out.write_text(json.dumps(occupancy, ensure_ascii=False, indent=1), "utf-8")
    stats = occupancy["stats"]
    print(f"\nГотово: {out}", file=sys.stderr)
    print(f"Событий {stats['events']}, без аудитории {stats['withoutRoom']}, "
          f"не разобрано {stats['skippedUnparsed']}, аудиторий {len(occupancy['rooms'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
