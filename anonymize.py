# -*- coding: utf-8 -*-
"""
Обезличивание расшифровки перед отправкой модели.

Модель должна читать текст, поэтому зашифровать его нельзя. Зато можно убрать всё, что
привязывает разговор к конкретному человеку: имена людей, названия компаний, телефоны,
почты, ссылки. Таблица замен («ключ») живёт только в памяти прогона: ответ модели проходит
обратную подстановку, и в лист попадают настоящие имена, а провайдер их не видит.

Что заменяется:
  - известные имена (клиент из карточки, менеджер) — по основам слов, с учётом склонений;
  - имена людей и организации, найденные распознавателем сущностей (natasha);
  - телефоны, почты, ссылки, telegram-ники — регулярками.
Что остаётся: города, суммы, ниши, содержание разговора — без этого разбор теряет смысл;
имена основателей и название тренинга — это наши данные, не клиента.
"""

import re

# наши имена — не трогаем: они нужны чек-листу («идёт к Паше на комитет»)
KEEP = {"гительман", "павел", "паша", "паши", "паше", "пашу", "пашей", "татьяна", "таня", "тани", "тане",
        "танюш", "дубай", "дубае", "стамбул", "стамбуле", "zoom", "зум"}

_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s\-().]?){10,12}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:ru|com|net|org|io|kz|by|ua|ae)\b(?:/\S*)?", re.I)
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{4,}")

_ner = None


def _tagger():
    """Распознаватель имён и организаций для русского. Ленивая загрузка: модели весят десятки мегабайт."""
    global _ner
    if _ner is None:
        from natasha import Segmenter, NewsEmbedding, NewsNERTagger, Doc
        emb = NewsEmbedding()
        _ner = {"seg": Segmenter(), "tag": NewsNERTagger(emb), "Doc": Doc}
    return _ner


def _stem(word):
    """Основа для поиска склонений: первые буквы имени без окончания."""
    w = word.lower().replace("ё", "е")
    return w[:max(3, len(w) - 2)] if len(w) >= 5 else w


class Anonymizer:
    def __init__(self, client="", manager="", company="", extra_people=()):
        self.map = {}          # плейсхолдер → оригинал
        self.stats = {"известные имена": 0, "люди": 0, "организации": 0, "контакты": 0}
        self._people = 0
        self._orgs = 0
        self.known = []        # (regex, плейсхолдер)
        for name, ph in ((client, "Клиент"), (manager, "Менеджер"), (company, "Компания_клиента")):
            for tok in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", name or ""):
                if tok.lower() in KEEP:
                    continue
                self.known.append((re.compile(r"\b" + re.escape(_stem(tok)) + r"[а-яёa-z]{0,4}\b", re.I), ph))
                self.map.setdefault(ph, tok)
        for i, p in enumerate(extra_people):
            for tok in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", p or ""):
                self.known.append((re.compile(r"\b" + re.escape(_stem(tok)) + r"[а-яёa-z]{0,4}\b", re.I), "Человек_%d" % (i + 1)))
                self.map.setdefault("Человек_%d" % (i + 1), tok)

    def _sub_known(self, text):
        for rx, ph in self.known:
            text, n = rx.subn(ph, text)
            self.stats["известные имена"] += n
        return text

    def _sub_contacts(self, text):
        for rx, ph in ((_EMAIL, "[почта]"), (_URL, "[ссылка]"), (_HANDLE, "[ник]"), (_PHONE, "[телефон]")):
            text, n = rx.subn(ph, text)
            self.stats["контакты"] += n
        return text

    def _sub_ner(self, text):
        try:
            ner = _tagger()
        except Exception:
            return text            # natasha не установлена — работаем тем, что есть
        doc = ner["Doc"](text)
        doc.segment(ner["seg"])
        doc.tag_ner(ner["tag"])
        spans = [s for s in doc.spans if s.type in ("PER", "ORG")]
        if not spans:
            return text
        # один и тот же человек — один и тот же плейсхолдер (по основе первого слова)
        keys = {}
        out, pos = [], 0
        for s in spans:
            raw = text[s.start:s.stop]
            low = raw.lower()
            if any(k in low for k in KEEP) or raw.startswith(("Клиент", "Менеджер", "Человек_", "Компания_")):
                continue
            key = (s.type, _stem(raw.split()[0]))
            if key not in keys:
                if s.type == "PER":
                    self._people += 1
                    ph = "Человек_%d" % self._people
                    self.stats["люди"] += 1
                else:
                    self._orgs += 1
                    ph = "Компания_%d" % self._orgs
                    self.stats["организации"] += 1
                keys[key] = ph
                self.map[ph] = raw
            out.append(text[pos:s.start])
            out.append(keys[key])
            pos = s.stop
        out.append(text[pos:])
        return "".join(out)

    def hide(self, text):
        text = self._sub_contacts(text)
        text = self._sub_known(text)
        text = self._sub_ner(text)
        return text

    def restore(self, obj):
        """Обратная подстановка во всех строках ответа модели (рекурсивно по JSON)."""
        if isinstance(obj, str):
            for ph in sorted(self.map, key=len, reverse=True):
                obj = obj.replace(ph, self.map[ph])
            return obj
        if isinstance(obj, list):
            return [self.restore(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.restore(v) for k, v in obj.items()}
        return obj

    def summary(self):
        return ", ".join("%s: %d" % kv for kv in self.stats.items())
