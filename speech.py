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
import winsound

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)  # Vosk иначе сыпет отладку в stderr

log = logging.getLogger("jarvis.speech")

SAMPLE_RATE = 16000
BLOCK_FRAMES = 4000  # четверть секунды — компромисс отзывчивости и нагрузки


class Listener:
    """Держит микрофон открытым и отдаёт распознанные фразы."""

    def __init__(self, model_path, device=None):
        log.info("Загружаю модель распознавания: %s", model_path)
        self.model = Model(model_path)
        self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self.rec.SetWords(False)
        self.device = device
        self._audio = queue.Queue()
        self._stream = None
        self.muted = threading.Event()  # выставлен — звук выбрасываем, не распознаём
        # выставлен — распознавание на паузе (напр. полноэкранная игра): звук
        # отбрасываем, не гоняем через Vosk — почти не грузим процессор
        self.suspended = threading.Event()

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        self._audio.put(bytes(indata))

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

    def reset(self):
        """Забыть недослушанное — после собственной речи или паузы."""
        self.rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        self.rec.SetWords(False)
        while not self._audio.empty():
            try:
                self._audio.get_nowait()
            except queue.Empty:
                break

    def phrases(self):
        """Генератор законченных фраз. Пустые строки пропускаем."""
        while True:
            data = self._audio.get()
            if self.muted.is_set() or self.suspended.is_set():
                continue
            if self.rec.AcceptWaveform(data):
                text = json.loads(self.rec.Result()).get("text", "").strip()
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
