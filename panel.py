# -*- coding: utf-8 -*-
"""Панель Джарвиса — окно настроек.

Маленький веб-сервер внутри самого Джарвиса: только 127.0.0.1, случайный
порт и случайный токен на каждый запуск (без него /api/* не отвечает).
Страница — panel/index.html, окно — Edge в режиме приложения, без вкладок и
адресной строки. Сервер живёт в том же процессе, поэтому сохранённое
применяется сразу.
"""

import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ai
import brain
import games
import keys
import memory
import models
import reminders
import runtime
import skills
import updater
import version
import windows
import youtube_bridge

log = logging.getLogger("jarvis.panel")

HOME = Path(__file__).resolve().parent
PAGE = HOME / "panel" / "index.html"
CONFIG = HOME / "config.json"
LOG_FILE = HOME / "logs" / "jarvis.log"
URL_FILE = HOME / "panel.url"
STARTUP_LNK = (Path(os.environ.get("APPDATA", ""))
               / "Microsoft/Windows/Start Menu/Programs/Startup/Jarvis.lnk")
TITLE = "Панель Джарвиса"
CREATE_NO_WINDOW = 0x08000000

_panel = None
_save_lock = threading.Lock()


# --------------------------------------------------------------------------
# Запуск и окно
# --------------------------------------------------------------------------

class Panel:
    def __init__(self, app):
        self.app = app
        self.token = secrets.token_urlsafe(24)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.panel = self
        self.port = self.httpd.server_address[1]
        # чужой сайт может подсунуть свой домен на 127.0.0.1 (DNS rebinding) —
        # отвечаем, только если обратились по нашему адресу
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.opened = False         # открывали ли окно в этот запуск

    @property
    def url(self):
        # токен — после #: в запросы к серверу и в журналы он не попадает
        return f"http://127.0.0.1:{self.port}/#t={self.token}"


def start(app):
    global _panel
    _panel = Panel(app)
    threading.Thread(target=_panel.httpd.serve_forever, daemon=True, name="panel").start()
    log.info("Панель ждёт на порту %d", _panel.port)
    # адрес для ярлыка «Панель Джарвиса» (open_panel.py) — меняется каждый запуск
    try:
        URL_FILE.write_text(_panel.url, encoding="utf-8")
    except OSError:
        log.exception("Не записал адрес панели")
    return _panel


def _browser():
    for path in (
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ):
        if path.exists():
            return str(path)
    return None


def open_window():
    """Показать панель: открыта этим запуском — вывести вперёд. Окно от прошлого
    запуска знает старый токен и не работает — закрываем и открываем новое."""
    if _panel is None:
        return False
    for w in windows.list_windows():
        if w.title.startswith(TITLE):
            if _panel.opened:
                windows.focus(w)
                return True
            windows.close(w)
    _panel.opened = True
    exe = _browser()
    if exe:
        subprocess.Popen([exe, f"--app={_panel.url}", "--window-size=1180,820"])
    else:
        import webbrowser
        webbrowser.open(_panel.url)
    return True


# --------------------------------------------------------------------------
# Настройки на диске
# --------------------------------------------------------------------------

