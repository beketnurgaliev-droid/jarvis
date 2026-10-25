// Связь с Джарвисом. Он держит WebSocket-сервер на 127.0.0.1:8765 и шлёт
// команды, а мы выполняем их прямо в плеере ютуба.

const JARVIS = "ws://127.0.0.1:8765/";
let ws = null;

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  try {
    ws = new WebSocket(JARVIS);
  } catch (e) {
    ws = null;
    return;
  }
  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    let reply;
    try {
      reply = await handle(msg);
    } catch (e) {
      reply = { error: String(e) };
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ id: msg.id, ...reply }));
    }
  };
  ws.onclose = () => {
    ws = null;
    setTimeout(connect, 5000);
  };
  // называем свою версию: если на диске новее, Джарвис попросит перезагрузиться
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "hello", version: chrome.runtime.getManifest().version }));
  };
}

// Джарвис не запущен — фон расширения засыпает; будильник раз в полминуты
// будит его и пробует подключиться снова
chrome.alarms.create("jarvis-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

async function handle(msg) {
  if (msg.type === "ping") {
    return { type: "pong" };
  }
  if (msg.type === "reload") {
    setTimeout(() => chrome.runtime.reload(), 200);
    return { ok: true };
  }
  if (msg.type === "open") {
    return openVideo(msg.url);
  }
  const tabs = await youtubeTabs(false);
  if (!tabs.length) {
    return { error: "no_tab" };
  }
  // листать — только если ютуб и правда играет: музыка может идти и из другой программы
  if ((msg.type === "next" || msg.type === "prev") && !tabs[0].audible) {
    return { error: "not_playing" };
  }
  const [res] = await chrome.scripting.executeScript({
    target: { tabId: tabs[0].id },
    world: "MAIN",
    func: msg.type === "next" || msg.type === "prev" ? skipInPlayer : inPlayer,
    args: [msg],
  });
  return (res && res.result) || { error: "no_player" };
}

// Следующее/предыдущее видео — как кнопки в самом плеере. Работает и там, где
// ютуб не даёт листать медиаклавишами (одно длинное видео, автовоспроизведение
// выключено). Ждём, пока сменится ролик, и называем его
async function skipInPlayer(msg) {
  const p = document.getElementById("movie_player");
  if (!p || typeof p.getVideoData !== "function") {
    return { error: "no_player" };
  }
  const before = p.getVideoData().video_id;
  if (msg.type === "next") {
    const btn = document.querySelector(".ytp-next-button");
    if (btn && btn.getAttribute("aria-disabled") !== "true" && getComputedStyle(btn).display !== "none") {
      btn.click();
    } else if (typeof p.nextVideo === "function") {
      p.nextVideo();
    } else {
      return { error: "no_next" };
    }
  } else {
    const btn = document.querySelector(".ytp-prev-button");
    if (btn && btn.getAttribute("aria-disabled") !== "true" && getComputedStyle(btn).display !== "none") {
      btn.click();
    } else {
      history.back();
    }
  }
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 200));
    const d = p.getVideoData();
    if (d && d.video_id && d.video_id !== before) {
      return { ok: true, title: d.title || "" };
    }
  }
  return { ok: true, title: "" };
}

// Вкладки ютуба по порядку. Для громкости первой — та, что играет; для новой
// песни — закреплённая: её держат под музыку
async function youtubeTabs(preferPinned) {
  const tabs = await chrome.tabs.query({ url: "*://*.youtube.com/*" });
  const pinned = (a, b) => Number(b.pinned) - Number(a.pinned);
  const audible = (a, b) => Number(!!b.audible) - Number(!!a.audible);
  const recent = (a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0);
  const order = preferPinned ? [pinned, audible, recent] : [audible, pinned, recent];
  return tabs.sort((a, b) => order.reduce((r, cmp) => r || cmp(a, b), 0));
}

// Включить ролик в уже открытой вкладке ютуба, а не плодить новые
async function openVideo(url) {
  const tabs = await youtubeTabs(true);
  if (!tabs.length) {
    await chrome.tabs.create({ url, active: true });
    return { opened: "new" };
  }
  const tab = tabs[0];
  await chrome.tabs.update(tab.id, { url });
  ensurePlaying(tab.id);   // не ждём: Джарвис ответит сразу
  return { opened: "tab", pinned: tab.pinned };
}

// Вкладка в фоне — ролик может встать на паузу до первого показа. Дожидаемся
// загрузки и, если не играет, нажимаем «играть» сами
async function ensurePlaying(tabId) {
  await waitLoaded(tabId, 15000);
  for (let i = 0; i < 10; i++) {
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: playIfStopped,
      });
      if (res && res.result && res.result.playing) {
        return;
      }
    } catch (e) {
      // страница ещё грузится — попробуем через полсекунды
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}

function waitLoaded(tabId, ms) {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") {
        done();
      }
    };
    const timer = setTimeout(done, ms);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function playIfStopped() {
  const p = document.getElementById("movie_player");
  if (!p || typeof p.getPlayerState !== "function") {
    return { playing: false };
  }
  const state = p.getPlayerState();   // 1 — играет, 3 — буферизуется
  if (state === 1 || state === 3) {
    return { playing: true };
  }
  p.playVideo();
  return { playing: false };
}

// Выполняется на странице ютуба, рядом с его собственным кодом: там у плеера
// есть setVolume/getVolume — те же, что двигает ползунок громкости
function inPlayer(msg) {
  const p = document.getElementById("movie_player");
  if (!p || typeof p.setVolume !== "function") {
    return { error: "no_player" };
  }
  if (msg.type === "volume") {
    let v = msg.percent ?? (p.getVolume() + (msg.delta || 0));
    v = Math.max(0, Math.min(100, Math.round(v)));
    p.setVolume(v);
    if (v > 0 && p.isMuted()) {
      p.unMute();
    }
    return { volume: v };
  }
  return { error: "unknown_command" };
}
