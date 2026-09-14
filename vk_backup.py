#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK Backup — полное скачивание переписки ВКонтакте
Скачивает: сообщения, фото, голосовые, кружочки, документы, стикеры
Требуется: Python 3.10+, без сторонних библиотек
"""

import os
import sys
import json
import time
import re
import ssl
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from pathlib import Path
from html import escape

# Фикс SSL для Windows (антивирус/провайдер перехватывает HTTPS)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Фикс для Windows: принудительно UTF-8 в терминале (иначе эмодзи падают)
# Применяем только при запуске как скрипт, не при импорте
def _fix_windows_encoding():
    if sys.platform == "win32":
        import io
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except AttributeError:
            pass  # уже обёрнут или нет buffer (например, при импорте в тестах)


# ──────────────────────────────────────────────
#  НАСТРОЙКИ
# ──────────────────────────────────────────────
ACCESS_TOKEN = os.environ.get("VK_ACCESS_TOKEN", "")
OUTPUT_DIR   = os.environ.get("VK_OUTPUT_DIR", "vk_backup_data")  # Папка для сохранения
API_VERSION  = "5.285"
DELAY        = 0.35           # Безопасная задержка (до 3 запр/сек по лимиту VK API, защищает от флуда и бана IP)
CHUNK        = 200            # Сообщений за раз (максимум)

API_BASE = "https://web.api.vk.ru/method/"

# ──────────────────────────────────────────────
#  API-ХЕЛПЕР
# ──────────────────────────────────────────────
def extract_token(text: str) -> str:
    """Извлекает vk1.a access_token из любого текста, cURL или строки."""
    if not text:
        return ""
    m = re.search(r'access_token=([a-zA-Z0-9_.-]+)', text)
    if m:
        t = m.group(1).strip().strip("'\"")
        if t.startswith("vk1.a."):
            return t
    tokens = re.findall(r'vk1\.a\.[\w-]+', text)
    for t in reversed(tokens):
        if len(t) > 50:
            return t
    return ""


def get_clipboard_text() -> str:
    """Безопасно читает текст из буфера обмена Windows."""
    try:
        from tkinter import Tk
        r = Tk()
        r.withdraw()
        txt = r.clipboard_get()
        r.destroy()
        return txt
    except Exception:
        pass
    try:
        import subprocess
        res = subprocess.run(["powershell", "-command", "Get-Clipboard"], capture_output=True, text=True, timeout=2)
        return res.stdout or ""
    except Exception:
        pass
    return ""


def find_token_in_chrome_leveldb() -> str:
    """Автоматически достает рабочий токен из хранилища Google Chrome на диске."""
    import glob
    appdata = os.environ.get("LOCALAPPDATA", "")
    for profile in ["Profile 1", "Default"]:
        profile_dir = os.path.join(appdata, "Google", "Chrome", "User Data", profile, "Local Storage", "leveldb")
        if not os.path.isdir(profile_dir):
            continue
        tokens = []
        for fpath in glob.glob(os.path.join(profile_dir, "*.*")):
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                for m in re.finditer(b"vk1\\.a\\.[a-zA-Z0-9_\\-]+", data):
                    tok = m.group(0).decode("ascii")
                    if len(tok) > 100 and tok != ACCESS_TOKEN and tok not in [t[1] for t in tokens]:
                        tokens.append((os.path.getmtime(fpath), tok))
            except Exception:
                pass
        tokens.sort(key=lambda x: x[0], reverse=True)
        for mtime, tok in tokens:
            data_enc = urllib.parse.urlencode({"v": API_VERSION, "client_id": 6287487, "access_token": tok}).encode()
            req = urllib.request.Request(f"{API_BASE}users.get", data=data_enc)
            try:
                with urllib.request.urlopen(req, context=_SSL_CTX, timeout=3) as r:
                    res = r.read().decode("utf-8")
                    if "response" in res and "error" not in res:
                        return tok
            except Exception:
                pass
    return ""


_last_call = 0.0


def api(method: str, **params) -> dict:
    """Выполняет запрос к VK API с автоматической обработкой ошибок и rate-limit."""
    global _last_call, ACCESS_TOKEN
    params["access_token"] = ACCESS_TOKEN
    params["v"] = API_VERSION
    params["client_id"] = "6287487"

    for attempt in range(12):
        now = time.time()
        wait = DELAY - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()

        url = API_BASE + method + "?" + urllib.parse.urlencode(params)
        print(f"  [API] Запрос к VK: {method}…", end=" ", flush=True)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                "Origin": "https://vk.ru",
                "Referer": "https://vk.ru/"
            })
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
                data = json.loads(r.read().decode("utf-8"))
            print("OK", flush=True)
        except urllib.error.URLError as e:
            print(f"\n  [!] Сетевая ошибка: {e}, повтор через 5с…")
            time.sleep(5)
            continue
        except Exception as e:
            print(f"\n  [!] Ошибка запроса: {e}")
            time.sleep(3)
            continue

        if "error" in data:
            code = data["error"]["error_code"]
            msg  = data["error"]["error_msg"]
            if code in (6, 29):
                print(f"\n  [!] Rate limit ({msg}), жду 3с…")
                time.sleep(3)
                continue
            elif code == 9:
                wait_flood = 15 + attempt * 5
                print(f"\n  [!] Flood control ({msg}), жду {wait_flood}с… (попытка {attempt+1}/12)")
                time.sleep(wait_flood)
                continue
            elif code == 5 and ("expired" in msg.lower() or "authorization failed" in msg.lower()):
                # Сначала пробуем достать свежий токен напрямую из Google Chrome на диске!
                cand = find_token_in_chrome_leveldb()
                if cand:
                    ACCESS_TOKEN = cand
                    params["access_token"] = ACCESS_TOKEN
                    print(f"\n  [✓] Свежий токен автоматически прочитан из Google Chrome на диске!")
                    continue

                try:
                    import winsound
                    winsound.Beep(1000, 600)  # Звуковой сигнал
                except Exception:
                    pass
                print(f"\n\n{'!'*60}")
                print("  [🔔 ВНИМАНИЕ] Токен истёк (прошло 15 минут)!")
                print("  1. Сделай 1 клик F5 в браузере")
                print("  2. В DevTools нажми: 'Копировать как cURL' (Copy as cURL)")
                print("  [i] Скрипт САМ подхватит токен из буфера обмена!")
                print("      (В терминал вставлять ничего не нужно!)")
                print(f"{'!'*60}")
                
                old_clip = get_clipboard_text()
                found_token = ""
                
                for wait_step in range(300):
                    # Проверяем файлы Chrome на диске каждые 5 секунд
                    if wait_step % 5 == 0:
                        cand_db = find_token_in_chrome_leveldb()
                        if cand_db and cand_db != ACCESS_TOKEN:
                            found_token = cand_db
                            print(f"\n  [✓] Свежий токен автоматически прочитан из файлов Google Chrome!")
                            break

                    clip = get_clipboard_text()
                    if clip and clip != old_clip:
                        cand = extract_token(clip)
                        if cand and cand != ACCESS_TOKEN:
                            found_token = cand
                            print(f"\n  [✓] Токен автоматически перехвачен из буфера обмена!")
                            break
                    time.sleep(1)
                    if wait_step % 10 == 0 and wait_step > 0:
                        print(f"  ...жду обновления токена ({wait_step}с)...", end="\r", flush=True)

                if found_token:
                    ACCESS_TOKEN = found_token
                    params["access_token"] = ACCESS_TOKEN
                    try:
                        import winsound
                        winsound.Beep(1200, 300)
                    except Exception:
                        pass
                    print("  [✓] Токен обновлён! Мгновенно продолжаем скачивание…\n")
                    continue
                else:
                    new_t = input("  Или вставь токен / cURL вручную: ").strip()
                    cand = extract_token(new_t)
                    if cand:
                        ACCESS_TOKEN = cand
                        params["access_token"] = ACCESS_TOKEN
                        print("  [✓] Токен обновлён! Продолжаем скачивание…\n")
                        continue
                    else:
                        raise RuntimeError(f"VK API #{code}: {msg}")
            else:
                raise RuntimeError(f"VK API #{code}: {msg}")

        return data["response"]

    raise RuntimeError(f"Превышено число попыток для метода {method}")


# ──────────────────────────────────────────────
#  УТИЛИТЫ
# ──────────────────────────────────────────────
def safe_name(s: str, max_len: int = 60) -> str:
    """Убирает символы, недопустимые в именах файлов/папок."""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s))
    return s[:max_len].strip(" ._") or "unnamed"


def ts_to_str(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def download_file(url: str, dest: Path, retries: int = 3) -> bool:
    """Скачивает файл по URL в dest. Пропускает если файл уже есть."""
    if not url or not url.startswith("http"):
        return False
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                "Referer": "https://vk.com/",
                "Origin": "https://vk.com"
            })
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r, open(dest, "wb") as f:
                f.write(r.read())
            return True
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    print(f"\n    [!] Не удалось скачать: {url}")
    return False


def get_best_photo_url(sizes: list) -> str:
    """Возвращает URL фото наибольшего размера."""
    order = ["w", "z", "y", "x", "r", "q", "p", "o", "m", "s"]
    size_map = {s["type"]: s["url"] for s in sizes if "url" in s}
    for t in order:
        if t in size_map:
            return size_map[t]
    return sizes[-1].get("url", "") if sizes else ""


# ──────────────────────────────────────────────
#  СКАЧИВАНИЕ ВЛОЖЕНИЙ
# ──────────────────────────────────────────────
def process_attachment(att: dict, media_dir: Path) -> dict:
    """
    Скачивает медиафайл вложения в соответствующую подпапку media_dir:
    - circles/  (кружочки)
    - videos/   (обычные видео)
    - photos/   (фотографии)
    - voice/    (голосовые сообщения)
    - stickers/ (стикеры)
    - docs/     (документы)
    """
    t = att.get("type", "")
    result: dict = {"type": t, "file": None, "url": "", "text": "", "duration": 0}

    try:
        if t == "photo":
            photo = att["photo"]
            url   = get_best_photo_url(photo.get("sizes", []))
            target_dir = media_dir / "photos"
            fname = f"photos/photo_{photo['owner_id']}_{photo['id']}.jpg"
            if url and download_file(url, media_dir / fname):
                result["file"] = fname
            result["url"] = url

        elif t == "doc":
            doc  = att["doc"]
            url  = doc.get("url", "")
            ext  = (doc.get("ext") or "bin").lstrip(".")
            base = safe_name(f"doc_{doc['owner_id']}_{doc['id']}_{doc.get('title', 'file')}")
            fname = f"docs/{base}.{ext}"
            if url and download_file(url, media_dir / fname):
                result["file"] = fname
            result["url"]  = url
            result["text"] = doc.get("title", "")

        elif t == "audio_message":
            am    = att["audio_message"]
            url   = am.get("link_ogg") or am.get("link_mp3", "")
            fname = f"voice/voice_{am['owner_id']}_{am['id']}.ogg"
            if url and download_file(url, media_dir / fname):
                result["file"] = fname
            result["url"]      = url
            result["duration"] = am.get("duration", 0)

        elif t == "video_message":
            vm       = att.get("video_message", {})
            owner_id = vm.get("owner_id", 0)
            vm_id    = vm.get("id", 0)
            dur      = vm.get("duration", 0)
            files    = vm.get("files") or {}
            direct   = (files.get("mp4_1080") or files.get("mp4_720") or
                        files.get("mp4_480")  or files.get("mp4_360") or
                        files.get("mp4_240"))
            fname = f"circles/circle_{owner_id}_{vm_id}.mp4"
            if direct and not direct.endswith(".m3u8"):
                if download_file(direct, media_dir / fname):
                    result["file"] = fname
            result["url"]      = vm.get("share_url") or vm.get("direct_url") or f"https://vk.com/video{owner_id}_{vm_id}"
            result["duration"] = dur
            result["text"]     = "⭕ Кружочек"

        elif t == "video":
            video      = att["video"]
            owner_id   = video.get("owner_id", 0)
            video_id   = video.get("id", 0)
            access_key = video.get("access_key", "")
            title_text = video.get("title", "").strip()

            # Обычные видео сохраняются в папку videos/
            subfolder = "videos"
            prefix    = "video"

            files  = video.get("files") or {}
            direct = (files.get("mp4_1080") or files.get("mp4_720") or
                      files.get("mp4_480")  or files.get("mp4_360") or
                      files.get("mp4_240"))

            if not direct:
                try:
                    video_key = f"{owner_id}_{video_id}"
                    if access_key:
                        video_key += f"_{access_key}"
                    vresp = api("video.get", videos=video_key, extended=0)
                    items = vresp.get("items", [])
                    if items:
                        vfiles = items[0].get("files") or {}
                        direct = (vfiles.get("mp4_1080") or vfiles.get("mp4_720") or
                                  vfiles.get("mp4_480")  or vfiles.get("mp4_360") or
                                  vfiles.get("mp4_240"))
                        if not direct:
                            direct = vfiles.get("external")
                except Exception:
                    pass

            if direct and not direct.endswith(".m3u8"):
                fname = f"{subfolder}/{prefix}_{owner_id}_{video_id}.mp4"
                if download_file(direct, media_dir / fname):
                    result["file"] = fname

            result["url"]  = f"https://vk.com/video{owner_id}_{video_id}"
            result["text"] = title_text or "Видео"

        elif t == "sticker":
            sticker = att["sticker"]
            images  = sticker.get("images_with_background") or sticker.get("images", [])
            url     = ""
            for img in reversed(images):
                if img.get("url"):
                    url = img["url"]
                    break
            fname = f"stickers/sticker_{sticker['sticker_id']}.png"
            if url and download_file(url, media_dir / fname):
                result["file"] = fname
            result["url"] = url

        elif t == "audio":
            audio  = att["audio"]
            artist = audio.get("artist", "")
            title  = audio.get("title", "")
            result["text"] = f"{artist} — {title}".strip(" —")
            url = audio.get("url", "")
            if url and url.startswith("http"):
                fname = "audio/" + safe_name(f"audio_{audio['owner_id']}_{audio['id']}_{artist}_{title}") + ".mp3"
                if download_file(url, media_dir / fname):
                    result["file"] = fname
                result["url"] = url

        elif t == "graffiti":
            graf  = att.get("graffiti", {})
            url   = graf.get("url", "")
            fname = f"stickers/graffiti_{graf.get('owner_id', 0)}_{graf.get('id', 0)}.png"
            if url and download_file(url, media_dir / fname):
                result["file"] = fname
            result["url"] = url

        elif t == "wall":
            wall = att.get("wall", {})
            oid  = wall.get("to_id") or wall.get("owner_id", "")
            result["text"] = "Запись со стены"
            result["url"]  = f"https://vk.com/wall{oid}_{wall.get('id', '')}"

        elif t == "link":
            link           = att.get("link", {})
            result["text"] = link.get("title", "Ссылка")
            result["url"]  = link.get("url", "")

        elif t == "call":
            call      = att.get("call", {})
            state_map = {
                "reached":                "завершён",
                "reached_as_video":       "видеозвонок",
                "cancelled_by_initiator": "отменён",
                "cancelled_by_receiver":  "пропущен",
            }
            state = state_map.get(call.get("state", ""), call.get("state", ""))
            dur   = call.get("duration", 0)
            result["text"] = f"📞 Звонок ({state}{f', {dur}с' if dur else ''})"

        elif t == "gift":
            result["text"] = "🎁 Подарок"

        elif t == "market":
            item           = att.get("market", {})
            result["text"] = item.get("title", "Товар")
            oid            = item.get("owner_id", "")
            iid            = item.get("id", "")
            result["url"]  = f"https://vk.com/market{oid}?w=product{oid}_{iid}"

    except Exception as e:
        result["text"] = f"[ошибка вложения {t}: {e}]"

    return result


# ──────────────────────────────────────────────
#  HTML-ГЕНЕРАТОР
# ──────────────────────────────────────────────
HTML_HEADER = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
      background:#0f0f0f;color:#e1e3e6;padding:16px 8px;padding-top:110px}}

/* Верхняя панель (как в VK Web) */
#nav-bar{{position:fixed;top:0;left:0;right:0;background:rgba(23,23,23,0.96);
          backdrop-filter:blur(15px);border-bottom:1px solid #2f3235;
          padding:10px 16px;z-index:1000;display:flex;flex-direction:column;gap:8px}}
.nav-row{{display:flex;align-items:center;justify-content:space-between;gap:12px}}
.title{{color:#71aaeb;font-weight:600;font-size:1.05em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}

/* Поиск и фильтры */
.controls{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
#search-input{{background:#1c1d1f;color:#fff;border:1px solid #3a3d42;padding:6px 12px;
              border-radius:8px;font-size:0.85em;width:240px;outline:none}}
#search-input:focus{{border-color:#71aaeb}}
.btn{{background:#27292d;color:#e1e3e6;border:1px solid #3a3d42;padding:5px 10px;
      border-radius:6px;font-size:0.8em;cursor:pointer;transition:0.15s}}
.btn:hover, .btn.active{{background:#3b4047;border-color:#71aaeb;color:#fff}}
#date-select{{background:#1c1d1f;color:#fff;border:1px solid #3a3d42;padding:6px 12px;
              border-radius:8px;font-size:0.85em;cursor:pointer;outline:none}}

/* Сообщения */
.msg{{display:flex;gap:10px;margin-bottom:8px;max-width:820px;margin-left:auto;margin-right:auto}}
.msg.out{{flex-direction:row-reverse}}
.avatar{{width:38px;height:38px;border-radius:50%;flex-shrink:0;background:#2d3034;
         object-fit:cover;align-self:flex-end}}
.bubble{{max-width:74%;padding:8px 12px;border-radius:18px;background:#232529;
         word-break:break-word;position:relative;min-width:60px}}
.msg.out .bubble{{background:#2b5278}}
.meta{{font-size:.72em;color:#82878d;margin-bottom:3px;display:flex;align-items:center;gap:6px}}
.msg.out .meta{{justify-content:flex-end;color:#99bfe8}}
.name{{color:#71aaeb;font-weight:600}}
.msg.out .name{{color:#c5e0ff}}
.text{{font-size:.9em;line-height:1.45;white-space:pre-wrap}}

/* Кружочки (круглые видео) */
.circle-video{{width:240px;height:240px;border-radius:50%;object-fit:cover;
               border:3px solid #71aaeb;margin-top:5px;display:block;background:#000}}

/* Вложения */
.att{{margin-top:6px}}
.att img{{max-width:100%;max-height:360px;border-radius:10px;cursor:pointer;display:block;margin-top:4px}}
.att audio{{width:100%;margin-top:4px;height:38px}}
.att video:not(.circle-video){{max-width:100%;max-height:360px;border-radius:10px;display:block;margin-top:4px}}
.att a{{color:#71aaeb;font-size:.85em;word-break:break-all;text-decoration:none}}
.att a:hover{{text-decoration:underline}}
.att-meta{{font-size:.75em;color:#82878d;margin-top:3px}}

/* Разделители дат */
.date-sep-wrap{{text-align:center;margin:24px 0 12px 0}}
.date-sep{{background:#1c1d1f;color:#82878d;font-size:.75em;padding:5px 14px;
           border-radius:14px;border:1px solid #2f3235;display:inline-block;font-weight:500}}

.fwd{{border-left:3px solid #4a5059;padding-left:10px;margin-top:6px;color:#aeb3b9;font-size:.85em}}
.reply{{background:rgba(0,0,0,0.25);border-radius:8px;padding:4px 10px;margin-bottom:6px;
        font-size:.8em;border-left:3px solid #71aaeb}}
.highlight{{background:#ffeb3b;color:#000;padding:1px 3px;border-radius:3px}}

/* Lightbox */
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.94);
     z-index:2000;align-items:center;justify-content:center;cursor:zoom-out}}
#lb.open{{display:flex}}
#lb img{{max-width:96vw;max-height:96vh;border-radius:8px;object-fit:contain}}
</style>
</head>
<body>
<div id="nav-bar">
  <div class="nav-row">
    <div class="title">{title}</div>
    <select id="date-select" onchange="if(this.value) location.hash = this.value;">
      <option value="">📅 Календарь (перейти к дате)…</option>
      {date_options}
    </select>
  </div>
  <div class="nav-row controls">
    <div style="display:flex;gap:4px">
      <input type="text" id="search-input" placeholder="🔍 Поиск сообщений…" onkeyup="if(event.key==='Enter') doSearch(1)">
      <button class="btn" onclick="doSearch(1)">Найти</button>
      <button class="btn" onclick="navSearch(-1)">▲</button>
      <button class="btn" onclick="navSearch(1)">▼</button>
      <span id="search-count" style="font-size:0.75em;color:#888;align-self:center"></span>
    </div>
    <div style="display:flex;gap:4px">
      <button class="btn active" onclick="filterType('all', this)">Все</button>
      <button class="btn" onclick="filterType('circle', this)">⭕ Кружочки</button>
      <button class="btn" onclick="filterType('voice', this)">🎙 Голосовые</button>
      <button class="btn" onclick="filterType('photo', this)">📷 Фото</button>
      <button class="btn" onclick="filterType('doc', this)">📁 Документы</button>
    </div>
  </div>
</div>
<div id="lb" onclick="this.classList.remove('open')"><img id="lb-img" src=""></div>
<script>
function lb(s){{document.getElementById('lb-img').src=s;document.getElementById('lb').classList.add('open')}}

// Поиск
let sMatches = [];
let sIdx = 0;
function doSearch(dir) {{
  const q = document.getElementById('search-input').value.trim().toLowerCase();
  sMatches = [];
  document.querySelectorAll('.highlight').forEach(el => {{
    el.outerHTML = el.innerText;
  }});
  if (!q) {{
    document.getElementById('search-count').innerText = '';
    return;
  }}
  document.querySelectorAll('.msg .text').forEach(el => {{
    const raw = el.innerText;
    if (raw.toLowerCase().includes(q)) {{
      sMatches.push(el);
      const regex = new RegExp(`(${{q.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&')}})`, 'gi');
      el.innerHTML = raw.replace(regex, '<span class="highlight">$1</span>');
    }}
  }});
  document.getElementById('search-count').innerText = sMatches.length ? `${{sMatches.length}} совпадений` : 'Не найдено';
  if (sMatches.length) {{
    sIdx = 0;
    sMatches[0].scrollIntoView({{behavior: 'smooth', block: 'center'}});
  }}
}}
function navSearch(dir) {{
  if (!sMatches.length) return;
  sIdx = (sIdx + dir + sMatches.length) % sMatches.length;
  sMatches[sIdx].scrollIntoView({{behavior: 'smooth', block: 'center'}});
}}

// Фильтры
function filterType(type, btn) {{
  document.querySelectorAll('.controls .btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.msg').forEach(msg => {{
    if (type === 'all') {{
      msg.style.display = '';
    }} else if (type === 'circle') {{
      msg.style.display = msg.querySelector('.circle-video') ? '' : 'none';
    }} else if (type === 'voice') {{
      msg.style.display = msg.querySelector('audio') ? '' : 'none';
    }} else if (type === 'photo') {{
      msg.style.display = msg.querySelector('.att img') ? '' : 'none';
    }} else if (type === 'doc') {{
      msg.style.display = msg.querySelector('.att a[href*="docs/"]') ? '' : 'none';
    }}
  }});
}}
</script>
"""

