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

    log("достаю запись из Zoom...")
    item = diarize.apify_audio(URL, PASSCODE)
    path = diarize.download(item)
    try:
        log("распознаю с разделением говорящих...")
        dg = diarize.transcribe(path)
    finally:
        if os.path.exists(path):
            os.remove(path)

    speech = diarize.measure(dg)
    alt = ((dg.get("results") or {}).get("channels") or [{}])[0].get("alternatives") or [{}]
    text = alt[0].get("transcript", "")
    log("расшифровка: %d символов, менеджер говорил %d%%" % (len(text), speech["share"]))

    metrics = okk.hard_metrics(text)
    headers = {"Authorization": "Bearer " + okk.OPENROUTER_API_KEY,
               "HTTP-Referer": "https://github.com/Gitelman-traning/okk",
               "X-Title": "OKK one-off"}
    models = okk.pick_models(headers)
    sent = text if len(text) <= okk.MAX_CHARS else (
        text[:okk.MAX_CHARS // 3] + "\n…\n" + text[-2 * okk.MAX_CHARS // 3:])
    review, model = okk.ask_model(models, headers, sent, metrics)

    # строка в лист «ОКК» — как у обычного прогона, плюс замеры речи
    okk.ensure_tab(sheets)
    hdr = values.get(spreadsheetId=okk.MARKETING_SHEET_ID,
                     range="'%s'!1:1" % okk.OKK_TAB).execute().get("values", [[]])[0]
    stamp = time.strftime("%d.%m.%Y %H:%M")
    row = okk.to_row({"ID": "", "first_name": CLIENT, "last_name": "",
                      "amo_link": "", "doc_url": ""},
                     review, metrics, model, stamp,
                     {"manager": MANAGER, "held_at": HELD_AT, "zoom": URL, "passcode": PASSCODE})
    line = dict(zip(okk.OKK_HEADERS, row))
    line.update({"речь менеджера %": speech["share"], "длинный монолог": speech["monolog"],
                 "реплик менеджер/клиент": speech["lines"], "минут записи": speech["minutes"],
                 "как определён менеджер": speech["how"]})
    values.append(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A1" % okk.OKK_TAB,
                  valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                  body={"values": [[line.get(c, "") for c in hdr]]}).execute()

    log("ГОТОВО: %s — цель %s, фиксация %s, речь менеджера %d%%, монолог %s"
        % (CLIENT, review.get("goal_achieved"),
           next((s.get("score") for s in review.get("stages", []) if s.get("key") == "close"), "—"),
           speech["share"], speech["monolog"]))


if __name__ == "__main__":
    main()
