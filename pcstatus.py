# -*- coding: utf-8 -*-
"""«Как там комп»: процессор, память, диски, видеокарта, батарея — вслух.

Без прав администратора и без ИИ. Температура — только видеокарты NVIDIA
(nvidia-smi ставится с драйвером); процессор Windows без админа не отдаёт.
"""

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

import psutil

from info import _plural

log = logging.getLogger("jarvis.pc")

CREATE_NO_WINDOW = 0x08000000
# облачные «диски» (Google Диск, OneDrive как диск) — место там не наше
_CLOUD_LABELS = ("google drive", "onedrive", "dropbox", "yandex")
_SKIP_PROCS = {"system idle process", "idle", "system", "registry", "memory compression"}

_ALL = re.compile(
    r"^(?:ну\s+)?как\s+(?:там\s+|себя\s+чувствует\s+|поживает\s+|дела\s+у\s+)*"
    r"(?:мой\s+|наш\s+)?(?:комп|компьютер|компа|компьютера|пк|ноут|ноутбук|система|железо)"
    r"(?:\s+себя\s+чувствует|\s+поживает|\s+там)?$"
    r"|^(?:состояние|статус|нагрузка|загрузка)\s+(?:компьютера|компа|системы|пк|ноутбука)$")
_WHY = re.compile(
    r"^(?:почему|чего|че|что)\s+(?:то\s+)?(?:комп|компьютер|ноут|ноутбук|все|пк)?\s*(?:так\s+)?"
    r"(?:тормозит|тупит|лагает|висит|гудит|шумит)$"
    r"|^что\s+(?:грузит|нагружает|жрет|ест|тормозит)\s+(?:комп|компьютер|процессор|память|"
    r"оперативку|пк|ноут|систему)$")
_MEM = re.compile(r"^сколько\s+(?:свободной\s+|занято\s+|осталось\s+)?(?:памяти|оперативки|"
                  r"оперативной\s+памяти|озу)(?:\s+(?:свободно|занято|осталось))?$")
_DISK = re.compile(r"^сколько\s+(?:свободного\s+)?места(?:\s+(?:на\s+диске(?:\s+\w)?|на\s+дисках|"
                   r"осталось|свободно|на\s+компе|на\s+компьютере))*$")
_CPU = re.compile(r"^(?:какая\s+)?загрузка\s+процессора$|^как\s+(?:там\s+)?процессор$")
_GPU = re.compile(r"^(?:какая\s+)?температура\s+(?:видеокарты|видюхи|компьютера|компа|железа)$"
                  r"|^как\s+(?:там\s+)?видеокарта$")


def handle(t):
    """Ответ вслух или None — не про компьютер."""
    if _ALL.match(t):
        return summary()
    if _WHY.match(t):
        return heavy()
    if _MEM.match(t):
        return memory_phrase()
    if _DISK.match(t):
        return disks_phrase() or "Дисков не нашёл, сэр"
    if _CPU.match(t):
        return f"Процессор загружен на {_pct(psutil.cpu_percent(interval=0.5))}"
    if _GPU.match(t):
        return gpu_phrase() or "Температуру видеокарты Windows мне не отдаёт, сэр"
    return None


# ------------------------------------------------------------------ фразы

def _pct(n):
    n = round(n)
    return f"{n} {_plural(n, 'процент', 'процента', 'процентов')}"


def _gb(n):
    """Гигабайты вслух: «4,5 гигабайта», «50 гигабайт»."""
    if n < 10:
        v = round(n, 1)
        if v != int(v):
            return f"{str(v).replace('.', ',')} гигабайта"   # дробные — всегда «гигабайта»
        n = v
    n = round(n)
    return f"{n} {_plural(n, 'гигабайт', 'гигабайта', 'гигабайт')}"


def memory_phrase():
    vm = psutil.virtual_memory()
    used, total = (vm.total - vm.available) / 2**30, vm.total / 2**30
    return f"Память занята на {_pct(vm.percent)}: {_gb(used)} из {round(total)}"