def save_config(cfg):
    """Записать config.json целиком: сначала во временный файл, прошлую версию —
    в config.json.bak. Так оборванная запись не испортит настройки."""
    with _save_lock:
        tmp = CONFIG.with_name("config.json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if CONFIG.exists():
            shutil.copy2(CONFIG, CONFIG.with_name("config.json.bak"))
        os.replace(tmp, CONFIG)


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _com(func):
    """Вызов, которому нужен COM (голоса SAPI, ярлыки), из потока сервера."""
    import pythoncom

    pythoncom.CoInitialize()
    try:
        return func()
    finally:
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# Что показывает панель
# --------------------------------------------------------------------------

def _models():
    """Модели распознавания рядом со скриптом — для выбора в панели.
    Маленькая грузит процессор и память в разы меньше (слабый ПК), большая
    слышит точнее. Плюс «Автоматически» (path "") — по памяти этого ПК."""
    out = []
    for folder in models.folders():
        small = models.is_small(folder)
        out.append({
            "path": folder.name,
            "small": small,
            "label": ("Маленькая — ~0,6 ГБ памяти, для слабого ПК" if small
                      else "Большая — точнее, но ~1,6 ГБ памяти и больше процессора"),
        })
    # маленькую показываем первой — это и есть выбор для слабого ПК
    out.sort(key=lambda m: not m["small"])
    return out


def _model_state(app):
    auto = models.choose("")
    auto_small = models.need_small("") or (auto is not None and models.is_small(auto))
    chosen = models.choose(app.cfg.get("model_path", ""))
    return {
        "now": app.model_folder.name,
        "now_small": models.is_small(app.model_folder),
        "auto": "маленькая" if auto_small else "большая",
        "ram_gb": round(models.ram_gb(), 1),
        "weak": models.weak_pc(),
        "downloading": models.STATE["downloading"],
        "error": models.STATE["error"],
        "pending": chosen is not None and chosen.resolve() != app.model_folder.resolve(),
    }


def _space(app):
    """Что можно удалить, чтобы освободить место на диске."""
    keep = [app.model_folder, models.choose(app.cfg.get("model_path", ""))]
    return models.space_items(keep) + runtime.space_items()


def api_space_free(p, body):
    item = next((i for i in _space(p.app) if i["id"] == str(body.get("id"))), None)
    if item is None:
        raise ValueError("Это уже удалено или сейчас нужно Джарвису")
    path = Path(item["path"]).resolve()
    if HOME.resolve() not in path.parents:
        raise ValueError("Это не в папке Джарвиса")
    shutil.rmtree(path)
    log.info("Освободил место: %s — %d МБ", item["title"], item["size_mb"])
    return {"ok": True, "freed_mb": item["size_mb"]}


def _apps_list(cfg):
    out = []
    for name, spec in cfg.get("apps", {}).items():
        if isinstance(spec, str):
            spec = {"run": spec}
        out.append({"name": name, "run": spec.get("run", ""),
                    "aliases": spec.get("aliases", []),
                    "process": spec.get("process", ""), "title": spec.get("title", "")})
    return out


def _ai_state(cfg):
    out = []
    for item in ai.providers(cfg):
        preset = ai.preset_of(item)
        key = keys.get(item["id"]) if ai.needs_key(item) else ""
        out.append({
            "id": item["id"], "preset": item.get("preset"), "type": item.get("type"),
            "name": item.get("name"), "enabled": item.get("enabled", True),
            "search": bool(item.get("search")), "model": item.get("model", ""),
            "base_url": item.get("base_url", ""), "status": ai.status(item),
            "needs_key": ai.needs_key(item), "has_key": bool(key), "key_hint": keys.hint(key),
            "allow_actions": bool(item.get("allow_actions")),
            "badge": preset.get("badge", ""), "about": preset.get("about", ""),
            "key_url": preset.get("key_url", ""), "edit_url": bool(preset.get("edit_url")),
            "can_install": bool(preset.get("install")),
            "can_login": item.get("type") in ("claude_code", "cli"),
            "experimental": bool(preset.get("experimental")),
        })
    return out


def api_state(p, _body):
    cfg = p.app.cfg
    return {
        "status": {
            "state": p.app.state,
            "paused": p.app.paused.is_set(),
            "game_mode": skills.GAME_MODE["on"],
            "youtube": youtube_bridge.connected(),
            "autostart": STARTUP_LNK.exists(),
            "folder": str(HOME),
            "version": version.VERSION,
            "version_date": version.DATE,
        },
        "changes": [{"version": v, "date": d, "items": items} for v, d, items in version.CHANGES],
        "update": updater.status(cfg),
        "settings": {
            "wake_words": cfg.get("wake_words", []),
            "wake_similarity": cfg.get("wake_similarity", 0.78),
            "command_timeout": cfg.get("command_timeout", 20),
            "follow_up": cfg.get("follow_up", False),
            "greeting": cfg.get("greeting", ""),
            "voice": cfg.get("voice", {}),
            "mic_device": cfg.get("mic_device"),
            "performance": {
                "low_priority": cfg.get("performance", {}).get("low_priority", True),
                "pause_in_fullscreen": cfg.get("performance", {}).get("pause_in_fullscreen", False),
                "skip_silence": cfg.get("performance", {}).get("skip_silence", True),
            },
            "model_path": cfg.get("model_path", ""),
            "models": _models(),
            "model": _model_state(p.app),
            "space": [{k: v for k, v in item.items() if k != "path"} for item in _space(p.app)],
            "runtime": runtime.status(),
        },
        "ai": {
            "providers": _ai_state(cfg),
            "presets": [{k: v for k, v in pr.items() if k not in ("install", "command", "login")}
                        for pr in ai.PRESETS],
            "agent": {"thinking_delay": brain.agent_conf(cfg).get("thinking_delay", 1.5)},
        },
        "apps": _apps_list(cfg),
        "games": sorted(games.installed_names()),
        "learned": skills._load_learned(),
        "modes": cfg.get("modes", []),
        "game_mode": cfg.get("game_mode", {}),
        "memory": {"facts": memory.facts(), "auto": cfg.get("memory", {}).get("auto", True)},
        "city": cfg.get("city", ""),
        "reminders": reminders.pending(),
    }


def api_voices(_p, _body):
    out = []
    try:
        from winsdk.windows.media.speechsynthesis import SpeechSynthesizer

        for v in SpeechSynthesizer.all_voices:
            out.append({"name": v.display_name, "engine": "onecore", "lang": v.language})
    except Exception:
        log.exception("Не получил голоса OneCore")

    def sapi():
        import win32com.client

        names = []
        for token in win32com.client.Dispatch("SAPI.SpVoice").GetVoices():
            names.append(token.GetDescription())
        return names

    try:
        for name in _com(sapi):
            out.append({"name": name, "engine": "sapi", "lang": ""})
    except Exception:
        log.exception("Не получил голоса SAPI")
    return {"voices": out}


def api_mics(_p, _body):
    import sounddevice as sd

    devices, apis = sd.query_devices(), sd.query_hostapis()
    default_in = sd.default.device[0]
    host = devices[default_in]["hostapi"] if default_in is not None and default_in >= 0 else 0
    mics = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and d["hostapi"] == host:
            # «имя, система» — так sounddevice найдёт ровно это устройство
            mics.append({"id": f"{d['name']}, {apis[host]['name']}", "name": d["name"],
                         "default": i == default_in})
    return {"mics": mics}


_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\s+(\w+)\s+(\S+)\s+(.*)$")
_LOG_KINDS = (("Команда:", "cmd"), ("Услышал:", "heard"), ("Говорю:", "say"),
              ("Ответ ", "brain"), ("Агент понял", "brain"), ("Понял «", "brain"),
              ("Запуск Джарвиса", "start"))


def api_log(_p, _body):
    if not LOG_FILE.exists():
        return {"lines": []}
    with LOG_FILE.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 300_000))
        text = f.read().decode("utf-8", errors="replace")
    lines = []
    for raw in text.splitlines():
        m = _LOG_LINE.match(raw)
        if not m:
            continue
        day, clock, level, _name, msg = m.groups()
        kind = "error" if level in ("ERROR", "CRITICAL") else \
            "warn" if level == "WARNING" else \
            next((k for prefix, k in _LOG_KINDS if msg.startswith(prefix)), None)
        if kind:
            lines.append({"day": day, "time": clock, "kind": kind, "text": msg})
    return {"lines": lines[-250:]}


