#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пакетный разбор встреч по записям из листа ZOOM: Apify → Deepgram с говорящими → модель → лист «ОКК».

В отличие от okk_analyze.py не требует готовой расшифровки в «дпд» — сам распознаёт запись.
Нужен для менеджеров, чьи встречи в «дпд» не попали, и для прогона свежих встреч сильной моделью.

Фильтры (переменные окружения): MANAGERS — часть имени через запятую; SINCE — дата dd.mm.yy,
берём встречи не раньше неё; LIMIT — сколько встреч; REDO=1 — перезаписать уже разобранные.
Ответы Deepgram складываются в CACHE_DIR/<ID>.json — workflow сохраняет их артефактом.
"""

import io
import json
import os
import re
import sys
import time

import diarize
import okk_analyze as okk
import one_off

MANAGERS = [m.strip().lower() for m in os.environ.get("MANAGERS", "").split(",") if m.strip()]
SINCE = os.environ.get("SINCE", "").strip()
LIMIT = int(os.environ.get("LIMIT") or "10")
REDO = os.environ.get("REDO", "").strip().lower() in ("1", "true", "yes")
CACHE_DIR = os.environ.get("CACHE_DIR", "cache").strip()
PAUSE = int(os.environ.get("PAUSE") or "5")


def log(*a):
    print(*a, flush=True)


def dkey(s):
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s or "")
    if not m:
        return ""
    y = m.group(3)[-2:]
    return "%s%02d%02d" % (y, int(m.group(2)), int(m.group(1)))


def zoom_rows(values):
    data = values.get(spreadsheetId=okk.MARKETING_SHEET_ID,
                      range="'%s'!%d:100000" % (okk.ZOOM_TAB, okk.ZOOM_HEADER_ROW)).execute().get("values", [])
    if not data:
        return []
    hdr = data[0]
    out = []
    for raw in data[1:]:
        r = dict(zip(hdr, raw + [""] * (len(hdr) - len(raw))))
        rid = str(r.get("ID") or "").strip()
        if not rid or not r.get("Ссылка zoom запись", "").strip():
            continue
        out.append({"id": rid, "client": (r.get("Основной контакт") or "").strip() or "без имени",
                    "manager": r.get("Ответственный", "").strip(),
                    "held_at": r.get("Дата Диагностика проведена", "").strip(),
                    "url": r.get("Ссылка zoom запись", "").strip(), "passcode": r.get("Код доступа", "").strip(),
                    "amo": r.get("ссылка", "").strip(), "source": r.get("Источник", "").strip(),
                    "turnover": r.get("Оборот млн. руб.", "").strip()})
    return out


def main():
    missing = [n for n, v in [("SHEET_ID", okk.MARKETING_SHEET_ID), ("APIFY_TOKEN", diarize.APIFY_TOKEN),
                              ("DEEPGRAM_TOKEN", diarize.DEEPGRAM_TOKEN),
                              ("OPENROUTER_API_KEY", okk.OPENROUTER_API_KEY)] if not v]
    if missing:
        log("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    sheets = diarize.sheets_client()
    values = sheets.values()
    hdr = okk.ensure_tab(sheets)
    done_rows = values.get(spreadsheetId=okk.MARKETING_SHEET_ID,
                           range="'%s'!A2:B100000" % okk.OKK_TAB).execute().get("values", [])
    done = {r[1].strip() for r in done_rows if len(r) > 1 and r[1].strip()}

    queue = []
    for d in zoom_rows(values):
        if MANAGERS and not any(m in d["manager"].lower() for m in MANAGERS):
            continue
        if SINCE and dkey(d["held_at"]) < dkey(SINCE):
            continue
        if d["id"] in done and not REDO:
            continue
        queue.append(d)
    queue.sort(key=lambda d: dkey(d["held_at"]), reverse=True)     # свежие первыми
    log("встреч под фильтр: %d, беру %d" % (len(queue), min(len(queue), LIMIT)))
    queue = queue[:LIMIT]
    if not queue:
        return

    headers = one_off.or_headers("OKK batch")
    models = okk.pick_models(headers)
    ok = fail = 0
    for d in queue:
        rid = d["id"]
        try:
            log("[%s] %s · %s · %s" % (rid, d["held_at"], d["manager"], d["client"][:30]))
            dg = one_off.get_transcript(d["url"], d["passcode"], os.path.join(CACHE_DIR, rid + ".json"))
            results = one_off.analyze(dg, d, [models], headers)
            if not results:
                raise RuntimeError("модель не дала разбор")
            line, review, model = results[0]
            log("[%s] %s" % (rid, one_off.write_line(values, hdr, line, key_id=rid)))
            log("[%s] готово: цель %s, вероятность %s, скрипт %s" % (
                rid, review.get("goal_achieved"), review.get("probability"), line.get("скрипт: выполнено") or "—"))
            ok += 1
        except Exception as e:
            fail += 1
            log("[%s] ОШИБКА: %s: %s" % (rid, type(e).__name__, str(e)[:300]))
        time.sleep(PAUSE)
    log("ГОТОВО. Разобрано: %d, ошибок: %d, модель: %s" % (ok, fail, models[0] if models else "—"))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
