from __future__ import annotations

from unittest.mock import Mock

import requests

from market_intelligence.edgar_form4 import fetch_company_form4_filings
from market_intelligence.registry_schema import TickerEntry


def _entry() -> TickerEntry:
    return TickerEntry(symbol="TEST", api_symbol="TEST", expected_name="Test Corp")


def _response(text: str) -> Mock:
    response = Mock()
    response.text = text
    response.raise_for_status.return_value = None
    return response


def test_fetch_form4_parses_structured_rss_entries() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <category term="4" />
        <title>4 - Test Corp (0000123456) (Issuer)</title>
        <updated>2026-06-02T16:00:00-04:00</updated>
        <summary>Reporting Owner: Jane Doe<br />Transaction Code: P</summary>
        <link href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;accession_number=0000123456-26-000001" />
      </entry>
    </feed>"""
    session = Mock()
    session.get.return_value = _response(xml)

    result = fetch_company_form4_filings(
        _entry(),
        cik_by_symbol={"TEST": "123456"},
        session=session,
    )

    assert result.data_issues == ()
    assert result.cik == "0000123456"
    assert len(result.filings) == 1
    filing = result.filings[0]
    assert filing.form_type == "4"
    assert filing.accession_number == "0000123456-26-000001"
    assert filing.filing_date == "2026-06-02T16:00:00-04:00"
    assert filing.issuer_name == "4 - Test Corp (0000123456) (Issuer)"
    assert filing.reporting_owner == "Jane Doe"
    assert filing.transaction_code == "P"
    assert filing.filing_url is not None


def test_fetch_form4_returns_empty_structured_result_when_no_filings() -> None:
    session = Mock()
    session.get.return_value = _response(
        """<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""
    )

    result = fetch_company_form4_filings(
        _entry(),
        cik_by_symbol={"TEST": "0000123456"},
        session=session,
    )

    assert result.filings == ()
    assert result.data_issues == ()


def test_fetch_form4_flags_fetch_failure_without_live_call() -> None:
    session = Mock()
    session.get.side_effect = requests.Timeout("timeout")

    result = fetch_company_form4_filings(
        _entry(),
        cik_by_symbol={"TEST": "0000123456"},
        session=session,
    )

    assert result.filings == ()
    assert result.data_issues == ("edgar_form4_fetch_failed",)


def test_fetch_form4_rejects_missing_cik_without_request() -> None:
    session = Mock()

    result = fetch_company_form4_filings(_entry(), cik_by_symbol={}, session=session)

    assert result.cik is None
    assert result.filings == ()
    assert result.data_issues == ("edgar_cik_missing",)
    session.get.assert_not_called()
