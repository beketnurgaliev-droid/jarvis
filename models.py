# -*- coding: utf-8 -*-
"""Модели распознавания (Vosk): какую грузить на этом ПК, докачать маленькую,
освободить место.

Почти вся память и процессор Джарвиса — это модель: большая занимает ~1,6 ГБ
памяти и 1,3 ГБ на диске, маленькая — ~0,6 ГБ и 87 МБ. На ПК с памятью меньше
WEAK_RAM_GB в режиме «Автоматически» (model_path пустой) берём маленькую, а
если её нет — скачиваем и переходим на неё на ходу, без перезапуска.
"""

import logging
import os
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import psutil

import version

log = logging.getLogger("jarvis.models")

HOME = Path(__file__).resolve().parent
SMALL = "vosk-model-small-ru-0.22"
SMALL_URL = f"https://alphacephei.com/vosk/models/{SMALL}.zip"
DOWNLOAD_DIR = HOME / "updates" / "model-download"   # не vosk-model-*: недокачанное не выбрать
WEAK_RAM_GB = 12
# у большой модели установщик отключал дорескоринг переименованием — эти части
# Джарвис не грузит никогда, а на диске они ~2,3 ГБ
DISABLED_PARTS = ("_rnnlm.off", "_rescore.off")
RETRY_HOURS = 1

STATE = {"downloading": False, "error": ""}
_started = threading.Lock()          # фоновая докачка — одна на процесс


def ram_gb():
    return psutil.virtual_memory().total / 2**30


def weak_pc():
    return ram_gb() < WEAK_RAM_GB


def is_small(folder):
    return "small" in Path(folder).name.lower()


def folders():
    """Готовые модели рядом со скриптом (недокачанные и пустые папки — мимо)."""
    return [d for d in sorted(HOME.glob("vosk-model-*"))
            if d.is_dir() and (d / "am" / "final.mdl").is_file()]


def choose(configured=""):
    """Папка модели: из настроек, иначе — по ПК: на слабом маленькая, на
    сильном большая (что есть). None — моделей нет вовсе."""
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = HOME / path
        if path.is_dir():
            return path
        log.warning("Модель по пути из настроек не найдена: %s", configured)
    dirs = folders()
    if not dirs:
        return None
    weak = weak_pc()
    return sorted(dirs, key=lambda d: is_small(d) != weak)[0]


def need_small(configured=""):
    """«Автоматически» на слабом ПК, а маленькой модели нет — надо скачать."""
    return not configured and weak_pc() and not any(is_small(d) for d in folders())


def ascii_path(path):
    """Vosk не открывает модель по пути с русскими буквами («C:\\Users\\Алексей\\…»).
    Короткое имя DOS (C:\\Users\\5D3B~1\\…) — латиницей, им и пользуемся."""
    path = str(path)
    if path.isascii():
        return path
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)) and buf.value.isascii():
        return buf.value
    log.warning("В пути к модели русские буквы, а короткого имени нет: %s", path)
    return path


# --------------------------------------------------------------------------
# Докачать маленькую
# --------------------------------------------------------------------------

def download_small():
    """Скачать и распаковать маленькую модель (~45 МБ). → папка модели.
    Сначала во временную папку, на место — одним переименованием."""
    target = HOME / SMALL
    if (target / "am" / "final.mdl").is_file():
        return target
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True)
    zpath = DOWNLOAD_DIR / "model.zip"
    log.info("Скачиваю маленькую модель: %s", SMALL_URL)
    req = urllib.request.Request(SMALL_URL, headers={"User-Agent": f"Jarvis/{version.VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, zpath.open("wb") as f:
        shutil.copyfileobj(r, f)
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if not name.startswith(SMALL + "/") or ".." in name.split("/") or ":" in name:
                raise RuntimeError("в архиве модели странные пути")
        z.extractall(DOWNLOAD_DIR)
    src = DOWNLOAD_DIR / SMALL
    if not (src / "am" / "final.mdl").is_file():
        raise RuntimeError("в архиве нет модели")
    if target.exists():                    # пустая или битая папка от прошлой попытки
        shutil.rmtree(target)
    os.replace(src, target)
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    log.info("Маленькая модель на месте: %s", target)
    return target


def start(app):
    """Фоном: на слабом ПК в режиме «Автоматически» докачать маленькую модель и
    перейти на неё. Не скачалось — попробовать через час. Можно звать
    повторно (панель: выбрали «Автоматически») — второй раз не качает."""
    if not need_small(app.cfg.get("model_path", "")) or not _started.acquire(blocking=False):
        return

    def loop():
        try:
            _loop()
        finally:
            _started.release()

    def _loop():
        while need_small(app.cfg.get("model_path", "")):
            STATE.update(downloading=True, error="")
            try:
                download_small()
                STATE["downloading"] = False
                log.info("На этом ПК %.0f ГБ памяти — перехожу на маленькую модель", ram_gb())
                app.switch_model()
                return
            except Exception as exc:
                STATE.update(downloading=False, error=str(exc))
                log.warning("Маленькая модель не скачалась: %s", exc)
                shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
            time.sleep(RETRY_HOURS * 3600)

    threading.Thread(target=loop, daemon=True, name="models").start()


# --------------------------------------------------------------------------
# Место на диске
# --------------------------------------------------------------------------

def size_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return round(total / 2**20)


def space_items(keep):
    """Что из моделей можно удалить. keep — папки, которые трогать нельзя:
    загруженная и та, что выбрана в настройках."""
    out = []
    keep = {Path(k).resolve() for k in keep if k}
    for d in folders():
        name = "маленькая" if is_small(d) else "большая"
        if d.resolve() not in keep:
            out.append({"id": f"model:{d.name}", "path": d, "size_mb": size_mb(d),
                        "title": f"Модель распознавания «{name}» — сейчас не используется",
                        "about": "Понадобится снова — скачается установщиком"})
        for part in DISABLED_PARTS:
            p = d / part
            if p.is_dir():
                out.append({"id": f"part:{d.name}/{part}", "path": p, "size_mb": size_mb(p),
                            "title": f"Отключённая часть модели «{name}» ({part.strip('_').split('.')[0]})",
                            "about": "Джарвис её не загружает — только место занимает"})
    return out
