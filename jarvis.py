# -*- coding: utf-8 -*-
"""Джарвис — голосовой помощник в системном трее.

Слушает микрофон офлайн, просыпается на слово «Джарвис», выполняет команду
и отвечает голосом. Запускается через start-jarvis.vbs (без окна консоли).
"""

import json
import logging
import os
import re
import threading
import time
import winsound
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import ai
import brain
import games
import panel
import perf
import reminders
import skills
import updater
import version
import windows
import youtube_bridge
from speech import Listener, Voice

HOME = Path(__file__).resolve().parent
LOG_FILE = HOME / "logs" / "jarvis.log"

STOP_WORDS = ("отбой", "хватит", "спасибо", "отмена", "все", "всё")
YES_WORDS = ("да", "подтверждаю", "давай", "ага", "конечно", "выполняй")
NO_WORDS = ("нет", "не надо", "отмена", "стой", "отбой")
NAME_WAIT = 8      # сек: после одного «Джарвис» ждём команду без имени


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return logging.getLogger("jarvis")


log = setup_logging()


def normalize(text):
    return text.lower().replace("ё", "е").strip()


def find_model(configured=""):
    """Папка модели Vosk: из настроек, иначе первая vosk-model-* рядом со скриптом.
    Так конфиг не привязан к конкретной машине."""
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = HOME / path
        if path.is_dir():
            return _ascii_path(str(path))
        log.warning("Модель по пути из настроек не найдена: %s", configured)
    for candidate in sorted(HOME.glob("vosk-model-*")):
        if candidate.is_dir():
            return _ascii_path(str(candidate))
    raise SystemExit("Не найдена модель распознавания — запусти install.ps1")


def _ascii_path(path):
    """Vosk не открывает модель по пути с русскими буквами («C:\\Users\\Алексей\\…»).
    Короткое имя DOS (C:\\Users\\5D3B~1\\…) — латиницей, им и пользуемся."""
    if path.isascii():
        return path
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)) and buf.value.isascii():
        return buf.value
    log.warning("В пути к модели русские буквы, а короткого имени нет: %s", path)
    return path


