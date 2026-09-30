#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Коуч по диагностикам: разбор встречи по методике «глубина диагностики» (ТЗ v2, сентябрь 2026).

Отличие от okk_analyze.py: не чек-лист «спросил / не спросил», а оценка того, какую информацию диагност
получил и что с ней сделал. Балл 100 из семи блоков, светофор с критическими элементами, отдельно статус
сделки, интерес клиента в начале и в конце, до трёх болей дословно, связка «боль → инструмент», три точки роста.
Модели отдаётся расшифровка с разметкой говорящих (Deepgram), поэтому перебивания и монологи видны.

Запуск — workflow «OKK coach»: одна запись, список моделей через запятую, по строке на модель в лист «Коуч».
Переменные: REC_URL / REC_PASSCODE (или REC_CACHE с готовой расшифровкой), REC_CLIENT, REC_MANAGER, REC_DATE,
COACH_MODELS, COACH_TAB (по умолчанию «Коуч»), COACH_RUBRIC_JSON (переопределить рубрику), MAX_CHARS.
"""

import io
import json
import os
import re
import sys
import time

import requests

import diarize
import okk_analyze as okk
from one_off import get_transcript, or_headers

TAB = os.environ.get("COACH_TAB", "Коуч").strip() or "Коуч"
CLIENT = os.environ.get("REC_CLIENT", "").strip() or "без имени"
MANAGER = os.environ.get("REC_MANAGER", "").strip()
HELD_AT = os.environ.get("REC_DATE", "").strip()
DEAL_ID = os.environ.get("REC_DEAL", "").strip()
OUTCOME = os.environ.get("REC_OUTCOME", "").strip()      # известный исход: комитет / оплата / нет — для калибровки
MAX_CHARS = int(os.environ.get("MAX_CHARS") or 400000)

# цены ProxyAPI за 1M токенов (вход, выход), рубли — на случай, если провайдер не отдаёт стоимость
okk.PRICES_RUB.setdefault("anthropic/claude-opus-5-5", [840, 4200])
okk.PRICES_RUB.setdefault("anthropic/claude-sonnet-5-5", [500, 2500])
okk.PRICES_RUB.setdefault("anthropic/claude-fable-5-1", [1580, 7900])
okk.PRICES_RUB.setdefault("openai/gpt-6-astra", [1580, 7900])
okk.PRICES_RUB.setdefault("openai/gpt-6-sol", [420, 2100])
okk.PRICES_RUB.setdefault("openai/gpt-6-luna", [30, 150])

HEADERS = ["дата разбора", "клиент", "ID сделки", "менеджер", "дата встречи", "исход", "модель",
           "балл", "светофор", "статус сделки", "интерес начало", "интерес конец",
           "б0 контакт", "б1 глубина", "б2 боль", "б3 резюме", "б4 намерение", "б5 барьеры", "б6 ведение",
           "критические: нет", "клиент говорит %", "диагност говорит %", "монолог", "комитет (раз)", "комитет: интеграция",
           "главная упущенная возможность", "точки роста",
           "токены вход", "токены выход", "секунд", "стоимость, ₽", "json", "запись zoom", "код доступа"]

# Рубрика: блоки, веса и критерии. Переопределяется COACH_RUBRIC_JSON (тот же формат).
RUBRIC = {
    "blocks": [
        {"id": "b0", "name": "Контакт и контекст", "max": 5, "items": [
            "что клиент уже знает о тренинге: выяснено / частично / нет",
            "как узнал: контент, рекомендация, знакомые или участники, другое",
            "были ли знакомые или друзья на программе и что именно клиент слышал",
            "с каким исходным запросом рассматривает продукт — словами клиента",
            "естественный контакт: диагност реагирует на ответы, а не идёт по списку вопросов; уточнено «на ты / на вы»"],
         "rule": "0–5 по числу качественно выполненных элементов; вопрос без использования ответа не засчитывать"},
        {"id": "b1", "name": "Глубина диагностики", "max": 20, "sub": [
            {"id": "b1_1", "name": "Текущая команда и роли", "items": ["кто сейчас есть из ключевых людей", "с кем собственник больше всего взаимодействует", "кто реально принимает решения кроме него"]},
            {"id": "b1_2", "name": "Качество людей и соответствие ролям", "items": ["может ли быть, что это не те люди на этих позициях", "почему собственник считает, что они способны вырасти", "если способны — почему пока не делают"]},
            {"id": "b1_3", "name": "Автономность", "items": ["что произойдёт, если собственника выключить из бизнеса на месяц", "как клиент оценивает инициативность и автономность команды — термины раскрыты, а не просто названы"]},
            {"id": "b1_4", "name": "Найм", "items": ["как сейчас нанимают", "кто этим занимается", "по каким критериям принимают решение", "где ломается процесс, какие ошибки повторяются"]},
            {"id": "b1_5", "name": "Цели и разрыв", "items": ["цели бизнеса на следующий год", "что должно измениться в роли собственника", "с этой командой цель реалистична", "какая команда нужна для цели и чем она отличается от текущей"]}],
         "rule": "каждый подблок 0–4: 0 не исследован; 1 только затронут; 2 частично раскрыт; 3 раскрыт фактами и примерами; 4 раскрыт глубоко, включая причины, последствия и связь с бизнес-целью"},
        {"id": "b2", "name": "Конкретизация боли", "max": 20, "items": [
            "проблема сформулирована конкретно", "есть пример или факт", "понятны последствия",
            "понятна цена проблемы или цена бездействия", "клиент признал, что текущий способ решения недостаточен"],
         "rule": "выдели до 3 главных болей дословно или максимально близко к формулировкам клиента; «команда слабая» без фактов, последствий и контекста — не диагностированная боль"},
        {"id": "b3", "name": "Резюме и связка «боль → решение»", "max": 20, "items": [
            "диагност сделал резюме запроса словами клиента", "клиент подтвердил корректность резюме",
            "для каждой ключевой боли показан конкретный инструмент или механика программы",
            "есть логика «ты сказал X → поэтому тебе релевантен Y», а не общая презентация продукта",
            "сколько ключевых запросов клиента покрыто презентацией: X из Y"]},
        {"id": "b4", "name": "Проверка ценности и намерения", "max": 15, "items": [
            "минимум 2 содержательных промежуточных вопроса во время презентации",
            "выяснено, что именно клиент считает применимым и почему", "проверена готовность к участию",
            "если использована шкала 1–10, ответ ниже 10 расшифрован: почему не выше и чего не хватает до 10"]},
        {"id": "b5", "name": "Барьеры и следующий шаг", "max": 15, "items": [
            "выявлены остаточные сомнения, что ещё нужно прояснить", "понятно, кто участвует в принятии решения",
            "выданы релевантные даты, максимум 2 потока", "понятна роль комитета и логика следующего этапа",
            "согласован конкретный следующий шаг и срок"],
         "rule": "«мы свяжемся», «подумаю», «спишемся» не считаются конкретным следующим шагом"},
        {"id": "b6", "name": "Качество ведения разговора", "max": 5, "items": [
            "перебивания и недослушивание клиента", "длинные монологи и презентация без проверки понимания",
            "повторение уже полученной информации", "ответ на реальный вопрос клиента вместо возврата к заготовленному скрипту",
            "использование языка и формулировок клиента"],
         "rule": "только наблюдаемое поведение в расшифровке; никаких «эмпатия 8/10»"},
    ],
    "critical": {"pain": "диагностирована боль", "summary": "сделано резюме", "pain_solution": "есть связка боль → решение",
                 "intent": "проверено намерение", "next_step": "есть конкретный следующий шаг"},
    "interest_start": {"0": "пришёл без понимания зачем, «просто узнать», не знает Павла и продукта",
                       "1": "общий интерес: видел контент, рилсы, YouTube, но конкретной задачи не сформулировал",
                       "2": "есть рекомендация, знакомые, позитивные отзывы и конкретная проблема"},
    "interest_end": {"0": "ценности не увидел", "1": "вежливый интерес без признаков движения", "2": "видит отдельные применимые элементы",
                     "3": "связывает программу со своей задачей", "4": "обсуждает даты и/или следующий шаг", "5": "выражает намерение участвовать и готов двигаться дальше"},
    "deal_status": {"high": "клиент демонстрирует конкретное намерение двигаться дальше, обсуждает поток или следующий этап",
                    "mid": "интерес есть, но остаются значимые сомнения, ограничения или неопределённость",
                    "low": "клиент не видит достаточной ценности, не рассматривает участие сейчас или нет подтверждённого следующего шага"},
}
try:
    RUBRIC.update(json.loads(os.environ.get("COACH_RUBRIC_JSON") or "{}"))
except ValueError:
    pass


def log(*a):
    print(*a, flush=True)


def rubric_text():
    out = []
    for b in RUBRIC["blocks"]:
        out.append("%s. %s — %d баллов" % (b["id"], b["name"], b["max"]))
        for it in b.get("items", []):
            out.append("   - " + it)
        for s in b.get("sub", []):
            out.append("   %s. %s: %s" % (s["id"], s["name"], "; ".join(s["items"])))
        if b.get("rule"):
            out.append("   Правило: " + b["rule"])
    out.append("Критические элементы (для светофора): " + "; ".join("%s = %s" % kv for kv in RUBRIC["critical"].items()))
    out.append("Интерес в начале 0–2: " + "; ".join("%s — %s" % kv for kv in RUBRIC["interest_start"].items()))
    out.append("Интерес в конце 0–5: " + "; ".join("%s — %s" % kv for kv in RUBRIC["interest_end"].items()))
    out.append("Статус сделки: " + "; ".join("%s — %s" % kv for kv in RUBRIC["deal_status"].items()))
    return "\n".join(out)


SYSTEM = """Ты — коуч по продажам и контролёр методологии диагностических встреч.
Диагност (менеджер второй линии) проводит зум с собственником бизнеса: цель встречи — согласие клиента подать заявку
на программный комитет тренинга по управлению командой. Твоя задача — быстро показать менеджеру и его руководителю,
насколько глубоко проведена диагностика, что упущено, как это повлияло на продажу и что конкретно улучшить в следующем разговоре.

