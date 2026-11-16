#!/usr/bin/env bash
# Установка / обновление бота + веб-админки на сервер (Ubuntu / Debian).
# Запуск от root:  bash robux_bot/install.sh
# Повторный запуск = обновление кода; .env, заказы и каталог сохраняются.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR=/opt/robux_bot
SERVICE=robux-bot
PORT=8080

say() { printf '\n\033[1;35m▶ %s\033[0m\n' "$1"; }

if [ "$(id -u)" != "0" ]; then
  echo "Запусти от root (или через sudo)"; exit 1
fi

say "Ставлю Python"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip curl
fi

say "Копирую файлы в $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"
if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
  cp -f "$SRC_DIR"/*.py "$SRC_DIR"/requirements.txt "$SRC_DIR"/README.md "$SRC_DIR"/.env.example "$INSTALL_DIR"/
  cp -f "$SRC_DIR"/install.sh "$INSTALL_DIR"/
fi
cd "$INSTALL_DIR"

say "Ставлю библиотеки (минута-две)"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  say "Настройки бота"
  echo "Их потом можно поменять в файле $INSTALL_DIR/.env"
  read -rp "Токен бота от @BotFather: " BOT_TOKEN </dev/tty
  read -rp "Твой Telegram ID (узнать в @userinfobot): " ADMIN_ID </dev/tty
  while true; do
    read -rsp "Придумай пароль для админки (минимум 8 символов): " ADMIN_PASSWORD </dev/tty; echo
    [ "${#ADMIN_PASSWORD}" -ge 8 ] && break
    echo "Слишком короткий, ещё раз"
  done
  cp .env.example .env
  python3 - "$BOT_TOKEN" "$ADMIN_ID" "$ADMIN_PASSWORD" "$INSTALL_DIR/data" "$PORT" <<'PY'
import sys
token, admin_id, password, data_dir, port = sys.argv[1:]
values = {"BOT_TOKEN": token.strip(), "ADMIN_ID": admin_id.strip(), "ADMIN_PASSWORD": password,
          "DATA_DIR": data_dir, "ADMIN_HOST": "0.0.0.0", "ADMIN_PORT": port}
out = []
for line in open(".env", encoding="utf-8").read().splitlines():
    key = line.split("=", 1)[0].strip()
    if not line.lstrip().startswith("#") and key in values:
        out.append(f"{key}={values.pop(key)}")
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in values.items()]
open(".env", "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
  chmod 600 .env
else
  echo "Файл .env уже есть, оставляю как есть"
fi

say "Включаю автозапуск"
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Robux shop: Telegram bot + web admin
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q $SERVICE
systemctl restart $SERVICE

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow $PORT/tcp >/dev/null && echo "Порт $PORT открыт в ufw"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd -q --permanent --add-port=$PORT/tcp && firewall-cmd -q --reload && echo "Порт $PORT открыт в firewalld"
fi

say "Проверяю запуск"
ok=""
for _ in $(seq 1 15); do
  sleep 2
  if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/login" | grep -q 200; then ok=1; break; fi
done

IP="$(curl -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
if [ -n "$ok" ]; then
  printf '\n\033[1;32m✅ Готово! Бот работает.\033[0m\n'
  echo "🖥  Админка:  http://$IP:$PORT   (пароль: тот, что ты придумал)"
  echo "🤖 Бот: открой его в Telegram и нажми /start"
else
  printf '\n\033[1;31m⚠️  Админка не ответила. Последние строки лога:\033[0m\n'
  journalctl -u $SERVICE -n 30 --no-pager || true
fi
cat <<EOF

Полезные команды:
  journalctl -u $SERVICE -f        — смотреть лог (выход: Ctrl+C)
  systemctl restart $SERVICE       — перезапустить
  nano $INSTALL_DIR/.env           — поменять токены/пароль (потом перезапустить)
Обновление: загрузи новый архив, распакуй и снова запусти install.sh
EOF
