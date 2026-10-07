#!/usr/bin/env python3
"""Run watch.py on Gemini: free models first, then one optional paid request via OpenRouter.

Order, one attempt each, never a loop (failed attempts count toward the free quota):
  1. gemini-3.7-flash on the free key (agentic video)
  2. gemini-3.8-flash on the free key; free quotas are per model, 20 requests/day each
  3. google/gemini-3.7-flash through OpenRouter, paid. Only when 1-2 failed on a daily limit
     (HTTP 429) or Google overload (HTTP 503), only when OPENROUTER_API_KEY is set, and only
     for YouTube links: OpenRouter's Google AI Studio route takes a YouTube URL, a local file
     would have to be sent inline. Every paid call is appended to ~/.config/watch/paid-usage.log
     with its cost.

Usage: watch-gemini.py <url-or-path> [watch.py options...]   (--engine gemini is forced)
Settings, from the environment or ~/.config/watch/.env (keys are never printed):
       GEMINI_API_KEY          free key, read by watch.py
       OPENROUTER_API_KEY      enables the paid step
       WATCH_GEMINI_MODELS     free model order (default below)
       WATCH_OPENROUTER_MODEL  paid model (default google/gemini-3.7-flash)
       WATCH_NO_PAID=1         never make the paid request
       WATCH_SCRIPT            path to watch.py (default: next to this file, environment only)
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config/watch/.env"


def config_value(name):
    value = os.environ.get(name, "").strip()
    if value or not CONFIG.is_file():
        return value
    for line in CONFIG.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


MODELS = [m.strip() for m in (config_value("WATCH_GEMINI_MODELS")
                              or "gemini-3.7-flash,gemini-3.8-flash").split(",") if m.strip()]
WATCH = Path(os.environ.get("WATCH_SCRIPT", Path(__file__).resolve().with_name("watch.py")))
PAID_LOG = Path.home() / ".config/watch/paid-usage.log"
PAID_MODEL = config_value("WATCH_OPENROUTER_MODEL") or "google/gemini-3.7-flash"
# quota = HTTP 429 (daily limit), service = HTTP 503 (Google overloaded): another model may still work.
SWITCH_ON = {"quota", "service"}
REASON = {"quota": "daily limit reached", "service": "Google overloaded"}
YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")
# watch.py appends the same instruction to every Gemini question; keep both paths alike.
DEFAULT_QUESTION = ("Give a thorough summary of this video: what is shown, any on-screen text or code, "
                    "and what is said, in chronological order.")
SUFFIX = ("\n\nCite an MM:SS (or H:MM:SS) timestamp for every claim about a specific moment. "
          "Separate what is seen from what is heard when it matters. If the video does not "
          "show or say something needed to answer, say so plainly instead of guessing.")


def strip_engine(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == "--engine":
            skip = True
        elif not a.startswith("--engine="):
            out.append(a)
    return out


def option(args, name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def log_paid(status, cost, url):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"{stamp} | {status} | ${cost} | {PAID_MODEL} | {url}\n"
    try:  # a sandbox (e.g. Codex) may forbid writing here; never lose a paid answer over the log
        PAID_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PAID_LOG.open("a") as f:
            f.write(line)
        PAID_LOG.chmod(0o600)
    except OSError as e:
        print(f"[watch-gemini] could not write the spending log ({e}): {line.strip()}", file=sys.stderr)


def paid(source, args, tried):
    """One request through OpenRouter. Returns (exit code, None) or (None, why it was skipped)."""
    m = YOUTUBE.search(source)
    key = config_value("OPENROUTER_API_KEY")
    if config_value("WATCH_NO_PAID") == "1":
        return None, "paid request disabled (WATCH_NO_PAID=1)"
    if not key:
        return None, "no paid request: OPENROUTER_API_KEY is not set"
    if not m:
        return None, "no paid request: not a YouTube link"
    url = f"https://www.youtube.com/watch?v={m.group(1)}"
    question = option(args, "--question") or DEFAULT_QUESTION
    start, end = option(args, "--start"), option(args, "--end")
    if start or end:  # OpenRouter has no clip option: the range goes into the question
        question = f"Watch only the part of the video from {start or '0:00'} to {end or 'the end'}.\n\n{question}"
    print(f"[watch-gemini] the free models did not answer, making one paid request through "
          f"OpenRouter ({PAID_MODEL})", file=sys.stderr)
    payload = {
        "model": PAID_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question + SUFFIX},
            {"type": "video_url", "video_url": {"url": url}},
        ]}],
        "provider": {"order": ["google-ai-studio"], "allow_fallbacks": False},  # YouTube links: AI Studio only
        "max_tokens": 12000,  # cost ceiling for the answer
        "usage": {"include": True},
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "watch"})
    t = time.time()
    print("\n# watch: video report\n")
    print(f"- **Source:** {source} (YouTube URL sent to Google via OpenRouter, paid)")
    print(f"- **Free tier:** " + "; ".join(tried))
    try:
        data = json.load(urllib.request.urlopen(req, timeout=600))
        if "error" in data:
            raise RuntimeError(json.dumps(data["error"], ensure_ascii=False)[:300])
        text = data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        error = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:  # network, timeout, malformed reply
        error = str(e)[:300]
    else:
        u = data.get("usage") or {}
        log_paid("ok", u.get("cost"), url)
        print(f"- **Engine:** {data.get('model')} via OpenRouter ({data.get('provider')}), {time.time() - t:.0f} s")
        print(f"- **Cost:** ${u.get('cost')} (input {u.get('prompt_tokens')}, output {u.get('completion_tokens')} tokens)")
        print("\n## Answer (from Gemini)\n")
        print("_These are Gemini's observations of the video, not frames you viewed yourself. "
              "Relay them as such._\n")
        print(text)
        print(f"[watch-gemini] answered by paid {PAID_MODEL}", file=sys.stderr)
        return 0, None
    log_paid("failed", 0, url)
    print(f"- **Engine:** {PAID_MODEL} via OpenRouter (failed)")
    print(f"\n## Unavailable evidence\n\n- OpenRouter: {error}")
    print(f"\n[watch-gemini] no model answered: " + "; ".join(tried) + f"; paid {PAID_MODEL} failed")
    return 1, None


def main():
    if not WATCH.is_file():
        sys.exit(f"[watch-gemini] watch.py not found: {WATCH}")
    raw = sys.argv[1:]
    if not raw:
        sys.exit("usage: watch-gemini.py <url-or-path> [watch.py options...]")
    args = strip_engine(raw) + ["--engine", "gemini"]
    tried, category = [], None
    for model in MODELS:
        env = dict(os.environ, WATCH_GEMINI_MODEL=model)
        proc = subprocess.run([sys.executable, str(WATCH), *args], env=env,
                              capture_output=True, text=True)
        sys.stderr.write(proc.stderr)
        if proc.returncode == 0:
            if tried:
                print(f"[watch-gemini] answered by {model}; before it: " + "; ".join(tried), file=sys.stderr)
            sys.stdout.write(proc.stdout)
            return 0
        m = re.search(r"Gemini (\w+):", proc.stdout)
        category = m.group(1) if m else "unknown"
        code = re.search(r"HTTP (\d+)", proc.stdout)
        tried.append(f"{model}: {REASON.get(category, category)}"
                     + (f" (HTTP {code.group(1)})" if code else ""))
        if category not in SWITCH_ON:
            break  # auth, rejected, upload…: another model won't help
        if model != MODELS[-1]:
            print(f"[watch-gemini] {tried[-1]}, trying the next model", file=sys.stderr)
    if category in SWITCH_ON:
        rc, skipped = paid(raw[0], raw, tried)
        if rc is not None:
            return rc
        tried.append(skipped)
    sys.stdout.write(proc.stdout)
    print(f"\n[watch-gemini] no model answered: " + "; ".join(tried))
    return proc.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
