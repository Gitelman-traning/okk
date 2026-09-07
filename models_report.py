#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Что доступно на OpenRouter: бесплатные модели с достаточным контекстом
и самые дешёвые платные — со сметой на месяц работы ОКК.

Смета считается по факту прогона: одна встреча — примерно 13 тыс. токенов
на вход (45 тыс. символов расшифровки) и 1,5 тыс. на ответ.
"""

import os
import requests

OR_URL = (os.environ.get("LLM_BASE_URL", "").strip().rstrip("/") or "https://openrouter.ai/api/v1") + "/models"
KEY = os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()

MEETINGS = int(os.environ.get("MEETINGS") or "300")   # диагностик в месяц
IN_TOK = 13000        # входных токенов на встречу
OUT_TOK = 1500        # выходных токенов на встречу
MIN_CTX = 60000       # меньше — полуторачасовая встреча не влезет


def price(m, key):
    try:
        return float(m.get("pricing", {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def month_cost(m):
    """Во сколько обойдётся месяц: цены OpenRouter — за один токен."""
    return MEETINGS * (IN_TOK * price(m, "prompt") + OUT_TOK * price(m, "completion"))


def row(m):
    ctx = int(m.get("context_length") or 0)
    return "%-52s ctx %7d   вход $%-9.3f выход $%-9.3f   месяц ≈ $%.2f" % (
        m["id"][:52], ctx,
        price(m, "prompt") * 1_000_000,
        price(m, "completion") * 1_000_000,
        month_cost(m),
    )


def ping():
    """Копеечные запросы: какой параметр отключает «мысли» у провайдера и как это меняет usage."""
    base = OR_URL.rsplit("/", 1)[0]
    variants = [("без параметров", {}),
                ("reasoning.effort=none", {"reasoning": {"effort": "none"}}),
                ("reasoning.enabled=false", {"reasoning": {"enabled": False}}),
                ("thinking.type=disabled", {"thinking": {"type": "disabled"}}),
                ("reasoning_effort=none", {"reasoning_effort": "none"}),
                ("reasoning_effort=minimal", {"reasoning_effort": "minimal"})]
    for name, extra in variants:
        body = {"model": os.environ.get("PING_MODEL", "anthropic/claude-sonnet-5"),
                "messages": [{"role": "user", "content": "Ответь одним словом: ок"}],
                "max_tokens": 200, "temperature": 0}
        body.update(extra)
        try:
            r = requests.post(base + "/chat/completions", headers={"Authorization": "Bearer " + KEY},
                              json=body, timeout=120)
            if r.status_code != 200:
                print("%-26s HTTP %s: %s" % (name, r.status_code, r.text[:160].replace(chr(10), " ")))
                continue
            d = r.json()
            u = d.get("usage") or {}
            det = u.get("completion_tokens_details") or {}
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            print("%-26s ok: prompt %s, completion %s, reasoning %s, content=%r" % (
                name, u.get("prompt_tokens"), u.get("completion_tokens"), det.get("reasoning_tokens"),
                (msg.get("content") or "")[:20]))
        except Exception as e:
            print("%-26s ошибка %s" % (name, str(e)[:120]))


def main():
    if os.environ.get("LLM_PING"):
        ping()
        return
    r = requests.get(OR_URL, headers={"Authorization": "Bearer " + KEY} if KEY else {}, timeout=60)
    r.raise_for_status()
    models = r.json().get("data", [])
    # у провайдеров без метаданных (ProxyAPI и др.) просто печатаем нужные имена
    print("Провайдер: %s, моделей: %d" % (OR_URL, len(models)))
    for key in ("claude", "gpt-5", "gemini", "deepseek", "minimax"):
        hits = [m["id"] for m in models if key in m.get("id", "").lower()]
        print("  %-8s %s" % (key, ", ".join(hits[:14]) or "—"))
    print()
    big = [m for m in models if int(m.get("context_length") or 0) >= MIN_CTX]
    if not big:
        return

    free = [m for m in big if m["id"].endswith(":free")]
    paid = [m for m in big if not m["id"].endswith(":free") and month_cost(m) > 0]
    paid.sort(key=month_cost)

    print("Всего моделей: %d, с контекстом от %d: %d\n" % (len(models), MIN_CTX, len(big)))

    print("=" * 110)
    print("БЕСПЛАТНЫЕ (%d) — цены нулевые, ограничены лимитами запросов" % len(free))
    print("=" * 110)
    for m in sorted(free, key=lambda x: -int(x.get("context_length") or 0)):
        print("%-52s ctx %7d" % (m["id"][:52], int(m.get("context_length") or 0)))

    print()
    print("=" * 110)
    print("ПЛАТНЫЕ — 25 самых дешёвых. Цены за 1 млн токенов, смета на %d встреч в месяц"
          % MEETINGS)
    print("=" * 110)
    for m in paid[:25]:
        print(row(m))

    print()
    print("Известные по имени (если доступны):")
    for want in ("google/gemini-2.5-flash", "google/gemini-2.0-flash-001",
                 "deepseek/deepseek-chat", "anthropic/claude-haiku-4.5",
                 "anthropic/claude-sonnet-5", "openai/gpt-5-mini", "qwen/qwen3-max"):
        found = [m for m in models if m["id"].startswith(want)]
        for m in found[:1]:
            print("  " + row(m))


if __name__ == "__main__":
    main()
