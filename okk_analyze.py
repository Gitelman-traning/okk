#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ОКК: разбор диагностик по расшифровкам.

  1. Читает лист «дпд» таблицы «маркетинг» — там ссылки на Google Docs с расшифровками
     зум-встреч, ключ связки со сделкой amoCRM — колонка ID.
  2. Достаёт текст расшифровки из документа.
  3. Считает метрики кодом (счётчики обязательных слов, объём речи, вопросы).
  4. Отправляет расшифровку в LLM через OpenRouter, получает строгий JSON по чек-листу.
  5. Пишет строку в лист «ОКК» и складывает out/okk.json — из него собирается дашборд.

Запуск: workflow «ОКК: разбор диагностик» (ручной, с лимитом встреч за прогон).
"""

import io
import json
import os
import re
import sys
import time

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

# Идентификаторы и чек-лист — не в коде: репозиторий публичный.
# SHEET_ID приходит переменной окружения, чек-лист — секретом CHECKLIST_JSON
# (локально можно положить рядом файл checklist.local.json, он в .gitignore).
MARKETING_SHEET_ID = os.environ.get("SHEET_ID", "").strip()
DPD_TAB = os.environ.get("DPD_TAB", "дпд").strip()        # ссылки на расшифровки
ZOOM_TAB = os.environ.get("ZOOM_TAB", "ZOOM").strip()     # менеджер, дата встречи, запись
ZOOM_HEADER_ROW = int(os.environ.get("ZOOM_HEADER_ROW") or "1931")
OKK_TAB = os.environ.get("OKK_TAB", "ОКК").strip()        # сюда пишем разбор

OR_URL = "https://openrouter.ai/api/v1"
# Приоритет вендоров: чем раньше в списке, тем охотнее берём.
# Порядок выставлен по бесплатному пулу — у gemma и nemotron лимиты выбираются
# первыми, поэтому крупные модели с большим контекстом идут впереди.
MODEL_PREFS = ("minimax", "z-ai", "thinkingmachines", "deepseek", "qwen",
               "inclusionai", "dots-studio", "cohere", "google", "nvidia")
MIN_CONTEXT = 60000      # расшифровка на 1,5 часа — это 20–30 тыс. токенов

MAX_CHARS = 45000        # обрезка расшифровки перед отправкой (хвост важнее — там закрытие)
LIMIT = int(os.environ.get("LIMIT") or "5")
PAUSE = int(os.environ.get("PAUSE") or "8")   # пауза между встречами, сек
MODEL_ENV = os.environ.get("OKK_MODEL", "").strip()
# Разбирать только встречи выбранных менеджеров: "Камилла,Мурад" — совпадение по части фамилии.
MANAGERS = [m.strip().lower() for m in os.environ.get("MANAGERS", "").split(",") if m.strip()]

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

OUT_DIR = "out"

# ============================================================
#  ЧЕК-ЛИСТ (черновик, см. CHECKLIST-draft.md — правится вместе с РОПом)
# ============================================================

# Обязательные упоминания проверяются кодом: модель к ним не допускается,
# счётчик ошибиться не может. Сами формулировки — коммерческая часть, они
# лежат в конфиге, а не в этом файле.
#
# Формат конфига:
# {
#   "context": "чем занимается компания и что продаём — идёт в промпт",
#   "must_say":     [{"id","name","patterns":[regex],"min":1,"all":true}],
#     "all": true — засчитываем, только если сработал каждый шаблон (например,
#     «назван месяц» И «названо число»); по умолчанию считается сумма совпадений
#   "must_not_say": [{"id","name","patterns":[regex]}]
# }

def load_checklist():
    raw = os.environ.get("CHECKLIST_JSON", "").strip()
    if not raw and os.path.exists("checklist.local.json"):
        raw = io.open("checklist.local.json", encoding="utf-8").read()
    if not raw:
        raise RuntimeError(
            "нет чек-листа: задай секрет CHECKLIST_JSON или положи checklist.local.json")
    cfg = json.loads(raw)
    return (cfg.get("context", ""), cfg.get("must_say", []),
            cfg.get("must_not_say", []), cfg.get("norms", {}), cfg.get("elements", []))


CONTEXT, MUST_SAY, MUST_NOT_SAY, NORMS, ELEMENTS = load_checklist()

# Перезаписывать уже разобранные встречи (после смены чек-листа), а не пропускать их.
REDO = os.environ.get("REDO", "").strip().lower() in ("1", "true", "yes")

STAGES = [
    ("contact", "Установление контакта"),
    ("needs", "Выявление потребностей"),
    ("present", "Презентация"),
    ("objections", "Отработка возражений"),
    ("close", "Фиксация договорённостей"),
]

SYSTEM_PROMPT = """Ты — методист отдела контроля качества.
Разбираешь расшифровку встречи менеджера с клиентом.

