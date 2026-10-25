# -*- coding: utf-8 -*-
"""Локальные команды — то, что Джарвис делает сам, мгновенно и без интернета.

Всё, что сюда не подошло, уходит наверх, в brain.py (полноценный агент).
"""

import asyncio
import ctypes
import datetime
import json
import logging
import os
import re
import subprocess
import threading
import time
import webbrowser
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import browser
import games
import info
import memory
import reminders
import version
import windows
import youtube_bridge

log = logging.getLogger("jarvis.skills")


class Result:
    """Что Джарвис ответит и, если надо, что спросит перед опасным действием."""

    def __init__(self, say=None, ask=None, action=None):
        self.say = say
        self.ask = ask
        self.action = action


# --------------------------------------------------------------------------
# Поиск программ в меню «Пуск» — чтобы «запусти что угодно» работало
# без предварительной настройки
# --------------------------------------------------------------------------

_JUNK = (
    "uninstall", "удалить", "remove", "readme", "справка", "документация",
    "help", "license", "лиценз", "website", "сайт", "changelog", "manual",
    "руководство", "обновл", "update", "настройк", "config", "командная строка",
)

_PROGRAMS = {}


def index_programs():
    """Собрать ярлыки из меню «Пуск» (пользовательского и общего)."""
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        Path(os.environ.get("PUBLIC", "")) / "Desktop",
    ]
    found = {}
    for root in roots:
        if not root or not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in (".lnk", ".url", ".exe"):
                continue
            low = path.stem.lower()
            if any(j in low for j in _JUNK):
                continue
            found.setdefault(low, path)
    _PROGRAMS.clear()
    _PROGRAMS.update(found)
    log.info("Найдено программ в меню «Пуск»: %d", len(found))
    return found


def _norm(s):
    return re.sub(r"[^a-zа-я0-9 ]", " ", s.lower().replace("ё", "е")).strip()


# Программы в меню «Пуск» названы латиницей («google chrome», «microsoft word»),
# а говорим мы по-русски («хром», «ворд»). Поэтому сравниваем не буквы, а
# транслитерацию и согласный «скелет» слова: «ворд» -> vrd, «word» -> vrd.

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "i",
    "ь": "", "э": "e", "ю": "iu", "я": "ia",
}


def _translit(s):
    return "".join(_TRANSLIT.get(ch, ch) for ch in _norm(s))


def _skeletons(word):
    """Согласные остовы слова. Латинская «c» читается и как к, и как с,
    поэтому вариантов может быть несколько: excel -> kskl и ksl."""
    base = _translit(word)
    for a, b in (("sch", "sh"), ("ch", "h"), ("kh", "h"), ("th", "t"),
                 ("dzh", "g"), ("ck", "k"), ("xc", "ks"), ("x", "ks"),
                 ("w", "v"), ("q", "k"), ("y", "i"), ("j", "i")):
        base = base.replace(a, b)
    out = set()
    for variant in (base.replace("c", "k"), base.replace("c", "s")):
        skel = re.sub(r"[aeiou0-9]", "", variant)
        if skel:
            out.add(skel)
    return out


def _word_score(qw, qskel, cw):
    """Похожесть одного произнесённого слова на одно слово из названия."""
    if qw == cw:
        return 1.0
    best = SequenceMatcher(None, qw, cw).ratio()
    # обрывок засчитываем только для длинных слов, иначе «а» из «a static»
    # цепляется к чему угодно
    if len(qw) >= 4 and len(cw) >= 4 and (qw in cw or cw in qw):
        best = max(best, 0.88)
    if qskel and len(qskel) >= 2:
        cskels = _skeletons(cw)
        if qskel in cskels:
            best = max(best, 0.95)
        elif len(qskel) >= 3 and any(len(c) >= 3 and qskel in c for c in cskels):
            best = max(best, 0.86)
    return best


def find_program(spoken):
    """Подобрать ярлык по произнесённому названию."""
    q_words = [w for w in _translit(spoken).split() if w]
    if not q_words:
        return None, 0.0
    q_skels = [next(iter(_skeletons(w)), "") for w in _translit(spoken).split()]
    phrase = " ".join(q_words)

    best, score = None, 0.0
    for name, path in _PROGRAMS.items():
        c_words = [w for w in _translit(name).split() if w]
        if not c_words:
            continue
        # каждое произнесённое слово должно найти себе пару в названии
        per_word = [max(_word_score(qw, qs, cw) for cw in c_words)
                    for qw, qs in zip(q_words, q_skels)]
        ratio = sum(per_word) / len(per_word)
        # лишние слова в названии — небольшой штраф, чтобы «Steam»
        # обходил «Steam Support Center»
        ratio *= 1 - 0.03 * max(0, len(c_words) - len(q_words))
        ratio = max(ratio, SequenceMatcher(None, phrase, " ".join(c_words)).ratio())
        if ratio > score:
            best, score = path, ratio
    return best, score


# --------------------------------------------------------------------------
# Свой список программ и игр из config.json — секция "apps".
# Формат: {"Дота 2": {"run": "steam://rungameid/570", "aliases": ["дота", ...]}}
# Ключ — то, что Джарвис скажет вслух («Запускаю Дота 2»).
# Старый формат {"хром": "chrome"} тоже понимаем.
# --------------------------------------------------------------------------

# «открой игру доту», «поиграем в доту пожалуйста» — лишнее для поиска
_APP_FILLER = re.compile(r"\b(?:игру|игра|программу|приложение|мне|пожалуйста|"
                         r"поиграем|поиграть|в|во|на)\b")


def _app_entries(cfg, with_games=True):
    """Развернуть секцию apps и найденные игры в тройки
    (как могут сказать, как назвать вслух, как запустить)."""
    for name, spec in cfg.get("apps", {}).items():
        if isinstance(spec, str):
            yield _norm(name), name, spec
            continue
        run = spec.get("run", name)
        yield _norm(name), name, run
        for alias in spec.get("aliases", []):
            yield _norm(alias), name, run
    if not with_games:
        return
    for alias, name, run in games.entries():
        yield _norm(alias), name, run


def find_app(spoken, cfg):
    """Найти программу или игру. Не узнал — перечитать лаунчеры (вдруг игру
    только что поставили) и попробовать ещё раз."""
    name, run = _match_app(spoken, cfg)
    if name is None:
        if games.refresh(cfg):
            name, run = _match_app(spoken, cfg)
    return name, run


