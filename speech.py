# -*- coding: utf-8 -*-
"""Слух и голос Джарвиса.

Listener — постоянное распознавание речи офлайн через Vosk.
Voice    — озвучка системным синтезатором: OneCore (мужской Pavel) или SAPI.
"""

import asyncio
import json
import logging
import queue
import threading
import time
import winsound
from collections import deque

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)  # Vosk иначе сыпет отладку в stderr

try:
    import audioop                      # в 3.12 есть (быстро, на C); в 3.13 убран

    def _level(block):
        """Громкость блока: самая громкая из четырёх долей по 1/16 с — так
        короткий слог («Дж…») заметен, даже если вокруг него тишина."""
        step = len(block) // 4 & ~1
        return max(audioop.rms(block[i:i + step], 2) for i in range(0, len(block) - step + 1, step))
except ImportError:                     # pragma: no cover — запас на новый Python
    from array import array

    def _level(block):
        a = array("h", block)
        step = max(1, len(a) // 4)
        parts = [a[i:i + step] for i in range(0, len(a), step)]
        return int(max((sum(x * x for x in p) / len(p)) ** 0.5 for p in parts if p))

log = logging.getLogger("jarvis.speech")

SAMPLE_RATE = 16000
BLOCK_FRAMES = 4000  # четверть секунды — компромисс отзывчивости и нагрузки

# Пропуск тишины. Vosk разбирает звук одинаково дорого, что речь, что тихая
# комната, — а комната тихая почти всегда. Пока звук не громче фона, Vosk не
# зовём; как только громче — отдаём ему и запас до этого момента, чтобы не
# потерять начало «Джарвис».
GATE_PREROLL = 3      # блоков (0,75 с) до начала звука отдаём Vosk вместе с ним
GATE_HANG = 6         # блоков (1,5 с) тишины после звука ещё слушаем — паузы между словами
GATE_RATIO = 2.0      # во сколько раз громче фона — уже звук (+6 дБ)
GATE_MIN = 40         # нижний порог: у микрофонов с шумодавом фон бывает почти нулевой
# фон — самый тихий блок за последние 8 с: включился вентилятор (ноутбук в
# игре) — через 8 с это уже фон и Vosk снова отдыхает; а в речи всегда есть
# паузы, и фон она не поднимает
GATE_FLOOR_BLOCKS = 32
GATE_REPORT = 2400    # раз в столько блоков (10 мин) — в журнал, сколько работал Vosk
# Отставание: в очереди копится звук, который Vosk ещё не разобрал (процессор
# занят игрой, а у нас пониженный приоритет). 8 блоков = 2 с — команда уже
# заметно запаздывает; предупреждаем не чаще раза в минуту
LAG_WARN = 8
LAG_WARN_EVERY = 60


class Listener:
    """Держит микрофон открытым и отдаёт распознанные фразы."""

    def __init__(self, model_path, device=None, skip_silence=True):
        log.info("Загружаю модель распознавания: %s", model_path)
        self.model_path = model_path
        self.model = Model(model_path)
        self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self.rec.SetWords(False)
        self.device = device
        self.skip_silence = skip_silence  # меняется на ходу из панели
        self._next_model = None           # (путь, Model) — подменить в потоке распознавания
        self._audio = queue.Queue()
        self._stream = None
        self.muted = threading.Event()  # выставлен — звук выбрасываем, не распознаём
        # выставлен — распознавание на паузе (напр. полноэкранная игра): звук
        # отбрасываем, не гоняем через Vosk — почти не грузим процессор
        self.suspended = threading.Event()
        # для журнала и панели; max_lag — в блоках, overflow — сколько раз
        # звуковая карта не дождалась нас и звук потерялся
        self.stats = {"blocks": 0, "decoded": 0, "max_lag": 0, "overflow": 0}
        self._lag_warned = 0.0

    def _callback(self, indata, frames, time_info, status):
        if status:
            if status.input_overflow:
                self.stats["overflow"] += 1
            log.debug("audio status: %s", status)
        self._audio.put(bytes(indata))

    def _check_lag(self):
        lag = self._audio.qsize()
        if lag > self.stats["max_lag"]:
            self.stats["max_lag"] = lag
        if lag >= LAG_WARN and time.monotonic() - self._lag_warned > LAG_WARN_EVERY:
            self._lag_warned = time.monotonic()
            log.warning("Распознавание отстаёт от звука на %.1f с — процессор занят",
                        lag * BLOCK_FRAMES / SAMPLE_RATE)

    def start(self):
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            device=self.device,
            dtype="int16",
            channels=1,
            callback=self._callback,
        )
        self._stream.start()
        name = sd.query_devices(self.device or sd.default.device[0])["name"]
        log.info("Микрофон открыт: %s", name)
        return name

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._audio.put(None)           # отпустить phrases()

    def reset(self):
        """Забыть недослушанное — после собственной речи или паузы."""
        self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self.rec.SetWords(False)
        while not self._audio.empty():
            try:
                self._audio.get_nowait()
            except queue.Empty:
                break

    def swap_model(self, model_path):
        """Сменить модель на ходу, без перезапуска: грузим здесь (в вызывающем
        потоке), а подменяем в потоке распознавания — там же, где Vosk работает.
        Прежняя модель освобождается, как только её отпустит распознаватель."""
        log.info("Загружаю модель распознавания: %s", model_path)
        self._next_model = (model_path, Model(model_path))

    def _accept(self, data):
        if self.rec.AcceptWaveform(data):
            return json.loads(self.rec.Result()).get("text", "").strip()
        return ""

    def _final(self):
        """Звук кончился — забрать недоговорённое; распознаватель готов к новой фразе."""
        return json.loads(self.rec.FinalResult()).get("text", "").strip()

    def _report(self):
        st = self.stats
        if st["blocks"] >= GATE_REPORT:
            extra = ""
            if st["max_lag"] > 1:
                extra += ", отставало до %.1f с" % (st["max_lag"] * BLOCK_FRAMES / SAMPLE_RATE)
            if st["overflow"]:
                extra += ", звук терялся %d раз" % st["overflow"]
            log.info("Слух: распознавание работало %d%% времени (остальное — тишина)%s",
                     round(100 * st["decoded"] / st["blocks"]), extra)
            st["blocks"] = st["decoded"] = st["max_lag"] = st["overflow"] = 0

    def phrases(self):
        """Генератор законченных фраз. Пустые строки пропускаем."""
        pre = deque(maxlen=GATE_PREROLL)    # последние тихие блоки — запас перед звуком
        levels = deque(maxlen=GATE_FLOOR_BLOCKS)  # громкость последних блоков → фон
        is_open, quiet = False, 0
        while True:
            data = self._audio.get()
            if data is None:                # stop() или конец записи в проверках
                return
            self._check_lag()
            if self._next_model is not None:
                (self.model_path, self.model), self._next_model = self._next_model, None
                self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
                self.rec.SetWords(False)
                pre.clear()
                is_open = False
                log.info("Модель распознавания сменена: %s", self.model_path)
            if self.muted.is_set() or self.suspended.is_set():
                pre.clear()
                is_open = False             # после своей речи reset() всё равно начнёт заново
                continue
            self.stats["blocks"] += 1
            self._report()

            if not self.skip_silence:
                self.stats["decoded"] += 1
                text = self._accept(data)
                if text:
                    yield text
                continue

            level = _level(data)
            levels.append(level)
            loud = level > max(min(levels) * GATE_RATIO, GATE_MIN)
            if not is_open:
                if not loud:
                    pre.append(data)
                    continue
                is_open, quiet = True, 0
                blocks = [*pre, data]
                pre.clear()
                log.debug("Звук: %d при фоне %d — слушаю", level, min(levels))
            else:
                quiet = 0 if loud else quiet + 1
                blocks = [data]
            for block in blocks:
                self.stats["decoded"] += 1
                text = self._accept(block)
                if text:
                    yield text
            if quiet >= GATE_HANG:
                is_open = False
                log.debug("Тишина (фон %d) — не слушаю", min(levels))
                text = self._final()
                if text:
                    yield text

    def partial(self):
        try:
            return json.loads(self.rec.PartialResult()).get("partial", "")
        except Exception:
            return ""