HTML_FOOTER = "\n</body></html>\n"


def _href(url: str) -> str:
    """Экранирует URL для вставки в HTML-атрибут."""
    return escape(url, quote=True)


def att_to_html(att: dict, media_rel: str) -> str:
    """Конвертирует обработанное вложение в HTML.
    Все поля text в att — сырые (не экранированные), escape делается здесь.
    """
    t   = att.get("type", "")
    f   = att.get("file")          # имя файла или None
    url = att.get("url", "")
    txt = att.get("text", "")      # сырой текст — escape делаем ниже
    dur = att.get("duration", 0)

    ref    = f"{media_rel}/{f}" if f else None
    # Для JS-атрибута onclick экранируем \ и ' чтобы не сломать строку
    ref_js = ref.replace("\\", "\\\\").replace("'", "\\'") if ref else ""

    if t == "photo":
        if ref:
            return f'<div class="att"><img src="{ref}" onclick="lb(\'{ref_js}\')" loading="lazy" title="Фото"></div>'
        if url:
            return f'<div class="att"><a href="{_href(url)}">📷 Фото (не скачано)</a></div>'
        return '<div class="att att-meta">📷 Фото</div>'

    elif t == "audio_message":
        label = f"🎙 Голосовое ({dur}с)" if dur else "🎙 Голосовое"
        if ref:
            return f'<div class="att">{label}<br><audio controls src="{ref}"></audio></div>'
        return f'<div class="att att-meta">{label}</div>'

    elif t in ("video", "video_message"):
        if ref:
            is_circle = (t == "video_message") or ("circles/" in str(f))
            cls_v = "circle-video" if is_circle else ""
            return f'<div class="att"><video class="{cls_v}" controls src="{ref}" preload="metadata"></video></div>'
        link_text = escape(txt) if txt else ("⭕ Кружочек" if t == "video_message" else "📹 Видео")
        if url:
            return f'<div class="att"><a href="{_href(url)}">{link_text}</a></div>'
        return f'<div class="att att-meta">{link_text}</div>'

    elif t == "sticker":
        if ref:
            return f'<div class="att"><img src="{ref}" style="max-height:110px;border-radius:4px" title="Стикер"></div>'
        return '<div class="att att-meta">🎨 Стикер</div>'

    elif t == "doc":
        icon  = "🎵" if f and f.endswith((".mp3", ".ogg", ".wav", ".flac")) else "📎"
        # FIX: txt — сырой, f — имя файла (уже safe), оба нужно escape()
        label = escape(txt) if txt else escape(f or "Документ")
        if ref:
            return f'<div class="att">{icon} <a href="{ref}">{label}</a></div>'
        if url:
            return f'<div class="att">{icon} <a href="{_href(url)}">{label}</a></div>'
        return f'<div class="att att-meta">{icon} {label}</div>'

    elif t == "audio":
        label = escape(txt) if txt else "🎵 Аудио"
        if ref:
            return f'<div class="att">🎵 {label}<br><audio controls src="{ref}"></audio></div>'
        return f'<div class="att att-meta">🎵 {label}</div>'

    elif t == "graffiti":
        if ref:
            return f'<div class="att"><img src="{ref}" style="max-height:150px" title="Граффити" onclick="lb(\'{ref_js}\')"></div>'
        return '<div class="att att-meta">🎨 Граффити</div>'

    elif t == "link":
        href = _href(url) if url else "#"
        return f'<div class="att">🔗 <a href="{href}">{escape(txt) if txt else href}</a></div>'

    elif t in ("wall", "market"):
        href = _href(url) if url else "#"
        return f'<div class="att">📋 <a href="{href}">{escape(txt) if txt else "Запись"}</a></div>'

    elif t in ("call", "gift"):
        return f'<div class="att att-meta">{escape(txt)}</div>'

    elif txt:
        return f'<div class="att att-meta">[{escape(t)}] {escape(txt)}</div>'

    return ""


