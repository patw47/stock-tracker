#!/usr/bin/env python3
"""
Warren HTTP bridge for n8n.
POST /filter     -- ticker-watch: classify tickers as NEW vs SKIP
POST /synthesize -- executive-synthesis: markdown briefing + write memory
"""
from __future__ import annotations

from collections.abc import Awaitable
from http.server import HTTPServer, BaseHTTPRequestHandler
import asyncio
import datetime
import inspect
import json
import logging
import os
import subprocess
import threading
import time
from typing import cast

try:
    from agents.warren.macro_provider import MacroProvider
except ImportError:
    MacroProvider = None

try:
    from agents.warren.macro_provider import get_snapshot, fetch_macro_snapshot
except ImportError:
    get_snapshot = None
    fetch_macro_snapshot = None

try:
    from agents.warren.models import MacroContext
    from agents.warren.prompt_builder import build_prompt
except ImportError:
    from agents.warren.models import MacroContext

    def build_prompt(macro_context: MacroContext | None, query: str) -> str:
        """Preserve existing prompt text when the prompt builder is unavailable."""
        _ = macro_context
        return query


logger = logging.getLogger(__name__)
OPENCLAW_CONFIG = "/home/warren/.openclaw/openclaw.json"
WORKSPACE = "/home/warren/.openclaw/workspace-warren"
MEMORY_DIR = os.path.join(WORKSPACE, "memory", "tickers")
MAX_MEMORY = 3


def read_memory(symbol):
    """Return last memory entries for symbol, empty string if none."""
    path = os.path.join(MEMORY_DIR, f"{symbol}.md")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_memory(symbol, raw_news, date):
    """Prepend raw_news entry for symbol; keep last MAX_MEMORY entries."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, f"{symbol}.md")
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


def _resolve_macro_result(
    result: MacroContext | Awaitable[MacroContext | None] | None,
) -> MacroContext | None:
    """Resolve sync or async macro-provider results."""
    if inspect.isawaitable(result):
        return asyncio.run(cast(Awaitable[MacroContext | None], result))
    return result


def fetch_macro_context() -> MacroContext | None:
    """Return macro context for Warren while keeping the HTTP bridge available."""
    try:
        if MacroProvider is not None:
            provider = MacroProvider()
            if callable(provider):
                return _resolve_macro_result(provider())
            if hasattr(provider, "fetch"):
                return _resolve_macro_result(provider.fetch())
            logger.warning("MacroProvider is not callable and has no fetch method")
            return None
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
        fat = result_obj.get("finalAssistantVisibleText")
        if fat and fat != "NO_REPLY":
            return fat
        payloads = result_obj.get("payloads", [])
        if payloads and isinstance(payloads, list):
            all_text = "\n".join(p.get("text", "") for p in payloads if p.get("text"))
            if all_text:
                return all_text
        raw = result_obj.get("finalAssistantRawText")
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

    def log_message(self, *_):
        """Silence default HTTPServer request logging."""
        pass


if __name__ == "__main__":
    os.makedirs(MEMORY_DIR, exist_ok=True)

    http_server = HTTPServer(("127.0.0.1", 18795), Handler)
    t = threading.Thread(target=http_server.serve_forever, daemon=True)
    t.start()
    print("Warren HTTP server listening on 127.0.0.1:18795")

    # HTTP-only bridge. Telegram — including /modifywatchlist and /modifyportfolio — is
    # owned by the OpenClaw gateway via workspace skills (see CLAUDE.md), not this process.
    t.join()