class _OneCore:
    """Системный синтезатор Windows. Через него доступен мужской русский Pavel,
    которого SAPI в своём списке не показывает."""

    def __init__(self, prefer, rate, volume, pitch):
        from winsdk.windows.media.speechsynthesis import SpeechSynthesizer

        self._synth = SpeechSynthesizer()
        self.name = None
        for voice in SpeechSynthesizer.all_voices:
            if prefer.lower() in voice.display_name.lower():
                self._synth.voice = voice
                self.name = voice.display_name
                break
        if self.name is None:
            raise LookupError(f"среди системных голосов нет «{prefer}»")
        opts = self._synth.options
        # в настройках скорость по-сапишному, от −10 до 10; тут это множитель
        opts.speaking_rate = max(0.5, min(6.0, 1 + rate * 0.06))
        opts.audio_volume = max(0.0, min(1.0, volume / 100))
        opts.audio_pitch = max(0.0, min(2.0, pitch))

    def speak(self, text):
        winsound.PlaySound(asyncio.run(self._render(text)), winsound.SND_MEMORY)

    async def _render(self, text):
        from winsdk.windows.storage.streams import DataReader

        stream = await self._synth.synthesize_text_to_stream_async(text)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(stream.size)
        return bytes(reader.read_buffer(stream.size))


