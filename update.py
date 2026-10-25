# -*- coding: utf-8 -*-
"""Установщик обновления — работает, пока Джарвис выключен.

Запускает его updater.py (копией из updates\\: этот файл тоже заменяется) и
сразу выходит из Джарвиса:

    pythonw _update.py АРХИВ --root ПАПКА --pid PID         — поставить версию
    pythonw _update.py --rollback --root ПАПКА --pid PID    — вернуть прежнюю

Порядок: дождаться выхода Джарвиса → прежние файлы в backup\\ → распаковать
новые (личное не трогаем) → библиотеки, если поменялся requirements.txt →
запустить → ждать, пока новая версия отзовётся в updates\\installed.json.
Не отозвалась — вернуть прежние файлы и запустить снова. Что бы ни случилось,
в конце Джарвис запущен.

Код Джарвиса отсюда не импортируем: работает старая копия этого файла с
новой версией на диске. По той же причине — запреты, а не список разрешённого:
новая версия может принести новые папки.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
OK_TIMEOUT = 180            # сек: столько ждём, что новая версия отзовётся
# личное — не трогаем никогда (как package.PRIVATE)
PRIVATE = {"gemini.key", "keys.dat", "update.key", "config.json", "config.json.bak",
           "learned.json", "games.json", "jarvis.log", "panel.url", "memory.json",
           "reminders.json"}
# папки, куда архив не пишет: окружение, модели, журналы, сами обновления
KEEP_DIRS = {"venv", "logs", "updates", "backup", "versions", "agent-home"}

log = logging.getLogger("update")


def _now():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def read_version(root):
    try:
        text = (root / "version.py").read_text(encoding="utf-8")
    except OSError:
        return "?"
    m = re.search(r'CHANGES\s*=\s*\[\s*\(\s*"([^"]+)"', text)
    return m.group(1) if m else "?"


def write_status(root, data):
    path = root / "updates" / "installed.json"
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_status(root):
    try:
        return json.loads((root / "updates" / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# Процессы
# --------------------------------------------------------------------------

def _jarvis_procs(root):
    """Джарвисы из этой папки (и шим venv, и сам python)."""
    import psutil

    target = str(root / "jarvis.py").lower()
    out = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if target in " ".join(p.info["cmdline"] or []).lower():
                out.append(p)
        except (psutil.Error, TypeError):
            pass
    return out


def wait_exit(root, pid):
    """Дождаться, пока Джарвис закроется; завис — закрыть."""
    import psutil

    if pid:
        try:
            psutil.Process(pid).wait(timeout=30)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            log.warning("Джарвис не закрылся за 30 с — закрываю")
    stop_jarvis(root)


def stop_jarvis(root):
    import psutil

    procs = _jarvis_procs(root)
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=10)


def start_jarvis(root):
    log.info("Запускаю Джарвиса")
    subprocess.Popen(["wscript.exe", str(root / "start-jarvis.vbs")], cwd=str(root),
                     creationflags=CREATE_NO_WINDOW)


def wait_ok(root, new):
    """Новая версия отозвалась? Ждём OK_TIMEOUT; если Джарвиса нет в процессах
    дольше 40 с подряд — он упал, ждать дальше незачем."""
    started = time.monotonic()
    gone_since = None
    while time.monotonic() - started < OK_TIMEOUT:
        st = read_status(root)
        if st.get("status") == "ok" and st.get("to") == new:
            return True
        try:
            alive = bool(_jarvis_procs(root))
        except Exception:
            alive = True
        if alive:
            gone_since = None
        elif gone_since is None:
            gone_since = time.monotonic()
        elif time.monotonic() - gone_since > 40:
            log.error("Новая версия закрылась, не отозвавшись")
            return False
        time.sleep(2)
    st = read_status(root)
    return st.get("status") == "ok" and st.get("to") == new


# --------------------------------------------------------------------------
# Файлы
# --------------------------------------------------------------------------

def _allowed(rel):
    parts = Path(rel).parts
    if not parts or parts[-1] in PRIVATE:
        return False
    if len(parts) > 1 and (parts[0] in KEEP_DIRS or parts[0].startswith("vosk-model-")):
        return False
    return ".." not in parts and not Path(rel).is_absolute()


def members(z):
    """Файлы архива: путь внутри папки Джарвиса → имя в архиве."""
    out = {}
    for name in z.namelist():
        if name.endswith("/") or not name.startswith("Jarvis/"):
            continue
        rel = name[len("Jarvis/"):]
        if rel and _allowed(rel):
            out[rel] = name
    return out


def backup(root, rels, old):
    """Прежние версии файлов, которые заменит архив, — в backup\\. Манифест:
    что было (вернуть при откате) и чего не было (удалить при откате)."""
    bdir = root / "backup"
    if bdir.exists():
        shutil.rmtree(bdir)
    files = bdir / "files"
    replaced, added = [], []
    for rel in rels:
        src = root / rel
        if src.is_file():
            dst = files / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            replaced.append(rel)
        else:
            added.append(rel)
    # манифест — последним: нет манифеста — нет и отката, но файлы ещё не тронуты
    (bdir / "manifest.json").write_text(json.dumps(
        {"version": old, "time": _now(), "replaced": replaced, "added": added},
        ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Прежняя версия %s сохранена: %d файлов", old, len(replaced))


def unpack(root, zpath, files):
    """Сначала всё в updates\\staging, потом быстро по местам — так оборванная
    распаковка не оставит половину старого и половину нового."""
    staging = root / "updates" / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    with zipfile.ZipFile(zpath) as z:
        for rel, name in files.items():
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, dst.open("wb") as out:
                shutil.copyfileobj(src, out)
    for rel in files:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / rel, dst)
    shutil.rmtree(staging, ignore_errors=True)
    log.info("Распаковано файлов: %d", len(files))


def pip_install(root):
    python = root / "venv" / "Scripts" / "python.exe"
    log.info("Поменялись библиотеки — ставлю")
    r = subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements.txt"),
                        "--disable-pip-version-check", "-q"],
                       cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1200, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        log.error("pip: %s", (r.stdout + r.stderr)[-2000:])
        raise RuntimeError("библиотеки не поставились")


def restore(root):
    """Вернуть файлы из backup\\. → версия, которую вернули ("" — нечего)."""
    bdir = root / "backup"
    try:
        m = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.error("Нет резервной копии — вернуть нечего")
        return ""
    for rel in m.get("replaced", []):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bdir / "files" / rel, dst)
    for rel in m.get("added", []):
        try:
            (root / rel).unlink()
        except OSError:
            pass
    log.info("Вернул версию %s", m.get("version"))
    # копия совпадает с тем, что теперь стоит, — кнопка «вернуть» больше не нужна
    shutil.rmtree(bdir, ignore_errors=True)
    return m.get("version", "")


# --------------------------------------------------------------------------

def install(root, zpath):
    old = read_version(root)
    with zipfile.ZipFile(zpath) as z:
        files = members(z)
        new_req = z.read("Jarvis/requirements.txt") if "requirements.txt" in files else None
        m = re.search(r'CHANGES\s*=\s*\[\s*\(\s*"([^"]+)"', z.read("Jarvis/version.py").decode("utf-8"))
    new = m.group(1) if m else "?"
    log.info("Обновление %s → %s", old, new)
    req = root / "requirements.txt"
    req_changed = new_req is not None and (not req.exists() or req.read_bytes() != new_req)
    backup(root, files, old)
    write_status(root, {"status": "installing", "from": old, "to": new, "time": _now()})
    try:
        unpack(root, zpath, files)
        if req_changed:
            pip_install(root)
    except Exception:
        log.exception("Установка %s не удалась", new)
        rollback(root, new, "error")
        return
    write_status(root, {"status": "starting", "from": old, "to": new, "time": _now()})
    start_jarvis(root)
    if wait_ok(root, new):
        log.info("Версия %s запустилась", new)
        try:
            Path(zpath).unlink()
        except OSError:
            pass
        return
    log.error("Версия %s не отозвалась — возвращаю %s", new, old)
    stop_jarvis(root)
    rollback(root, new, "no_start")


def rollback(root, attempted, by):
    """Вернуть прежнюю версию и запустить. attempted — та, от которой ушли:
    её updater сам больше не поставит, дождётся следующей."""
    back = restore(root)
    write_status(root, {"status": "rolled_back", "from": back or read_version(root),
                        "to": attempted, "by": by, "time": _now()})
    start_jarvis(root)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", nargs="?")
    ap.add_argument("--root", required=True)
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    (root / "logs").mkdir(exist_ok=True)
    logging.basicConfig(filename=root / "logs" / "update.log", encoding="utf-8",
                        level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    log.info("=" * 50)
    try:
        wait_exit(root, args.pid)
        if args.rollback:
            rollback(root, read_version(root), "user")
        else:
            install(root, Path(args.zip))
    except Exception:
        log.exception("Обновление упало")
        # что бы ни случилось — Джарвис должен работать
        try:
            if not _jarvis_procs(root):
                start_jarvis(root)
        except Exception:
            start_jarvis(root)


if __name__ == "__main__":
    sys.exit(main())