def _match_app(spoken, cfg):
    """Вернуть (имя, запуск) или (None, None).
    Сначала точное совпадение, потом похожее — Vosk любит «доту» слышать
    как «дота ту», а «пабг» как «пап же»."""
    q = _norm(spoken)
    variants = {q, re.sub(r"\s+", " ", _APP_FILLER.sub(" ", q)).strip()}
    variants.discard("")
    entries = list(_app_entries(cfg))

    for alias, name, run in entries:
        if alias in variants:
            return name, run

    best, score = (None, None), 0.0
    for alias, name, run in entries:
        for v in variants:
            r = SequenceMatcher(None, v, alias).ratio()
            # согласный остов: «кантр страйк» ≈ «контр страйк»
            # (короткие остовы вроде «кс» совпадают с чем попало — не берём)
            common = _skeletons(v.replace(" ", "")) & _skeletons(alias.replace(" ", ""))
            if any(len(s) >= 3 for s in common):
                r = max(r, 0.9)
            if r > score:
                best, score = (name, run), r
    if score >= cfg.get("app_similarity", 0.8):
        return best
    return None, None


# --------------------------------------------------------------------------
# Окна: «переключись на хром», «сверни телеграм», «закрой дискорд»
# --------------------------------------------------------------------------

def find_windows(spoken, cfg, fuzzy=True):
    """Окна программы, которую назвали. Сначала через свой список программ и
    игр (там известно имя процесса), иначе — по похожести на имя процесса или
    заголовок окна (если fuzzy)."""
    wins = windows.list_windows()
    if not wins:
        return []

    procs, titles = set(), set()
    name, run = _match_app(spoken, cfg)
    if name:
        spec = cfg.get("apps", {}).get(name)
        if isinstance(spec, dict) and spec.get("title"):
            # веб-приложение в окне браузера: процесс общий, отличаем по заголовку
            titles.add(_norm(spec["title"]))
        elif isinstance(spec, dict) and spec.get("process"):
            procs.update(p.strip().lower() for p in spec["process"].split(","))
        elif "://" not in run:
            procs.add(Path(run).stem.lower())
        # у игр заголовок окна обычно совпадает с английским названием
        for eng, say, grun in games.entries(strict=True):
            if grun == run:
                titles.add(_norm(eng))
    if procs or titles:
        hit = [w for w in wins if w.exe in procs
               or any(t and t in _norm(w.title) for t in titles)]
        if hit:
            return hit
    if not fuzzy:
        return []

    q_words = [w for w in _translit(spoken).split() if w]
    if not q_words:
        return []
    q_skels = [next(iter(_skeletons(w)), "") for w in q_words]
    best, score = [], 0.0
    for w in wins:
        for label in (w.exe, w.title):
            c_words = [x for x in _translit(label).split() if x][:8]
            if not c_words:
                continue
            ratio = sum(max(_word_score(qw, qs, cw) for cw in c_words)
                        for qw, qs in zip(q_words, q_skels)) / len(q_words)
            if ratio > score + 1e-9:
                best, score = [w], ratio
            elif abs(ratio - score) < 1e-9 and w not in best:
                best.append(w)
    if score >= cfg.get("window_similarity", 0.8):
        # одна программа — берём все её окна, чужие не трогаем
        exe = best[0].exe
        return [w for w in wins if w.exe == exe]
    return []


def _window_command(verb, spoken, cfg, soft=False):
    if spoken in ("окно", "это", "это окно", "текущее окно", "текущее"):
        w = windows.foreground()
        if w is None:
            return None if soft else Result(say="Не вижу активного окна, сэр")
        targets = [w]
        label = "окно"
    else:
        # по догадке — только программы из своего списка, без похожих заголовков,
        # чтобы «закрою глаза» не закрыло окно с похожим названием
        targets = find_windows(spoken, cfg, fuzzy=not soft)
        label = spoken
        if not targets:
            if verb == "focus":
                name, run = _match_app(spoken, cfg)
                if name:
                    launch(run)
                    return Result(say=f"{name} не был открыт, запускаю")
            return None if soft else Result(say=f"Не вижу окна {spoken}, сэр")

    if verb == "focus":
        windows.focus(targets[0])
        return Result(say=None)          # и так видно — молча
    if verb == "minimize":
        for w in targets:
            windows.minimize(w)
        return Result(say="Свернул")
    if verb == "close":
        for w in targets:
            windows.close(w)
        return Result(say=f"Закрываю {label}")
    return None


# «разложи код», «разложи окна вс кода» — VS Code по-русски не найти ни в
# списке программ, ни по заголовку, поэтому называем процесс сами
_VSCODE = re.compile(r"^(?:окна\s+)?(?:(?:вс|вэс|вес|vs|в\s?эс)\s*)?(?:код|кода|коды|code)$")


def _tile_command(spoken, cfg, soft=False):
    """Разложить по экрану все окна одной программы."""
    if _VSCODE.match(spoken):
        targets = [w for w in windows.list_windows() if w.exe == "code"]
        label = "VS Code"
    elif spoken in ("окна", "все окна", "эти окна", "окна этой программы"):
        # программа активного окна
        fg = windows.foreground()
        if fg is None:
            return None if soft else Result(say="Не вижу активного окна, сэр")
        targets = [w for w in windows.list_windows() if w.exe == fg.exe]
        label = fg.exe
    else:
        targets = find_windows(re.sub(r"^окна\s+", "", spoken), cfg, fuzzy=not soft)
        label = spoken
    if not targets:
        return None if soft else Result(say=f"Не вижу окон {label}, сэр")
    n = windows.tile(targets)
    return Result(say=None if n == 1 else f"Разложил {n}")


# --------------------------------------------------------------------------
# Игровой режим: закрыть рабочее, схема питания «максимальная
# производительность», звук погромче, а сам Джарвис — только локальные
# команды, без агента, и не цепляется к разговору в голосовом чате
# --------------------------------------------------------------------------

GAME_MODE = {"on": False, "power_before": None}
_HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"   # стандартная схема Windows


def _power_scheme():
    try:
        out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True,
                             creationflags=0x08000000).stdout.decode("cp866", "replace")
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", out)
        return m.group() if m else None
    except OSError:
        return None


def _set_power(guid):
    if guid:
        subprocess.run(["powercfg", "/setactive", guid], capture_output=True,
                       creationflags=0x08000000)


def _game_close_targets(cfg):
    """Что из списка «закрыть на время игры» сейчас запущено:
    {"1С": ["1cv8", "1cv8c"], ...}."""
    spec = cfg.get("game_mode", {}).get("close", {})
    wanted = {name: [p.strip().lower() for p in procs.split(",")]
              for name, procs in spec.items()}
    alive = windows.running([p for ps in wanted.values() for p in ps])
    return {name: ps for name, ps in wanted.items() if alive & set(ps)}