def disks():
    """[(буква, свободно ГБ, всего ГБ)] — только свои жёсткие диски."""
    import win32api

    out = []
    for part in psutil.disk_partitions(all=False):
        if "fixed" not in part.opts:
            continue
        try:
            label = win32api.GetVolumeInformation(part.mountpoint)[0].lower()
            if any(c in label for c in _CLOUD_LABELS):
                continue
            usage = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        out.append((part.device[0].upper(), usage.free / 2**30, usage.total / 2**30))
    return out


def disks_phrase():
    found = disks()
    if not found:
        return ""
    parts = []
    for i, (letter, free, total) in enumerate(found):
        low = " — мало" if free < 10 or free / total < 0.05 else ""
        parts.append(f"{'на диске ' if i == 0 else 'на '}{letter} {_gb(free)}{low}")
    return "Свободно " + ", ".join(parts)


def gpu():
    """(имя, температура, загрузка %) у NVIDIA, иначе None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=4, creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
        name, temp, load = [x.strip() for x in out.splitlines()[0].split(",")[:3]]
        return name, int(temp), int(load)
    except Exception:
        log.exception("nvidia-smi не ответил")
        return None


def gpu_phrase():
    g = gpu()
    if g is None:
        return ""
    _name, temp, load = g
    hot = ", горячая" if temp >= 83 else ""
    return (f"Видеокарта {temp} {_plural(temp, 'градус', 'градуса', 'градусов')}{hot}, "
            f"загружена на {_pct(load)}")


def battery_phrase():
    try:
        b = psutil.sensors_battery()
    except Exception:
        return ""
    if b is None:
        return ""
    state = "на зарядке" if b.power_plugged else "от батареи"
    return f"Батарея {_pct(b.percent)}, {state}"


def top_processes(sample=0.7):
    """(по процессору, по памяти): [(имя, % всего процессора)], [(имя, ГБ)].
    Процессы одной программы (у Chrome их десятки) складываем."""
    procs = list(psutil.process_iter(["name"]))
    for p in procs:
        try:
            p.cpu_percent(None)             # первый замер — точка отсчёта
        except psutil.Error:
            pass
    time.sleep(sample)
    cores = psutil.cpu_count() or 1
    cpu, mem = {}, {}
    for p in procs:
        name = Path(p.info.get("name") or "").stem
        if not name or name.lower() in _SKIP_PROCS:
            continue
        try:
            cpu[name] = cpu.get(name, 0) + p.cpu_percent(None) / cores
            mem[name] = mem.get(name, 0) + p.memory_info().rss / 2**30
        except psutil.Error:
            continue
    by_cpu = sorted(cpu.items(), key=lambda kv: -kv[1])[:3]
    by_mem = sorted(mem.items(), key=lambda kv: -kv[1])[:3]
    return by_cpu, by_mem


def _speakable(name):
    """chrome → Chrome, dota2 → Dota2: синтезатору так проще."""
    return name[:1].upper() + name[1:]


def _culprits(by_cpu, by_mem):
    """«Больше всего процессор грузит Dota2 — 35 процентов, память ест Chrome — 3,2 гигабайта»."""
    parts = []
    if by_cpu and by_cpu[0][1] >= 3:
        parts.append(f"больше всего процессор грузит {_speakable(by_cpu[0][0])} — {_pct(by_cpu[0][1])}")
    if by_mem:
        parts.append(f"{'память' if parts else 'больше всего памяти'} ест "
                     f"{_speakable(by_mem[0][0])} — {_gb(by_mem[0][1])}")
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]


def heavy():
    """Что грузит компьютер: общая загрузка и кто первый по процессору и памяти."""
    psutil.cpu_percent(interval=None)       # точка отсчёта для общей загрузки
    by_cpu, by_mem = top_processes()
    cpu = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    head = f"Процессор загружен на {_pct(cpu)}, память на {_pct(vm.percent)}"
    tail = _culprits(by_cpu, by_mem)
    return f"{head}. {tail}" if tail else head


def summary():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = memory_phrase()
    sentences = [f"Процессор загружен на {_pct(cpu)}, {mem[:1].lower()}{mem[1:]}"]
    sentences += [s for s in (disks_phrase(), gpu_phrase(), battery_phrase()) if s]
    # что-то не так — сразу сказать, кто виноват
    if cpu >= 80 or psutil.virtual_memory().percent >= 85:
        sentences.append(_culprits(*top_processes()))
    return ". ".join(s for s in sentences if s)
