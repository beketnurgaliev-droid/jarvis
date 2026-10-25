# -*- coding: utf-8 -*-
"""Обновление Джарвиса с GitHub — само, в тихий момент.

Версии лежат релизами в закрытом репозитории REPO: к релизу приложен тот же
архив, что собирает package.py. Ключ на чтение — в keys.dat под именем
"github" (другу он приезжает в архиве файлом update.key, keys.py переносит его
в зашифрованное хранилище). Нет ключа — обновления выключены: так у автора,
откуда версии и выпускаются, ничего само не меняется.

Порядок: проверка через пару минут после запуска и раз в CHECK_HOURS часов →
нашлась версия новее — скачать в updates\\ и проверить архив → дождаться
тихого момента (с Джарвисом не говорят, нет игры на весь экран) → запустить
update.py и выйти: сам себя на ходу Джарвис заменить не может. update.py
заменяет код, запускает новую версию и, если та не отозвалась, возвращает
прежнюю.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import keys
import version

log = logging.getLogger("jarvis.update")

HOME = Path(__file__).resolve().parent
UPDATES = HOME / "updates"
BACKUP = HOME / "backup"
STATUS_FILE = UPDATES / "installed.json"    # пишет update.py, подтверждает новая версия
REPO = "beketnurgaliev-droid/jarvis"
KEY_NAME = "github"
CHECK_HOURS = 6
FIRST_CHECK = 120          # сек после запуска — не мешать старту
QUIET_MINUTES = 3          # столько тишины перед установкой
CREATE_NO_WINDOW = 0x08000000
# в архиве обновления этого быть не должно (как package.PRIVATE)
PRIVATE = {"gemini.key", "keys.dat", "update.key", "config.json", "config.json.bak",
           "learned.json", "games.json", "jarvis.log", "panel.url", "memory.json",
           "reminders.json"}

STATE = {"checked": "", "latest": "", "notes": [], "error": "", "ready": "",
         "installing": False, "announced": ""}
_lock = threading.Lock()
_app = None


class UpdateError(Exception):
    """Понятная человеку причина, почему не получилось."""


def parse(text):
    """«v1.3» → (1, 3): сравнивать версии числами, а не строками (1.10 > 1.9)."""
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:4])


def spoken(ver):
    """«1.3» синтезатор может прочитать как дату — «1 точка 3»."""
    return str(ver).replace(".", " точка ")


def enabled():
    return bool(keys.get(KEY_NAME)) and not REPO.startswith("OWNER/")


def auto(cfg):
    return cfg.get("update", {}).get("auto", True)


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Архив релиза GitHub отдаёт переадресацией на своё хранилище. Туда ключ
    не шлём: хранилище отвечает ошибкой на лишнюю авторизацию, да и незачем."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).netloc
                                != urllib.parse.urlsplit(req.full_url).netloc):
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_NoAuthRedirect)


def _get(url, token, accept="application/vnd.github+json", timeout=20):
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"Jarvis/{version.VERSION}",
    })
    return _opener.open(req, timeout=timeout)


def _explain(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "ключ доступа не подходит или истёк"
        if exc.code == 403:
            return "GitHub не пускает — права ключа или лимит запросов"
        if exc.code == 404:
            return "версий не видно — у ключа нет доступа к репозиторию или релизов ещё нет"
        return f"GitHub ответил ошибкой {exc.code}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "нет связи с GitHub"
    return str(exc)


def _notes(body):
    """Описание релиза («- пункт» на строку) → список пунктов."""
    return [line.strip(" -*\t") for line in (body or "").splitlines() if line.strip(" -*\t")][:12]


def _now():
    return datetime.now().isoformat(sep=" ", timespec="minutes")


def check():
    """Последний релиз новее нашей версии: {"version", "asset", "size"}; иначе None.
    Не получилось спросить — UpdateError."""
    token = keys.get(KEY_NAME)
    if not token:
        raise UpdateError("нет ключа доступа")
    try:
        with _get(f"https://api.github.com/repos/{REPO}/releases/latest", token) as r:
            rel = json.load(r)
    except Exception as exc:
        raise UpdateError(_explain(exc)) from exc
    latest = str(rel.get("tag_name", "")).lstrip("vV")
    with _lock:
        STATE.update(checked=_now(), latest=latest, error="", notes=_notes(rel.get("body")))
    asset = next((a for a in rel.get("assets", [])
                  if str(a.get("name", "")).lower().endswith(".zip")), None)
    if asset is None or parse(latest) <= parse(version.VERSION):
        return None
    if latest == failed_version():
        log.info("Версия %s уже не прижилась — жду следующую", latest)
        return None
    return {"version": latest, "asset": asset["url"], "size": asset.get("size", 0)}


def check_now():
    """Проверить сразу (панель, голос) → (info | None, ошибка | "")."""
    try:
        return check(), ""
    except UpdateError as exc:
        with _lock:
            STATE.update(checked=_now(), error=str(exc))
        log.warning("Проверка обновлений: %s", exc)
        return None, str(exc)


def verify(path, expected):
    """Что не так с архивом обновления — или пустая строка."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if z.testzip() is not None:
                return "архив повреждён"
            for name in names:
                parts = name.split("/")
                if parts[0] != "Jarvis" or ".." in parts or ":" in name or "\\" in name:
                    return "в архиве странные пути"
                if parts[-1] in PRIVATE:
                    return "в архиве личные файлы"
            if "Jarvis/jarvis.py" not in names or "Jarvis/update.py" not in names:
                return "в архиве нет Джарвиса"
            text = z.read("Jarvis/version.py").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, OSError, UnicodeDecodeError):
        return "архив не читается"
    m = re.search(r'CHANGES\s*=\s*\[\s*\(\s*"([^"]+)"', text)
    if not m or m.group(1) != expected:
        return "номер версии в архиве не совпадает с релизом"
    return ""


