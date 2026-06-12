from __future__ import annotations

from pathlib import Path

SKILL_PATH = Path(__file__).parent.parent.parent / "skills" / "tickerbrief" / "SKILL.md"


def _content() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


class TestTickerbriefSkillExists:
    """SKILL.md existe et est lisible."""

    def test_file_exists(self):
        assert SKILL_PATH.exists(), f"skills/tickerbrief/SKILL.md not found at {SKILL_PATH}"

    def test_file_non_empty(self):
        assert len(_content()) > 200

    def test_frontmatter_name(self):
        assert "name: tickerbrief" in _content()

    def test_frontmatter_has_openclaw_metadata(self):
        assert "openclaw:" in _content()


class TestTickerbriefTriggers:
    """Les trois déclencheurs obligatoires sont présents."""

    def test_trigger_brief(self):
        assert "brief TICKER" in _content()

    def test_trigger_point_sur(self):
        assert "point sur TICKER" in _content()

    def test_trigger_actu(self):
        assert "actu TICKER" in _content()


class TestTickerbriefContextSources:
    """Chaque source de contexte est référencée."""

    def test_memory_path_referenced(self):
        assert "memory/tickers" in _content()

    def test_dedup_state_referenced(self):
        assert "dedup_state.json" in _content()

    def test_sector_factors_referenced(self):
        assert "sector_factors.json" in _content()

    def test_web_search_step_present(self):
        content = _content()
        assert "web search" in content.lower() or "web_search" in content.lower()

    def test_portfolio_and_watchlist_check(self):
        content = _content()
        assert "portfolio.json" in content
        assert "watchlist.json" in content


class TestTickerbriefReadOnly:
    """Le skill est déclaré lecture seule — aucune écriture."""

    def test_readonly_rule_present(self):
        content = _content()
        assert "Lecture seule" in content or "lecture seule" in content

    def test_no_write_memory_instruction(self):
        content = _content()
        assert "write_memory" not in content
        assert "écrire dans memory" not in content.lower()


class TestTickerbriefUnknownTicker:
    """Gestion explicite des tickers non suivis."""

    def test_unknown_ticker_handling_present(self):
        content = _content()
        assert "non suivi" in content

    def test_no_hallucination_rule(self):
        content = _content()
        assert "inventer" in content or "invention" in content


class TestTickerbriefResponseFormat:
    """Format de réponse signal-first documenté."""

    def test_signal_first_format(self):
        assert "signal-first" in _content().lower() or "Signal-first" in _content()

    def test_no_markdown_heading_rule(self):
        content = _content()
        assert "heading" in content or "Heading" in content

    def test_anomalie_section_present(self):
        assert "ANOMALIE" in _content()

    def test_single_telegram_response(self):
        content = _content()
        assert "seule réponse" in content or "une seule" in content.lower()


class TestTickerbriefMissingDataGraceDegradation:
    """Dégradation douce documentée pour chaque source absente."""

    def test_missing_memory_handled(self):
        content = _content()
        assert "aucune mémoire" in content

    def test_missing_alert_handled(self):
        content = _content()
        assert "aucune alerte" in content

    def test_missing_news_handled(self):
        content = _content()
        assert "aucune news" in content

    def test_missing_sector_handled(self):
        content = _content()
        assert "non mappé" in content
