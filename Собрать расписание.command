#!/bin/zsh
# Запуск сборщика расписания. Cookie вводится скрыто и живёт только в памяти процесса.
# Аргументы передаются в fetch_schedule.py, например: --probe 2026-09-23
set -euo pipefail
cd "${0:A:h}"

if [[ -z "${CU_LMS_BFF_COOKIE:-}" ]]; then
  echo "Вставьте значение bff.cookie из LMS (ввод не отображается)."
  read -s "CU_LMS_BFF_COOKIE?bff.cookie: "
  echo
  export CU_LMS_BFF_COOKIE
fi

python3 scraper/fetch_schedule.py "$@" || exit_code=$?
unset CU_LMS_BFF_COOKIE
exit ${exit_code:-0}