def download(info):
    """Скачать архив релиза в updates\\ и проверить. → путь к архиву."""
    UPDATES.mkdir(exist_ok=True)
    target = UPDATES / f"Jarvis-{info['version']}.zip"
    if target.exists() and not verify(target, info["version"]):
        return target                       # уже скачан в прошлый раз
    part = target.with_suffix(".part")
    try:
        with _get(info["asset"], keys.get(KEY_NAME), accept="application/octet-stream",
                  timeout=60) as r, part.open("wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(_explain(exc)) from exc
    problem = verify(part, info["version"])
    if problem:
        part.unlink(missing_ok=True)
        raise UpdateError(problem)
    os.replace(part, target)
    with _lock:
        STATE["ready"] = info["version"]
    log.info("Скачал версию %s", info["version"])
    return target


# --------------------------------------------------------------------------
# Что было при прошлой установке (updates\installed.json, пишет update.py)
# --------------------------------------------------------------------------

def _read_status():
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(data):
    UPDATES.mkdir(exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def failed_version():
    """Версия, с которой откатились, — её сами не ставим, ждём следующую."""
    st = _read_status()
    return st.get("to", "") if st.get("status") == "rolled_back" else ""


def backup_version():
    """Какую версию можно вернуть кнопкой (лежит в backup\\)."""
    try:
        old = json.loads((BACKUP / "manifest.json").read_text(encoding="utf-8")).get("version", "")
    except (OSError, ValueError):
        return ""
    return old if old and old != version.VERSION else ""


def after_start():
    """Вызывается, когда Джарвис поднялся. Если это свежая версия — сказать
    update.py «всё хорошо» (иначе тот вернёт прежнюю). → что сказать вместо
    приветствия или ""."""
    st = _read_status()
    if st.get("status") == "starting" and st.get("to") == version.VERSION:
        st.update(status="ok", ok_at=_now())
        _write_status(st)
        log.info("Обновился: %s → %s", st.get("from"), version.VERSION)
        items = version.CHANGES[0][2]
        news = f" Новое: {items[0]}" if items else ""
        return f"Сэр, я обновился до версии {version.spoken()}.{news}"
    if st.get("status") == "rolled_back" and not st.get("told"):
        st["told"] = True
        _write_status(st)
        if st.get("by") == "user":
            return f"Вернул версию {version.spoken()}, сэр"
        return (f"Сэр, версия {spoken(st.get('to', ''))} не запустилась, "
                f"я вернул версию {version.spoken()}")
    return ""


# --------------------------------------------------------------------------
# Установка
# --------------------------------------------------------------------------

def _quiet(app):
    """Тихий момент: с Джарвисом не говорят, нет игры на весь экран."""
    import skills
    import windows

    if app.busy.is_set() or app.mode != "idle" or app.pending is not None:
        return False
    if skills.GAME_MODE["on"]:
        return False
    if time.monotonic() - app.last_phrase_at < QUIET_MINUTES * 60:
        return False
    try:
        return not windows.fullscreen_foreground()
    except Exception:
        return True


def _launch(*args):
    """Запустить update.py его копией из updates\\ — сам файл сейчас заменится."""
    UPDATES.mkdir(exist_ok=True)
    runner = UPDATES / "_update.py"
    shutil.copy2(HOME / "update.py", runner)
    python = HOME / "venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(python), str(runner), *args, "--root", str(HOME),
                      "--pid", str(os.getpid())],
                     cwd=str(UPDATES), creationflags=CREATE_NO_WINDOW)


