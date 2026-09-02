#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Кто сколько говорил на встрече.

Расшифровки в «дпд» сделаны без разделения говорящих, поэтому долю речи по ним
посчитать нельзя. Этот прогон берёт запись заново и распознаёт её с диаризацией:

  1. Берёт из листа «ОКК» строки с записью Zoom (можно сузить до менеджеров).
  2. Apify достаёт из share-ссылки прямой адрес медиафайла, скачиваем.
  3. Deepgram с diarize=true возвращает реплики с номерами говорящих.
  4. Кто из говорящих менеджер, определяем по речи продавца — кто произносит
     «тренинг», «комитет», «поток», цену. Это проверяемо и не зависит от того,
     кто говорил больше: как раз это мы и измеряем.
  5. Пишем в лист «ОКК»: доля речи, длинный монолог, реплики, как определён менеджер.

Запуск: workflow «OKK diarize» либо локально с SHEET_ID / GOOGLE_SA_FILE.
"""

import io
import json
import os
import re
import sys
import tempfile
import time

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = os.environ.get("SHEET_ID", "").strip()
OKK_TAB = os.environ.get("OKK_TAB", "ОКК").strip()
SA_FILE = os.environ.get("GOOGLE_SA_FILE", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
DEEPGRAM_TOKEN = os.environ.get("DEEPGRAM_TOKEN", "").strip()
APIFY_TASK = os.environ.get("APIFY_TASK", "gyglem~my-actor-1-task")
DEEPGRAM_URL = ("https://api.deepgram.com/v1/listen"
                "?model=nova-3&language=ru&smart_format=true&diarize=true&utterances=true")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LIMIT = int(os.environ.get("LIMIT") or "5")
MANAGERS = [m.strip().lower() for m in os.environ.get("MANAGERS", "").split(",") if m.strip()]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL") or "30")
MAX_POLLS = int(os.environ.get("MAX_POLLS") or "40")

# слова продавца: по ним отличаем менеджера от клиента
SELLER_WORDS = re.compile(
    r"комитет|тренинг|поток|программ\w*|павел|паш[аиеу]|стамбул|"
    r"820|720|бронь|предоплат\w*|рассрочк\w*|заявк\w*", re.I)

COLUMNS = ["речь менеджера %", "длинный монолог", "реплик менеджер/клиент",
           "минут записи", "как определён менеджер"]


def log(*a):
    print(*a, flush=True)


def letter(idx0):
    s, n = "", idx0 + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def sheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = (Credentials.from_service_account_file(SA_FILE, scopes=scopes) if SA_FILE
             else Credentials.from_service_account_info(json.loads(SA_JSON), scopes=scopes))
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


# ---------- запись ----------

def apify_audio(share_url, passcode):
    r = requests.post("https://api.apify.com/v2/actor-tasks/%s/runs" % APIFY_TASK,
                      params={"token": APIFY_TOKEN},
                      json={"shareUrl": share_url, "passcode": passcode}, timeout=60)
    r.raise_for_status()
    run_id = r.json()["data"]["id"]
    for _ in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL)
        s = requests.get("https://api.apify.com/v2/actor-runs/%s" % run_id,
                         params={"token": APIFY_TOKEN}, timeout=60)
        s.raise_for_status()
        status = s.json()["data"]["status"]
        if status in ("READY", "RUNNING", "TIMING-OUT"):
            continue
        if status != "SUCCEEDED":
            raise RuntimeError("Apify: статус %s" % status)
        break
    else:
        raise RuntimeError("Apify: не дождались за %d минут" % (MAX_POLLS * POLL_INTERVAL // 60))

    items = requests.get("https://api.apify.com/v2/actor-runs/%s/dataset/items" % run_id,
                         params={"token": APIFY_TOKEN, "clean": "true"}, timeout=120).json()
    return items[0] if items else {}


def download(item):
    url = item.get("audio_url")
    if not url:
        raise RuntimeError("Apify не вернул ссылку на запись")
    r = requests.get(url, headers={
        "Referer": item.get("finalUrl", ""), "Cookie": item.get("cookieHeader", ""),
        "Origin": "https://us06web.zoom.us", "User-Agent": USER_AGENT,
    }, stream=True, timeout=900)
    r.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    for chunk in r.iter_content(1 << 20):
        tmp.write(chunk)
    tmp.close()
    return tmp.name


def transcribe(path):
    with open(path, "rb") as f:
        r = requests.post(DEEPGRAM_URL, headers={"Content-Type": "video/mp4",
                                                 "Authorization": "Token " + DEEPGRAM_TOKEN},
                          data=f, timeout=2400)
    r.raise_for_status()
    return r.json()


# ---------- кто сколько говорил ----------

def measure(dg):
    """Доли речи по говорящим и самый длинный монолог менеджера."""
    utts = (dg.get("results") or {}).get("utterances") or []
    if not utts:
        raise RuntimeError("Deepgram вернул ответ без разбивки по говорящим")

    talk, words, seller_hits, lines = {}, {}, {}, {}
    for u in utts:
        sp = u.get("speaker", 0)
        dur = float(u.get("end", 0)) - float(u.get("start", 0))
        text = u.get("transcript", "") or ""
        talk[sp] = talk.get(sp, 0) + max(dur, 0)
        words[sp] = words.get(sp, 0) + len(text.split())
        lines[sp] = lines.get(sp, 0) + 1
        seller_hits[sp] = seller_hits.get(sp, 0) + len(SELLER_WORDS.findall(text))

    # менеджер — тот, в чьей речи чаще звучат слова продавца;
    # если таких слов нет вовсе, честно помечаем это в отчёте
    how = "по словам продавца"
    if not any(seller_hits.values()):
        manager = max(talk, key=talk.get)
        how = "по объёму речи (слов продавца не найдено)"
    else:
        manager = max(seller_hits, key=seller_hits.get)

    total = sum(talk.values()) or 1
    share = round(100 * talk.get(manager, 0) / total)

    # длинный монолог: подряд идущие реплики менеджера без вставок клиента
    longest, current = 0, 0
    for u in utts:
        if u.get("speaker") == manager:
            current += float(u.get("end", 0)) - float(u.get("start", 0))
            longest = max(longest, current)
        else:
            current = 0

    others = [s for s in talk if s != manager]
    client_lines = sum(lines.get(s, 0) for s in others)
    return {
        "share": share,
        "monolog": "%d:%02d" % (int(longest // 60), int(longest % 60)),
        "lines": "%d / %d" % (lines.get(manager, 0), client_lines),
        "minutes": round(total / 60),
        "how": how,
        "speakers": len(talk),
    }


# ---------- прогон ----------

def main():
    missing = [n for n, v in [("SHEET_ID", SHEET_ID), ("APIFY_TOKEN", APIFY_TOKEN),
                              ("DEEPGRAM_TOKEN", DEEPGRAM_TOKEN)] if not v]
    if missing:
        log("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    sheets = sheets_client()
    values = sheets.values()
    data = values.get(spreadsheetId=SHEET_ID,
                      range="'%s'!1:100000" % OKK_TAB).execute().get("values", [])
    hdr = data[0]
    for col in COLUMNS:
        if col not in hdr:
            hdr.append(col)
    values.update(spreadsheetId=SHEET_ID, range="'%s'!A1" % OKK_TAB,
                  valueInputOption="RAW", body={"values": [hdr]}).execute()
    first = hdr.index(COLUMNS[0])

    queue = []
    for i, raw in enumerate(data[1:]):
        row = dict(zip(hdr, raw + [""] * (len(hdr) - len(raw))))
        if not row.get("запись zoom", "").strip():
            continue
        if row.get(COLUMNS[0], "").strip():       # уже посчитано
            continue
        if MANAGERS and not any(m in row.get("менеджер", "").lower() for m in MANAGERS):
            continue
        queue.append((i + 2, row))

    log("встреч к обсчёту: %d, беру %d" % (len(queue), min(len(queue), LIMIT)))
    queue = queue[:LIMIT]

    ok = fail = 0
    for rownum, row in queue:
        rid = row.get("ID сделки", "?")
        path = None
        try:
            log("[%s] достаю запись..." % rid)
            item = apify_audio(row.get("запись zoom", ""), row.get("код доступа", ""))
            path = download(item)
            log("[%s] распознаю с разделением говорящих..." % rid)
            m = measure(transcribe(path))
            values.update(spreadsheetId=SHEET_ID,
                          range="'%s'!%s%d" % (OKK_TAB, letter(first), rownum),
                          valueInputOption="RAW",
                          body={"values": [[m["share"], m["monolog"], m["lines"],
                                            m["minutes"], m["how"]]]}).execute()
            ok += 1
            log("[%s] менеджер говорил %d%%, длинный монолог %s, говоривших %d"
                % (rid, m["share"], m["monolog"], m["speakers"]))
        except Exception as e:
            fail += 1
            log("[%s] ОШИБКА: %s: %s" % (rid, type(e).__name__, str(e)[:200]))
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    log("ГОТОВО. Посчитано: %d, ошибок: %d" % (ok, fail))


if __name__ == "__main__":
    main()