def _clean_fwd(fwd_list: list):
    """Рекурсивно убирает _processed_atts из пересланных сообщений перед сохранением в JSON."""
    for fwd in fwd_list:
        fwd.pop("_processed_atts", None)
        if fwd.get("fwd_messages"):
            _clean_fwd(fwd["fwd_messages"])


def render_fwd(fwd_list: list, users: dict, media_rel: str, depth: int = 0) -> str:
    """Рекурсивно рендерит пересланные сообщения."""
    if depth > 3:
        return ""
    html = ""
    for fwd in fwd_list:
        from_id = fwd.get("from_id", 0)
        u = users.get(str(abs(from_id)), {})
        if from_id < 0:
            name = u.get("name", f"group{abs(from_id)}")
        else:
            name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or f"id{from_id}"

        fwd_text  = escape(fwd.get("text", ""))
        fwd_atts  = "".join(att_to_html(a, media_rel) for a in fwd.get("_processed_atts", []))
        inner_fwd = render_fwd(fwd.get("fwd_messages", []), users, media_rel, depth + 1)

        html += (f'<div class="fwd">'
                 f'<div class="fwd-name">{escape(name)}</div>'
                 f'{"<div class=text>" + fwd_text + "</div>" if fwd_text else ""}'
                 f'{fwd_atts}{inner_fwd}'
                 f'</div>')
    return html


