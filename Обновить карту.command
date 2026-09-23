#!/bin/zsh
# Пересобирает обе версии карты из самой свежей выгрузки raspisanie-spiskom*.xlsx
# (ищет в Загрузках и на Рабочем столе) и открывает главную версию (всё на одном экране).
# Версия по этажам: site/floors.html.
# Можно перетащить xlsx на этот файл в Терминале: ./Обновить\ карту.command путь/к/файлу.xlsx
set -euo pipefail
cd "${0:A:h}"
python3 build_site.py "$@"
open site/index.html