class Jarvis:
    # цвета значка в трее по состоянию
    COLORS = {
        "idle": (110, 118, 129),      # серый — жду обращения
        "listening": (46, 160, 67),   # зелёный — слушаю команду
        "working": (47, 129, 247),    # синий — выполняю
        "paused": (150, 60, 60),      # тёмно-красный — на паузе
    }
    TITLES = {
        "idle": "Джарвис — жду обращения",
        "listening": "Джарвис слушает команду",
        "working": "Джарвис выполняет",
        "paused": "Джарвис на паузе",
    }

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = "idle"
        self.mode = "idle"          # idle | command
        self.last_phrase_at = 0.0
        self.pending = None         # действие, ждущее голосового «да»
        self.paused = threading.Event()
        self.busy = threading.Event()
        self.speak_lock = threading.Lock()
        self.icon = None

        self.voice = self._make_voice()
        self.listener = Listener(find_model(cfg.get("model_path", "")),
                                 cfg.get("mic_device"))

    def _make_voice(self):
        voice_cfg = self.cfg.get("voice", {})
        return Voice(
            prefer=voice_cfg.get("name", "Pavel"),
            rate=voice_cfg.get("rate", 0),
            volume=voice_cfg.get("volume", 100),
            pitch=voice_cfg.get("pitch", 1.0),
            engine=voice_cfg.get("engine", "auto"),
        )

    def reload_voice(self):
        """Голос поменяли в панели — подхватить без перезапуска."""
        with self.speak_lock:
            old, self.voice = self.voice, self._make_voice()
        old.stop()

    # ---------------------------------------------------------------- речь

    def speak(self, text):
        """Сказать вслух, не слушая при этом самого себя."""
        if not text:
            return
        with self.speak_lock:
            self.listener.muted.set()
            self.voice.say(text)
            self.voice.wait_done(timeout=120)
            time.sleep(0.2)           # хвост звука из динамиков
            self.listener.reset()
            self.listener.muted.clear()

    def beep(self):
        try:
            winsound.Beep(900, 80)
        except RuntimeError:
            pass

    # -------------------------------------------------- обращение по имени

    def is_wake(self, word):
        w = normalize(word)
        threshold = self.cfg.get("wake_similarity", 0.78)
        for key in self.cfg["wake_words"]:
            if w == key:
                return True
            if len(w) >= 4 and SequenceMatcher(None, w, key).ratio() >= threshold:
                return True
        return False

    def split_wake(self, text):
        """Отделить обращение от самой команды."""
        words = normalize(text).split()
        for i, word in enumerate(words):
            if self.is_wake(word):
                after = " ".join(words[i + 1:]).strip()
                # «переключи песню, Джарвис» — имя в конце, команда перед ним.
                # Длинное перед именем — это разговор рядом, а не команда
                if not after and 0 < i <= 6:
                    after = " ".join(words[:i])
                return after, True
        return normalize(text), False

    # --------------------------------------------------------------- трей

    def set_state(self, state):
        self.state = state
        if self.icon is not None:
            self.icon.icon = self.make_icon(state)
            self.icon.title = self.TITLES[state]

    # ------------------------------------------------------------ действия

    def run_in_worker(self, func):
        """Выполнить что-то тяжёлое, не затыкая микрофон."""
        threading.Thread(target=self._worker, args=(func,),
                         daemon=True, name="worker").start()

    def _worker(self, func):
        import pythoncom

        pythoncom.CoInitialize()      # pycaw и SAPI требуют COM в своём потоке
        self.busy.set()
        self.set_state("working")
        try:
            said = func()
            if said:
                self.speak(said)
        except Exception:
            log.exception("Поручение упало")
            self.speak("Не получилось, сэр. Подробности в логе")
        finally:
            pythoncom.CoUninitialize()
            self.busy.clear()
            self.touch()
            if self.pending is not None:
                self.mode = "command"     # спросили «да или нет» — ответ ждём без имени
            self.set_state("listening" if self.mode == "command" else "idle")

    def dispatch(self, command):
        """Разобрать команду: сначала локально, потом — агентом."""
        log.info("Команда: %s", command)

        def respond():
            result = skills.handle(command, self.cfg)
            if result is not None:
                return self.apply(result)

            # локально не разобрали — отдаём агенту. В игровом режиме нет:
            # голосовой чат рядом, а агент — это десяток секунд и лишний шум
            if not brain.available(self.cfg) or skills.GAME_MODE["on"]:
                return "Не понял команду, сэр"

            # обрывки и ослышки («чем», «да рыть») агенту не шлём — это
            # десяток секунд впустую ради ответа «не расслышал»
            if len(command.split()) < brain.agent_conf(self.cfg).get("min_words", 2):
                log.info("Слишком коротко для агента — пропускаю")
                return "Не расслышал, сэр"

            # агент знает, что Джарвис умеет сам, и узнаёт ослышанные команды
            context = skills.command_hint(self.cfg)

            if brain.needs_confirmation(self.cfg):
                self.pending = lambda: self.after_agent(
                    command, brain.ask(command, self.cfg, context))
                return f"Передать агенту: {command}?"

            # «секунду» — только если агент задумался: Gemini отвечает быстрее,
            # чем эта фраза звучит
            answer = {}
            agent = threading.Thread(
                target=lambda: answer.setdefault(
                    "text", brain.ask(command, self.cfg, context)),
                daemon=True, name="agent")
            agent.start()
            agent_cfg = brain.agent_conf(self.cfg)
            agent.join(agent_cfg.get("thinking_delay", 1.5))
            if agent.is_alive():
                self.speak(agent_cfg.get("thinking_phrase", "Секунду"))
                agent.join()
            return self.after_agent(command, answer.get("text"))

        def job():
            said = respond()
            # и локальные команды, и ответы ИИ — в недавний разговор, чтобы
            # понимать продолжения («какая погода?» → «а завтра?»)
            brain.note(command, said)
            return said

        self.run_in_worker(job)

    def after_agent(self, command, text):
        """Ответ агента. Если он узнал в фразе ослышанную команду («КОМАНДА:
        сверни все окна») — выполняем её сами и запоминаем ослышку, чтобы в
        следующий раз обойтись без агента. Вызывается в рабочем потоке — там
        поднят COM, который нужен окнам и звуку."""
        text = text or "Не получилось, сэр"
        m = re.search(r"КОМАНДА\s*:\s*(.+)", text, re.I)
        if not m:
            return text
        cmd = m.group(1).strip(" .!«»\"'")
        log.info("Агент понял «%s» как «%s»", command, cmd)
        result = skills.handle(cmd, self.cfg)
        if result is None:
            return "Не понял команду, сэр"
        skills.remember(command, cmd)
        return self.apply(result)

    def apply(self, result):
        """Выполнить локальную команду: спросить, сделать или просто сказать."""
        if result.ask:
            self.pending = result.action
            return result.ask
        if result.action:
            result.action()
        return result.say

    def touch(self):
        self.last_phrase_at = time.monotonic()

    def follow_up(self):
        """Слушать ли после команды следующие фразы без имени. По умолчанию
        нет: иначе разговор рядом («зачем», «а почему…») становится командами."""
        return self.cfg.get("follow_up", False)

    def command_taken(self):
        """Команда принята — дальше снова только по имени (если не включено
        продолжение без имени)."""
        if not self.follow_up():
            self.mode = "idle"
            if not self.busy.is_set():
                self.set_state("idle")

    def confirm(self, text):
        """Ответ на вопрос об опасном действии. True — фраза съедена."""
        if self.pending is None:
            return False
        if any(text == w or text.startswith(w + " ") for w in YES_WORDS):
            action, self.pending = self.pending, None
            self.command_taken()
            self.run_in_worker(lambda: action() or "Выполнено, сэр")
            return True
        if any(text.startswith(w) for w in NO_WORDS):
            self.pending = None
            self.command_taken()
            self.speak("Отменил, сэр")
            return True
        self.pending = None           # любой другой ответ — тоже отказ
        return False

    # ------------------------------------------------------- главный цикл

    def watchdog(self):
        """Вернуть в режим ожидания, если после обращения давно тихо."""
        timeout = self.cfg.get("command_timeout", 20)
        games_every = self.cfg.get("games_check_minutes", 10) * 60
        games_at = time.monotonic()
        while True:
            time.sleep(1)
            self._update_fullscreen_pause()
            if games_every and time.monotonic() - games_at > games_every:
                games_at = time.monotonic()
                if not self.busy.is_set() and not self.paused.is_set():
                    try:
                        self.check_games()
                    except Exception:
                        log.exception("Проверка игр упала")
            # в игровом режиме после команды быстро перестаём слушать без
            # обращения по имени — иначе реплики из голосового чата станут командами
            if self.pending is not None:
                limit = timeout
            elif skills.GAME_MODE["on"]:
                limit = self.cfg.get("game_mode", {}).get("command_timeout", 5)
            elif not self.follow_up():
                limit = NAME_WAIT     # сказали только «Джарвис» — ждём саму команду
            else:
                limit = timeout
            if self.mode == "command" and not self.busy.is_set():
                if time.monotonic() - self.last_phrase_at > limit:
                    self.mode = "idle"
                    self.pending = None
                    self.set_state("idle")
                    log.info("Тишина — возвращаюсь в ожидание")

    def apply_performance(self):
        """Применить настройки производительности (вызывается и из панели)."""
        perf.apply(self.cfg)
        if not self.cfg.get("performance", {}).get("pause_in_fullscreen", False):
            if self.listener.suspended.is_set():
                self.listener.suspended.clear()
                self.listener.reset()

    def _update_fullscreen_pause(self):
        """Если включено — пауза распознавания, пока на весь экран игра/видео.
        Экономит процессор на слабом ПК ценой голоса внутри полноэкранной игры."""
        if not self.cfg.get("performance", {}).get("pause_in_fullscreen", False):
            return
        if self.paused.is_set() or self.busy.is_set():
            return
        try:
            full = windows.fullscreen_foreground()
        except Exception:
            return
        if full and not self.listener.suspended.is_set():
            self.listener.suspended.set()
            log.info("Полноэкранная игра — распознавание на паузе (экономия CPU)")
        elif not full and self.listener.suspended.is_set():
            self.listener.suspended.clear()
            self.listener.reset()
            log.info("Полный экран закрыт — распознавание снова включено")

    def check_games(self, announce=True):
        """Поискать новые игры. При первом знакомстве не перечисляем всё
        подряд — только то, что появилось с прошлого раза."""
        first_time = not games.CACHE.exists()
        new = games.refresh(self.cfg)
        if new and announce and not first_time:
            names = ", ".join(new)
            phrase = (f"Сэр, вижу новую игру: {names}" if len(new) == 1
                      else f"Сэр, появились новые игры: {names}")
            # говорим из рабочего потока — там поднят COM для синтезатора
            self.run_in_worker(lambda: phrase)
        return new

    def loop(self):
        self.listener.start()
        skills.index_programs()
        self.set_state("idle")
        # только что обновились — сказать об этом (и отметить для update.py,
        # что новая версия поднялась) вместо обычного приветствия
        self.speak(updater.after_start() or self.cfg.get("greeting", "Джарвис на связи"))
        # таймеры и напоминания говорят из рабочего потока — там поднят COM для голоса
        reminders.start(lambda text: self.run_in_worker(lambda: (self.beep(), text)[1]), self.cfg)
        self.check_games()

        for phrase in self.listener.phrases():
            if self.paused.is_set():
                continue
            text, woke = self.split_wake(phrase)
            # в лог — только то, что сказано Джарвису. Разговоры рядом
            # (звонки клиентам, пароли) на диск не попадают
            if woke or self.mode == "command" or self.cfg.get("log_all_speech"):
                log.info("Услышал: %s", phrase)

            if self.mode == "idle":
                if not woke:
                    continue                  # разговор не со мной
                self.mode = "command"
                self.touch()
                if not text:
                    self.set_state("listening")
                    self.beep()
                    continue
            else:
                # режим команды: после одного «Джарвис», в ожидании «да/нет»
                # или с включённым продолжением — имя не нужно
                self.touch()
                if not text:
                    if woke:
                        self.beep()           # «Джарвис?» — да, слушаю
                    continue

            if text in STOP_WORDS:
                self.mode = "idle"
                self.pending = None
                self.set_state("idle")
                self.speak("Отбой, сэр")
                continue
            answering = self.pending is not None
            if self.confirm(text):
                continue
            if answering and not woke and not self.follow_up():
                # ждали «да» или «нет», а это разговор рядом — считаем отказом
                self.command_taken()
                continue
            self.command_taken()              # до dispatch: вопрос «да/нет» вернёт режим
            self.dispatch(text)

    # ------------------------------------------------------- меню в трее

    def make_icon(self, state):
        color = self.COLORS[state]
        # логотип грузим один раз; нет файла — рисуем прежний кружок
        logo = getattr(self, "_logo", None)
        if logo is None:
            try:
                logo = Image.open(HOME / "panel" / "logo.png").convert("RGBA") \
                    .resize((64, 64), Image.LANCZOS)
            except OSError:
                logo = False
            self._logo = logo
        if logo is not False:
            img = logo.copy()
            d = ImageDraw.Draw(img)
            # точка состояния справа внизу: тёмный край и белое кольцо — видна на любой панели
            d.ellipse((41, 41, 63, 63), fill=(25, 25, 25, 255))
            d.ellipse((42, 42, 62, 62), fill=(255, 255, 255, 255))
            d.ellipse((44, 44, 60, 60), fill=color)
            return img
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=color)
        d.ellipse((20, 20, 44, 44), fill=(255, 255, 255, 235))
        d.ellipse((27, 27, 37, 37), fill=color)
        return img

    def on_pause(self, icon, item):
        if self.paused.is_set():
            self.paused.clear()
            self.listener.reset()
            self.set_state("idle")
        else:
            self.paused.set()
            self.mode = "idle"
            self.set_state("paused")

    def on_log(self, icon, item):
        os.startfile(str(LOG_FILE))

    def on_folder(self, icon, item):
        os.startfile(str(HOME))

    def on_reindex(self, icon, item):
        count = len(skills.index_programs())
        self.check_games(announce=False)
        self.speak(f"Перечитал меню Пуск, нашёл {count} программ "
                   f"и {len(games.installed_names())} игр")

    def on_quit(self, icon, item):
        log.info("Выход по команде из трея")
        if skills.GAME_MODE["on"]:
            skills.game_mode_off(self.cfg)   # вернуть схему питания
        self.listener.stop()
        icon.stop()
        os._exit(0)

    def run(self):
        menu = pystray.Menu(
            # default — открывается и обычным щелчком по значку
            pystray.MenuItem("Панель", lambda icon, item: panel.open_window(), default=True),
            pystray.MenuItem(
                lambda item: "Продолжить" if self.paused.is_set() else "Пауза",
                self.on_pause),
            pystray.MenuItem("Перечитать список программ", self.on_reindex),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть лог", self.on_log),
            pystray.MenuItem("Папка Джарвиса", self.on_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Версия {version.VERSION}", lambda icon, item: None, enabled=False),
            pystray.MenuItem("Выход", self.on_quit),
        )
        self.icon = pystray.Icon("jarvis", self.make_icon("idle"), "Джарвис", menu)
        # расширение Chrome «Джарвис для ютуба» подключается сюда
        youtube_bridge.start(self.cfg.get("youtube_bridge_port", youtube_bridge.PORT))
        try:
            panel.start(self)
        except Exception:
            log.exception("Панель не запустилась")
        updater.start(self)                 # новые версии с GitHub (если есть ключ)
        threading.Thread(target=self.loop, daemon=True, name="listen").start()
        threading.Thread(target=self.watchdog, daemon=True, name="watchdog").start()
        self.icon.run()


def main():
    cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    log.info("=" * 60)
    log.info("Запуск Джарвиса %s от %s", version.VERSION, version.DATE)
    perf.apply(cfg)                     # пониженный приоритет — не мешать играм/работе
    if ai.migrate(cfg):                 # старые разделы "gemini"/"claude" → "ai"
        panel.save_config(cfg)
    try:
        Jarvis(cfg).run()
    except Exception:
        log.exception("Джарвис упал на старте")
        raise


if __name__ == "__main__":
    main()
