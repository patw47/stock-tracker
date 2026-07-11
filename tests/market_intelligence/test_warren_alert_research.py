from __future__ import annotations

import json

import pytest
from unittest.mock import Mock

from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.edgar_form4 import EdgarForm4Result, Form4Filing
from market_intelligence.macro_snapshot import MacroEnrichedAlert, MacroSnapshot
from market_intelligence.registry_schema import Registry, TickerEntry
from market_intelligence.warren_alert_research import (
    MarketStructureStatus,
    ResearchItem,
    analyze_alerts,
    build_alert_research_context,
    build_alert_research_prompt,
    macro_snapshot_ids,
)


def _registry() -> Registry:
    return Registry(
        portfolio_tickers=(
            TickerEntry(symbol="TEST", api_symbol="TEST", expected_name="Test Corp"),
            TickerEntry(symbol="OTHER", api_symbol="OTHER", expected_name="Other Corp"),
        ),
        macro_tickers=(),
        alias_map={},
    )


def _macro() -> MacroSnapshot:
    return MacroSnapshot(
        as_of="2026-06-02",
        ten_year_yield=4.5,
        iwm_close=204.0,
        iwm_pct_change=2.0,
        oil_close=78.0,
        oil_pct_change=4.0,
        vix_close=19.0,
        dxy_close=103.5,
        data_issues=(),
    )


def _enriched(
    ticker: str = "TEST",
    *,
    squeeze_prone: bool | None = True,
    macro: MacroSnapshot | None = None,
) -> MacroEnrichedAlert:
    candidate = CandidateAlert(
        ticker=ticker,
        as_of="2026-06-02",
        classification="speculative",
        eligible=True,
        is_candidate=True,
        direction="up",
        signal_types=("residual_z",),
        z_resid=3.2,
        residual_threshold=2.5,
        short_history_fallback_applied=False,
        data_issues=(),
    )
    alert = DeduplicatedAlert(
        candidate=candidate,
        squeeze_prone=squeeze_prone,
        fire_reason="initial",
        signal_types=(
            ("residual_z", "squeeze_prone")
            if squeeze_prone is True
            else ("residual_z",)
        ),
    )
    return MacroEnrichedAlert(alert=alert, macro_snapshot=macro or _macro())


def _edgar(_: TickerEntry) -> EdgarForm4Result:
    return EdgarForm4Result(
        ticker="TEST",
        cik="0000123456",
        filings=(
            Form4Filing(
                ticker="TEST",
                cik="0000123456",
                accession_number="0000123456-26-000001",
                filing_date="2026-06-02",
                form_type="4",
                issuer_name="Test Corp",
                reporting_owner="Jane Doe",
                transaction_code="P",
                filing_url="https://www.sec.gov/Archives/test",
            ),
        ),
        data_issues=(),
    )


def test_warren_context_contains_edgar_form4_structured_data() -> None:
    context = build_alert_research_context(
        _enriched(),
        registry=_registry(),
        edgar_fetcher=_edgar,
    )

    payload = context.to_dict()

    filing = payload["edgar_form4"]["filings"][0]  # type: ignore[index]
    assert filing["form_type"] == "4"
    assert filing["accession_number"] == "0000123456-26-000001"
    assert filing["filing_date"] == "2026-06-02"
    assert filing["issuer_name"] == "Test Corp"
    assert filing["reporting_owner"] == "Jane Doe"
    assert filing["transaction_code"] == "P"


def test_warren_prompt_explicitly_allows_no_identifiable_catalyst() -> None:
    context = build_alert_research_context(
        _enriched(),
        registry=_registry(),
        edgar_fetcher=lambda entry: EdgarForm4Result(
            ticker=entry.symbol,
            cik="0000123456",
            filings=(),
            data_issues=(),
        ),
    )

    prompt = build_alert_research_prompt(context)

    assert "aucun catalyseur identifiable" in prompt
    assert "flux/technique/squeeze probable" in prompt
    assert "INTERDIT : Markdown" in prompt
    assert "traduction en français courant" in prompt


def test_squeeze_flag_is_surfaced_in_warren_context() -> None:
    known = build_alert_research_context(
        _enriched(squeeze_prone=True),
        registry=_registry(),
        edgar_fetcher=_edgar,
    )
    unknown = build_alert_research_context(
        _enriched(squeeze_prone=None),
        registry=_registry(),
        edgar_fetcher=_edgar,
    )

    assert known.to_dict()["squeeze"]["squeeze_prone"] is True  # type: ignore[index]
    assert unknown.to_dict()["squeeze"]["squeeze_prone"] is None  # type: ignore[index]


def test_only_deduplicated_alerts_trigger_warren_calls() -> None:
    warren_client = Mock(return_value="analysis")

    analyses = analyze_alerts(
        (_enriched("TEST"),),
        registry=_registry(),
        edgar_fetcher=_edgar,
        warren_client=warren_client,
    )

    assert len(analyses) == 1
    assert warren_client.call_count == 1
    assert "OTHER" not in warren_client.call_args.args[0]


def test_empty_dedup_alerts_skip_all_external_fetches_and_warren() -> None:
    edgar_fetcher = Mock(side_effect=AssertionError("no EDGAR call"))
    research_fetcher = Mock(side_effect=AssertionError("no research call"))
    warren_client = Mock(side_effect=AssertionError("no Warren call"))

    assert analyze_alerts(
        (),
        registry=_registry(),
        edgar_fetcher=edgar_fetcher,
        product_research_fetcher=research_fetcher,
        sector_research_fetcher=research_fetcher,
        warren_client=warren_client,
    ) == ()


