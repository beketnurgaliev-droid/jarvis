# -*- coding: utf-8 -*-
"""Ключи к ИИ — в файле keys.dat, зашифрованном Windows (DPAPI).

Расшифровать его может только этот пользователь на этом компьютере: если
папку Джарвиса скопируют или отправят другу, ключи там не прочитаются.
В config.json ключей нет.
"""

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("jarvis.keys")

HOME = Path(__file__).resolve().parent
KEYS_FILE = HOME / "keys.dat"
# открытые файлы с ключами — ключ Gemini пишет установщик, ключ обновлений
# приезжает в архиве для друга; переносим в keys.dat и удаляем
LEGACY = {"gemini": HOME / "gemini.key", "github": HOME / "update.key"}

_lock = threading.Lock()
_cache = None


def _protect(data):
    import win32crypt

    return win32crypt.CryptProtectData(data, "Jarvis keys", None, None, None, 0)


def _unprotect(blob):
    import win32crypt

    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]


def _save(data):
    tmp = KEYS_FILE.with_name("keys.dat.tmp")
    tmp.write_bytes(_protect(json.dumps(data).encode("utf-8")))
    os.replace(tmp, KEYS_FILE)


def _load():
    global _cache
    if _cache is not None:
        return _cache
    data = {}
    if KEYS_FILE.exists():
        try:
            data = json.loads(_unprotect(KEYS_FILE.read_bytes()).decode("utf-8"))
        except Exception:
            log.exception("Не расшифровал keys.dat — ключи придётся ввести заново")
    for name, path in LEGACY.items():
        if not path.exists():
            continue
        key = path.read_text(encoding="utf-8-sig").strip()
        if key:                       # свежий файл — новый ключ, он главнее
            data[name] = key
        try:
            _save(data)
            path.unlink()
            log.info("Ключ %s перенесён в зашифрованное хранилище", name)
        except OSError:
            log.exception("Не перенёс ключ %s", name)
    _cache = data
    return _cache


def get(name):
    with _lock:
        return _load().get(name, "")


def put(name, value):
    """Сохранить ключ; пустое значение — удалить."""
    with _lock:
        data = _load()
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        _save(data)


def hint(value):
    """«AQ.…7Q» — чтобы узнать ключ, не показывая его."""
    return f"{value[:3]}…{value[-2:]}" if len(value) > 8 else ("есть" if value else "")
