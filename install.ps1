# Установка Джарвиса на чистую машину с Windows.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Ставит Python, окружение, русскую модель распознавания и, если надо,
# Claude Code. Запускать от обычного пользователя, права администратора
# не нужны.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($text) { Write-Host "`n>>> $text" -ForegroundColor Cyan }
function Good($text) { Write-Host "    $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }

Write-Host "=== Джарвис: установка в $Root ===" -ForegroundColor White

# распознавание речи (Vosk) не открывает модель, если в пути есть русские буквы
if ($Root -match '[^\x00-\x7F]') {
    Warn "В пути к папке есть русские буквы: $Root"
    Warn "С ними не загрузится распознавание речи. Лучше перенести папку в C:\Jarvis"
    $answer = Read-Host "    Всё равно продолжить? (д/н)"
    if ($answer -notmatch "^[дdyY]") { exit 1 }
}

# --- 1. Python ------------------------------------------------------------
Step "Python"
$py = ""
foreach ($c in (Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
    # заглушка из Microsoft Store не годится
    if ($c.Source -notlike "*WindowsApps*") { $py = $c.Source; break }
}
if (-not $py) {
    $found = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter python.exe `
             -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $py = $found.FullName }
}
if (-not $py) {
    Warn "Python не найден, ставлю через winget"
    winget install --id Python.Python.3.12 --scope user `
        --accept-source-agreements --accept-package-agreements --disable-interactivity
    $found = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter python.exe `
             -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) { throw "Python поставить не удалось — поставь вручную с python.org" }
    $py = $found.FullName
}
Good "$py  ($(& $py --version))"

# --- 2. Окружение ---------------------------------------------------------
Step "Виртуальное окружение и библиотеки"
if (-not (Test-Path "$Root\venv\Scripts\python.exe")) {
    & $py -m venv "$Root\venv"
}
& "$Root\venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$Root\venv\Scripts\python.exe" -m pip install -r "$Root\requirements.txt" --quiet
Good "библиотеки на месте"

# --- 3. Модель распознавания ---------------------------------------------
Step "Русская модель распознавания речи"
$model = Get-ChildItem $Root -Directory -Filter "vosk-model-*" -ErrorAction SilentlyContinue |
         Select-Object -First 1
if ($model) {
    Good "уже есть: $($model.Name)"
} else {
    Write-Host "    Большая модель — 1,8 ГБ, слышит заметно лучше. Маленькая — 46 МБ."
    $answer = Read-Host "    Качать большую? (д/н)"
    if ($answer -match "^[дdyY]") { $name = "vosk-model-ru-0.42" } else { $name = "vosk-model-small-ru-0.22" }
    $url = "https://alphacephei.com/vosk/models/$name.zip"
    Warn "качаю $name, это небыстро"
    curl.exe -L --fail -o "$Root\model.zip" $url
    if ($LASTEXITCODE -ne 0) { throw "Модель не скачалась: $url" }
    tar.exe -xf "$Root\model.zip" -C "$Root"
    if ($LASTEXITCODE -ne 0) { Expand-Archive -Path "$Root\model.zip" -DestinationPath $Root -Force }
    Remove-Item "$Root\model.zip" -Force
    # у большой модели отключаем дорескоринг: грузится за 2 с вместо полутора
    # минут и берёт 1 ГБ памяти вместо 5, а точность та же
    foreach ($part in "rnnlm", "rescore") {
        if (Test-Path "$Root\$name\$part") { Rename-Item "$Root\$name\$part" "_$part.off" }
    }
    Good "модель распакована: $name"
}

# --- 4. Настройки ---------------------------------------------------------
Step "Настройки"
if (Test-Path "$Root\config.json") {
    Good "config.json уже есть, не трогаю"
} else {
    Copy-Item "$Root\config.example.json" "$Root\config.json"
    Good "создан config.json — псевдонимы программ правятся там"
}

# --- 5. Gemini ------------------------------------------------------------
Step "Gemini — быстрые ответы на вопросы (бесплатный ключ)"
if (Test-Path "$Root\gemini.key") {
    Good "ключ уже есть"
} elseif (Test-Path "$Root\keys.dat") {
    # Джарвис уже запускался и перенёс ключи в зашифрованный keys.dat
    Good "ключи уже сохранены — поменять можно в панели, вкладка «ИИ»"
} else {
    Write-Host "    Ключ бесплатный: открываю aistudio.google.com/apikey → «Create API key»."
    Write-Host "    Подписка Gemini не нужна. Можно и позже — в панели Джарвиса, вкладка «ИИ»."
    Start-Process "https://aistudio.google.com/apikey"
    $key = (Read-Host "    Вставь ключ и нажми Enter (просто Enter — пропустить)").Trim()
    if ($key) {
        Set-Content -Path "$Root\gemini.key" -Value $key -Encoding ASCII
        Good "ключ сохранён"
    } else {
        Warn "пропущено — без ключа Джарвис понимает только свои команды"
    }
}

# --- 6. Claude Code -------------------------------------------------------
Step "Claude Code — необязательно: поиск в интернете, нужна подписка Claude"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude -and (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")) {
    $claude = "$env:USERPROFILE\.local\bin\claude.exe"
}
if ($claude) {
    Good "найден"
} else {
    Warn "не найден. Без него не будет ответов про погоду и новости, остальное работает."
    $answer = Read-Host "    Поставить сейчас? (д/н)"
    if ($answer -match "^[дdyY]") {
        Invoke-Expression (Invoke-RestMethod https://claude.ai/install.ps1)
        Good "поставлен, не забудь один раз войти: claude"
    }
}

# --- 7. Ярлыки ------------------------------------------------------------
Step "Ярлыки"
function Make-Shortcut($path) {
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    $s.TargetPath = "wscript.exe"
    $s.Arguments = '"' + $Root + '\start-jarvis.vbs"'
    $s.WorkingDirectory = $Root
    $s.Description = "Голосовой помощник Джарвис"
    $s.IconLocation = "$Root\jarvis.ico"
    $s.Save()
}
Make-Shortcut "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Джарвис.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Панель Джарвиса.lnk")
$s.TargetPath = "$Root\venv\Scripts\pythonw.exe"
$s.Arguments = '"' + $Root + '\open_panel.py"'
$s.WorkingDirectory = $Root
$s.Description = "Настройки Джарвиса: ключ Gemini, голос, микрофон, программы"
$s.IconLocation = "$Root\jarvis.ico"
$s.Save()
Good "в меню «Пуск» — «Джарвис» и «Панель Джарвиса»"
$lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Jarvis.lnk"
if (Test-Path $lnk) {
    Good "ярлык уже в автозагрузке"
} else {
    $answer = Read-Host "    Запускать Джарвиса при входе в Windows? (д/н)"
    if ($answer -match "^[дdyY]") {
        Make-Shortcut $lnk
        Good "добавлен в автозагрузку"
    }
}

# --- 8. Проверка ----------------------------------------------------------
Step "Самопроверка"
$env:PYTHONIOENCODING = "utf-8"
& "$Root\venv\Scripts\python.exe" "$Root\selftest.py"

Write-Host "`n=== Готово ===" -ForegroundColor White
Write-Host "Запуск: меню «Пуск» → «Джарвис». Значок появится в трее, щелчок по нему — панель настроек."
Write-Host "Скажи «Джарвис», дождись писка — и командуй."
Write-Host ""
Write-Host "Ютуб (по желанию): в Chrome открой chrome://extensions → «Режим разработчика» →"
Write-Host "«Загрузить распакованное расширение» → папка $Root\chrome-extension"
