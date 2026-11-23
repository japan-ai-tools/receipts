# Claude Code Stop hook

`hooks/stop_hook.py` runs when Claude Code finishes a turn. It:

1. reads the Stop hook input on stdin (`session_id`, `transcript_path`, `cwd`, `stop_hook_active`,
   `last_assistant_message`);
2. takes the final assistant text (`last_assistant_message`, or the last assistant text in the
   transcript if that field is missing);
3. runs `receipts check` on it in `cwd`, using the transcript as the command log;
4. if any claim is **contradicted**, prints `{"decision": "block", "reason": "..."}` so Claude keeps
   working with a short list of what was contradicted; otherwise prints nothing (stop allowed).

It never runs your tests (that would make every stop slow); it uses the commands Claude already
ran. Unverifiable claims never block. Any internal error lets Claude stop.

## Loop guard

- `stop_hook_active: false` (a normal stop) resets the counter for the session.
- While Claude is continuing because of a stop hook, receipts blocks at most
  `RECEIPTS_MAX_BLOCKS` times in a row (default 2) and never blocks twice for the identical set of
  contradictions. Claude Code also has its own cap on consecutive stop-hook continuations.
- State is a tiny JSON file per session in `$TMPDIR/receipts-hook-<uid>/` (override with
  `RECEIPTS_STATE_DIR`).

## Install

Requires Python 3.9+ and git on PATH. With the skill cloned to `~/.claude/skills/receipts`, add
this to `~/.claude/settings.json` (user-wide) or `.claude/settings.json` (one project). Merge with
any `hooks` you already have:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/skills/receipts/hooks/stop_hook.py\"",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

Then restart Claude Code (or open `/hooks` to confirm it is listed).

## Options (environment variables)

| variable | default | meaning |
| --- | --- | --- |
| `RECEIPTS_MAX_BLOCKS` | `2` | consecutive blocks per session; `0` = never block (report on stderr only) |
| `RECEIPTS_HOOK_OUT` | unset | write the receipt JSON to this path on every stop |
| `RECEIPTS_LANG` | auto | `en` or `ja` for the block reason (auto: Japanese if the message is) |
| `RECEIPTS_STATE_DIR` | `$TMPDIR/receipts-hook-<uid>` | loop-guard state location |

## Try it without Claude Code

```sh
echo '{"session_id":"demo","cwd":"'"$PWD"'","stop_hook_active":false,
       "last_assistant_message":"Done. I created src/does_not_exist.py."}' \
  | python3 hooks/stop_hook.py
# {"decision": "block", "reason": "receipts: 1 claim(s) in your completion report are contradicted ..."}
```

## Notes

- The hook input format follows the Claude Code hooks documentation (Stop event, top-level
  `decision: "block"` with `reason`). If a future version changes it, the hook fails open (lets
  Claude stop) rather than trapping the session.
- For subagents, the same script can be registered under `SubagentStop`.