class _Sapi:
    """Классический SAPI: Irina и прочие голоса из «Панели управления»."""

    def __init__(self, prefer, rate, volume, pitch):
        import win32com.client

        self._engine = win32com.client.Dispatch("SAPI.SpVoice")
        self.name = None
        for token in self._engine.GetVoices():
            desc = token.GetDescription()
            if prefer.lower() in desc.lower():
                self._engine.Voice = token
                self.name = desc
                break
        self._engine.Rate = rate
        self._engine.Volume = volume
        self.name = self.name or "системный по умолчанию"

    def speak(self, text):
        self._engine.Speak(text)


class Voice:
    """Озвучка в отдельном потоке, чтобы не тормозить распознавание."""

    def __init__(self, prefer="Pavel", rate=0, volume=100, pitch=1.0, engine="auto"):
        self.prefer = prefer
        self.rate = rate
        self.volume = volume
        self.pitch = pitch
        self.engine = engine
        self.speaking = threading.Event()
        self._q = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="voice")
        self._thread.start()
        self._ready.wait(timeout=10)

    def _make_engine(self):
        """Поднять движок. «auto» — сначала системный, потом SAPI: голос из
        настроек может жить в любом из них, а на чужой машине — только в SAPI."""
        order = {"auto": (_OneCore, _Sapi), "onecore": (_OneCore,), "sapi": (_Sapi,)}
        for cls in order.get(self.engine, order["auto"]):
            try:
                engine = cls(self.prefer, self.rate, self.volume, self.pitch)
                log.info("Голос: %s (%s)", engine.name, cls.__name__.lstrip("_"))
                return engine
            except Exception as exc:
                log.warning("%s не подошёл: %s", cls.__name__.lstrip("_"), exc)
        raise RuntimeError("ни один синтезатор не поднялся")

    def _loop(self):
        import pythoncom

        pythoncom.CoInitialize()
        try:
            engine = self._make_engine()
            self._ready.set()

            while True:
                text = self._q.get()
                if text is None:
                    break
                self.speaking.set()
                try:
                    engine.speak(text)
                except Exception:
                    log.exception("Не смог озвучить")
                finally:
                    self.speaking.clear()
        except Exception:
            log.exception("Голосовой движок не поднялся")
            self._ready.set()

    def stop(self):
        """Отпустить поток озвучки — когда голос заменили новым."""
        self._q.put(None)

    def say(self, text):
        if text:
            log.info("Говорю: %s", text)
            self._q.put(text)

    def wait_done(self, timeout=None):
        """Дождаться, пока очередь опустеет и рот закроется."""
        self._q.join() if False else None
        deadline = threading.Event()
        import time

        start = time.time()
        while (not self._q.empty()) or self.speaking.is_set():
            if timeout is not None and time.time() - start > timeout:
                return False
            deadline.wait(0.05)
        return True