def game_mode_on(cfg, closing):
    gm = cfg.get("game_mode", {})
    closed = windows.close_processes([p for ps in closing.values() for p in ps]) \
        if closing else []
    if gm.get("power_plan", True):
        before = _power_scheme()
        if before != _HIGH_PERF:
            GAME_MODE["power_before"] = before
        _set_power(_HIGH_PERF)
    if gm.get("volume") is not None:
        set_volume(gm["volume"])
    GAME_MODE["on"] = True
    log.info("Игровой режим включён, закрыто: %s", closed)
    return "Игровой режим, сэр. Слушаю только команды по имени"


def game_mode_off(cfg):
    if GAME_MODE.get("power_before"):
        _set_power(GAME_MODE["power_before"])
    GAME_MODE.update(on=False, power_before=None)
    log.info("Игровой режим выключен")
    return "Обычный режим, сэр"


# --------------------------------------------------------------------------
# Свои режимы: одной фразой — несколько действий («рабочий режим» — открыть
# 1С и Телеграм, свернуть лишнее, поставить громкость). Настраиваются в панели,
# в config.json — список "modes": {"name", "phrases", "steps": [{"do", ...}]}
# --------------------------------------------------------------------------

_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"   # стандартная «Сбалансированная»
MODE_STEPS = ("open", "close", "minimize", "focus", "minimize_all", "restore_all", "tile",
              "volume", "pause", "url", "power", "wait", "say")


def _mode_phrases(mode):
    return {_norm(p) for p in list(mode.get("phrases", [])) + [mode.get("name", "")] if _norm(p)}


def find_mode(t, cfg):
    """Режим, который назвали: «рабочий режим», «включи рабочий режим», «режим кино»."""
    m = re.match(r"^(?:включи|запусти|давай)\s+(?:режим\s+)?(.+)$|^режим\s+(.+)$", t)
    said = {t} | ({(m.group(1) or m.group(2)).strip()} if m else set())
    for mode in cfg.get("modes", []):
        if said & _mode_phrases(mode):
            return mode
    return None


def _mode_command(t, cfg):
    mode = find_mode(t, cfg)
    if mode is None:
        return None
    # закрыть — значит, может пропасть несохранённое: сначала спрашиваем
    closing = [s.get("target", "") for s in mode.get("steps", [])
               if s.get("do") == "close" and find_windows(s.get("target", ""), cfg)]
    if closing and mode.get("confirm_close", True):
        return Result(ask=f"Закрою {', '.join(closing)}. Включить «{mode.get('name')}»?",
                      action=lambda: run_mode(mode, cfg))
    return Result(say=run_mode(mode, cfg))


def run_mode(mode, cfg):
    """Выполнить шаги режима по порядку. Вернуть, что сказать."""
    if GAME_MODE["on"]:
        game_mode_off(cfg)
    said, missed = [], []
    for step in mode.get("steps", []):
        target = str(step.get("target", "")).strip()
        try:
            if not _mode_step(step.get("do"), target, step.get("value"), cfg, said):
                missed.append(target or step.get("do"))
        except Exception:
            log.exception("Шаг режима %s упал: %s", mode.get("name"), step)
            missed.append(target or step.get("do"))
    log.info("Режим «%s» выполнен, не вышло: %s", mode.get("name"), missed)
    reply = " ".join(said) or f"{mode.get('name') or 'Режим'}, сэр"
    if missed:
        reply = reply.rstrip(" .") + ". Не нашёл: " + ", ".join(missed)
    return reply


def _mode_step(do, target, value, cfg, said):
    """Один шаг режима. False — не получилось (назовём вслух)."""
    if do == "open":
        # уже открыто — вывести вперёд, а не запускать второй раз (1С откроется заново)
        wins = find_windows(target, cfg)
        if wins:
            windows.focus(wins[0])
            return True
        name, run = find_app(target, cfg)
        if name:
            launch(run)
            return True
        path, score = find_program(target)
        if path is not None and score >= cfg.get("match_threshold", 0.70):
            launch(path)
            return True
        return False
    if do in ("close", "minimize", "focus"):
        act = {"close": windows.close, "minimize": windows.minimize, "focus": windows.focus}[do]
        for w in find_windows(target, cfg):
            act(w)
        return True                       # нечего закрывать — тоже хорошо
    if do == "minimize_all":
        windows.minimize_all()
        return True
    if do == "restore_all":
        windows.restore_all()
        return True
    if do == "tile":
        wins = ([w for w in windows.list_windows() if w.exe == "code"] if _VSCODE.match(_norm(target))
                else find_windows(target, cfg))
        if wins:
            windows.tile(wins)
        return bool(wins)
    if do == "volume":
        set_volume(int(value))
        return True
    if do == "pause":
        _press(_VK["play"])
        return True
    if do == "url":
        return browser.open_url(target)
    if do == "power":
        _set_power(_HIGH_PERF if value == "high" else _BALANCED)
        return True
    if do == "wait":
        time.sleep(max(0.0, min(30.0, float(value or 1))))
        return True
    if do == "say":
        said.append(target)
        return True
    return False


def launch(target):
    """Запустить ярлык, exe, ссылку steam:// или системную команду."""
    if isinstance(target, Path):
        os.startfile(str(target))
        return
    # в конфиге можно указать просто название ярлыка из меню «Пуск»
    shortcut = _PROGRAMS.get(target.lower())
    if shortcut is not None:
        os.startfile(str(shortcut))
        return
    try:
        os.startfile(target)  # работает для chrome, notepad, calc и т.п.
    except OSError:
        subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)


# --------------------------------------------------------------------------
# Звук, медиа-клавиши
# --------------------------------------------------------------------------

_VK = {"play": 0xB3, "next": 0xB0, "prev": 0xB1, "mute": 0xAD}


def _press(vk):
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


# --------------------------------------------------------------------------
# Следующая песня на ютубе. Медиаклавиша уходит тому плееру, который Windows
# считает текущим, — это не обязательно браузер. Поэтому сначала ищем
# медиасеанс браузера (SMTC) и переключаем именно его; нет такого — клавиша.
# --------------------------------------------------------------------------

# как браузеры подписаны в медиасеансах; 308046b0af4a39cb — это Firefox
_BROWSERS = ("chrome", "msedge", "firefox", "308046b0af4a39cb", "yandex",
             "browser", "opera", "brave", "vivaldi")


