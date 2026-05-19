#!/usr/bin/env python3
"""
Warren HTTP bridge for n8n.
POST /filter     — ticker-watch: classify tickers as NEW vs SKIP
POST /synthesize — executive-synthesis: markdown briefing + write memory
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess, json, os, time, datetime

OPENCLAW_CONFIG = "/home/warren/.openclaw/openclaw.json"
WORKSPACE = "/home/warren/.openclaw/workspace-warren"
MEMORY_DIR = os.path.join(WORKSPACE, "memory", "tickers")
MAX_MEMORY = 3


def read_memory(symbol):
    path = os.path.join(MEMORY_DIR, f"{symbol}.md")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_memory(symbol, raw_news, date):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, f"{symbol}.md")
    existing = read_memory(symbol)
    entries = [e.strip() for e in existing.split("\n---\n") if e.strip()] if existing else []
    entries.insert(0, f"## {date}\n{raw_news.strip()}")
    entries = entries[:MAX_MEMORY]
    with open(path, "w") as f:
        f.write("\n---\n".join(entries) + "\n")


def call_warren(message, tag):
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


def extract_inner(stdout):
    """Extract response text from OpenClaw JSON envelope.
    OpenClaw --json format: {"result": {"payloads": [{"text": "..."}], "finalAssistantVisibleText": "..."}}
    """
    try:
        outer = json.loads(stdout)
        result_obj = outer.get("result", {})
        # Primary: result.finalAssistantVisibleText (complete response)
        fat = result_obj.get("finalAssistantVisibleText")
        if fat:
            return fat
        # Fallback: result.payloads (may be truncated intermediate chunks)
        payloads = result_obj.get("payloads", [])
        if payloads and isinstance(payloads, list):
            all_text = "\n".join(p.get("text", "") for p in payloads if p.get("text"))
            if all_text:
                return all_text
        # Top-level fallback for other formats
        for key in ("response", "content", "message", "text", "output"):
            if key in outer and isinstance(outer[key], str):
                return outer[key]
        return stdout
    except Exception:
        return stdout


def _clean_synthesis(text):
    """Strip reasoning preamble and markdown code fences from Warren synthesis."""
    # Try to extract content from ```markdown ... ``` or ``` ... ``` fences
    import re
    m = re.search(r'```(?:markdown)?\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Find start of the actual briefing: first # heading
    idx = text.find("# 📈")
    if idx < 0:
        idx = text.find("# ")
    if idx > 0:
        return text[idx:].strip()
    return text.strip()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
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
        """Call Warren ticker-watch — returns {new:[...], skip:[...]}."""
        news = body.get("news", {})
        today = datetime.date.today().isoformat()

        lines = [
            "[TICKER-WATCH SKILL]",
            f"DATE: {today}",
            "",
            "Classify each ticker as NEW or SKIP based on the rules in your ticker-watch skill.",
            "",
        ]
        for sym, text in news.items():
            mem = read_memory(sym)
            lines += [
                f"=== {sym} ===",
                "RAW NEWS (from Haiku web_search):",
                text.strip() if text.strip() else "(no news retrieved)",
                "",
                "MEMORY (last 3 entries):",
                mem if mem else "(no previous entries)",
                "",
            ]

        try:
            stdout = call_warren("\n".join(lines), "filter")
            inner = extract_inner(stdout)
            start = inner.find("{")
            end = inner.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(inner[start:end])
            else:
                result = {"new": list(news.keys()), "skip": [], "reasons": {}}
        except Exception as e:
            result = {"error": str(e), "new": list(news.keys()), "skip": []}
        return json.dumps(result)

    def handle_synthesize(self, body):
        """Call Warren executive-synthesis — returns {synthesis:"..."} and writes memory."""
        news = body.get("news", {})
        today = datetime.date.today().isoformat()

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
            stdout = call_warren("\n".join(lines), "synth")
            raw = extract_inner(stdout)
            # Strip reasoning preamble and markdown code fences
            synthesis = _clean_synthesis(raw)
            for sym, text in news.items():
                if text.strip():
                    write_memory(sym, text, today)
        except Exception as e:
            synthesis = f"Synthesis error: {e}"
        return json.dumps({"synthesis": synthesis})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    os.makedirs(MEMORY_DIR, exist_ok=True)
    print("Warren HTTP server listening on 127.0.0.1:18795")
    HTTPServer(("127.0.0.1", 18795), Handler).serve_forever()
