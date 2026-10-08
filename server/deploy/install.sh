#!/usr/bin/env bash
# Установка LeoAgent на свежий Ubuntu 24.04 (нужен Python 3.12+).
# Запускать от root: bash install.sh <папка с исходниками>
# Ставит агента в /opt/leoagent под отдельным пользователем leoagent,
# остальные сервисы на сервере не трогает.

set -euo pipefail

SRC_DIR="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
TARGET_DIR="/opt/leoagent"
SERVICE_USER="leoagent"
ENV_FILE="/etc/leoagent.env"

echo ">>> Источник: $SRC_DIR"
echo ">>> Цель:     $TARGET_DIR"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root (или через sudo)." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv rsync libgomp1

# Подкачка: на VPS с 1 ГБ памяти pandas + sklearn легко упираются в OOM
mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "$mem_kb" -lt 2000000 ] && [ -z "$(swapon --show --noheadings)" ]; then
  echo ">>> Памяти меньше 2 ГБ и нет подкачки — создаю /swapfile на 2 ГБ"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$TARGET_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

mkdir -p "$TARGET_DIR/data" /var/log/leoagent

# Копируем код (если SRC_DIR != TARGET_DIR). data/ не трогаем — там БД агента.
if [ "$SRC_DIR" != "$TARGET_DIR" ]; then
  rsync -a --delete \
        --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude '.claude' \
        --exclude 'data' --exclude 'agent_state.db' --exclude '*.bak*' \
        "$SRC_DIR/" "$TARGET_DIR/"
fi

# Стартовый архив новостей, чтобы агенту было с чем сравнивать с первого тика
if [ -f "$TARGET_DIR/cbr_news.json" ] && [ ! -f "$TARGET_DIR/data/cbr_news.json" ]; then
  cp "$TARGET_DIR/cbr_news.json" "$TARGET_DIR/data/cbr_news.json"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$TARGET_DIR" /var/log/leoagent

# Виртуальное окружение
sudo -u "$SERVICE_USER" bash -c "
  set -e
  cd '$TARGET_DIR'
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements_server.txt
"

# env-файл (не перезаписываем существующий)
if [ ! -f "$ENV_FILE" ]; then
  cp "$TARGET_DIR/server/deploy/leoagent.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo ">>> Создан $ENV_FILE — впишите в него LEOAGENT_TG_TOKEN."
else
  echo ">>> $ENV_FILE уже существует, оставляю как есть."
fi

# systemd: агент и дашборд (дашборд слушает только 127.0.0.1, открывается через SSH-туннель)
cp "$TARGET_DIR/server/deploy/leoagent.service" /etc/systemd/system/leoagent.service
cp "$TARGET_DIR/server/deploy/leoagent-dashboard.service" /etc/systemd/system/leoagent-dashboard.service
# Ежемесячное переобучение ML-модели (нужна среда /root/research/.venv-ml)
cp "$TARGET_DIR/server/deploy/leoagent-retrain.service" /etc/systemd/system/leoagent-retrain.service
cp "$TARGET_DIR/server/deploy/leoagent-retrain.timer" /etc/systemd/system/leoagent-retrain.timer
systemctl daemon-reload
systemctl enable leoagent leoagent-dashboard leoagent-retrain.timer
systemctl restart leoagent-dashboard
systemctl start leoagent-retrain.timer

echo ""
echo "Готово. Дальше:"
echo "  nano $ENV_FILE                   # вписать токен бота"
echo "  systemctl restart leoagent       # запустить / перезапустить"
echo "  systemctl status leoagent        # статус"
echo "  journalctl -u leoagent -f        # живые логи"
