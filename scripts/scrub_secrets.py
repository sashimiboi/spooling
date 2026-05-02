"""Scrub secrets from the LOCAL Spool DB (the Postgres docker container).

Same regex set as the redactor that runs at push time. Use this to clean up
sessions that were synced before the redactor existed, or to run a periodic
defense-in-depth pass.

Tables scrubbed:
  messages.content       (text)
  spans.tool_input        (jsonb)
  spans.tool_output       (text)
  spans.agent_prompt      (text)
  spans.attrs             (jsonb)
  span_events.attrs       (jsonb)

Usage:
  cd /Users/anthonyloya/spool
  ./.venv/bin/python scripts/scrub_secrets.py            # dry-run
  ./.venv/bin/python scripts/scrub_secrets.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running this file directly without installing spool first.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spool.db import get_connection  # noqa: E402
from spool.redact import redact_text, redact_value  # noqa: E402


COARSE_RE = (
    r'(eyJ[A-Za-z0-9_\-]{10,}\.|AKIA|ASIA|ghp_|gho_|ghs_|sk-|sk_live_|sk_test_'
    r'|xox[abprs]-|AIza|BEGIN PRIVATE KEY|PASSWORD|SECRET|TOKEN|API_?KEY|PAT=)'
)


def scrub_messages(conn, apply: bool) -> tuple[int, int, int]:
    scanned = changed = total_hits = 0
    cur = conn.execute(
        "SELECT id, content FROM messages WHERE content ~ %s", (COARSE_RE,),
    )
    rows = cur.fetchall()
    for r in rows:
        scanned += 1
        new, hits = redact_text(r["content"] or "")
        if hits:
            changed += 1
            total_hits += len(hits)
            if apply:
                conn.execute("UPDATE messages SET content = %s WHERE id = %s",
                             (new, r["id"]))
    if apply:
        conn.commit()
    return scanned, changed, total_hits


def scrub_spans(conn, apply: bool) -> tuple[int, int, int]:
    scanned = changed = total_hits = 0
    cur = conn.execute(
        """SELECT id, tool_input, tool_output, agent_prompt, attrs
             FROM spans
            WHERE coalesce(tool_input::text, '') ~ %s
               OR coalesce(tool_output, '')      ~ %s
               OR coalesce(agent_prompt, '')     ~ %s
               OR attrs::text                    ~ %s""",
        (COARSE_RE, COARSE_RE, COARSE_RE, COARSE_RE),
    )
    rows = cur.fetchall()
    for r in rows:
        scanned += 1
        updates: dict[str, object] = {}
        if r["tool_input"] is not None:
            new_in, n = redact_value(r["tool_input"])
            if n:
                updates["tool_input"] = json.dumps(new_in)
                total_hits += n
        if r["tool_output"]:
            new_out, hits = redact_text(r["tool_output"])
            if hits:
                updates["tool_output"] = new_out
                total_hits += len(hits)
        if r["agent_prompt"]:
            new_p, hits = redact_text(r["agent_prompt"])
            if hits:
                updates["agent_prompt"] = new_p
                total_hits += len(hits)
        if r["attrs"]:
            new_attrs, n = redact_value(r["attrs"])
            if n:
                updates["attrs"] = json.dumps(new_attrs)
                total_hits += n
        if updates:
            changed += 1
            if apply:
                set_clause = ", ".join(f"{k} = %s" for k in updates)
                conn.execute(
                    f"UPDATE spans SET {set_clause} WHERE id = %s",
                    list(updates.values()) + [r["id"]],
                )
    if apply:
        conn.commit()
    return scanned, changed, total_hits


def scrub_span_events(conn, apply: bool) -> tuple[int, int, int]:
    cur = conn.execute("SELECT to_regclass('span_events')")
    if cur.fetchone()["to_regclass"] is None:
        return 0, 0, 0
    scanned = changed = total_hits = 0
    cur = conn.execute(
        "SELECT id, attrs FROM span_events WHERE attrs::text ~ %s", (COARSE_RE,),
    )
    rows = cur.fetchall()
    for r in rows:
        scanned += 1
        new_attrs, n = redact_value(r["attrs"] or {})
        if n:
            changed += 1
            total_hits += n
            if apply:
                conn.execute(
                    "UPDATE span_events SET attrs = %s WHERE id = %s",
                    (json.dumps(new_attrs), r["id"]),
                )
    if apply:
        conn.commit()
    return scanned, changed, total_hits


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually write changes. Default is a dry-run.")
    args = p.parse_args()

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== Local Spool DB scrub ({mode}) ===")

    conn = get_connection()
    try:
        print("\n[1/3] messages …")
        m_s, m_c, m_h = scrub_messages(conn, args.apply)
        print(f"     scanned {m_s}; {m_c} need scrubbing ({m_h} secrets)")

        print("\n[2/3] spans …")
        s_s, s_c, s_h = scrub_spans(conn, args.apply)
        print(f"     scanned {s_s}; {s_c} need scrubbing ({s_h} secrets)")

        print("\n[3/3] span_events …")
        e_s, e_c, e_h = scrub_span_events(conn, args.apply)
        print(f"     scanned {e_s}; {e_c} need scrubbing ({e_h} secrets)")
    finally:
        conn.close()

    total_changed = m_c + s_c + e_c
    total_hits = m_h + s_h + e_h
    print()
    if args.apply:
        print(f"=== Done. {total_changed} row(s) updated, {total_hits} secret(s) redacted. ===")
    else:
        print(f"=== Dry run. {total_changed} row(s) would be updated, "
              f"{total_hits} secret(s) found. Re-run with --apply to write. ===")


if __name__ == "__main__":
    main()