def _short_title(title):
    """«Песня (Official Video) [4K]» → «Песня»: скобки вслух не нужны."""
    title = re.sub(r"\s*[(\[【].*?[)\]】]", "", title or "").strip(" -|")
    return title[:60]


async def _browser_skip(direction):
    """Переключить песню в браузере. Вернуть название новой (или "", если не
    успело смениться), False — браузер переключать не даёт, None — в браузере
    ничего не играет."""
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager)

    mgr = await Manager.request_async()
    sessions = list(mgr.get_sessions())
    found = [s for s in sessions
             if any(b in s.source_app_user_model_id.lower() for b in _BROWSERS)]
    log.info("Медиасеансы: %s", [s.source_app_user_model_id for s in sessions])
    if not found:
        return None
    # играющий (статус 4) — первым, на паузе — потом
    found.sort(key=lambda s: s.get_playback_info().playback_status != 4)
    session = found[0]
    # сразу после переключения ютуб секунду-две не даёт листать дальше
    for _ in range(8):
        controls = session.get_playback_info().controls
        if (controls.is_next_enabled if direction == "next"
                else controls.is_previous_enabled):
            break
        await asyncio.sleep(0.25)
    else:
        return False
    before = (await session.try_get_media_properties_async()).title
    if direction == "next":
        done = await session.try_skip_next_async()
    else:
        done = await session.try_skip_previous_async()
    if not done:
        return False
    # ютуб грузит следующее видео секунду-две — ждём, чтобы назвать его
    try:
        for _ in range(10):
            await asyncio.sleep(0.25)
            title = (await session.try_get_media_properties_async()).title
            if title and title != before:
                return _short_title(title)
    except Exception:
        log.exception("Не прочитал название новой песни")
    return ""


async def _playing_now():
    """Играет ли сейчас хоть что-то (медиасеанс в статусе 4)."""
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager)

    mgr = await Manager.request_async()
    return any(s.get_playback_info().playback_status == 4 for s in mgr.get_sessions())


def _run_winrt(coro_fn, *args, timeout=6.0):
    """WinRT — в отдельном потоке: в рабочем потоке Джарвиса COM уже поднят
    как STA (для pycaw и голоса). Упало или зависло — None."""
    box = {}

    def work():
        try:
            box["value"] = asyncio.run(coro_fn(*args))
        except Exception:
            log.exception("Медиасеансы Windows недоступны")

    th = threading.Thread(target=work, daemon=True, name="smtc")
    th.start()
    th.join(timeout)
    return box.get("value")


def media_skip(direction):
    """Следующая ("next") или предыдущая ("prev") песня. Вернуть ответ вслух."""
    label = "Следующая" if direction == "next" else "Предыдущая"
    # сначала — кнопкой в самом плеере, через расширение: так листается и
    # одно длинное видео, где ютуб отключает медиаклавиши
    reply = youtube_bridge.call({"type": direction}, timeout=6)
    if reply and reply.get("ok"):
        title = _short_title(reply.get("title", ""))
        return f"{label}: {title}" if title else label
    if reply:
        log.info("Расширение ютуба не переключило (%s) — пробую через Windows", reply.get("error"))
    title = _run_winrt(_browser_skip, direction)
    if title is None:                  # браузер не играет — пусть решает Windows
        log.info("В браузере ничего не играет — жму медиаклавишу")
        _press(_VK[direction])
        return label
    if title is False:
        return "Ютуб не даёт переключить, сэр"
    return f"{label}: {title}" if title else label


def _volume():
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    speakers = AudioUtilities.GetSpeakers()
    # pycaw с осени 2025 отдаёт обёртку AudioDevice, а не голый IMMDevice
    if hasattr(speakers, "EndpointVolume"):
        return speakers.EndpointVolume
    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def set_volume(percent):
    percent = max(0, min(100, int(percent)))
    _volume().SetMasterVolumeLevelScalar(percent / 100.0, None)
    return percent


def nudge_volume(delta):
    vol = _volume()
    now = vol.GetMasterVolumeLevelScalar()
    new = max(0.0, min(1.0, now + delta / 100.0))
    vol.SetMasterVolumeLevelScalar(new, None)
    return int(round(new * 100))


def youtube_volume(percent=None, delta=None):
    """Громкость ютуба — прямо в плеере, через расширение Chrome «Джарвис для
    ютуба». Расширение не подключено — громкость браузера в микшере Windows.
    Общую громкость компьютера не трогаем. Вернуть ответ вслух."""
    reply = youtube_bridge.call({"type": "volume", "percent": percent, "delta": delta})
    if reply is not None:
        if "volume" in reply:
            return f"Громкость ютуба {reply['volume']}"
        if reply.get("error") == "no_tab":
            return "Ютуб не открыт, сэр"
        if reply.get("error") == "no_player":
            return "Не вижу плеер на ютубе, сэр"
        log.warning("Расширение ютуба ответило: %s", reply)
    new = browser_volume(percent, delta)
    if new is None:
        return "Ютуб сейчас без звука, сэр"
    return f"Громкость хрома {new}. Расширение для ютуба не подключено"


# запасной путь — громкость браузера в микшере Windows
_BROWSER_EXE = ("chrome", "msedge", "firefox", "browser", "opera", "brave", "vivaldi")


def browser_volume(percent=None, delta=None):
    """Поставить (percent) или сдвинуть (delta) громкость браузера в микшере.
    Вернуть новую в процентах или None, если браузер сейчас без звука."""
    from pycaw.pycaw import AudioUtilities

    found = []
    for session in AudioUtilities.GetAllSessions():
        try:
            name = session.Process.name().lower() if session.Process else ""
        except Exception:
            continue
        if any(b in name for b in _BROWSER_EXE):
            found.append(session.SimpleAudioVolume)
    if not found:
        return None
    now = found[0].GetMasterVolume()
    new = percent / 100.0 if percent is not None else now + (delta or 0) / 100.0
    new = max(0.0, min(1.0, new))
    for vol in found:
        vol.SetMasterVolume(new, None)
    return int(round(new * 100))


# --------------------------------------------------------------------------
# Числа словами: «громкость тридцать» → 30
# --------------------------------------------------------------------------

_ONES = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19,
    # «до десяти», «до пяти»
    "двух": 2, "трех": 3, "четырех": 4, "пяти": 5, "шести": 6, "семи": 7,
    "восьми": 8, "девяти": 9, "десяти": 10, "пятнадцати": 15,
}
_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100,
    "двадцати": 20, "тридцати": 30, "сорока": 40, "пятидесяти": 50,
    "шестидесяти": 60, "семидесяти": 70, "восьмидесяти": 80, "девяноста": 90,
    "ста": 100,
    # распознавание слышит «на сто» как «на стол» — привычнее звучит
    "стол": 100,
}


