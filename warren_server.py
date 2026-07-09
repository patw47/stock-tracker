#!/usr/bin/env python3
"""
Warren HTTP bridge for n8n.
POST /filter      -- ticker-watch: classify tickers as NEW vs SKIP
POST /synthesize  -- executive-synthesis: markdown briefing + write memory
POST /memorize    -- silent memory write for NEW tickers only, no synthesis
POST /macro-brief -- daily Market Context Brief (independent of ticker news)
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
import datetime
import html
import json
import logging
import os
import subprocess
import threading
import time

try:
    from agents.warren.macro_provider import get_snapshot, fetch_macro_snapshot, get_market_closes
except ImportError:
    get_snapshot = None
    fetch_macro_snapshot = None
    get_market_closes = None

try:
    from market_intelligence.sector_rotation import get_sector_rotation
    from market_intelligence.fear_greed import get_fear_greed
except ImportError:
    get_sector_rotation = None  # type: ignore[assignment]
    get_fear_greed = None  # type: ignore[assignment]

try:
    from agents.warren.models import MacroContext
    from agents.warren.prompt_builder import build_prompt
except ImportError:
    from agents.warren.models import MacroContext

    def build_prompt(macro_context: MacroContext | None, query: str, **kwargs: object) -> str:
        """Preserve existing prompt text when the prompt builder is unavailable."""
        _ = macro_context, kwargs
        return query


logger = logging.getLogger(__name__)
OPENCLAW_CONFIG = "/home/warren/.openclaw/openclaw.json"
WORKSPACE = "/home/warren/.openclaw/workspace-warren"
MEMORY_DIR = os.path.join(WORKSPACE, "memory", "tickers")
MAX_MEMORY = 3

# The bridge runs on ThreadingHTTPServer, so two requests can write ticker
# memory concurrently. write_memory is a read-modify-write on a shared file;
# serialize it so concurrent writes of the same symbol never interleave.
_MEMORY_LOCK = threading.Lock()


def read_memory(symbol):
    """Return last memory entries for symbol, empty string if none."""
    path = os.path.join(MEMORY_DIR, f"{symbol}.md")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_memory(symbol, raw_news, date):
    """Prepend raw_news entry for symbol; keep last MAX_MEMORY entries.

    The read-modify-write below is guarded by _MEMORY_LOCK so concurrent
    ThreadingHTTPServer requests writing the same file cannot interleave.
    """
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, f"{symbol}.md")
    with _MEMORY_LOCK:
        existing = read_memory(symbol)
        entries = [e.strip() for e in existing.split("\n---\n") if e.strip()] if existing else []
        entries.insert(0, f"## {date}\n{raw_news.strip()}")
        entries = entries[:MAX_MEMORY]
        with open(path, "w") as f:
            f.write("\n---\n".join(entries) + "\n")


def call_warren(message, tag):
    """Invoke warren agent via OpenClaw CLI; return raw stdout string."""
    session_id = f"n8n-{tag}-{int(time.time())}"
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = OPENCLAW_CONFIG
    r = subprocess.run(
        ["openclaw", "agent", "--agent", "warren",
         "--session-id", session_id, "--message", message,
         "--json", "--timeout", "180"],
        capture_output=True, text=True, timeout=200, env=env,
    )
    return r.stdout.strip()


def fetch_macro_context() -> MacroContext | None:
    """Return macro context for Warren while keeping the HTTP bridge available."""
    try:
        if get_snapshot is not None:
            return get_snapshot()
        logger.warning("No macro provider hook is available")
        return None
    except Exception as exc:
        logger.warning("Failed to fetch macro context: %s", exc)
        return None


def build_warren_prompt(query: str) -> str:
    """Build Warren prompt with macro snapshot when available."""
    macro_snapshot = None
    if fetch_macro_snapshot is not None:
        try:
            macro_snapshot = asyncio.run(fetch_macro_snapshot())
        except Exception as exc:
            logger.warning("Failed to fetch macro snapshot: %s", exc)
    if macro_snapshot is None:
        macro_context = fetch_macro_context()
        return build_prompt(macro_context, query)
    return build_prompt(None, query, macro_snapshot=macro_snapshot)


def extract_inner(stdout):
    """Extract response text from OpenClaw JSON envelope.
    OpenClaw --json: {"result": {"payloads": [...], "finalAssistantVisibleText": "..."}}
    """
    def decode_json_object(text):
        decoder = json.JSONDecoder()
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        return None

    try:
        outer = json.loads(stdout)
    except Exception:
        outer = decode_json_object(stdout)

    if outer:
        result_obj = outer.get("result", {})
        # OpenClaw >= 2026.5 nests the final text under result.meta.
        meta_obj = result_obj.get("meta", {}) if isinstance(result_obj, dict) else {}
        fat = result_obj.get("finalAssistantVisibleText") or meta_obj.get("finalAssistantVisibleText")
        if fat and fat != "NO_REPLY":
            return fat
        payloads = result_obj.get("payloads", [])
        if payloads and isinstance(payloads, list):
            all_text = "\n".join(p.get("text", "") for p in payloads if p.get("text"))
            if all_text:
                return all_text
        raw = result_obj.get("finalAssistantRawText") or meta_obj.get("finalAssistantRawText")
        if raw and raw != "NO_REPLY":
            return raw
        for key in ("response", "content", "message", "text", "output"):
            if key in outer and isinstance(outer[key], str):
                return outer[key]
        if result_obj:
            return "Aucune réponse textuelle produite par Warren (NO_REPLY)."
    return stdout


def is_no_news(text):
    """Treat Haiku's verbose refusals around NO_NEWS_TODAY as no-news results."""
    normalized = (text or "").strip()
    return not normalized or "NO_NEWS_TODAY" in normalized


