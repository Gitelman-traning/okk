#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Кладёт расшифровки разобранных встреч в хранилище дашборда — из них отвечает
блок «Спросить по встрече».

Сам скрипт только готовит файл для заливки; заливает wrangler:

  SHEET_ID=... GOOGLE_SA_FILE=ключ.json python push_transcripts.py
  cd ../okk-docs/site && npx wrangler kv bulk put ../../okk/out/kv.json --namespace-id <id>

Ключи в KV: t:<ID сделки>. Значение — текст расшифровки.
"""

import io
import json
import os
import re

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = os.environ.get("SHEET_ID", "").strip()
OKK_TAB = os.environ.get("OKK_TAB", "ОКК").strip()
SA_FILE = os.environ.get("GOOGLE_SA_FILE", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
OUT = os.environ.get("KV_OUT", os.path.join("out", "kv.json"))
MAX_CHARS = int(os.environ.get("KV_MAX_CHARS") or "120000")   # с запасом на длинную встречу


def main():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly",
              "https://www.googleapis.com/auth/drive.readonly"]
    creds = (Credentials.from_service_account_file(SA_FILE, scopes=scopes) if SA_FILE
             else Credentials.from_service_account_info(json.loads(SA_JSON), scopes=scopes))
    values = build("sheets", "v4", credentials=creds,
                   cache_discovery=False).spreadsheets().values()
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)

    data = values.get(spreadsheetId=SHEET_ID,
                      range="'%s'!1:100000" % OKK_TAB).execute().get("values", [])
    hdr = data[0]
    rows = [dict(zip(hdr, r + [""] * (len(hdr) - len(r)))) for r in data[1:]]

    items, skipped = [], 0
    for r in rows:
        rid = str(r.get("ID сделки") or "").strip()
        url = r.get("расшифровка", "")
        m = re.search(r"/document/d/([A-Za-z0-9_-]+)", url or "")
        if not rid or not m:
            skipped += 1
            continue
        doc = docs.documents().get(documentId=m.group(1)).execute()
        text = "".join(
            el.get("textRun", {}).get("content", "")
            for c in doc.get("body", {}).get("content", [])
            for el in c.get("paragraph", {}).get("elements", [])
        )
        if len(text) < 2000:
            skipped += 1
            continue
        items.append({"key": "t:" + rid, "value": text[:MAX_CHARS]})
        print("[%s] %d символов" % (rid, len(text)))

    out_dir = os.path.dirname(OUT)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(items, ensure_ascii=False))
    print("готово: %s, расшифровок %d, пропущено %d" % (OUT, len(items), skipped))


if __name__ == "__main__":
    main()