def parse_number(text):
    digits = re.search(r"\d+", text)
    if digits:
        return int(digits.group())
    total, seen = 0, False
    for word in _norm(text).split():
        if word in _TENS:
            total += _TENS[word]
            seen = True
        elif word in _ONES:
            total += _ONES[word]
            seen = True
    return total if seen else None


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------

def open_youtube(url):
    """Открыть ссылку в уже открытой вкладке ютуба (закреплённой — в первую
    очередь) через расширение Chrome. Расширения нет — новой вкладкой."""
    reply = youtube_bridge.call({"type": "open", "url": url}, timeout=5)
    if reply and reply.get("opened"):
        log.info("Ютуб: открыл в %s вкладке", {"tab": "своей", "new": "новой"}
                 .get(reply["opened"], reply["opened"]))
        return
    webbrowser.open(url)


def play_youtube(query):
    try:
        from yt_dlp import YoutubeDL

        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "skip_download": True, "socket_timeout": 12}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get("entries") or []
        if entries and entries[0].get("id"):
            item = entries[0]
            open_youtube(f"https://www.youtube.com/watch?v={item['id']}")
            title = (item.get("title") or query).strip()
            return f"Включаю: {title}"
    except Exception:
        log.exception("yt-dlp не смог найти трек")
    open_youtube("https://www.youtube.com/results?search_query=" + quote(query))
    return "Не смог выбрать сам, открыл поиск на ютубе"


# --------------------------------------------------------------------------
# Разное
# --------------------------------------------------------------------------

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница",
             "суббота", "воскресенье"]


def _plural(n, one, few, many):
    """Час / часа / часов — иначе Ирина говорит «двенадцать часов один минут»."""
    tail = abs(n) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def say_time(now):
    hours = f"{now.hour} {_plural(now.hour, 'час', 'часа', 'часов')}"
    if now.minute == 0:
        return f"Сейчас {hours} ровно"
    minutes = f"{now.minute} {_plural(now.minute, 'минута', 'минуты', 'минут')}"
    return f"Сейчас {hours} {minutes}"


def screenshot():
    from PIL import ImageGrab

    folder = Path(os.environ["USERPROFILE"]) / "Pictures" / "Jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screen-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return path


def lock_screen():
    ctypes.windll.user32.LockWorkStation()


# --------------------------------------------------------------------------
# Разбор фразы
# --------------------------------------------------------------------------

_LAUNCH = re.compile(r"^(?:запусти|запустить|запусть|открой|открыть|включи программу|"
                     r"запуск)\s+(.+)$")
# «включи доту», «давай доту» — только если название есть в своём списке,
# иначе «включи свет» ушло бы искать программу «свет»
_LAUNCH_SOFT = re.compile(r"^(?:включи|врубай|вруби|давай|поиграем в|хочу в)\s+(.+)$")
_YOUTUBE = re.compile(
    r"^(?:включи|поставь|найди|запусти)\s+"
    r"(?:на\s+)?(?:ютубе?|youtube|музыку|песню|трек|видео|клип)\s*(.*)$"
)
_YOUTUBE_TAIL = re.compile(r"^(?:включи|поставь)\s+(.+?)\s+на\s+(?:ютубе?|youtube)$")
# «поставь песню такую-то» — слово «песню» в поисковый запрос не нужно
_FILLER = re.compile(r"^(?:песню|песня|песни|трек|музыку|видео|клип|мне|"
                     r"пожалуйста)(?:\s+(?:песню|трек|видео|клип))*\s+")

# «следующая песня», «включи следующую на ютубе», «переключи трек», «давай
# другую». «На ютубе» — только в предложном падеже: «переключи на ютуб» — это
# про окно, а не про песню
_SONG = r"(?:трек\w*|песн\w*|видео|видос|клип\w*|музык\w*)"
_ON_YT = r"(?:\s+(?:на|в)\s+ютубе)?"
_NEXT = re.compile(
    rf"^(?:(?:включи|поставь|давай|переключи\s+на|перейди\s+на)\s+)?"
    rf"(?:следующ\w*|другую|другой|дальше|некст|скип)(?:\s+{_SONG})?{_ON_YT}$"
    rf"|^(?:переключи|пропусти|скипни|смени)(?:\s+{_SONG})?{_ON_YT}$")
_PREV = re.compile(
    rf"^(?:(?:включи|поставь|давай|верни)\s+)?(?:предыдущ\w*|прошл\w*)"
    rf"(?:\s+{_SONG})?{_ON_YT}$"
    rf"|^(?:назад|верни)(?:\s+{_SONG})?{_ON_YT}$")


# --------------------------------------------------------------------------
# Ослышки. Распознавание путает слова («сверни» → «севернее», «закрой» →
# «закрою»), и такая команда уходила агенту, который тратил секунды на «не
# расслышал». Прежде чем сдаться, угадываем, какую команду имели в виду.
# --------------------------------------------------------------------------

_VERBS = ("открой", "запусти", "закрой", "сверни", "разверни", "переключись",
          "перейди", "покажи", "включи", "выключи", "громкость", "поставь",
          "спрячь", "восстанови", "разложи")

# фразы целиком — только безобидные команды, которые легко отменить
_CANONICAL = ("сверни все окна", "разверни все окна", "покажи рабочий стол",
              "игровой режим", "обычный режим", "выключи игровой режим",
              "выключи звук", "включи звук", "сделай громче", "сделай тише",
              "следующий трек", "предыдущий трек", "поставь на паузу",
              "следующая песня",
              "который час", "сколько времени", "какое сегодня число",
              "сделай скриншот", "какие у меня игры", "закрой окно", "сверни окно",
              "разложи код", "разложи окна")
_CANONICAL_WORDS = ("громче", "тише", "пауза", "скриншот", "дальше", "назад")

# что агент однажды узнал как ослышанную команду — в следующий раз без него
LEARNED = Path(__file__).resolve().parent / "learned.json"
_learned = None


