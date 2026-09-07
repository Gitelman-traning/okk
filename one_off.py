#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разбор одной встречи по прямой ссылке на запись — когда её нет в листе «дпд».

Нужно для эталонных встреч: РОП присылает ссылку и код доступа, мы считаем
по ней всё то же самое, что и по остальным — доли речи, счётчики, оценку модели,
личный скрипт менеджера, оценки клиента.

Запуск: workflow «OKK one-off» с полями url / passcode / client / manager.
Пакетный прогон по листу ZOOM — batch_off.py, он использует функции отсюда.
"""

import io
import json
import os
import sys
import time

import diarize
import okk_analyze as okk

URL = os.environ.get("REC_URL", "").strip()
PASSCODE = os.environ.get("REC_PASSCODE", "").strip()
CLIENT = os.environ.get("REC_CLIENT", "").strip() or "без имени"
MANAGER = os.environ.get("REC_MANAGER", "").strip()
HELD_AT = os.environ.get("REC_DATE", "").strip()

SPEECH_COLS = {"share": "речь менеджера %", "monolog": "длинный монолог",
               "lines": "реплик менеджер/клиент", "minutes": "минут записи",
               "how": "как определён менеджер"}


def log(*a):
    print(*a, flush=True)


def or_headers(title="OKK one-off"):
    return {"Authorization": "Bearer " + okk.OPENROUTER_API_KEY,
            "HTTP-Referer": "https://github.com/Gitelman-traning/okk",
            "X-Title": title}


def get_transcript(url, passcode, cache=""):
    """Ответ Deepgram с говорящими. cache — файл: есть — берём из него (без Apify и Deepgram),
    нет — скачиваем, распознаём и сохраняем."""
    if cache and os.path.exists(cache):
        try:
            dg = json.load(io.open(cache, encoding="utf-8"))
            log("расшифровка взята из кэша: %s" % cache)
            return dg
        except ValueError:
            pass
    log("достаю запись из Zoom...")
    item = diarize.apify_audio(url, passcode)
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
    return dg


def analyze(dg, deal, runs, headers):
    """Разбор одной расшифровки. deal: id, client, manager, held_at, url, passcode, amo, source, turnover.
    runs — список списков моделей-кандидатов: по одной строке результата на каждый список.
    Возвращает [(line, review, model)], line — словарь по именам колонок листа."""
    speech = diarize.measure(dg)
    alt = ((dg.get("results") or {}).get("channels") or [{}])[0].get("alternatives") or [{}]
    text = alt[0].get("transcript", "")
    words = alt[0].get("words") or []
    log("расшифровка: %d символов, менеджер говорил %d%%" % (len(text), speech["share"]))

    metrics = okk.hard_metrics(text)
    # платным моделям отдаём расшифровку целиком: контекста хватает, а обрезка середины
    # прячет от модели презентацию и связку инструментов
    limit = int(os.environ.get("MAX_CHARS") or okk.MAX_CHARS)
    sent = text if len(text) <= limit else (text[:limit // 3] + "\n…\n" + text[-2 * limit // 3:])
    script = okk.script_for(deal.get("manager", ""))
    if script:
        log("личный скрипт: %s (%d пунктов)" % (script.get("name", ""), len(script.get("items", []))))
    stamp = time.strftime("%d.%m.%Y %H:%M")

    out = []
    for candidates in runs:
        try:
            review, model = okk.ask_model(candidates, headers, sent, metrics, script)
        except Exception as e:
            log("[%s] разбор не получился: %s: %s" % (candidates[0], type(e).__name__, str(e)[:200]))
            continue
        review = okk.normalize_checklist(review)
        review = okk.normalize_script(review, script, text)
        review["speech"] = speech
        review = okk.locate_quotes(review, words)
        located = sum(1 for c in review.get("checklist", []) if c.get("at"))
        log("[%s] таймингов у цитат чек-листа: %d из %d, стоимость $%s" % (
            model, located, len(review.get("checklist", [])), okk.LAST_USAGE.get("cost")))

        row = okk.to_row({"ID": deal.get("id", ""), "first_name": deal.get("client", ""), "last_name": "",
                          "amo_link": deal.get("amo", ""), "doc_url": ""},
                         review, metrics, model, stamp,
                         {"manager": deal.get("manager", ""), "held_at": deal.get("held_at", ""),
                          "zoom": deal.get("url", ""), "passcode": deal.get("passcode", ""),
                          "source": deal.get("source", ""), "turnover": deal.get("turnover", "")})
        line = dict(zip(okk.OKK_HEADERS, row))
        line.update({col: speech[k] for k, col in SPEECH_COLS.items()})
        out.append((line, review, model))
    return out


def write_line(values, hdr, line, key_id="", key_client="", key_model=""):
    """Строка в лист: такая встреча уже есть — перезаписываем, а не плодим дубли.
    Ключ — ID сделки (если есть) или клиент; при сверке моделей ещё и модель."""
    existing = values.get(spreadsheetId=okk.MARKETING_SHEET_ID,
                          range="'%s'!A2:ZZ100000" % okk.OKK_TAB).execute().get("values", [])
    ci, mi, ii = hdr.index("клиент"), hdr.index("модель"), hdr.index("ID сделки")

    def cell(r, i):
        return r[i].strip() if i < len(r) else ""

    def match(r):
        ok = (cell(r, ii) == key_id) if key_id else (cell(r, ci) == key_client)
        return ok and (not key_model or cell(r, mi) == key_model)

    rownum = next((i + 2 for i, r in enumerate(existing) if match(r)), None)
    payload = [[line.get(c, "") for c in hdr]]
    if rownum:
        values.update(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A%d" % (okk.OKK_TAB, rownum),
                      valueInputOption="RAW", body={"values": payload}).execute()
        return "строка %d перезаписана" % rownum
    values.append(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A1" % okk.OKK_TAB,
                  valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                  body={"values": payload}).execute()
    return "строка добавлена"


def main():
    if not URL:
        log("ОШИБКА: не задан REC_URL")
        sys.exit(1)

    sheets = diarize.sheets_client()
    values = sheets.values()
    dg = get_transcript(URL, PASSCODE, os.environ.get("REC_CACHE", "").strip())

    headers = or_headers()
    # REC_MODELS — список моделей через запятую: одна расшифровка, разбор каждой моделью,
    # по строке на модель (для сверки моделей между собой). Пусто — обычный выбор модели.
    wanted = [m.strip() for m in os.environ.get("REC_MODELS", "").split(",") if m.strip()]
    runs = [[m] for m in wanted] if wanted else [okk.pick_models(headers)]

    hdr = okk.ensure_tab(sheets)
    deal = {"id": "", "client": CLIENT, "manager": MANAGER, "held_at": HELD_AT,
            "url": URL, "passcode": PASSCODE}
    results = analyze(dg, deal, runs, headers)
    for line, review, model in results:
        log("[%s] %s" % (model, write_line(values, hdr, line, "", CLIENT, model if wanted else "")))
        log("ГОТОВО [%s]: %s — цель %s, вероятность %s, фиксация %s, речь менеджера %s%%"
            % (model, CLIENT, review.get("goal_achieved"), review.get("probability"),
               next((s.get("score") for s in review.get("stages", []) if s.get("key") == "close"), "—"),
               line.get("речь менеджера %")))
    if not results:
        log("ОШИБКА: ни одна модель не дала разбор")
        sys.exit(1)


if __name__ == "__main__":
    main()
