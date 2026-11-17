# -*- coding: utf-8 -*-
"""Свой Python внутри папки Джарвиса — чтобы не ставить Python в систему.

runtime\\<ИМЯ>\\ — встроенный Python с python.org («embeddable package») и все
библиотеки Джарвиса. Какой из них запускать, написано в runtime\\current.txt —
его читают start-jarvis.vbs и run.vbs. Нет runtime — запуск из venv, как раньше.

Новый runtime приходит релизом «runtime-<ИМЯ>» в том же репозитории, что и
версии (собирает package.py --runtime). Порядок: скачать в updates\\ →
распаковать → проверить импортом всех библиотек → дождаться тихого момента →
update.py --runtime ИМЯ: переключает current.txt, запускает и, если Джарвис не
отозвался, возвращает прежний Python.

Поменялись библиотеки (requirements.txt) — это новый runtime: новое имя WANTED.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import version

log = logging.getLogger("jarvis.runtime")

HOME = Path(__file__).resolve().parent
ROOT = HOME / "runtime"
POINTER = ROOT / "current.txt"
UPDATES = HOME / "updates"
STATUS = UPDATES / "runtime.json"      # пишет update.py --runtime, подтверждает новый запуск
VENV = HOME / "venv"
WANTED = "py3.12.10-1"                 # какой runtime нужен этому коду
PYTHON = "3.12.10"
CREATE_NO_WINDOW = 0x08000000
RETRY_HOURS = 6
# всё, без чего Джарвис не поднимется, — проверяем до переключения
CHECK = ("import vosk, sounddevice, pystray, PIL.Image, win32com.client, pythoncom, psutil, "
         "websockets, comtypes, pycaw.pycaw, winsdk.windows.media.control, "
         "winsdk.windows.media.speechsynthesis, anthropic, yt_dlp, audioop, ssl, sqlite3, ctypes; "
         "import speech, skills, brain, ai, panel, windows, info, reminders, updater, models, runtime; "
         "print('ok')")

STATE = {"downloading": False, "error": ""}


def asset_name(name=WANTED):
    return f"Jarvis-runtime-{name}.zip"


def tag(name=WANTED):
    return f"runtime-{name}"


def _valid(name):
    return bool(name) and (ROOT / name / "pythonw.exe").is_file() and (ROOT / name / "checked").is_file()


def current():
    """Runtime, который запустят start-jarvis.vbs и run.vbs ("" — venv)."""
    try:
        name = POINTER.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return name if (ROOT / name / "pythonw.exe").is_file() else ""


def running():
    """Из какого runtime запущен этот процесс ("" — из venv или системного Python).
    Пути сравниваем и как есть, и с раскрытыми ссылками: папка Джарвиса может
    лежать за junction (перенос на D:) — тогда HOME раскрыт, а sys.executable нет."""
    exes = {os.path.normcase(os.path.abspath(sys.executable)),
            os.path.normcase(os.path.realpath(sys.executable))}
    roots = {os.path.normcase(os.path.abspath(ROOT)), os.path.normcase(os.path.realpath(ROOT))}
    for exe in exes:
        for root in roots:
            if exe.startswith(root + os.sep):
                parts = exe[len(root) + 1:].split(os.sep)
                if len(parts) > 1:
                    return parts[0]
    return ""


def pythonw():
    """Чем запускать служебные процессы (update.py): тем же Python, что и сейчас."""
    exe = Path(sys.executable)
    gui = exe.with_name("pythonw.exe")
    return gui if gui.is_file() else exe


def dev_machine():
    """Машина, откуда версии выпускаются: там venv нужен для package.py."""
    return (HOME / ".git").exists()


# --------------------------------------------------------------------------
# Проверка, скачивание, переключение
# --------------------------------------------------------------------------

def check(name):
    """Поднимается ли Джарвис на этом runtime: импорт всех библиотек и модулей.
    → "" (всё хорошо) или что не так."""
    exe = ROOT / name / "python.exe"
    if not exe.is_file():
        return "нет python.exe"
    try:
        r = subprocess.run([str(exe), "-c", CHECK], cwd=str(HOME), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"не запустился: {exc}"
    if r.returncode != 0 or r.stdout.strip().splitlines()[-1:] != ["ok"]:
        out = (r.stderr or r.stdout).strip()
        return out.splitlines()[-1] if out else f"код {r.returncode}"
    return ""


def open_mic():
    """Открыть микрофон на долю секунды (ничего не записывая). → "" или ошибка.
    Зачем: новые сборки Windows 11 (Insider 26H2) не дают микрофон «непроверенным»
    программам — тот же python.exe по новому пути глохнет, хотя по старому слышит."""
    try:
        import sounddevice as sd

        s = sd.RawInputStream(samplerate=16000, blocksize=4000, dtype="int16", channels=1,
                              callback=lambda *a: None)
        s.start()
        s.stop()
        s.close()
        return ""
    except Exception as exc:
        return str(exc) or exc.__class__.__name__


def mic_problem(name):
    """Слышит ли свой Python name микрофон. → "" или что не так. Если микрофон
    не открывается и у нынешнего Python — дело не в своём (выдернут, занят),
    переходу это не мешает."""
    def try_with(exe):
        try:
            r = subprocess.run([str(exe), str(HOME / "runtime.py"), "--mic"], cwd=str(HOME),
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=60, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        return "" if r.returncode == 0 else ((r.stdout + r.stderr).strip().splitlines() or ["?"])[-1]

    problem = try_with(ROOT / name / "python.exe")
    if not problem:
        return ""
    here = Path(sys.executable).with_name("python.exe")
    if try_with(here if here.is_file() else sys.executable):
        log.info("Микрофон не открывается ни у какого Python — переходу не мешает: %s", problem)
        return ""
    return f"Windows не даёт ему микрофон ({problem})"


def wanted_in(zpath):
    """Какой runtime нужен версии из архива обновления ("" — не сказано)."""
    import re

    try:
        with zipfile.ZipFile(zpath) as z:
            text = z.read("Jarvis/runtime.py").decode("utf-8")
    except (KeyError, OSError, zipfile.BadZipFile, UnicodeDecodeError):
        return ""
    m = re.search(r'^WANTED\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def mark_checked(name):
    (ROOT / name / "checked").write_text(datetime.now().isoformat(timespec="seconds"),
                                         encoding="utf-8")


def unpack(zpath, name):
    """Архив runtime (пути runtime/<ИМЯ>/…) → runtime\\<ИМЯ>. Сначала рядом
    во временную папку, на место — переименованием."""
    prefix = f"runtime/{name}/"
    staging = UPDATES / "runtime-staging"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        for n in names:
            parts = n.split("/")
            if not n.startswith(prefix) or ".." in parts or ":" in n or "\\" in n:
                raise RuntimeError("в архиве runtime странные пути")
        if f"{prefix}pythonw.exe" not in names:
            raise RuntimeError("в архиве нет Python")
        z.extractall(staging)
    ROOT.mkdir(exist_ok=True)
    dst = ROOT / name
    if dst.exists():
        shutil.rmtree(dst)
    os.replace(staging / "runtime" / name, dst)
    shutil.rmtree(staging, ignore_errors=True)


def fetch(name=WANTED):
    """Скачать runtime релизом с GitHub, распаковать и проверить. → "" или ошибка."""
    import keys
    import updater

    token = keys.get(updater.KEY_NAME)
    if not token:
        return "нет ключа доступа"
    try:
        with updater._get(f"https://api.github.com/repos/{updater.REPO}/releases/tags/{tag(name)}",
                          token) as r:
            rel = json.load(r)
        asset = next((a for a in rel.get("assets", []) if a.get("name") == asset_name(name)), None)
        if asset is None:
            return "в релизе нет архива Python"
        UPDATES.mkdir(exist_ok=True)
        part = UPDATES / (asset_name(name) + ".part")
        log.info("Скачиваю свой Python %s (%d МБ)", name, asset.get("size", 0) // 2**20)
        with updater._get(asset["url"], token, accept="application/octet-stream",
                          timeout=120) as r, part.open("wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as exc:
        return updater._explain(exc)
    try:
        unpack(part, name)
    except Exception as exc:
        return f"не распаковался: {exc}"
    finally:
        part.unlink(missing_ok=True)
    problem = check(name)
    if problem:
        shutil.rmtree(ROOT / name, ignore_errors=True)
        return f"не прошёл проверку: {problem}"
    mark_checked(name)
    log.info("Свой Python %s скачан и проверен", name)
    return ""


def prepare(name=WANTED):
    """Runtime на месте и проверен (скачать, если надо). → "" или ошибка."""
    if _valid(name):
        return ""
    if (ROOT / name / "pythonw.exe").is_file():     # собран на месте (package.py) — проверить
        problem = check(name)
        if not problem:
            mark_checked(name)
        return problem
    return fetch(name)


def start(app):
    """Фоном: перейти на свой Python WANTED. Если его нет — скачать (нужен
    ключ обновлений, как у друга). Переключаем в тихий момент."""
    import updater

    def loop():
        time.sleep(updater.FIRST_CHECK + 60)          # после проверки обновлений
        while running() != WANTED:
            if _failed(WANTED):
                log.info("Свой Python %s уже не прижился — остаюсь как есть", WANTED)
                return
            if not _valid(WANTED) and not (ROOT / WANTED).is_dir() and not updater.enabled():
                return                                # ни собранного, ни ключа — нечего делать
            STATE.update(downloading=True, error="")
            problem = prepare(WANTED) or mic_problem(WANTED)
            STATE["downloading"] = False
            if problem:
                STATE["error"] = problem
                log.warning("Свой Python %s: %s — остаюсь на прежнем", WANTED, problem)
                time.sleep(RETRY_HOURS * 3600)
                continue
            while not updater._quiet(app) or app.paused.is_set() or updater.STATE["installing"]:
                time.sleep(30)
            app.run_in_worker(lambda: switch(app))
            return

    threading.Thread(target=loop, daemon=True, name="runtime").start()


def switch(app, name=WANTED):
    """Перезапуститься на runtime name (update.py --runtime). Из рабочего потока."""
    import updater

    with updater._lock:
        if updater.STATE["installing"]:
            return
        updater.STATE["installing"] = True
    log.info("Перехожу на свой Python %s (сейчас %s)", name, running() or "venv")
    updater._launch("--runtime", name)
    updater._quit(app)


# --------------------------------------------------------------------------
# Что было при переключении (updates\runtime.json, пишет update.py)
# --------------------------------------------------------------------------

def _status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(data):
    UPDATES.mkdir(exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATUS)


def _failed(name):
    st = _status()
    return st.get("status") == "rolled_back" and st.get("to") == name


def after_start():
    """Джарвис поднялся. Только что переключились на свой Python — сказать
    update.py «всё хорошо» и перевести ярлыки. → что сказать или ""."""
    st = _status()
    now = running()
    if st.get("status") == "starting" and now and st.get("to") == now:
        st.update(status="ok", ok_at=datetime.now().isoformat(sep=" ", timespec="seconds"))
        _write_status(st)
        log.info("Работаю на своём Python %s", now)
        fix_shortcuts()
        if dev_machine():
            return "Сэр, я перешёл на свой Python"
        return "Сэр, теперь у меня свой Python. Тот, что стоит в системе, мне больше не нужен"
    if st.get("status") == "rolled_back" and not st.get("told"):
        st["told"] = True
        _write_status(st)
        return "Сэр, на своём Python я не запустился и вернулся на прежний"
    return ""


def fix_shortcuts():
    """Ярлыки, которые запускали Python из venv напрямую (панель, раскладка
    окон), — через run.vbs: он сам выберет, каким Python запускать."""
    import pythoncom
    import win32com.client

    venv_dir = str(VENV).lower()
    folder = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        for lnk in folder.rglob("*.lnk"):
            try:
                s = shell.CreateShortcut(str(lnk))
                if not str(s.TargetPath).lower().startswith(venv_dir):
                    continue
                args = s.Arguments
                s.TargetPath = "wscript.exe"
                s.Arguments = f'"{HOME / "run.vbs"}" {args}'
                s.Save()
                log.info("Ярлык «%s» теперь через run.vbs", lnk.stem)
            except Exception:
                log.exception("Не поправил ярлык %s", lnk)
    finally:
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# Для панели
# --------------------------------------------------------------------------

def status():
    st = _status()
    return {"running": running(), "wanted": WANTED, "current": current(),
            "downloading": STATE["downloading"], "error": STATE["error"],
            "failed": _failed(WANTED), "switched": st.get("status") == "ok",
            "dev": dev_machine()}


def space_items():
    """Что из Python можно удалить: старые runtime и venv, когда он не нужен."""
    from models import size_mb

    out = []
    keep = {running(), current(), WANTED}
    if ROOT.is_dir():
        for d in sorted(ROOT.iterdir()):
            if d.is_dir() and d.name not in keep:
                out.append({"id": f"runtime:{d.name}", "path": d, "size_mb": size_mb(d),
                            "title": f"Прежний Python Джарвиса ({d.name})",
                            "about": "Джарвис работает на новом — этот не нужен"})
    if running() and VENV.is_dir() and not dev_machine():
        out.append({"id": "venv", "path": VENV, "size_mb": size_mb(VENV),
                    "title": "Старое окружение Python (venv)",
                    "about": "Джарвис работает на своём Python — venv больше не нужен"})
    return out


if __name__ == "__main__":
    # runtime.py --mic — открыть микрофон этим Python (для mic_problem и install.ps1)
    if "--mic" in sys.argv:
        err = open_mic()
        print(err or "ok")
        sys.exit(1 if err else 0)