""" + CONTEXT + """

ЖЁСТКИЕ ПРАВИЛА
1. Опирайся только на текст расшифровки. Ничего не додумывай.
2. Каждая оценка этапа подкрепляется цитатой из расшифровки. Нет подходящей цитаты —
   ставь "quote": "" и score: null. Пустой критерий лучше выдуманного.
3. Расшифровка автоматическая, в ней есть ошибки распознавания. Смысл восстанавливай,
   но факты не выдумывай: не уверен — оставляй поле пустым.
4. Реплики не размечены по говорящим. Определяй по смыслу, кто менеджер, а кто клиент.
   Если определить нельзя — так и пиши в поле notes.
5. Отвечай ТОЛЬКО валидным JSON по схеме, без markdown-обёртки и пояснений.
6. Для каждого ОБЯЗАТЕЛЬНОГО ЭЛЕМЕНТА (список ниже) отвечай двумя признаками:
   "asked" — менеджер сам спросил или сам проговорил это;
   "present" — тема в итоге прозвучала в диалоге (любой стороной, в том числе клиент рассказал
   сам, не дожидаясь вопроса). "present" не может быть false, если "asked" true.
   К каждому элементу — короткая цитата из расшифровки, подтверждающая ответ; нет цитаты — оба false.

ОБЯЗАТЕЛЬНЫЕ ЭЛЕМЕНТЫ
""" + "\n".join("- %s — %s. %s" % (e["id"], e["name"], e.get("hint", "")) for e in ELEMENTS) + """