# --------------------------------------------------------------------------
# Что панель меняет
# --------------------------------------------------------------------------

def api_settings(p, body):
    cfg = p.app.cfg
    restart = False
    if "wake_words" in body:
        words = [str(w).strip().lower().replace("ё", "е") for w in body["wake_words"]]
        words = list(dict.fromkeys(w for w in words if w))
        if not words:
            raise ValueError("Нужно хотя бы одно имя для обращения")
        cfg["wake_words"] = words
    if "wake_similarity" in body:
        cfg["wake_similarity"] = round(_clamp(float(body["wake_similarity"]), 0.6, 0.95), 2)
    if "command_timeout" in body:
        cfg["command_timeout"] = int(_clamp(int(body["command_timeout"]), 5, 120))
    if "follow_up" in body:
        cfg["follow_up"] = bool(body["follow_up"])
    if "greeting" in body:
        cfg["greeting"] = str(body["greeting"]).strip()[:120]
    if "mic_device" in body:
        mic = body["mic_device"] or None
        if mic != cfg.get("mic_device"):
            cfg["mic_device"] = mic
            restart = True
    if "voice" in body:
        src, voice = body["voice"], cfg.setdefault("voice", {})
        if "name" in src:
            voice["name"] = str(src["name"])[:100]
        if src.get("engine") in ("auto", "onecore", "sapi"):
            voice["engine"] = src["engine"]
        if "rate" in src:
            voice["rate"] = int(_clamp(int(src["rate"]), -10, 10))
        if "volume" in src:
            voice["volume"] = int(_clamp(int(src["volume"]), 0, 100))
        if "pitch" in src:
            voice["pitch"] = round(_clamp(float(src["pitch"]), 0.5, 1.5), 2)
        p.app.reload_voice()
    if "agent" in body:
        src, agent = body["agent"], cfg.setdefault("agent", {})
        if "thinking_delay" in src:
            agent["thinking_delay"] = round(_clamp(float(src["thinking_delay"]), 0, 5), 1)
    if "city" in body:
        cfg["city"] = str(body["city"]).strip()[:60]
    if "memory_auto" in body:
        cfg.setdefault("memory", {})["auto"] = bool(body["memory_auto"])
    if "performance" in body:
        src, perf_cfg = body["performance"], cfg.setdefault("performance", {})
        if "low_priority" in src:
            perf_cfg["low_priority"] = bool(src["low_priority"])
        if "pause_in_fullscreen" in src:
            perf_cfg["pause_in_fullscreen"] = bool(src["pause_in_fullscreen"])
        if "skip_silence" in src:
            perf_cfg["skip_silence"] = bool(src["skip_silence"])
        p.app.apply_performance()        # приоритет, пауза и тишина применяются сразу
    model_changed = False
    if "model_path" in body:
        model = str(body["model_path"]).strip()   # "" — автоматически, по памяти ПК
        if (not model or model in {m["path"] for m in _models()}) and model != cfg.get("model_path", ""):
            cfg["model_path"] = model
            model_changed = True
    save_config(cfg)
    if model_changed:                    # меняется на ходу, без перезапуска
        p.app.switch_model()
        models.start(p.app)              # «автоматически» на слабом ПК — докачать маленькую
    return {"ok": True, "restart": restart}


