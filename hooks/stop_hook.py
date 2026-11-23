#!/usr/bin/env python3
"""Claude Code Stop hook for receipts.

Reads the Stop hook input (JSON on stdin), takes the agent's final message
(`last_assistant_message`, or the last assistant text in `transcript_path`),
checks its claims against the repository in `cwd` (using the transcript as the
command log), and blocks the stop only when a claim is CONTRADICTED.

Output when blocking (documented Stop decision control):
    {"decision": "block", "reason": "..."}
Anything else (no claims, only verified/unverifiable claims, errors) lets
Claude stop normally: exit 0 with no output.

Loop guard:
  * stop_hook_active == false resets the per-session block counter;
  * while Claude is already continuing because of a stop hook, receipts blocks
    at most RECEIPTS_MAX_BLOCKS times in a row (default 2), then lets it stop;
  * an identical set of contradictions is never blocked twice in a row.

Environment variables (all optional):
  RECEIPTS_MAX_BLOCKS  consecutive blocks per session (default 2; 0 = report only)
  RECEIPTS_HOOK_OUT    path to write the receipt JSON (default: do not write)
  RECEIPTS_LANG        "en" or "ja" for the block reason (default: en)
  RECEIPTS_STATE_DIR   where the small loop-guard state files live
                       (default: <tmp>/receipts-hook-<uid>)
"""

import hashlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import receipts  # noqa: E402

DEFAULT_MAX_BLOCKS = 2


def _state_dir():
    d = os.environ.get("RECEIPTS_STATE_DIR")
    if not d:
        uid = getattr(os, "getuid", lambda: "user")()
        d = os.path.join(tempfile.gettempdir(), "receipts-hook-%s" % uid)
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default")[:120]
    return os.path.join(_state_dir(), safe + ".json")


def load_state(session_id):
    try:
        with open(_state_path(session_id), encoding="utf-8") as fh:
            st = json.load(fh)
            if isinstance(st, dict):
                return st
    except (OSError, ValueError):
        pass
    return {"blocks": 0, "last_key": None}


def save_state(session_id, state):
    try:
        path = _state_path(session_id)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def _max_blocks():
    try:
        return max(0, int(os.environ.get("RECEIPTS_MAX_BLOCKS", DEFAULT_MAX_BLOCKS)))
    except ValueError:
        return DEFAULT_MAX_BLOCKS


def build_reason(contradicted, lang="en"):
    if lang == "ja":
        head = "receipts: 完了報告のうち %d 件が実際の状態と矛盾しています。" % len(contradicted)
        tail = ("修正して報告を直すか、事実に合わせて報告を訂正してください"
                "(確認できないことは「未確認」と書いてください)。")
    else:
        head = "receipts: %d claim(s) in your completion report are contradicted by the repository." \
               % len(contradicted)
        tail = ("Fix the work or correct the report; say plainly what you did not verify.")
    lines = [head]
    for c in contradicted[:3]:
        lines.append("- \"%s\" -> %s" % (c["text"][:100], c["reason"][:200]))
    if len(contradicted) > 3:
        lines.append("- ... and %d more" % (len(contradicted) - 3))
    lines.append(tail)
    return "\n".join(lines)


def decide(payload, env_lang=None):
    """Return (output_dict_or_None, receipt_or_None). Pure apart from the state file."""
    session = str(payload.get("session_id") or "default")
    active = bool(payload.get("stop_hook_active"))
    state = load_state(session)
    if not active:
        state = {"blocks": 0, "last_key": None}

    text = payload.get("last_assistant_message")
    transcript = payload.get("transcript_path")
    if transcript:
        transcript = os.path.expanduser(transcript)
        if not os.path.isfile(transcript):
            transcript = None
    if not (isinstance(text, str) and text.strip()) and transcript:
        try:
            text = receipts.parse_log(transcript).get("last_assistant_message")
        except receipts.BadInput:
            text = None
    if not (isinstance(text, str) and text.strip()):
        save_state(session, state)
        return None, None

    cwd = payload.get("cwd") or os.getcwd()
    out = os.environ.get("RECEIPTS_HOOK_OUT") or None
    receipt = receipts.run_check(text, cwd, log_path=transcript, out_path=out)
    if out:
        try:
            receipts.write_receipt(receipt, out)
        except OSError:
            pass
    contradicted = [c for c in receipt["claims"] if c["status"] == "contradicted"]
    if not contradicted:
        state["last_key"] = None
        save_state(session, state)
        return None, receipt

    key = hashlib.sha256("\n".join(sorted(c["reason"] for c in contradicted)).encode()).hexdigest()
    limit = _max_blocks()
    if state.get("blocks", 0) >= limit or (active and key == state.get("last_key")):
        # loop guard: let Claude stop, but say why on stderr (shown in verbose mode)
        sys.stderr.write("receipts: not blocking again (loop guard); %d contradicted claim(s) remain\n"
                         % len(contradicted))
        save_state(session, state)
        return None, receipt
    state["blocks"] = state.get("blocks", 0) + 1
    state["last_key"] = key
    save_state(session, state)
    lang = env_lang or os.environ.get("RECEIPTS_LANG") or ("ja" if receipts.has_japanese(text) else "en")
    return {"decision": "block", "reason": build_reason(contradicted, lang)}, receipt


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        output, _ = decide(payload)
    except Exception as exc:  # noqa: BLE001 - a broken hook must never trap the session
        sys.stderr.write("receipts stop hook error (allowing stop): %s\n" % exc)
        return 0
    if output:
        stdout.write(json.dumps(output, ensure_ascii=False))
        stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
