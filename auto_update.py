#!/usr/bin/env python3
"""Ежедневное автообновление карты из свежей выгрузки cu-schedule.ru.

Делает то же, что кнопка на сайте «Скачать Excel → Списком → Всё расписание»:
эта кнопка скачивает файл по адресу EXPORT_URL, его и берём напрямую.

Если скачать не удалось или выгрузка подозрительная (пустая, битая, резко меньше
прошлой), страницы не трогаем: сайт остаётся таким, каким был.

На GitHub запускается workflow'ом .github/workflows/update.yml по расписанию.
Локально (без GitHub) можно запускать через launchd, см. automation/, или вручную:
python3 auto_update.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import build_site
from build_site import MSK

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
EXPORT_URL = "https://cu-schedule.ru/api/timetable/export?format=list&scope=all"
USER_AGENT = "cu-free-rooms/1.0 (daily update)"

ATTEMPTS = 3            # попыток скачать
RETRY_PAUSE = 60        # секунд между попытками
MIN_ROWS = 100          # меньше строк: считаем выгрузку сломанной
MIN_SHARE_OF_LAST = 0.5 # новая выгрузка не меньше половины прошлой удачной

LAST_GOOD_XLSX = DATA / "last-good.xlsx"
LAST_GOOD_META = DATA / "last-good.json"


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(MSK):%Y-%m-%d %H:%M:%S} мск] {msg}", flush=True)


def download(dest: Path) -> None:
    last_error = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            req = urllib.request.Request(EXPORT_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read()
            if not body.startswith(b"PK"):
                raise ValueError(f"ответ не похож на xlsx ({len(body)} байт)")
            dest.write_bytes(body)
            log(f"скачано {len(body) // 1024} КБ с попытки {attempt}")
            return
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            last_error = error
            log(f"попытка {attempt}/{ATTEMPTS} не удалась: {error}")
            if attempt < ATTEMPTS:
                time.sleep(RETRY_PAUSE)
    raise RuntimeError(f"не удалось скачать выгрузку: {last_error}")


def check(xlsx: Path) -> dict:
    """Проверяет выгрузку до сборки. Возвращает статистику или бросает ValueError."""
    try:
        zipfile.ZipFile(xlsx).testzip()
        events, unknown, rows, bad, days = build_site.load_events(xlsx, build_site.known_rooms())
    except Exception as error:  # битый zip, не тот формат, сломанные листы: всё это «выгрузка негодная»
        raise ValueError(f"файл не читается как расписание: {type(error).__name__}: {error}")
    if rows < MIN_ROWS:
        raise ValueError(f"в выгрузке всего {rows} строк: похоже, источник отдал пустое расписание")
    if not events:
        raise ValueError("в выгрузке нет ни одного занятия в аудиториях карты")
    if LAST_GOOD_META.exists():
        last = json.loads(LAST_GOOD_META.read_text("utf-8"))
        if rows < last["rows"] * MIN_SHARE_OF_LAST:
            raise ValueError(f"строк {rows}, а в прошлый раз было {last['rows']}: подозрительно резкое падение")
    return {"rows": rows, "events": len(events), "unknown": dict(unknown),
            "range": [min(days), max(days)] if days else None}


def main() -> int:
    DATA.mkdir(exist_ok=True)
    # временный файл обязательно с расширением .xlsx: openpyxl по расширению отказывается открывать .part
    part = DATA / "download-new.xlsx"
    log("старт обновления")
    try:
        download(part)
        check(part)
        stats = build_site.build(part)  # страницы подменяются атомарно, только целиком готовые
    except (RuntimeError, ValueError) as error:
        log(f"СТОП: {error}. Сайт оставлен без изменений.")
        part.unlink(missing_ok=True)
        return 1
    except Exception as error:
        log(f"СТОП: неожиданная ошибка при сборке: {type(error).__name__}: {error}. Сайт оставлен без изменений.")
        part.unlink(missing_ok=True)
        return 1
    part.replace(LAST_GOOD_XLSX)
    LAST_GOOD_META.write_text(json.dumps({
        "updatedAt": dt.datetime.now(MSK).isoformat(timespec="seconds"),
        "rows": stats["rows"], "events": stats["events"], "range": stats["range"],
    }, ensure_ascii=False, indent=1), "utf-8")
    log(f"карта обновлена: строк {stats['rows']}, занятий в аудиториях карты {stats['events']}, "
        f"период {stats['range'][0]} .. {stats['range'][1]}")
    if stats["unknown_rooms"]:
        log(f"ВНИМАНИЕ: новые аудитории S/N/W/E, которых нет на карте: {stats['unknown_rooms']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