""" + okk.CONTEXT + """

ПРИНЦИПЫ
1. Ты работаешь не как чек-лист. Факт заданного вопроса недостаточен: оценивай, какую информацию диагност получил и что с ней сделал.
2. Не придумывай мотивы, возражения и эмоции клиента, если они не подтверждены расшифровкой.
3. Каждый вывод опирается на конкретный эпизод, формулировку клиента или отсутствующий шаг. К каждому блоку — короткая цитата (дословно из расшифровки) или пустая строка, если цитаты нет.
4. Никаких общих рекомендаций вроде «глубже выявлять потребности»: пиши, что именно нужно было раскрыть.
5. Если данных недостаточно — пиши «не удалось определить по транскрипту», а не догадку.
6. Коротко и предметно: каждый вывод 1–2 предложения. Если блок сделан хорошо, не выдумывай замечания «для баланса». Не пересказывай звонок.
7. Реплики размечены: «Диагност:» и «Клиент:». Разметка автоматическая и местами ошибается — восстанавливай смысл, факты не выдумывай.
8. Отвечай ТОЛЬКО валидным JSON по схеме, без markdown-обёртки.

РУБРИКА (100 баллов)
""" + rubric_text() + """

СВЕТОФОР: green — 80–100 И присутствуют все пять критических элементов; yellow — 60–79 ИЛИ отсутствует один критический;
red — меньше 60 ИЛИ отсутствуют два и более. Светофор посчитает программа по твоим баллам и флагам — ты только честно расставь их.

