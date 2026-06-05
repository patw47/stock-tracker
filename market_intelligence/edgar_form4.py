from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Final
from xml.etree import ElementTree

import requests

from market_intelligence.registry_schema import TickerEntry

logger = logging.getLogger(__name__)

_SEC_COMPANY_FEED_URL: Final[str] = "https://www.sec.gov/cgi-bin/browse-edgar"
_DEFAULT_USER_AGENT: Final[str] = "stock-tracker anomaly-research contact@example.com"
_ATOM_NS: Final[dict[str, str]] = {"atom": "http://www.w3.org/2005/Atom"}


class EdgarForm4Error(Exception):
    """Base error for Sprint 7 EDGAR Form 4 context."""


@dataclass(frozen=True)
class Form4Filing:
    """Represent one structured EDGAR Form 4 filing."""

    ticker: str
    cik: str
    accession_number: str | None
    filing_date: str | None
    form_type: str
    issuer_name: str | None
    reporting_owner: str | None
    transaction_code: str | None
    filing_url: str | None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return asdict(self)


@dataclass(frozen=True)
class EdgarForm4Result:
    """Represent structured insider filing context for one ticker."""

    ticker: str
    cik: str | None
    filings: tuple[Form4Filing, ...]
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "ticker": self.ticker,
            "cik": self.cik,
            "filings": [filing.to_dict() for filing in self.filings],
            "data_issues": list(self.data_issues),
        }


def _normalized_cik(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits.zfill(10) if digits else None


def _child_text(element: ElementTree.Element, tag: str) -> str | None:
    child = element.find(f"atom:{tag}", _ATOM_NS)
    if child is None:
        return None
    value = " ".join(text.strip() for text in child.itertext() if text.strip())
    return value or None


def _entry_link(element: ElementTree.Element) -> str | None:
    link = element.find("atom:link", _ATOM_NS)
    if link is None:
        return None
    href = link.attrib.get("href")
    return href.strip() if isinstance(href, str) and href.strip() else None


def _accession_from_url(url: str | None) -> str | None:
    if url is None:
        return None
    marker = "accession_number="
    if marker not in url:
        return None
    value = url.split(marker, maxsplit=1)[1].split("&", maxsplit=1)[0]
    return value.strip() or None


def _parse_summary(summary: str | None) -> tuple[str | None, str | None]:
    if summary is None:
        return None, None
    owner_match = re.search(
        r"Reporting Owner:\s*(.*?)(?:\s+Transaction Code:|$)",
        summary,
        re.IGNORECASE,
    )
    code_match = re.search(
        r"Transaction Code:\s*([A-Z0-9]+)",
        summary,
        re.IGNORECASE,
    )
    owner = owner_match.group(1).strip() if owner_match is not None else None
    transaction_code = code_match.group(1).strip() if code_match is not None else None
    for raw_line in summary.replace("<br />", "\n").replace("<br/>", "\n").splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if owner is None and "reporting owner" in lower and ":" in line:
            owner = line.split(":", maxsplit=1)[1].strip() or owner
        if transaction_code is None and "transaction code" in lower and ":" in line:
            transaction_code = line.split(":", maxsplit=1)[1].strip() or transaction_code
    return owner, transaction_code


def _parse_filings(ticker: str, cik: str, payload: str) -> tuple[Form4Filing, ...]:
    root = ElementTree.fromstring(payload)
    filings: list[Form4Filing] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        category = entry.find("atom:category", _ATOM_NS)
        form_type = category.attrib.get("term") if category is not None else None
        if form_type != "4":
            continue
        url = _entry_link(entry)
        summary = _child_text(entry, "summary")
        reporting_owner, transaction_code = _parse_summary(summary)
        filings.append(
            Form4Filing(
                ticker=ticker,
                cik=cik,
                accession_number=_accession_from_url(url),
                filing_date=_child_text(entry, "updated"),
                form_type="4",
                issuer_name=_child_text(entry, "title"),
                reporting_owner=reporting_owner,
                transaction_code=transaction_code,
                filing_url=url,
            )
        )
    return tuple(filings)


def fetch_company_form4_filings(
    entry: TickerEntry,
    *,
    cik_by_symbol: dict[str, str] | None = None,
    session: requests.Session | None = None,
    count: int = 10,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> EdgarForm4Result:
    """Fetch structured Form 4 filings from EDGAR for one registry ticker."""
    cik = _normalized_cik((cik_by_symbol or {}).get(entry.symbol))
    if cik is None:
        return EdgarForm4Result(
            ticker=entry.symbol,
            cik=None,
            filings=(),
            data_issues=("edgar_cik_missing",),
        )

    client = session or requests.Session()
    try:
        response = client.get(
            _SEC_COMPANY_FEED_URL,
            params={
                "action": "getcompany",
                "CIK": cik,
                "type": "4",
                "owner": "include",
                "count": str(count),
                "output": "atom",
            },
            headers={"User-Agent": user_agent},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("EDGAR Form 4 fetch failed for %s: %s", entry.symbol, exc)
        return EdgarForm4Result(
            ticker=entry.symbol,
            cik=cik,
            filings=(),
            data_issues=("edgar_form4_fetch_failed",),
        )

    try:
        filings = _parse_filings(entry.symbol, cik, response.text)
    except ElementTree.ParseError:
        return EdgarForm4Result(
            ticker=entry.symbol,
            cik=cik,
            filings=(),
            data_issues=("edgar_form4_parse_failed",),
        )
    return EdgarForm4Result(
        ticker=entry.symbol,
        cik=cik,
        filings=filings,
        data_issues=(),
    )
