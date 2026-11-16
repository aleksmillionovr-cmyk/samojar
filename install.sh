#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  Установка / обновление магазина: Telegram-бот + веб-админка (Ubuntu / Debian).
#
#  Одна команда на сервере (от root):
#    curl -fsSL https://raw.githubusercontent.com/aleksmillionovr-cmyk/samojar/claude/bot-deployment-admin-panel-72uvk9/install.sh | bash
#
#  Что делает: ставит Python и библиотеки, скачивает бота в /opt/robux_bot,
#  создаёт пароль админки, включает автозапуск (systemd) и HTTPS (Caddy + Let's Encrypt).
#  Повторный запуск = обновление: .env, заказы, настройки и токены сохраняются.
#
#  Необязательные переменные:
#    DOMAIN=shop.example.ru  — свой домен (A-запись на этот сервер) вместо адреса по IP
#    NO_HTTPS=1              — без Caddy и HTTPS, админка на http://IP:8080
#    BRANCH=main             — другая ветка репозитория
#    BOT_TOKEN=… ADMIN_ID=…  — сразу прописать токен бота и свой Telegram ID
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/aleksmillionovr-cmyk/samojar.git}"
APP_DIR="${APP_DIR:-/opt/robux_bot}"
SERVICE=robux-bot
APP_USER=robuxbot
PORT="${ADMIN_PORT:-8080}"
DOMAIN="${DOMAIN:-}"
NO_HTTPS="${NO_HTTPS:-0}"

