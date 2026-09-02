#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Собирает дашборд из листа «ОКК»: подставляет разборы в шаблон dashboard.tpl.html.

Результат — обычная html-страница с данными клиентов, поэтому кладём её ВНЕ
публичного репозитория: путь задаётся переменной OUT (по умолчанию ../okk-docs).

Запуск локально:  python build_dashboard.py
"""

import datetime
import html
import io
import json
import os
import re
import time

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = os.environ.get("SHEET_ID", "").strip()
OKK_TAB = os.environ.get("OKK_TAB", "ОКК").strip()
SA_FILE = os.environ.get("GOOGLE_SA_FILE", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TPL = os.environ.get("TPL", "dashboard.tpl.html")
TPL_STATS = os.environ.get("TPL_STATS", "stats.tpl.html")
TPL_GUIDE = os.environ.get("TPL_GUIDE", "guide.tpl.html")
TPL_CHARTS = os.environ.get("TPL_CHARTS", "charts.tpl.html")
OUT = os.environ.get("OUT", os.path.join("..", "okk-docs", "dashboard.html"))
OUT_STATS = os.environ.get("OUT_STATS", "")   # пусто — кладём рядом с OUT как stats.html

STAGE_COLS = ["контакт", "потребности", "презентация", "возражения", "фиксация"]


def clean(s):
    """В исходной таблице встречаются html-сущности вида &amp; — разворачиваем."""
    return html.unescape(str(s or "")).strip()


def as_date(s):
    """Google мог сохранить дату числом (серийный формат) — возвращаем читаемую строку."""
    s = str(s or "").strip()
    m = re.match(r"^(\d+)[.,](\d+)$", s)
    if not m:
        return s
    base = datetime.datetime(1899, 12, 30)
    return (base + datetime.timedelta(days=float(s.replace(",", ".")))).strftime("%d.%m.%Y %H:%M")


def num(x):
    try:
        return int(float(str(x).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def creds():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    if SA_FILE:
        return Credentials.from_service_account_file(SA_FILE, scopes=scopes)
    return Credentials.from_service_account_info(json.loads(SA_JSON), scopes=scopes)


def main():
    values = build("sheets", "v4", credentials=creds(),
                   cache_discovery=False).spreadsheets().values()
    data = []
    for attempt in range(4):        # Google иногда отвечает 503 — просто пробуем ещё раз
        try:
            data = values.get(spreadsheetId=SHEET_ID,
                              range="'%s'!1:100000" % OKK_TAB).execute().get("values", [])
            break
        except Exception as e:
            if attempt == 3:
                raise
            print("повтор чтения таблицы (%s)" % type(e).__name__)
            time.sleep(5)
    if not data:
        raise SystemExit("лист «%s» пуст" % OKK_TAB)
    hdr = data[0]
    rows = [dict(zip(hdr, r + [""] * (len(hdr) - len(r)))) for r in data[1:]]

    items, generated, model = [], "", ""
    for r in rows:
        generated = as_date(r.get("дата разбора")) or generated
        model = r.get("модель") or model
        try:
            review = json.loads(r.get("json") or "{}")
        except ValueError:
            review = {}
        items.append({
            "date": r.get("дата разбора", ""),
            "client": clean(r.get("клиент")) or "без имени",
            "deal_id": r.get("ID сделки", ""),
            "amo": r.get("ссылка amo", ""),
            "doc": r.get("расшифровка", ""),
            "goal": clean(r.get("цель встречи")),
            "achieved": r.get("цель достигнута", ""),
            "why": clean(r.get("почему не достигнута")),
            "prob": num(r.get("вероятность")),
            "next": clean(r.get("следующий шаг")),
            "next_date": r.get("дата в шаге") == "да",
            "dm": r.get("ЛПР", ""),
            "s": [num(r.get(c)) for c in STAGE_COLS],
            "komitet": num(r.get("комитет (раз)")) or 0,
            "komitet_spread": r.get("комитет по трети") == "да",
            "scarcity": r.get("даты и ограниченность") == "да",
            "banned": r.get("запрет: каждый месяц") == "нарушено",
            "questions": num(r.get("вопросов")) or 0,
            "words": num(r.get("слов")) or 0,
            "summary": clean(r.get("резюме")),
            "mgr": clean(r.get("менеджер")),
            "held": clean(r.get("дата встречи")),
            "source": clean(r.get("источник")),
            "turnover": clean(r.get("оборот")),
            "zoom": clean(r.get("запись zoom")),
            "passcode": clean(r.get("код доступа")),
            "review": review,
        })

    payload = {"generated": generated, "model": model, "items": items}
    tpl = io.open(TPL, encoding="utf-8").read()
    data = json.dumps(payload, ensure_ascii=False)

    out_dir = os.path.dirname(OUT)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    io.open(OUT, "w", encoding="utf-8").write(tpl.replace("/*__DATA__*/", data))

    stats_out = OUT_STATS or os.path.join(out_dir, "stats.html")
    stats_tpl = io.open(TPL_STATS, encoding="utf-8").read()
    io.open(stats_out, "w", encoding="utf-8").write(stats_tpl.replace("/*__DATA__*/", data))

    charts_out = os.path.join(out_dir, "charts.html")
    charts_tpl = io.open(TPL_CHARTS, encoding="utf-8").read()
    io.open(charts_out, "w", encoding="utf-8").write(charts_tpl.replace("/*__DATA__*/", data))

    guide_out = os.path.join(out_dir, "guide.html")
    guide_tpl = io.open(TPL_GUIDE, encoding="utf-8").read()
    io.open(guide_out, "w", encoding="utf-8").write(guide_tpl.replace("/*__DATA__*/", data))

    managers = len({i["mgr"] for i in items if i["mgr"]})
    print("готово: %s и %s, встреч %d, менеджеров %d, модель %s"
          % (OUT, stats_out, len(items), managers, model))


if __name__ == "__main__":
    main()
