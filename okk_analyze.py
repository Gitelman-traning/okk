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
OKK_TAB = os.environ.get("OKK_TAB", "ОКК").strip()        # сюда пишем разбор

OR_URL = "https://openrouter.ai/api/v1"
# приоритет бесплатных моделей: чем раньше в списке, тем охотнее берём
MODEL_PREFS = ("deepseek", "qwen", "meta-llama", "mistralai", "google", "nvidia")
MIN_CONTEXT = 60000      # расшифровка на 1,5 часа — это 20–30 тыс. токенов

MAX_CHARS = 45000        # обрезка расшифровки перед отправкой (хвост важнее — там закрытие)
LIMIT = int(os.environ.get("LIMIT") or "5")
MODEL_ENV = os.environ.get("OKK_MODEL", "").strip()

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
#   "must_say":     [{"id","name","patterns":[regex],"min":1,"spread":true}],
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
    return cfg.get("context", ""), cfg.get("must_say", []), cfg.get("must_not_say", [])


CONTEXT, MUST_SAY, MUST_NOT_SAY = load_checklist()

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
    d = docs.documents().get(documentId=m.group(1)).execute()
    return "".join(
        el.get("textRun", {}).get("content", "")
        for c in d.get("body", {}).get("content", [])
        for el in c.get("paragraph", {}).get("elements", [])
    )


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
    for rule in MUST_SAY:
        hits = count_patterns(text, rule["patterns"])
        item = {"id": rule["id"], "name": rule["name"], "count": len(hits),
                "ok": len(hits) >= rule.get("min", 1)}
        if rule.get("spread"):
            thirds = {int(3 * h / n) for h in hits}
            item["spread_ok"] = len(thirds) >= 3
            item["thirds"] = sorted(thirds)
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
    ids = [m["id"] for m in free[:6]]
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
                content = r.json()["choices"][0]["message"]["content"]
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
               "комитет (раз)", "комитет по трети", "два потока", "запрет: каждый месяц",
               "вопросов", "слов", "резюме", "модель"]


def ensure_tab(sheets):
    meta = sheets.get(spreadsheetId=MARKETING_SHEET_ID).execute()
    names = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if OKK_TAB not in names:
        sheets.batchUpdate(spreadsheetId=MARKETING_SHEET_ID,
                           body={"requests": [{"addSheet": {"properties": {"title": OKK_TAB}}}]}).execute()
        log("создан лист «%s»" % OKK_TAB)
    vals = sheets.values().get(spreadsheetId=MARKETING_SHEET_ID,
                               range="'%s'!1:1" % OKK_TAB).execute().get("values", [])
    if not vals:
        sheets.values().update(spreadsheetId=MARKETING_SHEET_ID,
                               range="'%s'!A1" % OKK_TAB, valueInputOption="RAW",
                               body={"values": [OKK_HEADERS]}).execute()


def stage_score(review, key):
    for s in review.get("stages", []) or []:
        if s.get("key") == key:
            return s.get("score")
    return None


def to_row(row, review, metrics, model, stamp):
    ms = {m["id"]: m for m in metrics["must_say"]}
    mn = {m["id"]: m for m in metrics["must_not_say"]}
    return [
        stamp, row.get("ID", ""), (row.get("first_name", "") + " " + row.get("last_name", "")).strip(),
        row.get("amo_link", ""), row.get("doc_url", ""),
        review.get("goal", ""), review.get("goal_achieved", ""), review.get("goal_reason", ""),
        review.get("probability", ""), review.get("next_step", ""),
        "да" if review.get("next_step_has_date") else "нет", review.get("decision_maker", ""),
        stage_score(review, "contact"), stage_score(review, "needs"),
        stage_score(review, "present"), stage_score(review, "objections"),
        stage_score(review, "close"),
        ms.get("komitet", {}).get("count", 0),
        "да" if ms.get("komitet", {}).get("spread_ok") else "нет",
        "да" if ms.get("two_streams", {}).get("ok") else "нет",
        "нарушено" if not mn.get("monthly", {}).get("ok", True) else "чисто",
        metrics["questions"], metrics["words"],
        review.get("summary", ""), model,
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

    # уже разобранные — по ID сделки
    ensure_tab(sheets)
    done_rows = values.get(spreadsheetId=MARKETING_SHEET_ID,
                           range="'%s'!A2:B100000" % OKK_TAB).execute().get("values", [])
    done = {r[1].strip() for r in done_rows if len(r) > 1 and r[1].strip()}
    pending = [r for r in pending if r.get("ID", "").strip() not in done]

    # свежие сверху: лист заполняется сверху вниз, значит новые — в конце
    pending = list(reversed(pending))[:LIMIT]
    log("встреч с расшифровкой: %d, уже разобрано: %d, беру за прогон: %d"
        % (len(rows), len(done), len(pending)))
    if not pending:
        return {"ok": 0, "fail": 0}

    models = pick_models(headers)
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

            values.append(spreadsheetId=MARKETING_SHEET_ID, range="'%s'!A1" % OKK_TAB,
                          valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
                          body={"values": [to_row(row, review, metrics, model, stamp)]}).execute()
            results.append({"row": row, "metrics": metrics, "review": review})
            ok += 1
            # содержание разбора в лог не пишем: логи публичного репозитория видны всем
            log("[%s] готово, строка записана" % rid)
        except Exception as e:
            fail += 1
            log("[%s] ОШИБКА: %s: %s" % (rid, type(e).__name__, str(e)[:300]))

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
