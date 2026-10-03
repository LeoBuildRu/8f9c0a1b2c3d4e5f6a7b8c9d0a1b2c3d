# Развёртывание LeoAgent на VPS (Ubuntu 24.04)

Сервер: VPS на Ubuntu 24.04 вне РФ, вход `root@<IP-сервера>` по SSH-ключу.
Адрес сервера в репозиторий не кладём: для `dashboard.bat` он хранится в
локальном файле `dashboard.server` (в `.gitignore`). Агент живёт в `/opt/leoagent`
под системным пользователем `leoagent`, настройки — в `/etc/leoagent.env`.
Остальное на сервере (VPN Amnezia в Docker) установка не трогает.

Сервер вне РФ, поэтому Telegram работает напрямую: `LEOAGENT_TG_PROXY` и
`setup_proxy.sh` не нужны (они остались от старого российского VPS).

## 1. Залить код и установить

Из папки проекта на Windows (Git Bash):

```bash
tar czf - --exclude=.venv --exclude=__pycache__ --exclude=.claude --exclude=data \
    --exclude=agent_state.db --exclude='*.bak*' . \
  | ssh root@<IP-сервера> 'rm -rf /tmp/leoagent_src && mkdir -p /tmp/leoagent_src && tar xzf - -C /tmp/leoagent_src'
ssh root@<IP-сервера> 'bash /tmp/leoagent_src/server/deploy/install.sh /tmp/leoagent_src'
```

`install.sh` ставит python3-venv, создаёт пользователя `leoagent`, при нехватке
памяти добавляет подкачку на 2 ГБ, собирает `.venv` из `requirements_server.txt`
и регистрирует systemd-сервис `leoagent`. Повторный запуск обновляет код и не
трогает `/opt/leoagent/data` (БД агента) и `/etc/leoagent.env`.

## 2. Токен бота

Токен выдаёт @BotFather. Он хранится только в `/etc/leoagent.env` на сервере —
в файлах проекта его быть не должно.

```bash
ssh root@<IP-сервера> "sed -i 's|^LEOAGENT_TG_TOKEN=.*|LEOAGENT_TG_TOKEN=<токен>|' /etc/leoagent.env"
```

Желательно ещё задать `LEOAGENT_TG_ADMINS` — ваш chat_id. Пока список пуст,
подписаться на бота командой `/start` может любой, кто его найдёт.

## 3. Запуск и проверка

```bash
ssh root@<IP-сервера> 'systemctl restart leoagent && systemctl status leoagent --no-pager'
ssh root@<IP-сервера> 'journalctl -u leoagent -n 50 --no-pager'
```

Команды бота: `/status`, `/portfolio`, `/positions`, `/history`, `/decisions`,
`/lessons`, `/stop`. Об открытии и закрытии сделок бот пишет сам.

## 4. Что делает агент

| Воркер | Интервал | Что делает |
|---|---|---|
| `prices` | 5–15 мин | Последние цены 25 бумаг с MOEX — для P&L в боте |
| `news` | 10–30 мин | Парсит `t.me/s/cbrstocks` |
| `tick` | 5–10 мин | Анализ → управление открытыми сделками → поиск новых |

Агент торгует короткие сделки в обе стороны: анализ по часовым свечам за
последний месяц, лонг при сильном сигнале вверх, шорт — вниз, горизонт
сделки не больше 24 часов. Первыми берутся кандидаты, где цена быстрее
дойдёт до тейка (сила сигнала × часовая волатильность). Новые сделки
открываются только в основную сессию MOEX (будни 10:00–18:40 МСК).

Фильтр по IMOEX (лонги только над SMA100) отключён по решению владельца;
включается параметром `TraderAgent(regime_sma=100)`. Оценка стратегии на
истории: `python backtest.py --strategy agent_h1` (запускать на сервере).

## 5. Дашборд

Сервис `leoagent-dashboard` (ставится тем же `install.sh`) — веб-интерфейс
только для чтения: капитал, позиции, сделки, журнал агента, графики бумаг
с отметками сделок, котировки, курсы ЦБ, новости. Слушает только
`127.0.0.1:8765`, из интернета недоступен.

На ПК: двойной клик по `dashboard.bat` в корне проекта. Скрипт поднимает
SSH-туннель по ключу (`localhost:8765` → сервер) и открывает окно Edge в режиме
приложения. Туннель остаётся в фоне и переиспользуется при следующем запуске.

Кривая капитала строится из точек, которые дашборд пишет раз в минуту в
`data/equity_history.jsonl`; «сегодня» считается от первой точки текущих суток.

```bash
ssh root@<IP-сервера> 'systemctl status leoagent-dashboard --no-pager'
```

## 6. Полезные команды

```bash
# Обновить код: повторить шаг 1 и перезапустить
ssh root@<IP-сервера> 'systemctl restart leoagent'

# Сбросить портфель (вернуть капитал в 1 млн)
ssh root@<IP-сервера> "sqlite3 /opt/leoagent/data/agent_state.db \
    'UPDATE portfolio SET cash = initial_capital; DELETE FROM trades; DELETE FROM decisions;'"
```
