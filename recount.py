#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пересчитывает счётчики по уже разобранным встречам — без обращения к модели.

Нужно, когда поменялся чек-лист: оценки этапов остаются прежними, а всё, что
считает код (обязательные формулировки, вопросы, объём), обновляется по свежим
правилам. Дешевле и честнее, чем гонять разбор заново.

Запуск локально:
  SHEET_ID=... GOOGLE_SA_FILE=путь_к_ключу.json python recount.py
"""

import json
import os
import re

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import okk_analyze as okk

SA_FILE = os.environ.get("GOOGLE_SA_FILE", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

# колонка в листе → что в неё кладём
COLUMNS = ["комитет (раз)", "комитет по трети", "даты и ограниченность",
           "запрет: каждый месяц", "вопросов", "слов"]


def letter(idx0):
    s, n = "", idx0 + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive.readonly"]
    creds = (Credentials.from_service_account_file(SA_FILE, scopes=scopes) if SA_FILE
             else Credentials.from_service_account_info(json.loads(SA_JSON), scopes=scopes))
    values = build("sheets", "v4", credentials=creds,
                   cache_discovery=False).spreadsheets().values()
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)

    data = values.get(spreadsheetId=okk.MARKETING_SHEET_ID,
                      range="'%s'!1:100000" % okk.OKK_TAB).execute().get("values", [])
    if not data:
        raise SystemExit("лист пуст")
    hdr = data[0]
    missing = [c for c in COLUMNS if c not in hdr]
    if missing:
        raise SystemExit("в листе нет колонок: %s" % ", ".join(missing))

    updates, done, skipped = [], 0, 0
    for i, raw in enumerate(data[1:]):
        rownum = i + 2
        row = dict(zip(hdr, raw + [""] * (len(hdr) - len(raw))))
        doc_url = row.get("расшифровка", "")
        if not doc_url:
            skipped += 1
            continue
        text = okk.read_doc(docs, doc_url)
        if len(text) < 2000:
            skipped += 1
            continue
        m = okk.hard_metrics(text)
        ms = {x["id"]: x for x in m["must_say"]}
        mn = {x["id"]: x for x in m["must_not_say"]}
        cells = [
            ms.get("komitet", {}).get("count", 0),
            "да" if ms.get("komitet", {}).get("spread_ok") else "нет",
            "да" if (ms.get("stream_dates", {}).get("ok") and ms.get("scarcity", {}).get("ok")) else "нет",
            "нарушено" if not mn.get("monthly", {}).get("ok", True) else "чисто",
            m["questions"], m["words"],
        ]
        # колонки идут подряд — пишем одним диапазоном
        first = hdr.index(COLUMNS[0])
        updates.append({"range": "'%s'!%s%d" % (okk.OKK_TAB, letter(first), rownum),
                        "values": [cells]})
        done += 1
        print("[%s] комитет %s, даты и дефицит %s" % (row.get("ID сделки", "?"), cells[0], cells[2]))

    if updates:
        values.batchUpdate(spreadsheetId=okk.MARKETING_SHEET_ID,
                           body={"valueInputOption": "RAW", "data": updates}).execute()
    print("пересчитано: %d, пропущено: %d" % (done, skipped))


if __name__ == "__main__":
    main()