def test_macro_snapshot_is_reused_for_all_alert_contexts() -> None:
    snapshot = _macro()
    alerts = (_enriched("TEST", macro=snapshot), _enriched("OTHER", macro=snapshot))

    assert macro_snapshot_ids(alerts) == (id(snapshot), id(snapshot))


def test_product_and_sector_search_are_mocked_and_included_as_context() -> None:
    item = ResearchItem(
        source="mock",
        title="Product launch",
        url="https://example.test/product",
        summary="New product context",
    )
    sector = ResearchItem(
        source="mock",
        title="Sector demand",
        url=None,
        summary="Sector context",
    )

    context = build_alert_research_context(
        _enriched(),
        registry=_registry(),
        edgar_fetcher=_edgar,
        product_research_fetcher=lambda _: (item,),
        sector_research_fetcher=lambda _: (sector,),
        market_status_fetcher=lambda _: MarketStructureStatus(
            halt_status="unknown",
            ssr_status="unknown",
            data_issues=(),
        ),
    )
    prompt = build_alert_research_prompt(context)

    assert "Product launch" in prompt
    assert "New product context" in prompt
    assert "Sector demand" in prompt
    assert "Sector context" in prompt


# --- Sprint 1: ticker_news_memory ---


def test_ticker_news_memory_present(tmp_path: pytest.TempDir, monkeypatch: pytest.MonkeyPatch) -> None:
    memory_file = tmp_path / "TEST.md"
    memory_file.write_text("# TEST\nsome news", encoding="utf-8")
    monkeypatch.setenv("WARREN_MEMORY_DIR", str(tmp_path))

    context = build_alert_research_context(
        _enriched(),
        registry=_registry(),
        edgar_fetcher=_edgar,
    )

    assert context.ticker_news_memory == "# TEST\nsome news"


def test_ticker_news_memory_absent(tmp_path: pytest.TempDir, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARREN_MEMORY_DIR", str(tmp_path))

    context = build_alert_research_context(
        _enriched(),
        registry=_registry(),
        edgar_fetcher=_edgar,
    )

    assert context.ticker_news_memory is None


def test_to_dict_includes_ticker_news_memory_key(tmp_path: pytest.TempDir, monkeypatch: pytest.MonkeyPatch) -> None:
    memory_file = tmp_path / "TEST.md"
    memory_file.write_text("# TEST\nsome news", encoding="utf-8")
    monkeypatch.setenv("WARREN_MEMORY_DIR", str(tmp_path))

    context_present = build_alert_research_context(
        _enriched(), registry=_registry(), edgar_fetcher=_edgar
    )
    monkeypatch.setenv("WARREN_MEMORY_DIR", str(tmp_path / "empty"))
    context_absent = build_alert_research_context(
        _enriched(), registry=_registry(), edgar_fetcher=_edgar
    )

    assert context_present.to_dict()["ticker_news_memory"] == "# TEST\nsome news"
    assert context_absent.to_dict()["ticker_news_memory"] is None


def test_ticker_news_memory_env_var_override(tmp_path: pytest.TempDir, monkeypatch: pytest.MonkeyPatch) -> None:
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_b / "TEST.md").write_text("from dir_b", encoding="utf-8")
    monkeypatch.setenv("WARREN_MEMORY_DIR", str(dir_b))

    context = build_alert_research_context(
        _enriched(), registry=_registry(), edgar_fetcher=_edgar
    )

    assert context.ticker_news_memory == "from dir_b"


def test_prompt_includes_memory_section_when_present(tmp_path: pytest.TempDir, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "TEST.md").write_text("latest news", encoding="utf-8")
    monkeypatch.setenv("WARREN_MEMORY_DIR", str(tmp_path))

    context = build_alert_research_context(
        _enriched(), registry=_registry(), edgar_fetcher=_edgar
    )
    prompt = build_alert_research_prompt(context)

    assert "=== MÉMOIRE NEWS LAYER A (dernières sessions) ===" in prompt
    assert "latest news" in prompt
    assert prompt.index("=== MÉMOIRE NEWS LAYER A") < prompt.index("=== CONTEXTE STRUCTURE ===")


def test_prompt_excludes_memory_section_when_absent(tmp_path: pytest.TempDir, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARREN_MEMORY_DIR", str(tmp_path))

    context = build_alert_research_context(
        _enriched(), registry=_registry(), edgar_fetcher=_edgar
    )
    prompt = build_alert_research_prompt(context)

    assert "=== MÉMOIRE NEWS LAYER A" not in prompt


def test_default_warren_client_extracts_openclaw_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The S7 client must never return the raw OpenClaw --json envelope (incident 2026-07-07)."""
    import warren_server
    from market_intelligence.warren_alert_research import _default_warren_client

    # Format OpenClaw >= 2026.5 : texte final sous result.meta, payloads
    # potentiellement tronqué — le texte complet de meta doit gagner.
    envelope = json.dumps({
        "result": {
            "payloads": [{"text": "# ANOMALY-ALERT-RESEARCH S7 — XYL", "mediaUrl": None}],
            "meta": {
                "finalAssistantVisibleText": "# ANOMALY-ALERT-RESEARCH S7 — XYL\nanalyse propre",
                "systemPromptReport": {"tools": [{"name": "web_search", "schemaChars": 1209}]},
            },
        },
    })
    monkeypatch.setattr(warren_server, "call_warren", lambda message, tag: envelope)

    analysis = _default_warren_client("prompt")

    assert analysis == "# ANOMALY-ALERT-RESEARCH S7 — XYL\nanalyse propre"
    assert "schemaChars" not in analysis
