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
ALT_TAB = os.environ.get("ALT_TAB", "ОКК модели").strip()
SA_FILE = os.environ.get("GOOGLE_SA_FILE", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TPL = os.environ.get("TPL", "dashboard.tpl.html")
TPL_STATS = os.environ.get("TPL_STATS", "stats.tpl.html")
TPL_GUIDE = os.environ.get("TPL_GUIDE", "guide.tpl.html")
TPL_CHARTS = os.environ.get("TPL_CHARTS", "charts.tpl.html")
TPL_MANAGERS = os.environ.get("TPL_MANAGERS", "managers.tpl.html")
TPL_COMPARE = os.environ.get("TPL_COMPARE", "compare.tpl.html")
# ручные оценки для сверки лежат в приватной папке, не в репозитории
MANUAL = os.environ.get("MANUAL_SCORES", os.path.join("..", "okk-docs", "manual-scores.json"))
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


def row_item(r):
    try:
        review = json.loads(r.get("json") or "{}")
    except ValueError:
        review = {}
    return {
        "date": r.get("дата разбора", ""),
        "model": r.get("модель", ""),
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
        "speech": num(r.get("речь менеджера %")),
        "monolog": clean(r.get("длинный монолог")),
        "lines": clean(r.get("реплик менеджер/клиент")),
        "minutes": num(r.get("минут записи")),
        "review": review,
    }


def read_tab(values, tab):
    data = []
    for attempt in range(4):        # Google иногда отвечает 503 — просто пробуем ещё раз
        try:
            data = values.get(spreadsheetId=SHEET_ID,
                              range="'%s'!1:100000" % tab).execute().get("values", [])
            break
        except Exception as e:
            if "Unable to parse range" in str(e):
                return []          # такого листа нет
            if attempt == 3:
                raise
            print("повтор чтения таблицы (%s)" % type(e).__name__)
            time.sleep(5)
    if not data:
        return []
    hdr = data[0]
    return [dict(zip(hdr, r + [""] * (len(hdr) - len(r)))) for r in data[1:]]


def main():
    values = build("sheets", "v4", credentials=creds(),
                   cache_discovery=False).spreadsheets().values()
    rows = read_tab(values, OKK_TAB)
    if not rows:
        raise SystemExit("лист «%s» пуст" % OKK_TAB)
    # прогоны эталонов разными моделями — для сверки моделей между собой
    alts = [row_item(r) for r in read_tab(values, ALT_TAB)]

    items, generated, model = [], "", ""
    for r in rows:
        generated = as_date(r.get("дата разбора")) or generated
        model = r.get("модель") or model
        items.append(row_item(r))

    manual = {}
    if os.path.exists(MANUAL):
        manual = json.loads(io.open(MANUAL, encoding="utf-8").read())
    elements = []
    if os.path.exists("checklist.local.json"):
        cfg = json.loads(io.open("checklist.local.json", encoding="utf-8").read())
        elements = [{"id": e["id"], "name": e["name"]} for e in cfg.get("elements", [])]
    payload = {"generated": generated, "model": model, "items": items, "manual": manual,
               "elements": elements, "alts": alts}
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

    for tpl_name, fname in ((TPL_MANAGERS, "managers.html"), (TPL_COMPARE, "compare.html")):
        if os.path.exists(tpl_name):
            page_tpl = io.open(tpl_name, encoding="utf-8").read()
            io.open(os.path.join(out_dir, fname), "w", encoding="utf-8").write(
                page_tpl.replace("/*__DATA__*/", data))

    guide_out = os.path.join(out_dir, "guide.html")
    guide_tpl = io.open(TPL_GUIDE, encoding="utf-8").read()
    io.open(guide_out, "w", encoding="utf-8").write(guide_tpl.replace("/*__DATA__*/", data))

    managers = len({i["mgr"] for i in items if i["mgr"]})
    print("готово: %s и %s, встреч %d, менеджеров %d, модель %s"
          % (OUT, stats_out, len(items), managers, model))


if __name__ == "__main__":
    main()