# --------------------------------------------------------------------------
# Режимы и игровой режим
# --------------------------------------------------------------------------

def _clean_mode(src, n):
    name = str(src.get("name", "")).strip()[:60]
    if not name:
        raise ValueError(f"У режима №{n} нет названия")
    phrases = [str(x).strip().lower()[:80] for x in src.get("phrases", []) if str(x).strip()]
    steps = []
    for step in src.get("steps", []):
        do = step.get("do")
        if do not in skills.MODE_STEPS:
            continue
        item = {"do": do}
        if step.get("target"):
            item["target"] = str(step["target"]).strip()[:200]
        if do in ("volume", "wait"):
            item["value"] = _clamp(float(step.get("value") or 0), 0, 100 if do == "volume" else 30)
        if do == "power":
            item["value"] = "high" if step.get("value") == "high" else "balanced"
        steps.append(item)
    return {"id": str(src.get("id") or f"mode-{n}")[:40], "name": name,
            "phrases": list(dict.fromkeys(phrases)), "confirm_close": bool(src.get("confirm_close", True)),
            "steps": steps}


def api_modes(p, body):
    modes = [_clean_mode(m, i + 1) for i, m in enumerate(body.get("modes", []))]
    # одна и та же фраза у двух режимов — сработает первый, предупредим
    seen, clash = {}, []
    for m in modes:
        for ph in skills._mode_phrases(m):
            if ph in seen and seen[ph] != m["name"]:
                clash.append(f"«{ph}» — у «{seen[ph]}» и «{m['name']}»")
            seen.setdefault(ph, m["name"])
    p.app.cfg["modes"] = modes
    save_config(p.app.cfg)
    return {"ok": True, "modes": modes, "clash": clash}


