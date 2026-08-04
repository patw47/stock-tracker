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

# Frontières de découpage. Un en-tête ``<b>…</b>`` et la prose qui le suit forment
# une unité insécable : la ligne vide qui les sépare n'est plus une frontière
# candidate, sinon un morceau peut se terminer sur un titre de survivant dont
# l'explication part dans le message suivant. Restent les vraies ruptures de
# paragraphe et la ligne de séparation de sections du digest (« ──── »), qui est
# précisément la frontière que l'on veut : elle sépare deux survivants.
_BOUNDARY_RE = re.compile(r"(?<!</b>)\n\n+|\n─+\n")


def _strip_html_tags(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def _blocks(text: str) -> list[str]:
    """Cut ``text`` on split boundaries, each block keeping its trailing separator.

    Garder le séparateur collé au bloc qui le précède rend la concaténation des
    morceaux exactement égale au texte d'entrée : rien à restaurer, donc rien à
    perdre — y compris quand deux frontières de nature différente se suivent.
    """
    blocks: list[str] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(text):
        blocks.append(text[start : match.end()])
        start = match.end()
    blocks.append(text[start:])
    return blocks


def split_telegram_html(text: str, limit: int = 4000) -> list[str]:
    """Split HTML text into <=limit chunks without orphaning a tag or a title.

    Splits on the boundaries above; since each ``<b>`` tag is contained in one
    block, no chunk cuts a tag, and no chunk ends on a survivor header without its
    prose. A single oversized block (> limit) is degraded to plain text (tags
    stripped) then hard-sliced: better a formatting-free alert than an alert
    Telegram refuses.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in _blocks(text):
        candidate = current + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
        else:
            plain = _strip_html_tags(block)
            for start in range(0, len(plain), limit):
                chunks.append(plain[start : start + limit])
    if current:
        chunks.append(current)
    return chunks