СХЕМА
{
  "blocks": [
    {"id": "b0", "score": 0-5, "found": "что выяснено, 1–2 предложения", "lost": "что потеряно или пусто", "quote": ""},
    {"id": "b1", "score": 0-20, "sub": [
       {"id": "b1_1", "score": 0-4, "note": "1 предложение", "quote": ""},
       {"id": "b1_2", "score": 0-4, "note": "", "quote": ""},
       {"id": "b1_3", "score": 0-4, "note": "", "quote": ""},
       {"id": "b1_4", "score": 0-4, "note": "", "quote": ""},
       {"id": "b1_5", "score": 0-4, "note": "", "quote": ""}], "found": "", "lost": ""},
    {"id": "b2", "score": 0-20, "found": "", "lost": "", "quote": ""},
    {"id": "b3", "score": 0-20, "found": "", "lost": "", "quote": "", "coverage": {"covered": X, "total": Y}},
    {"id": "b4", "score": 0-15, "found": "", "lost": "", "quote": "", "readiness": число 1-10 или null, "readiness_explained": true|false|null},
    {"id": "b5", "score": 0-15, "found": "", "lost": "", "quote": "", "next_step": "словами из разговора или пусто", "next_step_concrete": true|false},
    {"id": "b6", "score": 0-5, "found": "", "lost": "", "quote": ""}
  ],
  "critical": {"pain": true|false, "summary": true|false, "pain_solution": true|false, "intent": true|false, "next_step": true|false},
  "pains": [{"pain": "боль словами клиента", "quote": "дословная цитата", "consequence": "последствие или пусто", "cost": "цена бездействия или пусто",
             "solution": "инструмент программы, который диагност привязал к этой боли, или пусто", "linked": true|false}],
  "interest": {"start": 0-2, "start_why": "", "end": 0-5, "end_why": ""},
  "deal_status": "high" | "mid" | "low",
  "deal_status_why": "1 предложение с опорой на слова клиента",
  "committee": {"mentions_quality": "содержательно" | "частично механически" | "механически" | "не упоминался", "why": ""},
  "missed_opportunity": "главная упущенная возможность, 1–2 предложения с эпизодом",
  "deepen": "как можно было углубить: одна реплика, которую стоило сказать",
  "growth": ["точка роста 1", "точка роста 2", "точка роста 3"],
  "notes": "что мешало разобрать встречу, если мешало"
}"""


def labeled_transcript(dg, manager_speaker):
    """Расшифровка по репликам с ролями и временем: «[12:03] Диагност: …»"""
    utts = (dg.get("results") or {}).get("utterances") or []
    lines = []
    for u in sorted(utts, key=lambda x: float(x.get("start", 0))):
        t = int(float(u.get("start", 0)))
        who = "Диагност" if u.get("speaker") == manager_speaker else "Клиент"
        txt = (u.get("transcript") or "").strip()
        if txt:
            lines.append("[%d:%02d] %s: %s" % (t // 60, t % 60, who, txt))
    return "\n".join(lines)


def manager_speaker(dg):
    """Тот же выбор говорящего-менеджера, что в diarize.measure (по словам продавца)."""
    utts = (dg.get("results") or {}).get("utterances") or []
    talk, hits = {}, {}
    for u in utts:
        sp = u.get("speaker", 0)
        talk[sp] = talk.get(sp, 0) + max(float(u.get("end", 0)) - float(u.get("start", 0)), 0)
        hits[sp] = hits.get(sp, 0) + len(diarize.SELLER_WORDS.findall(u.get("transcript") or ""))
    if not any(hits.values()):
        return max(talk, key=talk.get) if talk else 0
    return max(hits, key=hits.get)


def extra_for(model):
    """Параметры «мыслей» под конкретную модель. COACH_EXTRA_JSON: {"подстрока модели": {...}} переопределяет.
    Старые Claude принимают thinking.type=disabled (секрет LLM_EXTRA_JSON), новые (5.5, Fable) — только adaptive + effort,
    OpenAI параметр thinking не знает вовсе."""
    try:
        custom = json.loads(os.environ.get("COACH_EXTRA_JSON") or "{}")
    except ValueError:
        custom = {}
    for key, val in custom.items():
        if key in model:
            return dict(val)
    m = model.lower()
    if "claude" in m:
        if any(x in m for x in ("5-5", "5.5", "fable", "mythos")):
            return {"thinking": {"type": "adaptive"}, "output_config": {"effort": os.environ.get("COACH_EFFORT", "medium")}}
        try:
            return json.loads(os.environ.get("LLM_EXTRA_JSON") or "{}")
        except ValueError:
            return {}
    return {}


def call_model(model, headers, transcript, tech):
    body = {"model": model,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "Расшифровка встречи с разметкой говорящих:\n\n" + transcript
                          + "\n\n---\nТехнический срез, посчитанный программой (ему доверяй):\n" + json.dumps(tech, ensure_ascii=False, indent=1)
                          + "\n\nРазбери встречу по схеме."}],
            "temperature": 0.2, "max_tokens": 16000, "response_format": {"type": "json_object"}}
    body.update(extra_for(model))
    if model.lower().startswith("openai/"):          # у новых моделей OpenAI лимит ответа зовётся иначе
        body["max_completion_tokens"] = body.pop("max_tokens")
    t0 = time.time()
    r = requests.post(okk.OR_URL + "/chat/completions", headers=headers, json=body, timeout=900)
    secs = round(time.time() - t0, 1)
    if r.status_code != 200:
        raise RuntimeError("%s: HTTP %s %s" % (model, r.status_code, r.text[:200]))
    data = r.json()
    u = data.get("usage") or {}
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content.strip():
        for tc in msg.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments")
            if args:
                content = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                break
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    review = json.loads(content)
    usage = {"prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens"),
             "cost": okk.cost_rub(model, u), "secs": secs}
    return review, usage


def score(review):
    """Сумма блоков (подблоки b1 складываются сами), светофор по правилу ТЗ."""
    total, per = 0, {}
    for b in review.get("blocks") or []:
        bid = b.get("id")
        if bid == "b1" and b.get("sub"):
            s = sum(int(x.get("score") or 0) for x in b["sub"])
            b["score"] = s
        s = int(b.get("score") or 0)
        mx = next((x["max"] for x in RUBRIC["blocks"] if x["id"] == bid), 0)
        s = max(0, min(s, mx))
        b["score"] = s
        per[bid] = s
        total += s
    crit = review.get("critical") or {}
    missing = [RUBRIC["critical"][k] for k in RUBRIC["critical"] if not crit.get(k)]
    if total >= 80 and not missing:
        light = "green"
    elif total >= 60 and len(missing) <= 1:
        light = "yellow"
    else:
        light = "red"
    return total, per, light, missing


def main():
    if not okk.OPENROUTER_API_KEY:
        sys.exit("нет ключа модели (LLM_API_KEY)")
    models = [m.strip() for m in os.environ.get("COACH_MODELS", "").split(",") if m.strip()] or ["anthropic/claude-sonnet-5"]
    url, passcode = os.environ.get("REC_URL", "").strip(), os.environ.get("REC_PASSCODE", "").strip()
    cache = os.environ.get("REC_CACHE", "").strip()
    if not url and not (cache and os.path.exists(cache)):
        sys.exit("нужен REC_URL или готовая расшифровка в REC_CACHE")
    dg = get_transcript(url, passcode, cache)
    speech = diarize.measure(dg)
    ms = manager_speaker(dg)
    text = labeled_transcript(dg, ms)
    plain = ((dg.get("results") or {}).get("channels") or [{}])[0].get("alternatives", [{}])[0].get("transcript", "")
    hm = okk.hard_metrics(plain)
    komitet = next((m["count"] for m in hm.get("must_say", []) if "комитет" in (m.get("name") or "").lower() or m.get("id") == "komitet"), None)
    tech = {"client_share": 100 - speech["share"], "manager_share": speech["share"], "longest_monolog": speech["monolog"],
            "minutes": speech.get("minutes"), "committee_mentions": komitet, "questions": hm.get("questions")}
    log("расшифровка: %d реплик, %d символов; диагност %d%%, монолог %s, комитет %s" % (
        len(text.splitlines()), len(text), speech["share"], speech["monolog"], komitet))
    sent = text if len(text) <= MAX_CHARS else text[:MAX_CHARS // 3] + "\n…\n" + text[-2 * MAX_CHARS // 3:]
    anon = okk.anonymizer_for(client=CLIENT, manager=MANAGER)
    if anon:
        sent = anon.hide(sent)
        log("обезличено: %s" % anon.summary())

    headers = or_headers("OKK coach")
    sheets = diarize.sheets_client()
    meta = sheets.get(spreadsheetId=okk.MARKETING_SHEET_ID).execute()
    if TAB not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
        sheets.batchUpdate(spreadsheetId=okk.MARKETING_SHEET_ID, body={"requests": [{"addSheet": {"properties": {"title": TAB}}}]}).execute()
    vals = sheets.values().get(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!1:1" % TAB).execute().get("values", [])
    hdr = list(vals[0]) if vals else []
    missing_cols = [c for c in HEADERS if c not in hdr]
    if missing_cols or not hdr:
        hdr = hdr + missing_cols
        sheets.values().update(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A1" % TAB, valueInputOption="RAW", body={"values": [hdr]}).execute()
    existing = sheets.values().get(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A2:ZZ10000" % TAB).execute().get("values", [])
    ci, mi = hdr.index("клиент"), hdr.index("модель")

    stamp = time.strftime("%d.%m.%Y %H:%M")
    ok = 0
    for model in models:
        log("[%s] разбор…" % model)
        try:
            review, usage = call_model(model, headers, sent, tech)
        except Exception as e:
            log("[%s] не получилось: %s" % (model, str(e)[:300]))
            continue
        if anon:
            review = anon.restore(review)
        total, per, light, missing = score(review)
        review["score"], review["light"], review["missing_critical"], review["usage"], review["tech"] = total, light, missing, usage, tech
        log("[%s] балл %d, светофор %s, статус %s, %s+%s токенов, %s с, %s ₽" % (
            model, total, light, review.get("deal_status"), usage["prompt"], usage["completion"], usage["secs"], usage["cost"]))
        inter = review.get("interest") or {}
        line = {"дата разбора": stamp, "клиент": CLIENT, "ID сделки": DEAL_ID, "менеджер": MANAGER, "дата встречи": HELD_AT, "исход": OUTCOME,
                "модель": model, "балл": total, "светофор": light, "статус сделки": review.get("deal_status", ""),
                "интерес начало": inter.get("start", ""), "интерес конец": inter.get("end", ""),
                "б0 контакт": per.get("b0", ""), "б1 глубина": per.get("b1", ""), "б2 боль": per.get("b2", ""), "б3 резюме": per.get("b3", ""),
                "б4 намерение": per.get("b4", ""), "б5 барьеры": per.get("b5", ""), "б6 ведение": per.get("b6", ""),
                "критические: нет": "; ".join(missing), "клиент говорит %": tech["client_share"], "диагност говорит %": tech["manager_share"],
                "монолог": tech["longest_monolog"], "комитет (раз)": komitet if komitet is not None else "",
                "комитет: интеграция": (review.get("committee") or {}).get("mentions_quality", ""),
                "главная упущенная возможность": review.get("missed_opportunity", ""), "точки роста": " | ".join(review.get("growth") or []),
                "токены вход": usage["prompt"], "токены выход": usage["completion"], "секунд": usage["secs"], "стоимость, ₽": usage["cost"],
                "json": json.dumps(review, ensure_ascii=False), "запись zoom": url, "код доступа": passcode}
        payload = [[line.get(c, "") for c in hdr]]
        rownum = next((i + 2 for i, r in enumerate(existing) if len(r) > max(ci, mi) and r[ci].strip() == CLIENT and r[mi].strip() == model), None)
        if rownum:
            sheets.values().update(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A%d" % (TAB, rownum), valueInputOption="RAW", body={"values": payload}).execute()
            log("[%s] строка %d перезаписана" % (model, rownum))
        else:
            sheets.values().append(spreadsheetId=okk.MARKETING_SHEET_ID, range="'%s'!A1" % TAB, valueInputOption="RAW",
                                   insertDataOption="INSERT_ROWS", body={"values": payload}).execute()
            log("[%s] строка добавлена" % model)
        ok += 1
    if not ok:
        sys.exit("ни одна модель не дала разбор")
    log("ГОТОВО: %d из %d моделей" % (ok, len(models)))


if __name__ == "__main__":
    main()
