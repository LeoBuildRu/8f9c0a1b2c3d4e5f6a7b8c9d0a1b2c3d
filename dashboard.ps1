# Дашборд LeoAgent на ПК: SSH-туннель до сервера и окно приложения.
# Дашборд слушает на сервере только 127.0.0.1:8765 — снаружи он не виден,
# а туннель пробрасывает этот порт на localhost по вашему SSH-ключу.
# Туннель живёт в фоне и переиспользуется при следующем запуске.

# Адрес сервера (user@host) не храним в репозитории: он берётся из переменной
# LEOAGENT_SERVER или из файла dashboard.server рядом со скриптом (в .gitignore).
$Server = $env:LEOAGENT_SERVER
$ServerFile = Join-Path $PSScriptRoot "dashboard.server"
if (-not $Server -and (Test-Path $ServerFile)) {
    $Server = (Get-Content $ServerFile | Where-Object { $_ -and -not $_.StartsWith("#") } | Select-Object -First 1).Trim()
}
if (-not $Server) {
    Write-Host "Не задан адрес сервера." -ForegroundColor Red
    Write-Host "Создайте рядом со скриптом файл dashboard.server с одной строкой вида  root@203.0.113.10"
    Read-Host "Нажмите Enter, чтобы закрыть"
    exit 1
}
$Port = 8765
$Url = "http://localhost:$Port/"

function Test-Dashboard {
    try {
        (Invoke-WebRequest -Uri "${Url}api/ping" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200
    } catch {
        $false
    }
}

if (-not (Test-Dashboard)) {
    Start-Process -WindowStyle Hidden -FilePath "ssh" -ArgumentList @(
        "-N",
        "-L", "${Port}:127.0.0.1:${Port}",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        $Server
    )
    for ($i = 0; $i -lt 30 -and -not (Test-Dashboard); $i++) {
        Start-Sleep -Milliseconds 500
    }
}

if (-not (Test-Dashboard)) {
    Write-Host "Не удалось подключиться к дашборду на $Server." -ForegroundColor Red
    Write-Host "Проверьте вход по ключу:  ssh $Server"
    Write-Host "и сервис на сервере:      ssh $Server systemctl status leoagent-dashboard"
    Read-Host "Нажмите Enter, чтобы закрыть"
    exit 1
}

# Отдельное окно без панелей браузера; если Edge не найден — браузер по умолчанию
try {
    Start-Process "msedge" -ArgumentList "--app=$Url" -ErrorAction Stop
} catch {
    Start-Process $Url
}