def _load_learned():
    global _learned
    if _learned is None:
        try:
            _learned = json.loads(LEARNED.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _learned = {}
    return _learned


def remember(heard, command):
    """Запомнить: так распознавание слышит эту команду."""
    data = _load_learned()
    data[_norm(heard)] = _norm(command)
    _save_learned(data)


def forget(heard):
    """Забыть выученную ослышку — если агент угадал неверно (из панели)."""
    data = _load_learned()
    if data.pop(heard, None) is not None or data.pop(_norm(heard), None) is not None:
        _save_learned(data)


def _save_learned(data):
    try:
        LEARNED.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    except OSError:
        log.exception("Не смог сохранить выученные ослышки")


def _fix_verb(word):
    """Ближайший глагол команды к ослышанному слову или None."""
    if word in _VERBS or len(word) < 4:
        return None
    skels = {s for s in _skeletons(word) if len(s) >= 3}
    scored = []
    for verb in _VERBS:
        ratio = SequenceMatcher(None, word, verb).ratio()
        same = bool(skels & _skeletons(verb))   # «севернее» и «сверни» — оба svrn
        if (same and ratio >= 0.6) or ratio >= 0.8:
            scored.append((ratio + (0.2 if same else 0.0), verb))
    if not scored:
        return None
    scored.sort(reverse=True)
    # «вклюни» одинаково похоже на «включи» и «выключи» — не гадаем
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1:
        return None
    return scored[0][1]


def _closest(t, phrases, threshold):
    scored = sorted(((SequenceMatcher(None, t, c).ratio(), c) for c in phrases),
                    reverse=True)
    best = scored[0]
    second = scored[1] if len(scored) > 1 else (0.0, "")
    if best[0] >= threshold and best[0] - second[0] >= 0.05:
        return best[1]
    return None


def _guesses(t):
    """Что могли иметь в виду — от самого правдоподобного."""
    out = []
    learned = _load_learned().get(t)
    if learned:
        out.append(learned)
    words = t.split()
    for n in (1, 2):              # «свер ни все окна» — слово разорвало надвое
        if len(words) > n:
            verb = _fix_verb("".join(words[:n]))
            if verb:
                out.append(" ".join([verb] + words[n:]))
    guess = (_closest(t, _CANONICAL, 0.75) if len(words) >= 2
             else _closest(t, _CANONICAL_WORDS, 0.8))
    if guess:
        out.append(guess)
    return [g for g in dict.fromkeys(out) if g != t]


_HINT_COMMANDS = (
    "открой или запусти <программа>", "переключись на <программа>",
    "сверни <программа>", "закрой <программа>", "закрой окно", "сверни все окна",
    "разверни все окна", "разложи код (окна VS Code по экрану)",
    "разложи окна (окна активной программы)", "разложи <программа>", "громкость <число>", "громче", "тише", "выключи звук",
    "громкость ютуба <число> (только браузер, не весь компьютер)", "ютуб тише", "ютуб громче",
    "пауза", "следующая песня (переключает ютуб в браузере; так же «давай другую», "
    "«переключи песню», «пропусти»)", "предыдущая песня",
    "включи на ютубе <что>",
    "открой сайт <что> / открой в браузере <что> (телеграм, ютуб, почта, вк…)",
    "найди в браузере <что> / загугли <что>", "открой вкладку (<сайт>)",
    "какая погода (в <городе>) (завтра)", "будет ли дождь", "курс доллара", "сто долларов в тенге",
    "таймер на <время>", "напомни через <время> / в <часы> <что>", "какие напоминания",
    "отмени таймер", "открой панель (настройки Джарвиса)", "запомни что <факт о пользователе>",
    "забудь про <что>", "что ты обо мне знаешь",
    "игровой режим", "обычный режим", "который час", "какое сегодня число",
    "какая у тебя версия", "проверь обновления", "обновись (поставить новую версию Джарвиса)",
    "сделай скриншот", "какие у меня игры", "заблокируй компьютер",
    "выключи компьютер", "перезагрузи компьютер",
)


def command_hint(cfg):
    """Что Джарвис умеет сам — для агента, чтобы тот узнавал ослышанные команды."""
    names = sorted({name for _, name, _ in _app_entries(cfg)})
    modes = [m.get("name", "") for m in cfg.get("modes", []) if m.get("name")]
    return (
        "Джарвис сам, без тебя, выполняет команды: " + "; ".join(_HINT_COMMANDS) + ". "
        "Программы и игры, которые он умеет открывать: " + ", ".join(names) + ". "
        + ("Его режимы (включаются названием): " + ", ".join(modes) + ". " if modes else "") +
        "Фраза пришла из распознавания речи и может быть искажена. Если она — "
        "ослышанная версия одной из этих команд (например, «севернее все окна» — "
        "это «сверни все окна»), ответь ровно одной строкой: КОМАНДА: <команда в "
        "правильном виде>. Ничего больше не пиши и инструменты не вызывай. "
        "Частые ослышки: «ютуб» слышится как «и тут», «тут», «джерри тут»; "
        "«сто» — как «стол»; «Джарвис» — как «джарис», «жарвис»."
    )


def handle(text, cfg):
    """Вернуть Result, если команда локальная. Иначе None — пусть решает агент."""
    t = _norm(text)
    if not t:
        return None
    # 1) как сказано, но искать только в своём списке — без «похожих» ярлыков
    result = _handle(t, cfg, soft=True)
    if result is not None:
        return result
    # 2) догадки: «сверни все окно» — это «сверни все окна», а не окно «все окно»
    for guess in _guesses(t):
        result = _handle(guess, cfg, soft=True)
        if result is not None:
            log.info("Понял «%s» как «%s»", t, guess)
            return result
    # 3) как сказано, с поиском по меню «Пуск» и заголовкам окон; если и там
    # пусто — честное «не нашёл программу …»
    return _handle(t, cfg)


_VOL_TARGET = re.compile(r"ютуб|музык|браузер|хром")
_VOL_WORDS = re.compile(r"громкост|тише|громче|убав|прибав")


def _browser_volume_command(t):
    """«громкость ютуба на десять», «на ютубе музыку потише», «сделай музыку
    громче» — громкость только браузера. Иначе None."""
    # «ютуб на двадцать» — тоже громкость, хоть и без слова «громкость».
    # «Ютуб» распознавание часто слышит как «и тут», «тут», «ю туб»
    short = re.fullmatch(r"(?:на\s+)?(?:ютуб\w*|ютюб\w*|ю\s?туб\w*|(?:\w+\s+)?тут)"
                         r"\s+(?:на|до)\s+\S+(?:\s+\S+)?", t) \
        and parse_number(t) is not None
    if not ((_VOL_TARGET.search(t) and _VOL_WORDS.search(t)) or short):
        return None
    # «включи на ютубе тише воды» — это поиск песни, а не громкость
    if "громкост" not in t and (_YOUTUBE.match(t) or _YOUTUBE_TAIL.match(t)):
        return None
    n = parse_number(t)
    if n is not None:
        return Result(say=youtube_volume(percent=n))
    if re.search(r"тише|убав", t):
        return Result(say=youtube_volume(delta=-10))
    if re.search(r"громче|прибав", t):
        return Result(say=youtube_volume(delta=10))
    return Result(say=youtube_volume(delta=0))     # «какая громкость у ютуба»


# --------------------------------------------------------------------------
# Браузер: открыть сайт, поиск в браузере, новая вкладка. Триггеры нарочно
# явные («в браузере», «загугли», «сайт»), чтобы не перехватывать «какая
# погода» (это info.py) и «найди на ютубе» (это YouTube).
# --------------------------------------------------------------------------

_BROWSER_SEARCH = re.compile(
    r"^(?:загугли|погугли|нагугли|гугли)\s+(.+)$"
    r"|^(?:найди|поищи|ищи|найти|поиск)\s+(?:мне\s+)?"
    r"(?:в\s+(?:браузере|гугле|интернете|сети|яндексе)|через\s+браузер)\s+(.+)$"
    r"|^(?:найди|поищи|ищи)\s+(.+?)\s+в\s+(?:браузере|гугле|интернете|сети)$"
    r"|^поиск\s+в\s+(?:браузере|интернете|гугле)\s+(.+)$")
# «перейди на X» без слова «сайт» — это окно (ниже), поэтому тут только «сайт»
_BROWSER_SITE = re.compile(
    r"^(?:открой|открыть|запусти|покажи)\s+(?:мне\s+)?(?:сайт|страницу|ссылку)\s+(.+)$"
    r"|^(?:открой|открыть|запусти|покажи)\s+(?:мне\s+)?в\s+браузере\s+(.+)$"
    r"|^(?:открой|открыть)\s+(.+?)\s+в\s+браузере$"
    r"|^(?:зайди|заходи)\s+(?:на|в)\s+(?:сайт\s+)?(.+)$"
    r"|^(?:перейди|перейти)\s+на\s+сайт\s+(.+)$")
_BROWSER_TAB = re.compile(
    r"^(?:открой|открыть|создай|давай)\s+(?:мне\s+)?(?:новую\s+|пустую\s+)?вкладку(?:\s+(.+))?$"
    r"|^(?:новая|новую)\s+вкладка?у?$")


def _first_group(m):
    return next((g for g in m.groups() if g), "").strip()


def _browser_command(t):
    """Команда про браузер — Result, иначе None."""
    m = _BROWSER_TAB.match(t)
    if m:
        target = re.sub(r"^(?:с|со|для|на|по)\s+", "", _first_group(m)).strip()
        return Result(say=browser.open_tab(target))
    m = _BROWSER_SEARCH.match(t)
    if m:
        query = _first_group(m)
        return Result(say=browser.search(query)) if query else None
    m = _BROWSER_SITE.match(t)
    if m:
        target = _first_group(m)
        return Result(say=browser.open_site(target)) if target else None
    return None


def _handle(t, cfg, soft=False):
    """Разбор уже нормализованной фразы. soft — это догадка: не нашли, что
    открыть или закрыть, — значит, догадка неверна, возвращаем None."""

    # --- звук ---
    result = _browser_volume_command(t)
    if result is not None:
        return result
    if re.match(r"^(выключи|убери|отключи)\s+(звук|громкость)$|^тишина$|^без звука$", t):
        _press(_VK["mute"])
        return Result(say="Звук выключен")
    if re.match(r"^(включи|верни)\s+звук$", t):
        _press(_VK["mute"])
        return Result(say="Звук включён")
    if t.startswith("громкость") or re.match(r"^(сделай\s+)?громкость", t):
        n = parse_number(t)
        if n is not None:
            return Result(say=f"Громкость {set_volume(n)}")
        return Result(say="На сколько, сэр?")
    if re.match(r"^(громче|сделай громче|прибавь звук)$", t):
        return Result(say=f"Громкость {nudge_volume(10)}")
    if re.match(r"^(тише|сделай тише|убавь звук)$", t):
        return Result(say=f"Громкость {nudge_volume(-10)}")

    # --- медиа ---
    # «стоп», когда ничего не играет, — это «хватит слушать»: медиаклавиша
    # тогда, наоборот, включила бы музыку
    if t in ("стоп", "останови") and _run_winrt(_playing_now, timeout=2) is False:
        return Result(say="Отбой, сэр")
    if re.match(r"^(пауза|стоп|останови|поставь на паузу|продолжи|играй)$", t):
        _press(_VK["play"])
        return Result(say="Готово, сэр")
    if _NEXT.match(t):
        return Result(say=media_skip("next"))
    if _PREV.match(t):
        return Result(say=media_skip("prev"))

    # --- браузер: открыть сайт, поиск, вкладка (явные триггеры) ---
    result = _browser_command(t)
    if result is not None:
        return result

    # --- свои режимы из панели — раньше встроенных: «рабочий режим» может быть своим ---
    result = _mode_command(t, cfg)
    if result is not None:
        return result

    # --- погода, курсы валют, таймеры и напоминания ---
    for module in (reminders, info):
        say = module.handle(t, cfg)
        if say:
            return Result(say=say)

    # --- память о пользователе ---
    m = re.match(r"^(?:запомни|запиши в память)\s+(?:что\s+|себе\s+)?(.+)$", t)
    if m:
        return Result(say="Запомнил, сэр" if memory.add(m.group(1)) else "Это я уже знаю, сэр")
    m = re.match(r"^забудь\s+(?:что\s+|про\s+|о\s+|об\s+)?(.+)$", t)
    if m:
        gone = memory.forget(m.group(1))
        return Result(say=f"Забыл: {gone['text']}" if gone else "Такого не помню, сэр")
    if re.match(r"^что ты (?:обо мне |про меня )?(?:знаешь|помнишь|запомнил)(?: обо мне| про меня)?$", t):
        facts = memory.facts()
        if not facts:
            return Result(say="Пока ничего, сэр. Скажите: запомни, что…")
        last = "; ".join(f["text"] for f in facts[-3:])
        return Result(say=f"Помню {len(facts)} {_plural(len(facts), 'вещь', 'вещи', 'вещей')}. "
                          f"Последнее: {last}")

    # --- игровой режим ---
    if re.match(r"^((включи|врубай|вруби|давай)\s+)?(игровой режим|режим игры)$", t):
        if GAME_MODE["on"]:
            return Result(say="Игровой режим уже включён, сэр")
        closing = _game_close_targets(cfg)
        if closing:
            # закрываем рабочие программы — там может быть несохранённое
            names = ", ".join(closing)
            return Result(ask=f"Закрою {names}. Включить игровой режим?",
                          action=lambda: game_mode_on(cfg, closing))
        return Result(say=game_mode_on(cfg, {}))
    if re.match(r"^(обычный режим|рабочий режим|(выключи|выйди из|отключи)\s+"
                r"(игровой режим|игрового режима|режим игры))$", t):
        if not GAME_MODE["on"]:
            return Result(say="Игровой режим и так выключен")
        return Result(say=game_mode_off(cfg))

    # --- обновления Джарвиса (до ютуба: «поставь обновление» — не песня) ---
    if re.match(r"^(?:проверь|проверить|поищи|посмотри)\s+(?:есть ли\s+)?(?:новые\s+)?"
                r"(?:обновления|обновление|новую версию)$"
                r"|^есть (?:ли )?(?:новые обновления|обновления|новая версия)$", t):
        import updater
        return Result(say=updater.check_phrase(cfg))
    if re.match(r"^(?:обновись|обновляйся|обнови себя|обновиться|"
                r"(?:установи|поставь) (?:обновление|новую версию))$", t):
        import updater
        return Result(say=updater.update_now())

    # --- панель Джарвиса. «Открой панель управления» — это Windows, не мы ---
    if re.match(r"^(?:открой|открыть|покажи)\s+(?:свою\s+|твою\s+)?панель(?:\s+джарвиса)?$"
                r"|^панель джарвиса$|^(?:твои|свои) настройки$", t):
        import panel
        return Result(say="Открываю панель", action=panel.open_window)

    # --- окна ---
    if re.match(r"^(сверни|сверните|свернуть)\s+(все|все окна|окна)$|^покажи рабочий стол$", t):
        windows.minimize_all()
        return Result(say="Свернул")
    if re.match(r"^(разверни|верни|восстанови)\s+(все|все окна|окна)$", t):
        windows.restore_all()
        return Result(say="Вернул окна")
    m = re.match(r"^(?:разложи|раскидай|расставь|упорядочи)\s+(.+)$", t)
    if m:
        return _tile_command(m.group(1).strip(), cfg, soft)
    m = re.match(r"^(?:переключись|перейди|переключи)\s+(?:на|в|во)\s+(.+)$|"
                 r"^(?:покажи|разверни)\s+(.+)$", t)
    if m:
        return _window_command("focus", (m.group(1) or m.group(2)).strip(), cfg, soft)
    m = re.match(r"^(?:сверни|сверните|свернуть|спрячь)\s+(.+)$", t)
    if m:
        return _window_command("minimize", m.group(1).strip(), cfg, soft)
    m = re.match(r"^(?:закрой|закрыть|выключи программу)\s+(.+)$", t)
    if m:
        return _window_command("close", m.group(1).strip(), cfg, soft)

    # --- YouTube ---
    m = _YOUTUBE_TAIL.match(t) or _YOUTUBE.match(t)
    if m:
        query = _FILLER.sub("", m.group(1).strip()).strip()
        if not query:
            return Result(say="Что именно включить, сэр?")
        return Result(say=play_youtube(query))

    # --- запуск программ ---
    m = _LAUNCH.match(t) or _LAUNCH_SOFT.match(t)
    if m:
        spoken = m.group(1).strip()
        name, run = find_app(spoken, cfg)
        if name:
            launch(run)
            return Result(say=f"Запускаю {name}")
        # по догадке меню «Пуск» не перебираем: там найдётся «похожее» что угодно
        if _LAUNCH.match(t) and not soft:
            path, score = find_program(spoken)
            if path is not None and score >= cfg.get("match_threshold", 0.70):
                launch(path)
                return Result(say=f"Запускаю {path.stem}")
            return Result(say=f"Не нашёл программу {spoken}, сэр")

    # «Джарвис, дота» — просто название, без глагола. Варианты от агента тут
    # не берём: среди них бывают обычные слова, игра запускалась бы от разговора
    bare = [(_norm(a), n, r) for a, n, r in games.entries(strict=True)]
    for alias, name, run in list(_app_entries(cfg, with_games=False)) + bare:
        if alias == t:
            launch(run)
            return Result(say=f"Запускаю {name}")

    # --- какие игры стоят ---
    if re.match(r"^(какие|что)( у меня)? (игры|есть игры|за игры)( есть| установлены| стоят)?$", t):
        games.refresh(cfg)
        names = games.installed_names()
        if not names:
            return Result(say="Игр не нашёл, сэр")
        return Result(say=f"Установлено {len(names)}: " + ", ".join(sorted(names)))

    # --- время и дата ---
    if re.match(r"^(который час|сколько времени|скажи время)$", t):
        return Result(say=say_time(datetime.datetime.now()))
    if re.match(r"^(какое (сегодня )?число|какой (сегодня )?день|какая дата)$", t):
        now = datetime.datetime.now()
        return Result(say=f"Сегодня {now.day} {_MONTHS[now.month - 1]}, "
                          f"{_WEEKDAYS[now.weekday()]}")
    if re.match(r"^(какая )?(у тебя |твоя )?версия( у тебя)?$", t):
        day = datetime.date.fromisoformat(version.DATE)
        return Result(say=f"Версия {version.spoken()} от {day.day} {_MONTHS[day.month - 1]}")

    # --- скриншот ---
    if re.match(r"^(скриншот|сделай скриншот|снимок экрана|сфотографируй экран)$", t):
        path = screenshot()
        return Result(say="Скриншот в папке Изображения, Jarvis")

    # --- опасное: спрашиваем подтверждение голосом ---
    if re.match(r"^(заблокируй|блокировка)(\s+(компьютер|экран))?$", t):
        return Result(ask="Заблокировать компьютер, сэр?", action=lock_screen)
    if re.match(r"^(выключи компьютер|выключайся|выключение)$", t):
        return Result(ask="Выключить компьютер, сэр? Скажи «да» для подтверждения",
                      action=lambda: subprocess.Popen(["shutdown", "/s", "/t", "5"]))
    if re.match(r"^(перезагрузи( компьютер)?|перезагрузка)$", t):
        return Result(ask="Перезагрузить компьютер, сэр? Скажи «да» для подтверждения",
                      action=lambda: subprocess.Popen(["shutdown", "/r", "/t", "5"]))

    return None