def api_modes_run(p, body):
    mode = next((m for m in p.app.cfg.get("modes", []) if m.get("id") == body.get("id")), None)
    if mode is None:
        raise ValueError("Сначала сохраните режим")
    said = _com(lambda: skills.run_mode(mode, p.app.cfg))
    threading.Thread(target=p.app.speak, args=(said,), daemon=True).start()
    return {"ok": True, "said": said}


def api_game_mode(p, body):
    gm = p.app.cfg.setdefault("game_mode", {})
    close = {}
    for item in body.get("close", []):
        name = str(item.get("name", "")).strip()[:60]
        procs = ",".join(x.strip().lower().removesuffix(".exe")
                         for x in str(item.get("process", "")).split(",") if x.strip())
        if name and procs:
            close[name] = procs
    gm["close"] = close
    gm["power_plan"] = bool(body.get("power_plan", True))
    vol = body.get("volume")
    gm["volume"] = None if vol in (None, "") else int(_clamp(int(vol), 0, 100))
    save_config(p.app.cfg)
    return {"ok": True, "game_mode": gm}


def api_windows(_p, _body):
    """Что сейчас открыто — чтобы добавить в игровой режим одним выбором."""
    seen, out = set(), []
    for w in windows.list_windows():
        if w.exe not in seen:
            seen.add(w.exe)
            out.append({"exe": w.exe, "title": w.title[:80]})
    return {"windows": out}


# --------------------------------------------------------------------------
# Память о пользователе
# --------------------------------------------------------------------------

def api_memory_add(_p, body):
    fact = memory.add(str(body.get("text", "")))
    if fact is None:
        raise ValueError("Пусто или такое уже есть")
    return {"ok": True, "facts": memory.facts()}


def api_memory_update(_p, body):
    memory.update(int(body.get("id", 0)), str(body.get("text", "")))
    return {"ok": True, "facts": memory.facts()}


def api_memory_remove(_p, body):
    memory.remove(int(body.get("id", 0)))
    return {"ok": True, "facts": memory.facts()}


def api_reminders_cancel(_p, body):
    reminders.cancel(body.get("id"))
    return {"ok": True, "reminders": reminders.pending()}


# --------------------------------------------------------------------------
# ИИ: подключить, порядок, ключ, проверить, модели, установить, войти
# --------------------------------------------------------------------------

def _provider(p, pid):
    for item in ai.providers(p.app.cfg):
        if item["id"] == pid:
            return item
    raise ValueError("Нет такого подключения")


def api_ai_add(p, body):
    preset = str(body.get("preset", ""))
    if preset not in ai.PRESET:
        raise ValueError("Нет такого варианта")
    item = ai.add(p.app.cfg, preset)
    save_config(p.app.cfg)
    return {"ok": True, "id": item["id"], "providers": _ai_state(p.app.cfg)}


def api_ai_save(p, body):
    """Порядок, включён ли, ищет ли в интернете, модель, адрес. Ключи — отдельно."""
    current = {item["id"]: item for item in ai.providers(p.app.cfg)}
    ordered = []
    for src in body.get("providers", []):
        item = current.pop(str(src.get("id")), None)
        if item is None:
            continue
        if "enabled" in src:
            item["enabled"] = bool(src["enabled"])
        if "search" in src:
            item["search"] = bool(src["search"])
        if "model" in src:
            model = str(src["model"]).strip()
            if model and not re.fullmatch(r"[\w.\-:/@]+", model):
                raise ValueError("Странное имя модели")
            item["model"] = model
        if "base_url" in src and ai.preset_of(item).get("edit_url"):
            url = str(src["base_url"]).strip()
            if url and not re.fullmatch(r"https?://[^\s\"'<>]+", url):
                raise ValueError("Адрес должен начинаться с http:// или https://")
            item["base_url"] = url
        ordered.append(item)
    ordered += current.values()          # кого панель не прислала — в конец
    p.app.cfg["ai"] = ordered
    save_config(p.app.cfg)
    return {"ok": True, "providers": _ai_state(p.app.cfg)}