def msg_to_html(msg: dict, my_id: int, users: dict, media_rel: str) -> str:
    """Генерирует HTML одного сообщения. Использует _processed_atts из msg."""
    from_id = msg.get("from_id", 0)
    is_out  = (from_id == my_id)
    cls     = "msg out" if is_out else "msg"

    u          = users.get(str(abs(from_id)), {})
    avatar_src = u.get("photo_50", "")
    avatar_html = (f'<img class="avatar" src="{escape(avatar_src, quote=True)}" alt="" loading="lazy">'
                   if avatar_src else '<div class="avatar"></div>')

    if from_id < 0:
        name = u.get("name", f"group{abs(from_id)}")
    else:
        name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or f"id{from_id}"

    date_str = ts_to_str(msg.get("date", 0))
    meta     = f'<div class="meta"><span class="name">{escape(name)}</span> · {date_str}</div>'

    reply_html = ""
    if msg.get("reply_message"):
        r      = msg["reply_message"]
        r_u    = users.get(str(abs(r.get("from_id", 0))), {})
        r_name = r_u.get("first_name", "?")
        r_text = escape((r.get("text") or "")[:100])
        reply_html = f'<div class="reply">↩ <b>{escape(r_name)}</b>: {r_text}</div>'

    text_val = msg.get("text", "")
    if msg.get("action"):
        action = msg["action"]
        atype  = action.get("type", "")
        action_texts = {
            "chat_create":        "создал беседу",
            "chat_title_update":  f"переименовал беседу в «{escape(action.get('text', ''))}»",
            "chat_photo_update":  "обновил фото беседы",
            "chat_photo_remove":  "удалил фото беседы",
            "chat_invite_user":   "добавил участника",
            "chat_kick_user":     "исключил участника",
            "chat_pin_message":   "закрепил сообщение",
            "chat_unpin_message": "открепил сообщение",
        }
        text_val = f"[{action_texts.get(atype, atype)}]"

    text_html = f'<div class="text">{escape(text_val)}</div>' if text_val else ""
    atts_html = "".join(att_to_html(a, media_rel) for a in msg.get("_processed_atts", []))
    fwd_html  = render_fwd(msg.get("fwd_messages", []), users, media_rel)

    bubble = f'<div class="bubble">{meta}{reply_html}{text_html}{atts_html}{fwd_html}</div>'
    return f'<div class="{cls}">{avatar_html}{bubble}</div>\n'


