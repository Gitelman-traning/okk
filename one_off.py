#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разбор одной встречи по прямой ссылке на запись — когда её нет в листе «дпд».

Нужно для эталонных встреч: РОП присылает ссылку и код доступа, мы считаем
по ней всё то же самое, что и по остальным — доли речи, счётчики, оценку модели.

Запуск: workflow «OKK one-off» с полями url / passcode / client / manager.
"""

import io
import json
import os
import sys
import time

import requests

import diarize
import okk_analyze as okk

URL = os.environ.get("REC_URL", "").strip()
PASSCODE = os.environ.get("REC_PASSCODE", "").strip()
CLIENT = os.environ.get("REC_CLIENT", "").strip() or "без имени"
MANAGER = os.environ.get("REC_MANAGER", "").strip()
HELD_AT = os.environ.get("REC_DATE", "").strip()


def log(*a):
    print(*a, flush=True)


def main():
    if not URL:
        log("ОШИБКА: не задан REC_URL")
        sys.exit(1)

    sheets = diarize.sheets_client()
    values = sheets.values()

    # REC_CACHE — файл с ответом Deepgram. Есть — не скачиваем и не распознаём заново
    # (перепрогон другой моделью стоит тогда только запрос к модели). Нет — создаём.
    cache = os.environ.get("REC_CACHE", "").strip()
    dg = None
    if cache and os.path.exists(cache):
        try:
            dg = json.load(io.open(cache, encoding="utf-8"))
            log("расшифровка взята из кэша: %s" % cache)
        except ValueError:
            dg = None
    if dg is None:
        log("достаю запись из Zoom...")
        item = diarize.apify_audio(URL, PASSCODE)
        path = diarize.download(item)
        try:
            log("распознаю с разделением говорящих...")
            dg = diarize.transcribe(path)
        finally:
            if os.path.exists(path):
                os.remove(path)
        if cache:
            d = os.path.dirname(cache)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            io.open(cache, "w", encoding="utf-8").write(json.dumps(dg, ensure_ascii=False))
            log("расшифровка сохранена в кэш")

    speech = diarize.measure(dg)
    alt = ((dg.get("results") or {}).get("channels") or [{}])[0].get("alternatives") or [{}]
    text = alt[0].get("transcript", "")
    log("расшифровка: %d символов, менеджер говорил %d%%" % (len(text), speech["share"]))

    metrics = okk.hard_metrics(text)
    headers = {"Authorization": "Bearer " + okk.OPENROUTER_API_KEY,
               "HTTP-Referer": "https://github.com/Gitelman-traning/okk",
               "X-Title": "OKK one-off"}
    # платным моделям отдаём расшифровку целиком: контекста хватает, а обрезка середины
    # прячет от модели презентацию и связку инструментов
    limit = int(os.environ.get("MAX_CHARS") or okk.MAX_CHARS)
    sent = text if len(text) <= limit else (text[:limit // 3] + "\n…\n" + text[-2 * limit // 3:])

    # REC_MODELS — список моделей через запятую: одна расшифровка, разбор каждой моделью,
    # по строке на модель (для сверки моделей между собой). Пусто — обычный выбор модели.
    wanted = [m.strip() for m in os.environ.get("REC_MODELS", "").split(",") if m.strip()]
    runs = [[m] for m in wanted] if wanted else [okk.pick_models(headers)]

    hdr = okk.ensure_tab(sheets)
    stamp = time.strftime("%d.%m.%Y %H:%M")
    done = 0
    for candidates in runs:
        try:
            review, model = okk.ask_model(candidates, headers, sent, metrics)
        except Exception as e:
            log("[%s] разбор не получился: %s: %s" % (candidates[0], type(e).__name__, str(e)[:200]))
            continue
        review = okk.normalize_checklist(review)
        review = okk.locate_quotes(review, alt[0].get("words") or [])
        located = sum(1 for c in review.get("checklist", []) if c.get("at"))
        log("[%s] таймингов у цитат чек-листа: %d из %d" % (model, located, len(review.get("checklist", []))))

        row = okk.to_row({"ID": "", "first_name": CLIENT, "last_name": "",
                          "amo_link": "", "doc_url": ""},
                         review, metrics, model, stamp,
                         {"manager": MANAGER, "held_at": HELD_AT, "zoom": URL, "passcode": PASSCODE})
        line = dict(zip(okk.OKK_HEADERS, row))
        line.update({"речь менеджера %": speech["share"], "длинный монолог": speech["monolog"],
                     "реплик менеджер/клиент": speech["lines"], "минут записи": speech["minutes"],
                     "как определён менеджер": speech["how"]})
        # такая встреча уже есть — перезаписываем, а не плодим дубли;
        # при сверке моделей ключ — клиент + модель
        existing = values.get(spreadsheetId=okk.MARKETING_SHEET_ID,
                              range="'%s'!A2:ZZ100000" % okk.OKK_TAB).execute().get("values", [])
        ci, mi = hdr.index("клиент"), hdr.index("модель")

        def cell(r, i):
            return r[i].strip() if i < len(r) else ""
        rownum = next((i + 2 for i, r in enumerate(existing)
                       if cell(r, ci) == CLIENT and (not wanted or cell(r, mi) == model)), None)
        payload = [[line.get(c, "") for c in hdr]]
        if rownum:
            values.update(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A%d" % (okk.OKK_TAB, rownum),
                          valueInputOption="RAW", body={"values": payload}).execute()
            log("[%s] строка %d перезаписана" % (model, rownum))
        else:
            values.append(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A1" % okk.OKK_TAB,
                          valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                          body={"values": payload}).execute()
        done += 1
        log("ГОТОВО [%s]: %s — цель %s, вероятность %s, фиксация %s, речь менеджера %d%%"
            % (model, CLIENT, review.get("goal_achieved"), review.get("probability"),
               next((s.get("score") for s in review.get("stages", []) if s.get("key") == "close"), "—"),
               speech["share"]))
    if not done:
        log("ОШИБКА: ни одна модель не дала разбор")
        sys.exit(1)


if __name__ == "__main__":
    main()