def api_ai_remove(p, body):
    item = _provider(p, str(body.get("id")))
    ai.providers(p.app.cfg).remove(item)
    keys.put(item["id"], "")
    save_config(p.app.cfg)
    return {"ok": True, "providers": _ai_state(p.app.cfg)}


def api_ai_key(p, body):
    item = _provider(p, str(body.get("id")))
    key = str(body.get("key", "")).strip()
    if key and not re.fullmatch(r"[A-Za-z0-9._\-]{16,300}", key):
        raise ValueError("Не похоже на ключ — проверьте, что скопировали целиком")
    keys.put(item["id"], key)
    return {"ok": True, "providers": _ai_state(p.app.cfg)}


def api_ai_test(p, body):
    item = _provider(p, str(body.get("id")))
    started = time.monotonic()
    text, err = ai.ask(dict(item, search=False), "Ответь одним коротким предложением: связь есть?",
                       "Отвечай по-русски, очень коротко.")
    ms = int((time.monotonic() - started) * 1000)
    if text:
        return {"ok": True, "ms": ms, "text": brain.clean(text)[:200]}
    return {"ok": False, "ms": ms, "text": err or "нет ответа"}


def api_ai_models(p, body):
    item = _provider(p, str(body.get("id")))
    try:
        return {"models": ai.models(item)}
    except Exception as exc:
        log.warning("Список моделей %s: %s", item.get("name"), exc)
        raise ValueError(f"Не получил список моделей: {exc}") from exc


def api_ai_install(p, body):
    ai.install(_provider(p, str(body.get("id"))))
    return {"ok": True}


def api_ai_login(p, body):
    ai.login(_provider(p, str(body.get("id"))))
    return {"ok": True}


def api_apps(p, body):
    apps = {}
    for item in body.get("apps", []):
        name, run = str(item.get("name", "")).strip(), str(item.get("run", "")).strip()
        if not name or not run:
            continue
        spec = {"run": run}
        aliases = [a.strip().lower() for a in item.get("aliases", []) if str(a).strip()]
        if aliases:
            spec["aliases"] = list(dict.fromkeys(aliases))
        for extra in ("process", "title"):
            if str(item.get(extra, "")).strip():
                spec[extra] = str(item[extra]).strip()
        apps[name] = spec
    p.app.cfg["apps"] = apps
    save_config(p.app.cfg)
    return {"ok": True, "count": len(apps)}


def api_launch(_p, body):
    skills.launch(str(body.get("run", "")))
    return {"ok": True}


def api_forget(_p, body):
    skills.forget(str(body.get("phrase", "")))
    return {"ok": True, "learned": skills._load_learned()}


def api_voice_test(p, body):
    text = str(body.get("text") or "Добрый вечер, сэр. Так я звучу сейчас.")[:200]
    threading.Thread(target=p.app.speak, args=(text,), daemon=True).start()
    return {"ok": True}


def api_pause(p, _body):
    p.app.on_pause(p.app.icon, None)
    return {"ok": True, "paused": p.app.paused.is_set()}


def api_restart(p, _body):
    threading.Timer(0.5, _restart, args=(p.app,)).start()
    return {"ok": True}


def _restart(app):
    log.info("Перезапуск из панели")
    if skills.GAME_MODE["on"]:
        skills.game_mode_off(app.cfg)
    vbs = HOME / "start-jarvis.vbs"
    # новый Джарвис стартует через пару секунд, когда этот уже закроется
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & wscript "{vbs}"',
                     creationflags=CREATE_NO_WINDOW)
    try:
        app.listener.stop()
        app.icon.stop()
    except Exception:
        pass
    os._exit(0)


def api_update_check(p, _body):
    updater.check_now()
    return {"ok": True, "update": updater.status(p.app.cfg)}


def api_update_install(p, _body):
    # проверить, скачать и поставить — в рабочем потоке: там голос, а Джарвис
    # потом закроется сам. Ставить нечего — скажет почему
    p.app.run_in_worker(updater.update_now)
    return {"ok": True}


def api_update_rollback(p, _body):
    if not updater.backup_version():
        raise ValueError("Прежней версии нет")
    p.app.run_in_worker(updater.rollback_now)
    return {"ok": True}