def _quit(app):
    import skills

    if skills.GAME_MODE["on"]:
        skills.game_mode_off(app.cfg)       # вернуть схему питания
    try:
        app.listener.stop()
        app.icon.stop()
    except Exception:
        pass
    os._exit(0)


def install(path, new_version):
    """Поставить скачанную версию: запустить update.py и выйти.
    Вызывать из рабочего потока — там поднят COM для голоса."""
    with _lock:
        if STATE["installing"]:
            return
        STATE["installing"] = True
    log.info("Ставлю версию %s (сейчас %s)", new_version, version.VERSION)
    try:
        _app.speak(f"Сэр, обновляюсь до версии {spoken(new_version)}. Вернусь через минуту")
        _launch(str(path))
    except Exception:
        with _lock:
            STATE["installing"] = False
        raise
    _quit(_app)


def update_now():
    """«Обновись» голосом или кнопкой: проверить, скачать и поставить сразу.
    Если ставить нечего — что сказать."""
    if not enabled():
        return "Обновления не настроены: нет ключа доступа"
    info, err = check_now()
    if err:
        return f"Не смог проверить обновления: {err}"
    if info is None:
        return f"У меня последняя версия, {version.spoken()}"
    try:
        path = download(info)
    except UpdateError as exc:
        return f"Не смог скачать обновление: {exc}"
    install(path, info["version"])
    return None


def rollback_now():
    """Вернуть прежнюю версию из backup\\ (кнопка в панели)."""
    old = backup_version()
    if not old:
        return "Прежней версии нет, сэр"
    log.info("Возвращаю версию %s по просьбе", old)
    _app.speak(f"Возвращаю версию {spoken(old)}")
    _launch("--rollback")
    _quit(_app)


def check_phrase(cfg):
    """«Проверь обновления» — ответ голосом."""
    if not enabled():
        return "Обновления не настроены: нет ключа доступа"
    info, err = check_now()
    if err:
        return f"Не смог проверить обновления: {err}"
    if info is None:
        return f"Обновлений нет, у меня последняя версия, {version.spoken()}"
    tail = ", или обновлюсь сам, когда будет тихо" if auto(cfg) else ""
    return f"Есть новая версия {spoken(info['version'])}. Скажите: обновись{tail}"


# --------------------------------------------------------------------------
# Фоном: проверить, скачать, дождаться тишины, поставить
# --------------------------------------------------------------------------

def start(app):
    global _app
    _app = app
    threading.Thread(target=_loop, daemon=True, name="update").start()


def _loop():
    time.sleep(FIRST_CHECK)
    while True:
        if enabled():
            try:
                _cycle()
            except Exception:
                log.exception("Обновление упало")
        time.sleep(CHECK_HOURS * 3600)


def _cycle():
    info, err = check_now()
    if err or info is None:
        return
    try:
        path = download(info)
    except UpdateError as exc:
        with _lock:
            STATE["error"] = f"не скачалось: {exc}"
        log.warning("Обновление %s не скачалось: %s", info["version"], exc)
        return
    # на паузе не трогаем: после перезапуска пауза снялась бы сама
    while not _quiet(_app) or _app.paused.is_set():
        time.sleep(30)
    if not auto(_app.cfg):
        # сами не ставим — один раз скажем, что есть
        with _lock:
            told = STATE["announced"] == info["version"]
            STATE["announced"] = info["version"]
        if not told:
            _app.run_in_worker(lambda: f"Сэр, вышла новая версия {spoken(info['version'])}. "
                                       "Скажите «обновись», когда будет удобно")
        return
    _app.run_in_worker(lambda: install(path, info["version"]))


def status(cfg):
    """Для панели."""
    token = keys.get(KEY_NAME)
    with _lock:
        st = dict(STATE)
    newer = bool(st["latest"]) and parse(st["latest"]) > parse(version.VERSION) \
        and st["latest"] != failed_version()
    return {
        "enabled": enabled(), "has_key": bool(token), "key_hint": keys.hint(token),
        "auto": auto(cfg), "current": version.VERSION, "latest": st["latest"],
        "available": st["latest"] if newer else "", "notes": st["notes"] if newer else [],
        "checked": st["checked"], "error": st["error"], "installing": st["installing"],
        "backup": backup_version(), "failed": failed_version(),
    }