def _clean_synthesis(text):
    """Strip reasoning preamble and markdown code fences from Warren synthesis."""
    import re
    m = re.search(r'```(?:markdown)?\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    idx = text.find("# 📈")
    if idx < 0:
        idx = text.find("📈")
    if idx < 0:
        idx = text.find("# ")
    if idx > 0:
        return text[idx:].strip()
    return text.strip()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        """Route POST /filter and /synthesize to their handlers."""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}

        if self.path == "/filter":
            resp = self.handle_filter(body)
        elif self.path == "/synthesize":
            resp = self.handle_synthesize(body)
        elif self.path == "/memorize":
            resp = self.handle_memorize(body)
        elif self.path == "/macro-brief":
            resp = self.handle_macro_brief(body)
        else:
            self.send_response(404)
            self.end_headers()
            return

        payload = resp.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_filter(self, body):
        """Classify tickers as NEW vs SKIP.

        Python-level rules (no Warren call needed for first two):
          - empty memory               -> NEW  (bootstrap: capture first response)
          - NO_NEWS_TODAY + memory     -> SKIP
          - has news + non-empty memory -> ask Warren for semantic dedup
        """
        news = body.get("news", {})
        today = datetime.date.today().isoformat()

        auto_new = {}     # empty memory bootstrap
        auto_skip = {}    # NO_NEWS_TODAY or empty text
        check_dedup = {}  # non-empty memory + has news -> Warren

        for sym, text in news.items():
            text_clean = text.strip() if text else ""
            mem = read_memory(sym)
            if not mem:
                auto_new[sym] = "Bootstrap - first response captured (no prior memory)"
            elif is_no_news(text_clean):
                auto_skip[sym] = "No news found"
            else:
                check_dedup[sym] = (text_clean, mem)

        result = {
            "new": list(auto_new.keys()),
            "skip": list(auto_skip.keys()),
            "reasons": {**auto_new, **auto_skip},
        }

        if check_dedup:
            lines = [
                "[TICKER-WATCH SKILL]",
                f"DATE: {today}",
                "",
                "Classify each ticker as NEW or SKIP.",
                "SKIP only if the news is semantically identical to an existing memory entry.",
                "If the news contains any genuinely new information not in memory, classify as NEW.",
                "",
            ]
            for sym, (text_clean, mem) in check_dedup.items():
                lines += [
                    f"=== {sym} ===",
                    "RAW NEWS (from Haiku web_search):",
                    text_clean,
                    "",
                    "MEMORY (last 3 entries):",
                    mem,
                    "",
                ]
            try:
                query = "\n".join(lines)
                prompt = build_warren_prompt(query)
                stdout = call_warren(prompt, "filter")
                inner = extract_inner(stdout)
                start = inner.find("{")
                end = inner.rfind("}") + 1
                if start >= 0 and end > start:
                    warren_result = json.loads(inner[start:end])
                else:
                    warren_result = {"new": list(check_dedup.keys()), "skip": [], "reasons": {}}
            except Exception as e:
                warren_result = {
                    "new": list(check_dedup.keys()),
                    "skip": [],
                    "reasons": {sym: str(e) for sym in check_dedup},
                }

            result["new"].extend(warren_result.get("new", []))
            result["skip"].extend(warren_result.get("skip", []))
            result["reasons"].update(warren_result.get("reasons", {}))

        return json.dumps(result)

    def handle_synthesize(self, body):
        """Call Warren executive-synthesis -- returns {synthesis:"..."} and writes memory."""
        news = body.get("news", {})
        today = datetime.date.today().isoformat()

        if not news:
            return json.dumps({"synthesis": "Aucune actualité pertinente aujourd'hui."})

        lines = [
            "[EXECUTIVE-SYNTHESIS SKILL]",
            f"DATE: {today}",
            "",
            "Synthesize the following new market information into today's executive briefing.",
            "",
        ]
        for sym, text in news.items():
            lines += [f"=== {sym} ===", text.strip(), ""]

        try:
            query = "\n".join(lines)
            prompt = build_warren_prompt(query)
            stdout = call_warren(prompt, "synth")
            raw = extract_inner(stdout)
            synthesis = _clean_synthesis(raw)
            for sym, text in news.items():
                text_clean = text.strip() if text else "NO_NEWS_TODAY"
                write_memory(sym, text_clean, today)
        except Exception as e:
            synthesis = f"Synthesis error: {e}"
        return json.dumps({"synthesis": synthesis})

    def handle_memorize(self, body: dict) -> str:
        """Write memory for NEW tickers only; no Warren call, no synthesis."""
        new_tickers = body.get("newTickers", [])
        all_news = body.get("allNews", {})
        today = datetime.date.today().isoformat()
        written: list[str] = []
        for sym in new_tickers:
            text = all_news.get(sym, "")
            write_memory(sym, text.strip() if text else "", today)
            written.append(sym)
        return json.dumps({"status": "ok", "written": written})

    def handle_macro_brief(self, body: dict) -> str:
        """Build and return daily Market Context Brief, independent of ticker news."""
        today = datetime.date.today().isoformat()

        macro_context: MacroContext | None = None
        if get_snapshot is not None:
            try:
                macro_context = get_snapshot()
            except Exception as exc:
                logger.warning("get_snapshot failed for macro brief: %s", exc)

        market_closes: dict | None = None
        if get_market_closes is not None:
            try:
                market_closes = get_market_closes()
            except Exception as exc:
                logger.warning("get_market_closes failed: %s", exc)

        macro_snapshot = None
        if fetch_macro_snapshot is not None:
            try:
                macro_snapshot = asyncio.run(fetch_macro_snapshot())
            except Exception as exc:
                logger.warning("fetch_macro_snapshot failed for macro brief: %s", exc)

        sector_data: dict | None = None
        if get_sector_rotation is not None:
            try:
                result = get_sector_rotation()
                sector_data = {
                    "entering": [
                        (s.ticker, s.name, s.rel_perf_1d) for s in result.entering
                    ],
                    "exiting": [
                        (s.ticker, s.name, s.rel_perf_1d) for s in result.exiting
                    ],
                    "iwm_rel_1d": result.iwm_rel_1d,
                    "small_caps_trend": result.small_caps_trend,
                }
                if result.data_issues:
                    logger.info("sector_rotation data_issues: %s", result.data_issues)
            except Exception as exc:
                logger.warning("get_sector_rotation failed: %s", exc)

        fear_greed_data: dict | None = None
        if get_fear_greed is not None:
            try:
                fg = get_fear_greed()
                if fg is not None:
                    fear_greed_data = {"score": fg.score, "label": fg.label}
            except Exception as exc:
                logger.warning("get_fear_greed failed: %s", exc)

        query = f"[MACRO-BRIEF SKILL]\nDATE: {today}\n"
        try:
            prompt = build_prompt(
                macro_context,
                query,
                macro_snapshot=macro_snapshot,
                briefing_date=today,
                market_closes=market_closes,
                sector_data=sector_data,
                fear_greed_data=fear_greed_data,
            )
            stdout = call_warren(prompt, "macro-brief")
            brief = extract_inner(stdout).strip()
            if not brief:
                brief = "Données macro insuffisantes pour le brief d'aujourd'hui."
        except Exception as exc:
            logger.error("handle_macro_brief failed: %s", exc)
            brief = f"Erreur lors de la génération du brief macro : {exc}"

        # Producer-side HTML escaping: Send Telegram runs parse_mode=HTML, so the
        # macro-brief prose must be escaped here (not in the n8n Code node).
        return json.dumps({"brief": html.escape(brief)})

    def log_message(self, *_):
        """Silence default HTTPServer request logging."""
        pass


if __name__ == "__main__":
    os.makedirs(MEMORY_DIR, exist_ok=True)

    http_server = ThreadingHTTPServer(("127.0.0.1", 18795), Handler)
    t = threading.Thread(target=http_server.serve_forever, daemon=True)
    t.start()
    print("Warren HTTP server listening on 127.0.0.1:18795")

    # HTTP-only bridge. Telegram — including /modifywatchlist and /modifyportfolio — is
    # owned by the OpenClaw gateway via workspace skills (see CLAUDE.md), not this process.
    t.join()