def api_update_settings(p, body):
    if "auto" in body:
        p.app.cfg.setdefault("update", {})["auto"] = bool(body["auto"])
        save_config(p.app.cfg)
    if "key" in body:
        key = str(body["key"]).strip()
        if not re.fullmatch(r"(github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,255}", key):
            raise ValueError("Не похоже на ключ GitHub — он начинается с github_pat_")
        keys.put(updater.KEY_NAME, key)
        updater.check_now()
    return {"ok": True, "update": updater.status(p.app.cfg)}


def api_autostart(_p, body):
    on = bool(body.get("on"))
    if on and not STARTUP_LNK.exists():
        def make():
            import win32com.client

            link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(STARTUP_LNK))
            link.TargetPath = "wscript.exe"
            link.Arguments = f'"{HOME / "start-jarvis.vbs"}"'
            link.WorkingDirectory = str(HOME)
            link.Description = "Голосовой помощник Джарвис"
            link.Save()
        _com(make)
    elif not on and STARTUP_LNK.exists():
        STARTUP_LNK.unlink()
    return {"ok": True, "autostart": STARTUP_LNK.exists()}


def api_open(_p, body):
    what = body.get("what")
    target = {"folder": HOME, "log": LOG_FILE, "extension": HOME / "chrome-extension"}.get(what)
    if target is None or not target.exists():
        raise ValueError("Нечего открыть")
    os.startfile(str(target))
    return {"ok": True}


GET_ROUTES = {
    "/api/state": api_state,
    "/api/voices": api_voices,
    "/api/mics": api_mics,
    "/api/log": api_log,
    "/api/windows": api_windows,
}
POST_ROUTES = {
    "/api/settings": api_settings,
    "/api/apps": api_apps,
    "/api/apps/launch": api_launch,
    "/api/learned/forget": api_forget,
    "/api/ai/add": api_ai_add,
    "/api/ai/save": api_ai_save,
    "/api/ai/remove": api_ai_remove,
    "/api/ai/key": api_ai_key,
    "/api/ai/test": api_ai_test,
    "/api/ai/models": api_ai_models,
    "/api/ai/install": api_ai_install,
    "/api/ai/login": api_ai_login,
    "/api/modes": api_modes,
    "/api/modes/run": api_modes_run,
    "/api/game_mode": api_game_mode,
    "/api/memory/add": api_memory_add,
    "/api/memory/update": api_memory_update,
    "/api/memory/remove": api_memory_remove,
    "/api/reminders/cancel": api_reminders_cancel,
    "/api/voice/test": api_voice_test,
    "/api/pause": api_pause,
    "/api/restart": api_restart,
    "/api/autostart": api_autostart,
    "/api/update/check": api_update_check,
    "/api/update/install": api_update_install,
    "/api/update/rollback": api_update_rollback,
    "/api/update/settings": api_update_settings,
    "/api/open": api_open,
    "/api/space/free": api_space_free,
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "Jarvis"

    def log_message(self, fmt, *args):
        pass                        # каждый запрос в журнал не пишем

    @property
    def panel(self):
        return self.server.panel

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self, api):
        if self.headers.get("Host", "") not in self.panel.hosts:
            return False
        if api:
            return hmac.compare_digest(self.headers.get("X-Jarvis-Token", ""), self.panel.token)
        return True

    def _route(self, routes, body):
        path = self.path.split("?")[0]
        if not self._allowed(path.startswith("/api/")):
            return self._send(403, {"error": "нет доступа"})
        if path in ("/", "/index.html") and routes is GET_ROUTES:
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path in ("/logo.png", "/favicon.png") and routes is GET_ROUTES:
            image = PAGE.parent / path[1:]  # логотип и значок вкладки — без токена
            if image.is_file():
                return self._send(200, image.read_bytes(), "image/png")
        func = routes.get(path)
        if func is None:
            return self._send(404, {"error": "нет такого"})
        try:
            self._send(200, func(self.panel, body))
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            log.exception("Панель: %s", path)
            self._send(500, {"error": f"не получилось: {exc}"})

    def do_GET(self):
        self._route(GET_ROUTES, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self._send(413, {"error": "слишком много"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "не JSON"})
        self._route(POST_ROUTES, body if isinstance(body, dict) else {})