# ──────────────────────────────────────────────
#  ОПРЕДЕЛЕНИЕ ИМЕНИ И ДАННЫХ ДИАЛОГА
# ──────────────────────────────────────────────
def resolve_peer_details(raw_input: str, users_cache: dict) -> tuple:
    """
    Определяет peer_id, тип, название и настройки диалога.
    Поддерживает:
      - ссылки (vk.com/im?sel=..., vk.com/im/convo/..., vk.com/id..., vk.com/club...)
      - короткие имена (@username, username)
      - числовые ID (123456789, -12345, 2000000001)
      - идентификаторы бесед (c1, chat1)
    Возвращает: (peer_id: int, peer_type: str, title: str, chat_settings: dict)
    """
    raw = str(raw_input).strip()
    if not raw:
        raise ValueError("Пустой ввод диалога.")

    # Отрезаем протокол, мобильные/веб домены и якоря
    raw = re.sub(r"^https?://(?:[a-zA-Z0-9-]+\.)?vk\.(?:com|ru)/", "", raw)
    raw = raw.split("#")[0]

    target = None

    # 1. Проверка на параметр sel=... (например vk.com/im?sel=123 или sel=c1)
    m_sel = re.search(r"[?&]sel=([a-zA-Z0-9_-]+)", raw)
    if m_sel:
        val = m_sel.group(1)
        if re.match(r"^c\d+$", val, re.IGNORECASE):
            target = 2000000000 + int(val[1:])
        elif val.lstrip("-").isdigit():
            target = int(val)
        else:
            raw = val

    # 2. Проверка на путь im/convo/...
    if target is None:
        m_convo = re.search(r"im/convo/([a-zA-Z0-9_-]+)", raw)
        if m_convo:
            val = m_convo.group(1)
            if re.match(r"^c\d+$", val, re.IGNORECASE):
                target = 2000000000 + int(val[1:])
            elif val.lstrip("-").isdigit():
                target = int(val)
            else:
                raw = val

    # 3. Беседа вида c1, c12, chat5
    if target is None:
        m_c = re.match(r"^(?:c|chat)(\d+)$", raw, re.IGNORECASE)
        if m_c:
            target = 2000000000 + int(m_c.group(1))

    # 4. Числовой ID (положительный или отрицательный)
    if target is None and raw.lstrip("-").isdigit():
        target = int(raw)

    # 5. Префиксы id123, club123, public123, event123
    if target is None:
        m_id = re.match(r"^id(\d+)$", raw, re.IGNORECASE)
        if m_id:
            target = int(m_id.group(1))
        else:
            m_club = re.match(r"^(?:club|public|event)(\d+)$", raw, re.IGNORECASE)
            if m_club:
                target = -int(m_club.group(1))

    # 6. Если до сих пор не определился ID — ищем через resolveScreenName
    if target is None:
        clean_name = raw.lstrip("@").strip("/")
        res = api("utils.resolveScreenName", screen_name=clean_name)
        if not res:
            raise ValueError(f"Пользователь или сообщество '{clean_name}' не найдено в ВКонтакте.")
        tp = res.get("type")
        oid = res["object_id"]
        target = oid if tp == "user" else -oid

    # Определяем тип пира
    if target >= 2000000000:
        peer_type = "chat"
    elif target < 0:
        peer_type = "group"
    else:
        peer_type = "user"

    title = ""
    chat_settings = {}

    if peer_type == "user":
        u = users_cache.get(str(target))
        if not u:
            try:
                res = api("users.get", user_ids=target, fields="first_name,last_name,photo_50,screen_name")
                if res:
                    u = res[0]
                    users_cache[str(target)] = u
            except Exception as e:
                print(f"  [!] Не удалось запросить профиль пользователя {target}: {e}")
        if u:
            title = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        if not title:
            title = f"user{target}"

    elif peer_type == "group":
        g = users_cache.get(str(target)) or users_cache.get(str(-target))
        if not g:
            try:
                res = api("groups.getById", group_id=abs(target), fields="name,photo_50")
                if res:
                    g = res[0] if isinstance(res, list) else res.get("items", [{}])[0]
                    users_cache[str(target)] = g
                    users_cache[str(-target)] = g
            except Exception as e:
                print(f"  [!] Не удалось запросить сообщество {abs(target)}: {e}")
        if g:
            title = g.get("name", "")
        if not title:
            title = f"group{abs(target)}"

    elif peer_type == "chat":
        try:
            res = api("messages.getConversationsById", peer_ids=target, extended=1)
            items = res.get("items", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
            if items:
                first = items[0]
                if isinstance(first, dict):
                    chat_settings = first.get("chat_settings") or first.get("conversation", {}).get("chat_settings", {})
                    title = chat_settings.get("title", "")
            if isinstance(res, dict):
                for u in res.get("profiles", []):
                    users_cache.setdefault(str(u["id"]), u)
                for g in res.get("groups", []):
                    users_cache.setdefault(str(-g["id"]), g)
        except Exception as e:
            print(f"  [!] Не удалось запросить данные беседы {target}: {e}")
        if not title:
            title = f"chat{target}"

    return target, peer_type, title, chat_settings


# ──────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ВСЕХ СООБЩЕНИЙ
# ──────────────────────────────────────────────
def get_all_messages(peer_id: int, users_cache: dict, dialog_dir: Path = None, media_dir: Path = None, title: str = "", icon: str = "💬", my_id: int = 0) -> list:
    """Скачивает ВСЕ сообщения диалога и СРАЗУ скачивает медиафайлы на диск в реальном времени."""
    json_file = dialog_dir / "messages.json" if dialog_dir else None
    existing_messages = []
    
    # Если файл уже есть — загружаем уже скачанное, чтобы не качать заново!
    if json_file and json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                existing_messages = json.load(f)
            print(f"  [i] Найдено ранее скачанных сообщений: {len(existing_messages):,}")
        except Exception:
            existing_messages = []

    messages = existing_messages
    offset   = len(existing_messages)
    total    = None

    while True:
        resp = api("messages.getHistory",
                   peer_id=peer_id,
                   count=CHUNK,
                   offset=offset)

        if total is None:
            total = resp.get("count", 0)

        for u in resp.get("profiles", []):
            users_cache.setdefault(str(u["id"]), u)
        for g in resp.get("groups", []):
            users_cache.setdefault(str(-g["id"]), g)

        items = resp.get("items", [])
        if not items:
            break

        # СРАЗУ СКАЧИВАЕМ МЕДИАФАЙЛЫ ДЛЯ ЭТОЙ ПОРЦИИ НА ДИСК!
        if media_dir:
            for msg in items:
                msg["_processed_atts"] = [process_attachment(a, media_dir) for a in msg.get("attachments", [])]
                download_fwd_attachments(msg.get("fwd_messages", []), media_dir)

        messages.extend(items)
        offset += len(items)
        pct = min(100, int(offset * 100 / max(total, 1)))
        print(f"  ↓ {offset:,}/{total:,} сообщений ({pct}%)", end="\r", flush=True)

        # СРАЗУ ПИШЕМ НА ДИСК КАЖДЫЕ 200 СООБЩЕНИЙ!
        if json_file and len(items) > 0:
            clean_part = []
            for m in messages:
                cl = dict(m)
                cl.pop("_processed_atts", None)
                clean_part.append(cl)
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(clean_part, f, ensure_ascii=False, indent=2)

            # Обновляем index.html каждые 1000 сообщений на лету!
            if dialog_dir and offset % 1000 == 0:
                try:
                    build_html(messages, my_id, users_cache, title or f"Диалог {peer_id}", icon, dialog_dir)
                except Exception:
                    pass

        if offset >= total:
            break

    print()
    if json_file:
        clean_part = []
        for m in messages:
            cl = dict(m)
            cl.pop("_processed_atts", None)
            clean_part.append(cl)
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(clean_part, f, ensure_ascii=False, indent=2)

    return messages


def download_fwd_attachments(fwd_list: list, media_dir: Path):
    """Рекурсивно скачивает вложения в пересланных сообщениях."""
    for fwd in fwd_list:
        fwd["_processed_atts"] = [process_attachment(a, media_dir) for a in fwd.get("attachments", [])]
        if fwd.get("fwd_messages"):
            download_fwd_attachments(fwd["fwd_messages"], media_dir)


# ──────────────────────────────────────────────
#  СОХРАНЕНИЕ ОДНОГО ДИАЛОГА
# ──────────────────────────────────────────────
def backup_conversation(conv: dict, my_id: int, base_dir: Path, users_cache: dict) -> int:
    peer      = conv["conversation"]["peer"]
    peer_id   = peer["id"]
    peer_type = peer["type"]

    if peer_type == "user":
        u     = users_cache.get(str(peer_id), {})
        title = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        icon  = "👤"
    elif peer_type == "group":
        u     = users_cache.get(str(-peer_id), {}) or users_cache.get(str(peer_id), {})
        title = u.get("name", "")
        icon  = "📢"
    elif peer_type == "chat":
        cs    = conv["conversation"].get("chat_settings", {})
        title = cs.get("title", "")
        icon  = "👥"
    else:
        title = ""
        icon  = "💬"

    # Если имя не найдено в кэше — определяем через API
    if not title or title == f"user{peer_id}" or title == f"chat{peer_id}":
        try:
            _, _, resolved_title, _ = resolve_peer_details(str(peer_id), users_cache)
            if resolved_title:
                title = resolved_title
        except Exception:
            pass

    if not title:
        title = f"user{peer_id}" if peer_type == "user" else f"peer{peer_id}"

    safe_title = safe_name(title)
    dialog_dir = base_dir / f"{peer_id}_{safe_title}"
    media_dir  = dialog_dir / "media"
    dialog_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{icon} {title}  (peer_id={peer_id})")

    # Скачиваем сообщения и МЕДИА НА ЛЕТУ НА ДИСК!
    messages = get_all_messages(peer_id, users_cache, dialog_dir, media_dir, title=title, icon=icon, my_id=my_id)
    total = len(messages)
    # Генерируем красивый HTML с навигацией по датам
    print("  Генерируем HTML страницу…", flush=True)
    build_html(messages, my_id, users_cache, title, icon, dialog_dir)
    print("  Генерируем TXT файл для быстрого поиска…", flush=True)
    build_txt(messages, my_id, users_cache, title, dialog_dir)
    print(f"  ✓ {total:,} сообщений сохранено → {dialog_dir.resolve()}")
    return total


def build_html(messages: list, my_id: int, users_cache: dict, title: str, icon: str, dialog_dir: Path):
    """Генерирует HTML страницу переписки с навигацией по месяцам и датам."""
    if not messages:
        return

    # Собираем статистику по месяцам для выпадающего списка
    months = {}
    for msg in messages:
        ts = msg.get("date", 0)
        dt = datetime.fromtimestamp(ts)
        m_key = dt.strftime("%Y-%m")
        m_label = dt.strftime("%B %Y")
        months[m_key] = months.get(m_key, {"label": m_label, "count": 0})
        months[m_key]["count"] += 1

    # Формируем пункты выпадающего меню дат
    date_options = []
    for m_key, m_info in months.items():
        date_options.append(f'<option value="#month_{m_key.replace("-", "_")}">{escape(m_info["label"])} ({m_info["count"]:,} сообщ.)</option>')

    header = HTML_HEADER.format(
        title=escape(f"{icon} {title}"),
        date_options="".join(date_options)
    )

    html_parts = [header]
    prev_date = None
    seen_months = set()

    for msg in messages:
        dt = datetime.fromtimestamp(msg.get("date", 0))
        msg_date = dt.strftime("%d %B %Y")
        m_key = dt.strftime("%Y_%m")

        if msg_date != prev_date:
            month_anchor = f'id="month_{m_key}" ' if m_key not in seen_months else ''
            seen_months.add(m_key)
            d_anchor = f'date_{dt.strftime("%Y_%m_%d")}'
            html_parts.append(
                f'<div class="date-sep-wrap" {month_anchor}><div class="date-sep" id="{d_anchor}">── {escape(msg_date)} ──</div></div>\n'
            )
            prev_date = msg_date

        html_parts.append(msg_to_html(msg, my_id, users_cache, "media"))

    html_parts.append(HTML_FOOTER)

    with open(dialog_dir / "index.html", "w", encoding="utf-8") as f:
        f.writelines(html_parts)


def build_txt(messages: list, my_id: int, users_cache: dict, title: str, dialog_dir: Path):
    """
    Генерирует чистый текстовый файл переписки (TXT),
    идеальный для чтения в блокноте, быстрого поиска Ctrl+F и анализа в ИИ.
    Работает локально без сетевых запросов.
    """
    if not messages:
        return

    txt_lines = [
        "============================================================\n",
        f" Переписка : {title}\n",
        f" Сообщений : {len(messages):,}\n",
        f" Экспорт   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        "============================================================\n\n"
    ]

    for msg in messages:
        ts = msg.get("date", 0)
        dt_str = ts_to_str(ts)
        from_id = msg.get("from_id", 0)

        if from_id == my_id:
            sender = "Вы"
        elif from_id > 0:
            u = users_cache.get(str(from_id), {})
            sender = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or f"id{from_id}"
        elif from_id < 0:
            g = users_cache.get(str(-from_id), {}) or users_cache.get(str(from_id), {})
            sender = g.get("name") or f"club{abs(from_id)}"
        else:
            sender = "Система"

        text = msg.get("text", "").strip()

        # Описание вложений
        att_descs = []
        for att in msg.get("attachments", []):
            atype = att.get("type", "")
            if atype == "photo":
                att_descs.append("[Фото]")
            elif atype == "audio_message":
                dur = att.get("audio_message", {}).get("duration", 0)
                att_descs.append(f"[Голосовое {dur}с]")
            elif atype == "video_message":
                dur = att.get("video_message", {}).get("duration", 0)
                att_descs.append(f"[Кружочек {dur}с]")
            elif atype == "video":
                title_v = att.get("video", {}).get("title", "")
                att_descs.append(f"[Видео: {title_v}]" if title_v else "[Видео]")
            elif atype == "sticker":
                sid = att.get("sticker", {}).get("sticker_id", "")
                att_descs.append(f"[Стикер #{sid}]")
            elif atype == "doc":
                d_title = att.get("doc", {}).get("title", "файл")
                att_descs.append(f"[Документ: {d_title}]")
            elif atype == "audio":
                a = att.get("audio", {})
                att_descs.append(f"[Аудио: {a.get('artist', '')} — {a.get('title', '')}]")
            elif atype == "call":
                att_descs.append("[Звонок]")
            elif atype:
                att_descs.append(f"[{atype}]")

        fwd_count = len(msg.get("fwd_messages", []))
        if fwd_count:
            att_descs.append(f"[{fwd_count} пересл. сообщ.]")

        att_str = (" " + " ".join(att_descs)) if att_descs else ""
        if text and att_str:
            txt_lines.append(f"[{dt_str}] {sender}:\n{text}{att_str}\n\n")
        elif text:
            txt_lines.append(f"[{dt_str}] {sender}:\n{text}\n\n")
        elif att_str:
            txt_lines.append(f"[{dt_str}] {sender}: {att_str}\n\n")

    txt_file = dialog_dir / "messages.txt"
    with open(txt_file, "w", encoding="utf-8") as f:
        f.writelines(txt_lines)


# ──────────────────────────────────────────────
#  СПИСОК ВСЕХ ДИАЛОГОВ
# ──────────────────────────────────────────────
def get_all_conversations(users_cache: dict) -> list:
    convs  = []
    offset = 0

    while True:
        resp = api("messages.getConversations",
                   count=200,
                   offset=offset,
                   extended=1,
                   fields="first_name,last_name,photo_50,name,screen_name")

        for u in resp.get("profiles", []):
            users_cache.setdefault(str(u["id"]), u)
        for g in resp.get("groups", []):
            users_cache.setdefault(str(-g["id"]), g)

        items = resp.get("items", [])
        if not items:
            break

        convs.extend(items)
        total = resp.get("count", 0)
        offset += len(items)
        print(f"  Диалоги: {offset}/{total}", end="\r", flush=True)
        if offset >= total:
            break

    print(f"  Всего диалогов: {len(convs)}")
    return convs


# ──────────────────────────────────────────────
#  ГЛАВНАЯ СТРАНИЦА-ОГЛАВЛЕНИЕ
# ──────────────────────────────────────────────
def create_index(base_dir: Path, convs: list, users_cache: dict, my_name: str):
    rows = []
    for conv in convs:
        peer      = conv["conversation"]["peer"]
        peer_id   = peer["id"]
        peer_type = peer["type"]
        if peer_type == "user":
            u     = users_cache.get(str(peer_id), {})
            title = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or f"user{peer_id}"
            icon  = "👤"
        elif peer_type == "group":
            u     = users_cache.get(str(-peer_id), {})
            title = u.get("name", f"group{peer_id}")
            icon  = "📢"
        else:
            cs    = conv["conversation"].get("chat_settings", {})
            title = cs.get("title", f"chat{peer_id}")
            icon  = "👥"
        safe   = safe_name(title)
        folder = f"{peer_id}_{safe}"
        # FIX: используем urllib.parse.quote для href (не escape — он HTML, а не URL)
        folder_href = urllib.parse.quote(folder, safe="_-.")
        rows.append(
            f'<tr><td>{icon}</td>'
            f'<td><a href="{folder_href}/index.html">{escape(title)}</a></td>'
            f'<td style="color:#666;font-size:.8em">{escape(peer_type)} · id {peer_id}</td></tr>'
        )

    html = (
        f'<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">'
        f'<title>VK Backup — {escape(my_name)}</title>'
        f'<style>'
        f'body{{background:#0d0d0d;color:#e4e4e4;font-family:Arial,sans-serif;padding:30px}}'
        f'h1{{color:#5b9bd5;margin-bottom:4px}}'
        f'p{{color:#666;font-size:.85em;margin-bottom:20px}}'
        f'table{{border-collapse:collapse;width:100%;max-width:700px}}'
        f'tr:hover td{{background:#1a1a1a}}'
        f'td{{padding:9px 14px;border-bottom:1px solid #1a1a1a}}'
        f'a{{color:#5b9bd5;text-decoration:none}}a:hover{{text-decoration:underline}}'
        f'</style></head><body>'
        f'<h1>📦 VK Backup</h1>'
        f'<p>Аккаунт: {escape(my_name)} · {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>'
        f'<table>{"".join(rows)}</table></body></html>'
    )

    with open(base_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n  Главная страница: {(base_dir / 'index.html').resolve()}")


# ──────────────────────────────────────────────
#  ТОЧКА ВХОДА
# ──────────────────────────────────────────────
def main():
    global ACCESS_TOKEN

    print("=" * 60)
    print("   VK Backup — скачивание переписки ВКонтакте")
    print("=" * 60)

    if not ACCESS_TOKEN:
        print("\nКак получить токен:")
        print("  1. Открой эту ссылку в браузере:")
        print()
        print("     https://oauth.vk.com/authorize?client_id=2685278"
              "&scope=messages,photos,docs,offline"
              "&redirect_uri=https://oauth.vk.com/blank.html"
              "&display=page&response_type=token")
        print()
        print("  2. Войди в ВК → нажми «Разрешить»")
        print("  3. Страница будет пустой — скопируй адрес из адресной строки")
        print("  4. Найди access_token=XXXXXXXX и скопируй только токен (до &)")
        print("     (можно вставить весь адрес — скрипт разберётся сам)")
        print()
        ACCESS_TOKEN = input("Вставь токен: ").strip()

    # Достаем user_id из ссылки, если он там есть
    parsed_uid = None
    if "user_id=" in ACCESS_TOKEN:
        try:
            parsed_uid = int(ACCESS_TOKEN.split("user_id=")[1].split("&")[0])
        except Exception:
            pass

    # Очищаем токен
    if "access_token=" in ACCESS_TOKEN:
        ACCESS_TOKEN = ACCESS_TOKEN.split("access_token=")[1]
    if "&" in ACCESS_TOKEN:
        ACCESS_TOKEN = ACCESS_TOKEN.split("&")[0]
    ACCESS_TOKEN = ACCESS_TOKEN.strip()

    if not ACCESS_TOKEN:
        sys.exit("Токен не введён. Выход.")

    print("\n[*] Авторизация…")
    my_id = parsed_uid
    my_name = f"id{my_id}" if my_id else "Пользователь"

    try:
        me_list = api("users.get", fields="photo_50")
        if me_list:
            me = me_list[0]
            my_id = me["id"]
            my_name = f"{me.get('first_name', '')} {me.get('last_name', '')}".strip()
            print(f"[✓] Авторизован как: {my_name} (id{my_id})")
    except Exception as e:
        if my_id:
            print(f"[✓] Вход выполнен: id{my_id}")
        else:
            sys.exit(f"Ошибка авторизации: {e}")

    if not my_id:
        sys.exit("Не удалось определить ID пользователя.")

    base_dir = Path(OUTPUT_DIR) / f"id{my_id}"
    base_dir.mkdir(parents=True, exist_ok=True)
    print(f"[*] Папка сохранения: {base_dir.resolve()}")

    print("\nЧто скачать?")
    print("  1 — Все диалоги")
    print("  2 — Только личные переписки")
    print("  3 — Только беседы и группы")
    print("  4 — Один конкретный диалог")
    choice = input("\nВыбор [1]: ").strip() or "1"

    users_cache: dict = {}

    if choice == "4":
        raw = input("Введи ссылку на диалог, peer_id или @username: ").strip()
        try:
            target, peer_type, title, chat_settings = resolve_peer_details(raw, users_cache)
        except Exception as e:
            sys.exit(f"Ошибка поиска диалога: {e}")

        icon = "👤" if peer_type == "user" else ("📢" if peer_type == "group" else "👥")
        print(f"  [✓] Выбран диалог: {icon} {title} (peer_id: {target})")
        convs = [{"conversation": {"peer": {"id": target, "type": peer_type}, "chat_settings": chat_settings}}]
    else:
        print("\n[*] Получаем список диалогов…")
        convs = get_all_conversations(users_cache)
        if choice == "2":
            convs = [c for c in convs if c["conversation"]["peer"]["type"] == "user"]
        elif choice == "3":
            convs = [c for c in convs if c["conversation"]["peer"]["type"] in ("chat", "group")]

    print(f"\n[*] Будет скачано диалогов: {len(convs)}")
    if len(convs) > 1:
        ok = input("Продолжить? [Y/n]: ").strip().lower()
        if ok == "n":
            sys.exit("Отменено.")

    total_msgs = 0
    errors: list = []
    t0 = time.time()

    for i, conv in enumerate(convs, 1):
        pid = conv["conversation"]["peer"]["id"]
        print(f"\n[{i}/{len(convs)}]", end=" ")
        try:
            count = backup_conversation(conv, my_id, base_dir, users_cache)
            total_msgs += count
        except KeyboardInterrupt:
            print("\n[!] Прервано пользователем")
            break
        except Exception as e:
            print(f"\n  [!] Ошибка в диалоге {pid}: {e}")
            errors.append((pid, str(e)))

    elapsed = time.time() - t0
    create_index(base_dir, convs, users_cache, my_name)

    print("\n" + "=" * 60)
    print(f"  ✅ Готово!")
    print(f"  Сообщений скачано : {total_msgs:,}")
    print(f"  Время             : {int(elapsed // 60)}м {int(elapsed % 60)}с")
    print(f"  Данные            : {base_dir.resolve()}")
    if errors:
        print(f"  ⚠️  Ошибок: {len(errors)}")
        for pid, err in errors:
            print(f"      peer {pid}: {err}")
    print("=" * 60)
    print(f"\n  Открой в браузере: {(base_dir / 'index.html').resolve()}")


if __name__ == "__main__":
    _fix_windows_encoding()
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[!] Прервано.")
