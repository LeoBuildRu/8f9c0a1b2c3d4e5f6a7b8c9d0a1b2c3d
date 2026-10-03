#!/usr/bin/env bash
# Поднимает SOCKS5-прокси (Dante) для проксирования Telegram-трафика
# с заблокированного российского VPS. Запускать на сервере ВНЕ РФ, от root.
# Нужен, только если агент живёт на российском VPS.
#
# Параметры через env-переменные:
#   ALLOWED_CLIENT_IP  — IP российского VPS, которому разрешён доступ (обязательно)
#   PROXY_PORT         — порт SOCKS5 (1080)
#   PROXY_USER         — имя пользователя (leoagent)
#   PROXY_PASS         — пароль (по умолчанию генерируется openssl)
#   PUBLIC_IP          — внешний IP сервера (по умолчанию определяется автоматически)

set -euo pipefail

ALLOWED_CLIENT_IP="${ALLOWED_CLIENT_IP:?задайте ALLOWED_CLIENT_IP — IP российского VPS}"
PROXY_PORT="${PROXY_PORT:-1080}"
PROXY_USER="${PROXY_USER:-leoagent}"
PROXY_PASS="${PROXY_PASS:-$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root (sudo)." >&2
  exit 1
fi

echo ">>> Установка dante-server"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dante-server openssl curl iproute2 >/dev/null

# Внешний интерфейс
EXT_IFACE="$(ip route get 1.1.1.1 2>/dev/null \
  | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -n1)"
EXT_IFACE="${EXT_IFACE:-eth0}"
echo ">>> Внешний интерфейс: $EXT_IFACE"

# Внешний IP (для вывода готовой строки)
PUBLIC_IP="${PUBLIC_IP:-$(curl -fsSL https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')}"
echo ">>> Публичный IP: $PUBLIC_IP"

# Пользователь без shell для PAM-аутентификации
if ! id "$PROXY_USER" >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -M "$PROXY_USER"
  echo ">>> Создан пользователь: $PROXY_USER"
fi
echo "$PROXY_USER:$PROXY_PASS" | chpasswd
echo ">>> Пароль для $PROXY_USER установлен"

# Конфиг dante
cat > /etc/danted.conf <<EOF
logoutput: syslog
internal: 0.0.0.0 port = ${PROXY_PORT}
external: ${EXT_IFACE}
clientmethod: none
socksmethod: username
user.privileged: root
user.notprivileged: nobody

# Разрешаем подключаться только с указанного IP — без него прокси
# превратится в открытый relay и его быстро найдут боты.
client pass {
    from: ${ALLOWED_CLIENT_IP}/32 to: 0.0.0.0/0
    log: error
}

socks pass {
    from: ${ALLOWED_CLIENT_IP}/32 to: 0.0.0.0/0
    log: error
}
EOF

# PAM-стек для авторизации dante
cat > /etc/pam.d/sockd <<'EOF'
auth required pam_unix.so
account required pam_unix.so
EOF

# Открыть порт (UFW + iptables — на случай если что-то одно настроено)
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from "$ALLOWED_CLIENT_IP" to any port "$PROXY_PORT" proto tcp >/dev/null || true
  echo ">>> UFW: разрешён ${ALLOWED_CLIENT_IP} → :${PROXY_PORT}"
fi
if command -v iptables >/dev/null 2>&1; then
  iptables -C INPUT -s "$ALLOWED_CLIENT_IP" -p tcp --dport "$PROXY_PORT" -j ACCEPT 2>/dev/null \
    || iptables -A INPUT -s "$ALLOWED_CLIENT_IP" -p tcp --dport "$PROXY_PORT" -j ACCEPT
  echo ">>> iptables: разрешён ${ALLOWED_CLIENT_IP} → :${PROXY_PORT}"
fi

systemctl enable danted >/dev/null 2>&1 || true
systemctl restart danted
sleep 2

if ! systemctl is-active --quiet danted; then
  echo ""
  echo "!!! danted не стартовал. Логи:"
  journalctl -u danted -n 30 --no-pager || true
  exit 1
fi

# Самопроверка из коробки
if ss -tlnp 2>/dev/null | grep -q ":${PROXY_PORT}"; then
  echo ">>> Порт ${PROXY_PORT} слушается"
else
  echo "!!! Порт ${PROXY_PORT} не слушается, проверьте /etc/danted.conf"
fi

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  SOCKS5-прокси готов."
echo ""
echo "  Подключение разрешено только с IP: ${ALLOWED_CLIENT_IP}"
echo ""
echo "  На московском сервере в /etc/leoagent.env пропишите:"
echo ""
echo "  LEOAGENT_TG_PROXY=socks5://${PROXY_USER}:${PROXY_PASS}@${PUBLIC_IP}:${PROXY_PORT}"
echo ""
echo "════════════════════════════════════════════════════════════════════"
