# -*- coding: utf-8 -*-
"""Производительность: чтобы Джарвис в фоне не мешал играм и работе.

Главный приём — пониженный приоритет процесса. Распознавание речи (Vosk)
крутится непрерывно и ест заметно CPU; с пониженным приоритетом Windows
отдаёт процессор игре или рабочей программе первым, а Джарвису — остаток.
На отзывчивость команд это почти не влияет: доли секунды погоды не делают.

Через psutil, а не голый ctypes: GetCurrentProcess() возвращает 64-битный
псевдохэндл, и без argtypes ctypes обрезает его до int — SetPriorityClass
молча отказывает.
"""

import logging

import psutil

log = logging.getLogger("jarvis.perf")


def set_low_priority(low=True):
    """Понизить (или вернуть обычный) приоритет текущего процесса.
    True — приоритет действительно стал таким, как просили."""
    want = psutil.BELOW_NORMAL_PRIORITY_CLASS if low else psutil.NORMAL_PRIORITY_CLASS
    try:
        proc = psutil.Process()
        proc.nice(want)
        ok = proc.nice() == want
    except (psutil.Error, OSError):
        log.exception("Не смог сменить приоритет процесса")
        return False
    if ok:
        log.info("Приоритет процесса: %s", "пониженный" if low else "обычный")
    else:
        log.warning("Приоритет процесса не сменился")
    return ok


def apply(cfg):
    """Применить настройки производительности при запуске и после правок в панели."""
    perf = cfg.get("performance", {})
    set_low_priority(perf.get("low_priority", True))
