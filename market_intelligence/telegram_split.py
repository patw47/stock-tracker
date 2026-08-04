"""Découpage des messages Telegram — implémentation unique (Epic 9 Sprint 4).

Telegram rejette un message de plus de 4096 caractères ; un message rejeté est une
alerte perdue, pas tronquée. Le texte est donc découpé sur des frontières de
paragraphe, ce qui garantit qu'aucun morceau ne coupe une balise ``<b>`` (chaque
balise tient sur une ligne, doctrine partagée du digest).

Cet algorithme vivait en trois exemplaires : ici et dans deux nœuds n8n
JavaScript identiques au caractère près. Les nœuds ne font plus que relayer les
morceaux produits ici ; ce module est la seule implémentation, et la seule testée.

Aucune dépendance lourde : les trois producteurs (digest EOD, heartbeat
hebdomadaire, rapport mensuel) l'importent, et deux d'entre eux n'embarquent pas
pandas.
"""

from __future__ import annotations

import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def split_telegram_html(text: str, limit: int = 4000) -> list[str]:
    """Split HTML text into <=limit chunks without orphaning a tag.

    Splits on paragraph (``\\n\\n``) boundaries; since each ``<b>`` tag is
    contained in one paragraph, no chunk cuts a tag. A single oversized paragraph
    (> limit) is degraded to plain text (tags stripped) then hard-sliced: better a
    formatting-free alert than an alert Telegram refuses.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\n+", text):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
        else:
            plain = _strip_html_tags(paragraph)
            for start in range(0, len(plain), limit):
                chunks.append(plain[start : start + limit])
    if current:
        chunks.append(current)
    return chunks