say()  { printf '\n\033[1;35m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✖ %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" = 0 ] || die "Запусти от root:  sudo -i  и повтори команду"
command -v apt-get >/dev/null 2>&1 || die "Нужен Ubuntu или Debian (apt-get)"

DEFAULT_BRANCH="claude/bot-deployment-admin-panel-72uvk9"
# ветка: из переменной, из прошлой установки или ветка по умолчанию
if [ -z "${BRANCH:-}" ]; then
  BRANCH="$(cat "$APP_DIR/.install-branch" 2>/dev/null || echo "$DEFAULT_BRANCH")"
fi

# ─── 1. Системные пакеты ───
say "Ставлю системные пакеты (минута-две)"
export DEBIAN_FRONTEND=noninteractive
# на свежем сервере apt часто занят автообновлениями — ждём до 5 минут
apt-get() { command apt-get -o DPkg::Lock::Timeout=300 "$@"; }
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates sqlite3 >/dev/null
ok "Python $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Нужен Python 3.9 или новее"

# ─── 2. Код бота ───
say "Скачиваю бота (ветка $BRANCH)"
if [ "${SKIP_FETCH:-0}" = 1 ] && [ -f "$APP_DIR/bot.py" ]; then
  ok "Код уже обновлён"
elif [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q --depth 1 origin "$BRANCH"
  git -C "$APP_DIR" reset -q --hard FETCH_HEAD
elif [ -e "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
  # старая установка без git (из архива) — переносим данные в новую
  OLD="$APP_DIR.old.$(date +%Y%m%d%H%M%S)"
  mv "$APP_DIR" "$OLD"
  git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$APP_DIR"
  mkdir -p "$APP_DIR/data"
  [ -f "$OLD/.env" ] && cp -a "$OLD/.env" "$APP_DIR/.env"
  [ -d "$OLD/data" ] && cp -a "$OLD/data/." "$APP_DIR/data/"
  for f in orders.db catalog.json catalog.backup.json .admin_secret; do
    [ -f "$OLD/$f" ] && [ ! -f "$APP_DIR/data/$f" ] && cp -a "$OLD/$f" "$APP_DIR/data/$f"
  done
  sed -i "s#^DATA_DIR=.*#DATA_DIR=$APP_DIR/data#" "$APP_DIR/.env" 2>/dev/null || true
  warn "Старая версия сохранена в $OLD (данные перенесены)"
else
  git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
echo "$BRANCH" > .install-branch
ok "Версия: $(git log -1 --format='%h %cd' --date=format:'%d.%m.%Y %H:%M' 2>/dev/null || echo '?')"

# ─── 3. Библиотеки Python ───
say "Ставлю библиотеки Python"
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install -q --disable-pip-version-check --upgrade pip
venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
venv/bin/python -m compileall -q . -x 'venv|data' >/dev/null 2>&1 || true
ok "Готово"

# ─── 4. Пользователь и папка данных ───
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p data

IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
[ -n "$IP" ] || die "Не удалось узнать IP сервера"

# ─── 5. Настройки (.env) ───
set_env() {  # set_env КЛЮЧ ЗНАЧЕНИЕ — заменить или добавить строку в .env
  if grep -q "^$1=" .env 2>/dev/null; then
    sed -i "s#^$1=.*#$1=$2#" .env
  else
    echo "$1=$2" >> .env
  fi
}

NEW_PASS=""
if [ ! -f .env ]; then
  NEW_PASS="$(python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(16)))')"
  cat > .env <<EOF
# Создано install.sh $(date '+%d.%m.%Y %H:%M')
# Токены бота и платёжек задаются в веб-админке (раздел «Подключения»), здесь только запуск.
ADMIN_PASSWORD=$NEW_PASS
ADMIN_PORT=$PORT
DATA_DIR=$APP_DIR/data
TZ_OFFSET=3
EOF
fi
[ -n "${BOT_TOKEN:-}" ] && set_env BOT_TOKEN "$BOT_TOKEN"
[ -n "${ADMIN_ID:-}" ] && set_env ADMIN_ID "$ADMIN_ID"
set_env DATA_DIR "$APP_DIR/data"
set_env ADMIN_PORT "$PORT"

use_http() {   # админка напрямую на http://IP:PORT
  set_env ADMIN_HOST 0.0.0.0
  set_env TRUST_PROXY 0
  set_env COOKIE_SECURE 0
  set_env PUBLIC_URL "http://$IP:$PORT"
  PUBLIC_URL="http://$IP:$PORT"
}

# ─── 6. Автозапуск (systemd) ───
write_service() {
  cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Robux shop: Telegram bot + web admin
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable -q $SERVICE
}

fix_perms() {
  chown -R "$APP_USER:$APP_USER" data
  chown "root:$APP_USER" .env
  chmod 640 .env
  chmod 750 data
}

wait_local() {  # ждём, пока админка ответит на 127.0.0.1
  for _ in $(seq 1 30); do
    curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/healthz" && return 0
    sleep 2
  done
  return 1
}

open_port() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    for p in "$@"; do ufw allow "$p/tcp" >/dev/null; done
    ok "Открыл в ufw порты: $*"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    for p in "$@"; do firewall-cmd -q --permanent --add-port="$p/tcp"; done
    firewall-cmd -q --reload
    ok "Открыл в firewalld порты: $*"
  fi
}

# ─── 7. HTTPS через Caddy ───
HTTPS_URL=""
setup_caddy() {
  # порты 80/443 должны быть свободны (или заняты самим Caddy)
  if ss -ltnp 2>/dev/null | grep -E ':(80|443)\s' | grep -vq caddy; then
    warn "Порты 80/443 заняты другой программой (nginx/apache?) — HTTPS через Caddy пропускаю"
    return 1
  fi
  if ! command -v caddy >/dev/null 2>&1 || ! caddy version 2>/dev/null | grep -qE '^v2\.(1[0-9]|[2-9][0-9])'; then
    say "Ставлю Caddy (веб-сервер с автоматическим HTTPS)"
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null 2>&1 || true
    if curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
       && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list \
       && apt-get update -qq && apt-get install -y -qq caddy >/dev/null; then
      ok "Caddy $(caddy version | awk '{print $1}')"
    elif apt-get install -y -qq caddy >/dev/null 2>&1; then
      ok "Caddy $(caddy version | awk '{print $1}') (из репозитория системы)"
    else
      warn "Не удалось установить Caddy"
      return 1
    fi
  fi

  local sslip="${IP//./-}.sslip.io"
  local cf=/etc/caddy/Caddyfile
  if [ -f "$cf" ] && ! grep -q "robux-bot" "$cf"; then
    cp "$cf" "$cf.bak.$(date +%Y%m%d%H%M%S)"
  fi
  local sites
  if [ -n "$DOMAIN" ]; then
    sites="$DOMAIN"
    cat > "$cf" <<EOF
# robux-bot: создано install.sh
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
  else
    # сертификат Let's Encrypt прямо на IP (профиль shortlived) + запасной адрес на sslip.io
    sites="$IP $sslip"
    cat > "$cf" <<EOF
# robux-bot: создано install.sh
$IP, $sslip {
	tls {
		issuer acme {
			dir https://acme-v02.api.letsencrypt.org/directory
			profile shortlived
		}
	}
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
    if ! caddy validate --config "$cf" --adapter caddyfile >/dev/null 2>&1; then
      # старый Caddy без профилей — только домен sslip.io с обычным сертификатом
      sites="$sslip"
      cat > "$cf" <<EOF
# robux-bot: создано install.sh
$sslip {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
    fi
  fi
  caddy validate --config "$cf" --adapter caddyfile >/dev/null 2>&1 || { warn "Caddyfile не прошёл проверку"; return 1; }
  open_port 80 443
  systemctl enable -q caddy
  systemctl restart caddy

  say "Получаю HTTPS-сертификат (до 2 минут)"
  for _ in $(seq 1 40); do
    for s in $sites; do
      if curl -fsS -o /dev/null --max-time 5 --connect-to "$s:443:127.0.0.1:443" "https://$s/healthz" 2>/dev/null; then
        HTTPS_URL="https://$s"
        return 0
      fi
    done
    sleep 3
  done
  warn "Сертификат пока не получен. Лог Caddy: journalctl -u caddy -n 50"
  return 1
}

write_service
if [ "$NO_HTTPS" = 1 ]; then
  use_http
else
  # админка слушает только локально, наружу её отдаёт Caddy по HTTPS
  set_env ADMIN_HOST 127.0.0.1
  set_env TRUST_PROXY 1
  set_env COOKIE_SECURE 0
  fix_perms
  systemctl restart $SERVICE
  wait_local || true
  if setup_caddy; then
    set_env COOKIE_SECURE 1
    set_env PUBLIC_URL "$HTTPS_URL"
    PUBLIC_URL="$HTTPS_URL"
    # адрес админки в «Подключениях» тоже обновляем (если он уже сохранялся там)
    if [ -f data/config.json ]; then
      venv/bin/python - "$HTTPS_URL" <<'PY'
import json, sys
p = "data/config.json"
c = json.load(open(p, encoding="utf-8"))
if c.get("public_url", "").startswith("http://") or not c.get("public_url"):
    c["public_url"] = sys.argv[1]
    json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
    fi
  else
    warn "Работаю без HTTPS: админка будет на http://$IP:$PORT"
    use_http
  fi
fi
[ "${PUBLIC_URL:-}" = "http://$IP:$PORT" ] && open_port "$PORT"

fix_perms
systemctl restart $SERVICE

# ─── 8. Помощник robux-bot ───
install -m 755 "$APP_DIR/robux-bot.sh" /usr/local/bin/robux-bot

# ─── 9. Проверка ───
say "Проверяю запуск"
if wait_local; then
  printf '\n\033[1;32m══════════════════════════════════════════════\033[0m\n'
  printf '\033[1;32m  ✅ Готово! Магазин установлен и запущен\033[0m\n'
  printf '\033[1;32m══════════════════════════════════════════════\033[0m\n\n'
  echo "  🖥  Админка:  $PUBLIC_URL"
  if [ -n "$NEW_PASS" ]; then
    echo "  🔑 Пароль:   $NEW_PASS"
    echo "     (сохрани его! сменить можно в админке → «Система»)"
  else
    echo "  🔑 Пароль:   прежний. Забыл — выполни: robux-bot password"
  fi
  echo
  echo "  Дальше в админке:"
  echo "   1. «Подключения» → вставь токен бота от @BotFather и свой Telegram ID"
  echo "   2. «Подключения» → BoomKassa: API-ключ, затем отправь в поддержку BoomKassa"
  echo "      адрес вебхука: $PUBLIC_URL/webhook/boomkassa"
  echo "   3. «Настройки» → ник поддержки, способы оплаты"
else
  printf '\n\033[1;31m⚠️  Админка не ответила. Последние строки лога:\033[0m\n'
  journalctl -u $SERVICE -n 40 --no-pager || true
fi
cat <<EOF

  Команды на сервере:
    robux-bot status     — работает ли бот
    robux-bot logs       — журнал в реальном времени (выход: Ctrl+C)
    robux-bot restart    — перезапустить
    robux-bot update     — обновить до последней версии
    robux-bot password   — новый пароль для админки
    robux-bot backup     — резервная копия данных в /root

EOF