СХЕМА
{
  "goal": "цель встречи одной фразой",
  "goal_achieved": "Да" | "Частично" | "Нет",
  "goal_reason": "почему не достигнута; пусто, если достигнута",
  "summary": "резюме встречи, 3-5 предложений: о чём договорились, что мешает",
  "probability": число 0-100,
  "probability_why": "на чём основана оценка",
  "next_step": "следующий шаг словами менеджера",
  "next_step_has_date": true | false,
  "decision_maker": "Да" | "Нет" | "Неизвестно",
  "facts": ["факты о клиенте: ниша, размер команды, роль, ситуация"],
  "pains": ["боли и потребности словами клиента"],
  "objections": ["возражения клиента"],
  "stages": [
    {"key": "contact",    "score": 0-100 или null, "good": "", "bad": "", "fix": "", "quote": ""},
    {"key": "needs",      "score": 0-100 или null, "good": "", "bad": "", "fix": "", "quote": ""},
    {"key": "present",    "score": 0-100 или null, "good": "", "bad": "", "fix": "", "quote": ""},
    {"key": "objections", "score": 0-100 или null, "good": "", "bad": "", "fix": "", "quote": ""},
    {"key": "close",      "score": 0-100 или null, "good": "", "bad": "", "fix": "", "quote": ""}
  ],
  "checklist": [
    {"id": "<id элемента из списка>", "asked": true|false, "present": true|false, "quote": ""}
    // по одному объекту на КАЖДЫЙ элемент из списка, в том же порядке
  ],
  "notes": "что помешало разобрать встречу, если мешало"
}"""


# ============================================================
#  Хелперы
# ============================================================

def log(*a):
    print(*a, flush=True)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN,
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                            "disable_web_page_preview": True}, timeout=30)
    except Exception as ex:
        log("Telegram: %s" % ex)


def google_clients():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.readonly"])
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    return sheets, docs


def read_doc(docs, url):
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", url or "")
    if not m:
        return ""
    for attempt in range(3):
        try:
            d = docs.documents().get(documentId=m.group(1)).execute()
            break
        except Exception as e:
            if attempt == 2:
                raise
            log("   повтор чтения документа (%s)" % type(e).__name__)
            time.sleep(5)
    return "".join(
        el.get("textRun", {}).get("content", "")
        for c in d.get("body", {}).get("content", [])
        for el in c.get("paragraph", {}).get("elements", [])
    )


def read_zoom_meta(values):
    """ID сделки → менеджер, дата встречи, запись. Всё это есть в листе ZOOM."""
    data = values.get(spreadsheetId=MARKETING_SHEET_ID,
                      range="'%s'!%d:100000" % (ZOOM_TAB, ZOOM_HEADER_ROW)).execute().get("values", [])
    if not data:
        return {}
    hdr = data[0]
    meta = {}
    for raw in data[1:]:
        row = dict(zip(hdr, raw + [""] * (len(hdr) - len(raw))))
        rid = str(row.get("ID") or "").strip()
        if not rid:
            continue
        meta[rid] = {
            "amo": row.get("ссылка", "").strip(),
            "manager": row.get("Ответственный", "").strip(),
            "held_at": row.get("Дата Диагностика проведена", "").strip(),
            "source": row.get("Источник", "").strip(),
            "turnover": row.get("Оборот млн. руб.", "").strip(),
            "zoom": row.get("Ссылка zoom запись", "").strip(),
            "passcode": row.get("Код доступа", "").strip(),
        }
    return meta


# ---------- метрики кодом ----------

def count_patterns(text, patterns):
    """Сколько раз встретился любой из шаблонов + позиции (доля от длины текста)."""
    hits = []
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            hits.append(m.start())
    hits.sort()
    return hits


def hard_metrics(text):
    """Всё, что можно посчитать без модели. Здесь ошибиться нельзя — и модель сюда не лезет."""
    n = max(len(text), 1)
    words = len(text.split())
    res = {
        "chars": len(text),
        "words": words,
        "questions": text.count("?"),
        "must_say": [],
        "must_not_say": [],
        # ролей в расшифровке нет — доля речи считается только после
        # повторной транскрибации с диаризацией
        "speech_share": None,
    }
    qmin = NORMS.get("questions_min")
    if qmin:
        res["questions_norm"] = qmin
        res["questions_ok"] = res["questions"] >= qmin
    for rule in MUST_SAY:
        hits = count_patterns(text, rule["patterns"])
        if rule.get("all"):
            each = [len(count_patterns(text, [p])) for p in rule["patterns"]]
            rule_ok = all(c > 0 for c in each)
        else:
            rule_ok = None
        thirds = sorted({int(3 * h / n) for h in hits})
        item = {"id": rule["id"], "name": rule["name"], "count": len(hits),
                "thirds": thirds, "spread_ok": len(thirds) >= 3,
                "ok": rule_ok if rule_ok is not None else len(hits) >= rule.get("min", 1)}
        if rule.get("spread_required"):
            item["ok"] = item["ok"] and item["spread_ok"]
        res["must_say"].append(item)
    for rule in MUST_NOT_SAY:
        hits = count_patterns(text, rule["patterns"])
        res["must_not_say"].append({"id": rule["id"], "name": rule["name"],
                                    "count": len(hits), "ok": len(hits) == 0})
    return res


# ---------- OpenRouter ----------

def pick_models(headers):
    """Список кандидатов: бесплатные модели с достаточным контекстом, в порядке приоритета.
    Бесплатные лимиты выбираются быстро и по-разному у разных вендоров, поэтому
    держим запасные — на 429 переходим к следующей."""
    if MODEL_ENV:
        log("модель задана вручную: %s" % MODEL_ENV)
        return [MODEL_ENV]
    r = requests.get(OR_URL + "/models", headers=headers, timeout=60)
    r.raise_for_status()
    models = r.json().get("data", [])
    free = [m for m in models
            if m.get("id", "").endswith(":free")
            and int(m.get("context_length") or 0) >= MIN_CONTEXT]
    if not free:
        raise RuntimeError("бесплатных моделей с контекстом от %d не нашлось" % MIN_CONTEXT)

    def rank(m):
        vendor = m["id"].split("/")[0]
        pref = MODEL_PREFS.index(vendor) if vendor in MODEL_PREFS else len(MODEL_PREFS)
        return (pref, -int(m.get("context_length") or 0))

    free.sort(key=rank)
    ids = [m["id"] for m in free[:10]]
    log("бесплатных моделей подходит: %d, кандидаты: %s" % (len(free), ", ".join(ids)))
    return ids


def ask_model(models, headers, transcript, metrics):
    prompt = (
        "Расшифровка встречи (автоматическая, без разметки говорящих):\n\n"
        + transcript
        + "\n\n---\nСчётчики, посчитанные программой (им доверяй, они точные):\n"
        + json.dumps(metrics, ensure_ascii=False, indent=1)
        + "\n\nРазбери встречу по схеме."
    )
    body = {
        "model": None,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    last = ""
    for model in models:
        body["model"] = model
        for attempt in range(2):
            try:
                r = requests.post(OR_URL + "/chat/completions", headers=headers,
                                  json=body, timeout=600)
                if r.status_code == 429:
                    last = "429 у %s" % model
                    log("   %s занята (429), пробую следующую" % model)
                    time.sleep(3)
                    break          # к следующей модели, ждать бесполезно
                r.raise_for_status()
                msg = (r.json().get("choices") or [{}])[0].get("message") or {}
                content = msg.get("content") or msg.get("reasoning") or ""
                if not content.strip():
                    raise ValueError("пустой ответ модели")
                # некоторые модели всё равно оборачивают ответ в ```json
                content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
                return json.loads(content), model
            except json.JSONDecodeError as e:
                last = "%s вернула не JSON: %s" % (model, str(e)[:100])
                log("   %s" % last)
            except Exception as e:
                last = "%s: %s: %s" % (model, type(e).__name__, str(e)[:120])
                log("   %s" % last)
            time.sleep(4)
    raise RuntimeError(last or "ни одна модель не ответила")


# ---------- запись ----------

OKK_HEADERS = ["дата разбора", "ID сделки", "клиент", "ссылка amo", "расшифровка",
               "цель встречи", "цель достигнута", "почему не достигнута",
               "вероятность", "следующий шаг", "дата в шаге", "ЛПР",
               "контакт", "потребности", "презентация", "возражения", "фиксация",
               "комитет (раз)", "комитет по трети", "даты и ограниченность", "запрет: каждый месяц",
               "вопросов", "слов", "резюме", "модель", "json",
               "элементы: спросил", "элементы: прозвучало",
               "менеджер", "дата встречи", "источник", "оборот", "запись zoom", "код доступа"]


def ensure_tab(sheets):
    meta = sheets.get(spreadsheetId=MARKETING_SHEET_ID).execute()
    names = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if OKK_TAB not in names:
        sheets.batchUpdate(spreadsheetId=MARKETING_SHEET_ID,
                           body={"requests": [{"addSheet": {"properties": {"title": OKK_TAB}}}]}).execute()
        log("создан лист «%s»" % OKK_TAB)
    vals = sheets.values().get(spreadsheetId=MARKETING_SHEET_ID,
                               range="'%s'!1:1" % OKK_TAB).execute().get("values", [])
    hdr = list(vals[0]) if vals else []
    # недостающие колонки — в конец, чтобы ничего не сдвигать под уже записанными строками
    missing = [c for c in OKK_HEADERS if c not in hdr]
    if missing or not hdr:
        hdr = hdr + missing
        sheets.values().update(spreadsheetId=MARKETING_SHEET_ID,
                               range="'%s'!A1" % OKK_TAB, valueInputOption="RAW",
                               body={"values": [hdr]}).execute()
    return hdr


def stage_score(review, key):
    for s in review.get("stages", []) or []:
        if s.get("key") == key:
            return s.get("score")
    return None


def elements_count(review, key):
    """Сколько обязательных элементов отмечено моделью — «спросил» или «прозвучало»."""
    items = review.get("checklist") or []
    if not items:
        return ""
    return "%d из %d" % (sum(1 for x in items if x.get(key)), len(ELEMENTS) or len(items))


def to_row(row, review, metrics, model, stamp, meta=None):
    meta = meta or {}
    ms = {m["id"]: m for m in metrics["must_say"]}
    mn = {m["id"]: m for m in metrics["must_not_say"]}
    return [
        stamp, row.get("ID", ""), (row.get("first_name", "") + " " + row.get("last_name", "")).strip(),
        row.get("amo_link", "").strip() or meta.get("amo", ""), row.get("doc_url", ""),
        review.get("goal", ""), review.get("goal_achieved", ""), review.get("goal_reason", ""),
        review.get("probability", ""), review.get("next_step", ""),
        "да" if review.get("next_step_has_date") else "нет", review.get("decision_maker", ""),
        stage_score(review, "contact"), stage_score(review, "needs"),
        stage_score(review, "present"), stage_score(review, "objections"),
        stage_score(review, "close"),
        ms.get("komitet", {}).get("count", 0),
        "да" if ms.get("komitet", {}).get("spread_ok") else "нет",
        "да" if (ms.get("stream_dates", {}).get("ok") and ms.get("scarcity", {}).get("ok")) else "нет",
        "нарушено" if not mn.get("monthly", {}).get("ok", True) else "чисто",
        metrics["questions"], metrics["words"],
        review.get("summary", ""), model,
        json.dumps(review, ensure_ascii=False)[:48000],   # лимит ячейки Google — 50 тыс.
        elements_count(review, "asked"), elements_count(review, "present"),
        meta.get("manager", ""), meta.get("held_at", ""), meta.get("source", ""),
        meta.get("turnover", ""), meta.get("zoom", ""), meta.get("passcode", ""),
    ]


# ============================================================
#  Основная логика
# ============================================================

def main():
    missing = [n for n, v in [("OPENROUTER_API_KEY", OPENROUTER_API_KEY),
                              ("GOOGLE_SERVICE_ACCOUNT_JSON", GOOGLE_SA_JSON),
                              ("SHEET_ID", MARKETING_SHEET_ID)] if not v]
    if missing:
        log("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    headers = {"Authorization": "Bearer " + OPENROUTER_API_KEY,
               "HTTP-Referer": "https://github.com/Gitelman-traning/okk",
               "X-Title": "OKK review"}

    sheets, docs = google_clients()
    values = sheets.values()

    data = values.get(spreadsheetId=MARKETING_SHEET_ID,
                      range="'%s'!1:100000" % DPD_TAB).execute().get("values", [])
    hdr = data[0]
    rows = [dict(zip(hdr, r + [""] * (len(hdr) - len(r)))) for r in data[1:]]
    pending = [r for r in rows if r.get("doc_url", "").strip()]

    zoom_meta = read_zoom_meta(values)

    # уже разобранные — по ID сделки
    hdr = ensure_tab(sheets)
    done_rows = values.get(spreadsheetId=MARKETING_SHEET_ID,
                           range="'%s'!A2:B100000" % OKK_TAB).execute().get("values", [])
    done = {r[1].strip() for r in done_rows if len(r) > 1 and r[1].strip()}
    done_row = {r[1].strip(): i + 2 for i, r in enumerate(done_rows) if len(r) > 1 and r[1].strip()}
    if REDO:
        log("режим перезаписи: разобранные встречи будут обновлены")
    else:
        pending = [r for r in pending if r.get("ID", "").strip() not in done]

    # свежие сверху: лист заполняется сверху вниз, значит новые — в конце
    pending = list(reversed(pending))

    if MANAGERS:
        before = len(pending)
        pending = [r for r in pending
                   if any(m in (zoom_meta.get(str(r.get("ID", "")).strip(), {})
                                .get("manager", "").lower()) for m in MANAGERS)]
        log("фильтр по менеджерам %s: осталось %d из %d" % (MANAGERS, len(pending), before))

    pending = pending[:LIMIT]
    log("встреч с расшифровкой: %d, уже разобрано: %d, беру за прогон: %d"
        % (len(rows), len(done), len(pending)))
    if not pending:
        return {"ok": 0, "fail": 0}

    models = pick_models(headers)
    log("карточек встреч в листе «%s»: %d" % (ZOOM_TAB, len(zoom_meta)))
    stamp = time.strftime("%d.%m.%Y %H:%M")

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    results, ok, fail = [], 0, 0
    for row in pending:
        rid = row.get("ID", "").strip() or "?"
        try:
            log("[%s] читаю расшифровку..." % rid)
            text = read_doc(docs, row.get("doc_url", ""))
            if len(text) < 2000:
                log("[%s] расшифровка пустая или слишком короткая (%d) — пропуск" % (rid, len(text)))
                fail += 1
                continue
            metrics = hard_metrics(text)
            sent = text if len(text) <= MAX_CHARS else text[:MAX_CHARS // 3] + "\n…\n" + text[-2 * MAX_CHARS // 3:]
            log("[%s] разбор моделью (%d символов)..." % (rid, len(sent)))
            review, model = ask_model(models, headers, sent, metrics)

            fields = dict(zip(OKK_HEADERS, to_row(row, review, metrics, model, stamp, zoom_meta.get(rid))))
            if REDO and rid in done_row:
                # перезаписываем только колонки разбора; всё остальное в строке (замеры речи) сохраняем
                rownum = done_row[rid]
                current = values.get(spreadsheetId=MARKETING_SHEET_ID,
                                     range="'%s'!%d:%d" % (OKK_TAB, rownum, rownum)).execute().get("values", [[]])[0]
                current = current + [""] * (len(hdr) - len(current))
                merged = [fields.get(c, current[i]) for i, c in enumerate(hdr)]
                values.update(spreadsheetId=MARKETING_SHEET_ID,
                              range="'%s'!A%d" % (OKK_TAB, rownum),
                              valueInputOption="RAW", body={"values": [merged]}).execute()
            else:
                values.append(spreadsheetId=MARKETING_SHEET_ID, range="'%s'!A1" % OKK_TAB,
                              valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                              body={"values": [[fields.get(c, "") for c in hdr]]}).execute()
            results.append({"row": row, "metrics": metrics, "review": review})
            ok += 1
            # содержание разбора в лог не пишем: логи публичного репозитория видны всем
            log("[%s] готово, строка записана" % rid)
        except Exception as e:
            fail += 1
            log("[%s] ОШИБКА: %s: %s" % (rid, type(e).__name__, str(e)[:300]))
        time.sleep(PAUSE)

    with io.open(os.path.join(OUT_DIR, "okk.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": stamp, "models": models, "items": results},
                  f, ensure_ascii=False, indent=1)

    log("ГОТОВО. Разобрано: %d, ошибок: %d" % (ok, fail))
    return {"ok": ok, "fail": fail, "model": ", ".join(models[:2])}


if __name__ == "__main__":
    try:
        s = main()
        if s["ok"] or s["fail"]:
            send_telegram("🎧 ОКК: разбор диагностик\nРазобрано: %d, ошибок: %d\nМодель: %s"
                          % (s["ok"], s["fail"], s.get("model", "—")))
    except Exception as e:
        send_telegram("❌ ОКК: прогон упал\n%s: %s" % (type(e).__name__, str(e)[:300]))
        raise
