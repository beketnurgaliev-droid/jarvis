# -*- coding: utf-8 -*-
"""Сам находит установленные игры — Steam и Epic — и помнит, как их называют.

Список не надо вести руками: при запуске и каждый раз, когда Джарвис не узнал
название, он заново читает библиотеки лаунчеров (это доли секунды — только
маленькие файлы-манифесты, без обхода диска).

Названия у игр английские, а говорим мы по-русски и на сленге («пабг», «кс»).
Поэтому для каждой новой игры Джарвис один раз в фоне спрашивает агента, как её
произносят, и запоминает ответ в games.json. Этот файл можно править руками:
"say" — как Джарвис называет игру вслух, "aliases" — ваши варианты (по ним
игра запускается даже просто по названию), "auto_aliases" — подобранные
агентом (только после «открой»/«запусти»). Ручные варианты не затираются.
"""

import json
import logging
import os
import re
import threading
import time
import winreg
from pathlib import Path

import brain

log = logging.getLogger("jarvis.games")

HOME = Path(__file__).resolve().parent
CACHE = HOME / "games.json"

# не игры, а служебные пакеты Steam
_NOT_GAMES = re.compile(r"redistributable|steamworks|proton|steam linux runtime|"
                        r"steamvr|dedicated server|soundtrack|sdk", re.I)

_lock = threading.Lock()
_games = {}          # ключ "steam:570" -> запись
_installed = set()   # ключи того, что стоит прямо сейчас
_naming = set()      # кому агент подбирает название прямо сейчас
_tried = {}          # когда последний раз пробовали — не чаще раза в час


# --------------------------------------------------------------------------
# Поиск в лаунчерах
# --------------------------------------------------------------------------

def _steam_root():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return None


def scan_steam():
    root = _steam_root()
    if root is None:
        return {}
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        text = vdf.read_text(encoding="utf-8", errors="replace")
        libs += [Path(p.replace("\\\\", "\\"))
                 for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    found = {}
    for lib in dict.fromkeys(libs):
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            text = acf.read_text(encoding="utf-8", errors="replace")
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if not appid or not name or _NOT_GAMES.search(name.group(1)):
                continue
            found[f"steam:{appid.group(1)}"] = {
                "name": name.group(1),
                "run": f"steam://rungameid/{appid.group(1)}",
            }
    return found


def scan_epic():
    folder = Path(os.environ.get("ProgramData", "")) / "Epic/EpicGamesLauncher/Data/Manifests"
    found = {}
    for item in folder.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        if data.get("bIsIncompleteInstall") or not data.get("AppName"):
            continue
        if "games" not in [c.lower() for c in data.get("AppCategories", ["games"])]:
            continue
        app = data["AppName"]
        found[f"epic:{app}"] = {
            "name": data.get("DisplayName") or app,
            "run": f"com.epicgames.launcher://apps/{app}?action=launch&silent=true",
        }
    return found


# --------------------------------------------------------------------------
# Кэш с русскими названиями
# --------------------------------------------------------------------------

def _load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save():
    CACHE.write_text(json.dumps(_games, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh(cfg, on_new=None):
    """Перечитать лаунчеры. Новым играм в фоне подобрать русские названия.
    Возвращает список названий игр, которых раньше не было."""
    with _lock:
        if not _games:
            _games.update(_load())
        found = {}
        for scan in (scan_steam, scan_epic):
            try:
                found.update(scan())
            except Exception:
                log.exception("Не смог прочитать библиотеку: %s", scan.__name__)

        new = [k for k in found if k not in _games]
        for key, item in found.items():
            entry = _games.setdefault(key, {"aliases": []})
            entry.update(item)            # путь запуска мог смениться
        _installed.clear()
        _installed.update(found)
        if new:
            _save()
            log.info("Новые игры: %s", ", ".join(_games[k]["name"] for k in new))

        # у кого ещё нет русского названия. Каждое «не узнал» перечитывает
        # лаунчеры — без этой проверки агент запускался бы на одно и то же
        # по нескольку раз, а после неудачи — на каждой команде
        now = time.monotonic()
        unnamed = [k for k in _installed if not _games[k].get("say")
                   and k not in _naming and now - _tried.get(k, -1e9) > 3600]
        for k in unnamed:
            _naming.add(k)
            _tried[k] = now
    if unnamed and brain.available(cfg):
        threading.Thread(target=_name_games, args=(unnamed, cfg, on_new),
                         daemon=True, name="games").start()
    elif unnamed:
        with _lock:
            _naming.difference_update(unnamed)
    return [found[k]["name"] for k in new]


def _name_games(keys, cfg, on_new=None):
    """Спросить агента, как по-русски называют эти игры."""
    try:
        _ask_names(keys, cfg, on_new)
    finally:
        with _lock:
            _naming.difference_update(keys)


def _ask_names(keys, cfg, on_new=None):
    names = [_games[k]["name"] for k in keys]
    prompt = (
        "Для голосового помощника на русском: как русскоязычные игроки произносят "
        "и называют эти игры — полное название русскими буквами, сленг, сокращения, "
        "а также как это может ошибочно услышать распознавание речи. Только "
        "варианты самого названия — без переводов, описаний и обычных фраз, "
        "иначе игра будет запускаться от случайного разговора. До 15 вариантов "
        "на игру, каждый — одно-три слова. Ответь ТОЛЬКО "
        'JSON-объектом {"<название как дано>": {"say": "как произнести по-русски, '
        'коротко", "aliases": ["вариант", ...]}} без пояснений. '
        "Игры: " + json.dumps(names, ensure_ascii=False)
    )
    raw = brain.complete(prompt, cfg)
    match = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(match.group()) if match else {}
    except ValueError:
        data = {}
    if not data:
        log.warning("Агент не подобрал названия для: %s", ", ".join(names))
        return

    with _lock:
        for key in keys:
            info = data.get(_games[key]["name"])
            if not isinstance(info, dict):
                continue
            _games[key]["say"] = str(info.get("say") or _games[key]["name"])
            # подобранное агентом — отдельно от того, что вписано руками
            _games[key]["auto_aliases"] = [
                a for a in info.get("aliases", [])
                if isinstance(a, str) and len(a.split()) <= 3][:15]
        _save()
    log.info("Подобрал русские названия для %d игр", len(keys))
    if on_new:
        on_new()


# --------------------------------------------------------------------------
# Для skills.py
# --------------------------------------------------------------------------

def entries(strict=False):
    """Тройки (как могут сказать, как назвать вслух, как запустить)
    для установленных сейчас игр. strict — без вариантов от агента: по этим
    Джарвис запускает игру даже без слова «открой»."""
    with _lock:
        items = [_games[k] for k in _installed if k in _games]
    for g in items:
        say = g.get("say") or g["name"]
        yield g["name"], say, g["run"]
        yield say, say, g["run"]
        for alias in g.get("aliases", []):
            yield alias, say, g["run"]
        if strict:
            continue
        for alias in g.get("auto_aliases", []):
            yield alias, say, g["run"]


def installed_names():
    with _lock:
        return [_games[k].get("say") or _games[k]["name"] for k in _installed if k in _games]
