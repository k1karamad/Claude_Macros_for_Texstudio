#!/usr/bin/env python3
"""
Claude helper for TeXstudio macros (Persian thesis edition).

The macro writes a small JSON "job" file and passes it with --input-file:
    {
      "mode":        "chat" | "replace" | "review" | "whole",
      "label":       "name shown in the report header",
      "instruction": "what Claude should do with the text",
      "text":        "selected text (or the whole editor text for 'whole')",
      "mark":        true -> wrap the result in %N lines (N = running counter, claude_counter.txt),
      "source_file": "path of the current .tex file (used by 'whole' to expand \\input/\\include)"
    }

Modes
  chat     free-form: `text` is sent as-is, the answer replaces the selection.
  replace  edit: the corrected/rewritten text replaces the selection.
  review   report: the ORIGINAL text is kept and a report is added right after it
           as LaTeX comment lines (each line starts with "% ").
  whole    like review, but for the whole document (\\input/\\include files are
           inlined); only the report is written (at the cursor).

Safety: if anything goes wrong (API error, truncated answer, ...) the original
selection is written back unchanged, followed by a "% ERROR: ..." comment.

Settings (environment variable -> claude_config.json next to this script -> default)
  CLAUDE_API_KEY          required (ANTHROPIC_API_KEY also accepted)
  CLAUDE_BASE_URL         https://api.anthropic.com
  CLAUDE_MODEL            claude-sonnet-5
  CLAUDE_API_FORMAT       anthropic | openai   (openai = OpenAI-compatible gateway)
  CLAUDE_MAX_TOKENS       16000
  CLAUDE_TIMEOUT          300   (seconds)
  CLAUDE_MAX_INPUT_CHARS  400000
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULTS = {
    "CLAUDE_BASE_URL": "https://api.anthropic.com",
    "CLAUDE_MODEL": "claude-sonnet-5",
    "CLAUDE_API_FORMAT": "anthropic",
    "CLAUDE_MAX_TOKENS": "16000",
    "CLAUDE_TIMEOUT": "300",
    "CLAUDE_MAX_INPUT_CHARS": "400000",
}

# ---- Edit this if your thesis setup differs (e.g. not xepersian) -------------
BASE_SYSTEM = (
    "You are an expert assistant embedded in the TeXstudio LaTeX editor, helping "
    "the author write a Persian (Farsi) physics PhD thesis. The thesis is typeset "
    "with LaTeX (typically XeLaTeX with the xepersian package), so the text mixes "
    "right-to-left Persian prose with left-to-right English terms, mathematics and "
    "LaTeX commands. Preserve LaTeX commands, math, labels and citation keys "
    "exactly unless the task explicitly says to change them."
)
EDIT_RULES = (
    " You are a DIRECT EDITOR of the LaTeX source, not a reporter. Your reply is "
    "pasted straight over the original text, so output ONLY the final corrected "
    "text: no report, list of issues, explanation, preamble, conclusion or "
    "suggestions; no markdown; no code fences (never ```latex); no <text> tags. "
    "Apply every correction directly. Keep the scientific meaning, structure, "
    "symbols, names, citations and numbering; add nothing that is not in the "
    "text; do not change technical terms unless their current use is wrong. "
    "Fix LaTeX errors directly (formulas, refs, labels, environments, spacing). "
    "Leave text that needs no fix unchanged. If you are unsure whether something "
    "is scientifically or semantically wrong, do not guess: leave it unchanged. "
    "For Persian follow Persian orthography and proper XePersian structure."
)
REVIEW_RULES = (
    " Write your review report in Persian as plain text only (no markdown, no "
    "LaTeX environments, no code fences, no tables). The report will be inserted "
    "into the document as LaTeX comments. Number the findings; for each one quote "
    "a SHORT fragment, explain the problem, and propose a fix. Put the most "
    "important findings first, skip trivia, and never restate the whole text. "
    "Do not fabricate anything: if you are unsure whether something is wrong, say "
    "so explicitly (use the phrase «نیاز به بررسی»). If you find no real problems, "
    "say so in one sentence."
)
# -----------------------------------------------------------------------------

MODES = ("chat", "replace", "review", "whole")
INCLUDE_RE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")


COUNTER_FILE = Path(__file__).resolve().parent / "claude_counter.txt"


def next_count():
    """Running number used for the %N markers (stored next to this script)."""
    try:
        n = int(COUNTER_FILE.read_text().strip()) + 1
    except Exception:
        n = 1
    try:
        COUNTER_FILE.write_text(str(n))
    except Exception:
        pass
    return n


class JobError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def load_config_file():
    cfg_path = Path(__file__).resolve().parent / "claude_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


CONFIG_FILE = load_config_file()


def setting(name):
    value = os.environ.get(name) or CONFIG_FILE.get(name)
    return value if value not in (None, "") else DEFAULTS.get(name)


def parse_job(raw):
    """New JSON job, or (legacy) plain prompt text."""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "text" in obj:
            mode = obj.get("mode", "chat")
            if mode not in MODES:
                raise JobError(3, f"Unknown mode '{mode}'.")
            return {
                "mode": mode,
                "label": str(obj.get("label", "") or ""),
                "instruction": str(obj.get("instruction", "") or ""),
                "text": str(obj.get("text", "") or ""),
                "source_file": str(obj.get("source_file", "") or ""),
                "mark": bool(obj.get("mark", False)),
            }
    except ValueError:
        pass
    return {"mode": "chat", "label": "", "instruction": "", "text": raw, "source_file": "", "mark": False}


# --------------------------- \input / \include expansion ----------------------
def _commented(line, pos):
    return re.search(r"(?<!\\)%", line[:pos]) is not None


def _resolve(base_dir, name):
    for cand in (base_dir / name, base_dir / (name + ".tex")):
        if cand.is_file():
            return cand
    return None


def expand_includes(text, base_dir, depth=0, stack=()):
    if depth > 3:
        return text
    out = []
    for line in text.splitlines(keepends=True):
        m = INCLUDE_RE.search(line)
        if m and not _commented(line, m.start()):
            name = m.group(1).strip()
            path = _resolve(base_dir, name)
            if path and path not in stack:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    out.append(line)
                    continue
                out.append(f"%%% ===== begin file: {name} =====\n")
                out.append(expand_includes(content, base_dir, depth + 1, stack + (path,)))
                if not content.endswith("\n"):
                    out.append("\n")
                out.append(f"%%% ===== end file: {name} =====\n")
                continue
        out.append(line)
    return "".join(out)


# --------------------------------- API layer ----------------------------------
def build_request(fmt, base_url, api_key, model, max_tokens, system, user):
    base = base_url.rstrip("/")
    if fmt == "openai":
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    else:
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",  # some gateways want Bearer instead
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    return url, headers, body


def extract_text(fmt, data):
    """Return (text, truncated)."""
    if fmt == "openai":
        choice = data["choices"][0]
        return choice["message"]["content"] or "", choice.get("finish_reason") == "length"
    parts = data.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return text, data.get("stop_reason") == "max_tokens"


def call_claude(system, user):
    api_key = (setting("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
               or CONFIG_FILE.get("ANTHROPIC_API_KEY"))
    if not api_key:
        raise JobError(2, "No API key found. Put CLAUDE_API_KEY and CLAUDE_BASE_URL in "
                          "claude_config.json next to claude_python_script.py.")
    fmt = str(setting("CLAUDE_API_FORMAT")).lower()
    if fmt not in ("anthropic", "openai"):
        raise JobError(3, f"CLAUDE_API_FORMAT must be 'anthropic' or 'openai', got '{fmt}'.")

    url, headers, body = build_request(
        fmt, setting("CLAUDE_BASE_URL"), api_key, setting("CLAUDE_MODEL"),
        int(setting("CLAUDE_MAX_TOKENS")), system, user,
    )
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=int(setting("CLAUDE_TIMEOUT"))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400].replace("\n", " ")
        raise JobError(6, f"API returned HTTP {exc.code} from {url} : {detail}")
    except urllib.error.URLError as exc:
        raise JobError(7, f"Could not reach {url}: {exc.reason}")
    return extract_text(fmt, data)


# ------------------------------ output formatting -----------------------------
def strip_wrappers(text):
    m = re.fullmatch(r"\s*```[a-zA-Z0-9_-]*\n(.*?)\n?```\s*", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    m = re.fullmatch(r"\s*<text>\n?(.*?)\n?</text>\s*", text, flags=re.DOTALL)
    return m.group(1) if m else text


def comment_block(label, report, note=""):
    head = f"% ====== Claude | {label} ======" if label else "% ====== Claude ======"
    lines = [head]
    for ln in report.strip().splitlines():
        ln = ln.rstrip()
        lines.append("% " + ln if ln else "%")
    if note:
        lines.append("% " + note)
    lines.append("% ====== end of report ======")
    return "\n".join(lines) + "\n"


def with_newline(text):
    return text if text.endswith("\n") else text + "\n"


def emit_error(err, original):
    """Never lose the user's text: write it back, then the error as comments."""
    out = with_newline(original) if original else ""
    out += "".join(f"% ERROR: {ln}\n" for ln in err.msg.splitlines() or [""])
    sys.stdout.write(out)
    sys.stdout.flush()
    sys.exit(err.code)


