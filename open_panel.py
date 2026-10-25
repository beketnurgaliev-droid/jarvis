# -*- coding: utf-8 -*-
"""Открыть панель Джарвиса — для ярлыка «Панель Джарвиса» в меню «Пуск».

Адрес панели (с токеном) Джарвис при каждом запуске пишет в panel.url.
Джарвис не запущен — запускаем и ждём, пока он поднимется.
"""

import subprocess
import time
import urllib.request
from pathlib import Path

import psutil

import windows

HOME = Path(__file__).resolve().parent
URL_FILE = HOME / "panel.url"
TITLE = "Панель Джарвиса"


def read_url():
    try:
        return URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def alive(url):
    try:
        urllib.request.urlopen(url.split("#")[0], timeout=2).read(1)
        return True
    except Exception:
        return False


def jarvis_running():
    for proc in psutil.process_iter(["cmdline"]):
        if any(str(part).endswith("jarvis.py") for part in proc.info["cmdline"] or []):
            return True
    return False


def browser():
    import os

    for path in (
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ):
        if path.exists():
            return str(path)
    return None


def main():
    # открытое окно могло остаться от прошлого запуска Джарвиса со старым
    # токеном — закрываем и открываем заново, это быстро
    for w in windows.list_windows():
        if w.title.startswith(TITLE):
            windows.close(w)
    url = read_url()
    if not (url and alive(url)):
        if not jarvis_running():
            subprocess.Popen(["wscript", str(HOME / "start-jarvis.vbs")])
        # модель распознавания грузится несколько секунд — ждём до минуты
        for _ in range(120):
            time.sleep(0.5)
            url = read_url()
            if url and alive(url):
                break
        else:
            return
    exe = browser()
    if exe:
        subprocess.Popen([exe, f"--app={url}", "--window-size=1180,820"])
    else:
        import webbrowser
        webbrowser.open(url)


if __name__ == "__main__":
    main()
