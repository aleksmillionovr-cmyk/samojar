#!/usr/bin/env bash
# Помощник для сервера: robux-bot status | logs | restart | update | password | backup | url
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/robux_bot}"
SERVICE=robux-bot

[ "$(id -u)" = 0 ] || { echo "Запусти от root (sudo robux-bot $*)"; exit 1; }
cd "$APP_DIR"

url() { grep -E '^PUBLIC_URL=' .env 2>/dev/null | cut -d= -f2- || true; }

case "${1:-}" in
  status)
    systemctl status $SERVICE --no-pager -n 15 || true
    echo; echo "Админка: $(url)"
    ;;
  logs)
    journalctl -u $SERVICE -f -n 50
    ;;
  restart)
    systemctl restart $SERVICE && echo "✅ Перезапущен"
    ;;
  update)
    if [ ! -d .git ]; then
      echo "Бот установлен из архива. Для обновления распакуй новый архив и запусти: bash robux_bot/install.sh"
      exit 1
    fi
    BRANCH="$(cat .install-branch 2>/dev/null || echo claude/bot-deployment-admin-panel-72uvk9)"
    git fetch -q --depth 1 origin "$BRANCH"
    git reset -q --hard FETCH_HEAD
    SKIP_FETCH=1 BRANCH="$BRANCH" bash "$APP_DIR/install.sh"
    ;;
  password)
    NEW="$(python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(16)))')"
    if grep -q '^ADMIN_PASSWORD=' .env; then
      sed -i "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=$NEW#" .env
    else
      echo "ADMIN_PASSWORD=$NEW" >> .env
    fi
    # пароль, заданный в админке, имеет приоритет — убираем его
    if [ -f data/config.json ]; then
      venv/bin/python - <<'PY'
import json
p = "data/config.json"
c = json.load(open(p, encoding="utf-8"))
c.pop("admin_password_hash", None)
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
    fi
    systemctl restart $SERVICE
    echo "🔑 Новый пароль админки: $NEW"
    echo "🖥  Админка: $(url)"
    ;;
  backup)
    OUT="/root/robux_backup_$(date +%Y-%m-%d_%H-%M).tar.gz"
    sqlite3 data/orders.db ".backup data/.backup.db"
    tar -czf "$OUT" -C "$APP_DIR" .env data/.backup.db data/catalog.json data/config.json 2>/dev/null || \
      tar -czf "$OUT" -C "$APP_DIR" .env data/.backup.db data/catalog.json
    rm -f data/.backup.db
    echo "💾 Копия: $OUT"
    ;;
  url)
    url
    ;;
  *)
    cat <<EOF
Использование: robux-bot <команда>
  status    — работает ли бот, адрес админки
  logs      — журнал в реальном времени (выход: Ctrl+C)
  restart   — перезапустить бота и админку
  update    — обновить до последней версии из GitHub
  password  — сгенерировать новый пароль для админки
  backup    — резервная копия заказов и настроек в /root
  url       — адрес админки
EOF
    ;;
esac