# ------------------------------------ main ------------------------------------
def run(job):
    mode, text = job["mode"], job["text"]
    if not text.strip():
        raise JobError(4, "The selected text is empty.")

    if mode == "whole" and job["source_file"]:
        src = Path(job["source_file"])
        text = expand_includes(text, src.parent, stack=(src,))

    instr = job["instruction"].strip()
    if mode == "chat" or not instr:
        user = text
    else:
        tag = "thesis" if mode == "whole" else "text"
        user = f"{instr}\n\n<{tag}>\n{text}\n</{tag}>"

    limit = int(setting("CLAUDE_MAX_INPUT_CHARS"))
    if len(user) > limit:
        raise JobError(9, f"Text too long ({len(user)} characters, limit {limit}). "
                          "Select a smaller part, or raise CLAUDE_MAX_INPUT_CHARS.")

    system = BASE_SYSTEM + (REVIEW_RULES if mode in ("review", "whole") else EDIT_RULES)
    answer, truncated = call_claude(system, user)
    if not answer.strip():
        raise JobError(5, "Claude returned no text output.")

    original = job["text"]
    if mode in ("review", "whole"):
        note = ("(report cut off: output limit reached - raise CLAUDE_MAX_TOKENS "
                "or use a smaller selection)") if truncated else ""
        block = comment_block(job["label"], answer, note)
        return block if mode == "whole" else with_newline(original) + block

    if truncated:
        raise JobError(10, "Answer was cut off (output limit reached), so your text was "
                           "left unchanged. Select a smaller part or raise CLAUDE_MAX_TOKENS.")
    out = strip_wrappers(answer).lstrip("\n").rstrip("\n")
    if job["mark"]:
        n = next_count()
        # closing marker always ends with a newline so text after the selection is never commented out
        return f"%{n}\n{out}\n%{n}\n"
    if mode == "replace" and original.endswith("\n"):
        out += "\n"
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", required=True)
    args = parser.parse_args()
    input_path = Path(args.input_file)

    job = None
    try:
        try:
            job = parse_job(input_path.read_text(encoding="utf-8"))
            output = run(job)
        except JobError as err:
            keep = job["text"] if job and job["mode"] != "whole" else None
            emit_error(err, keep)
        except Exception as exc:  # anything unexpected
            keep = job["text"] if job and job["mode"] != "whole" else None
            emit_error(JobError(8, f"Claude request failed: {exc}"), keep)
        sys.stdout.write(output)
        sys.stdout.flush()
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
