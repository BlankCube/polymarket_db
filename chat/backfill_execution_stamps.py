"""One-shot: stamp `execution` metadata onto historical assistant messages
whose session ran under the old code (pre-eacfecb) that persisted
session_executions rows but never wrote the per-message field the UI uses
to render the Download CSV button.

Heuristic: for each session, list all rc>=0 sql executions ordered by
executed_at, list all assistant messages whose preceding user message is
a confirmation token, and — only when the two counts match exactly —
zip them 1:1. Stamp only rc>0 executions (rc=0 never emits csv_url). Skip
any session where counts don't align; we'd rather leave missing buttons
than stamp the wrong execution onto the wrong message.

Idempotent: already-stamped messages are left alone.
"""

import json
import re
import sys

sys.path.insert(0, ".")
from db_pool import get_sync_conn
import sessions_repo

_CONFIRMS = {
    "ok", "okay", "yes", "y", "yep", "yeah", "sure", "fine", "confirmed",
    "go", "run", "run it", "do it", "execute", "proceed", "go ahead",
    "yes please",
    "对", "是", "是的", "好", "好的", "行", "可以", "确认", "执行",
    "跑", "跑吧", "开始", "开始吧", "就这样", "就这样跑",
    "就这么跑", "就按这个跑", "就按这个",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s.strip().lower())


def main(apply: bool) -> None:
    conn = get_sync_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT s.session_id, s.user_id, s.conversation
           FROM user_sessions s
           WHERE s.conversation IS NOT NULL
             AND EXISTS (SELECT 1 FROM session_executions e
                         WHERE e.session_id = s.session_id AND e.code_type='sql')"""
    )
    sessions = cur.fetchall()

    total_stamped = 0
    total_sessions_touched = 0
    skipped = []

    for sid, user_id, conv in sessions:
        if isinstance(conv, str):
            conv = json.loads(conv)

        cur.execute(
            """SELECT id, (result_obj->>'row_count')::int,
                      COALESCE((result_obj->>'all_rows_truncated')::bool, false)
               FROM session_executions
               WHERE session_id=%s AND code_type='sql'
               ORDER BY executed_at""",
            (sid,),
        )
        execs = cur.fetchall()

        cands = []
        for i, m in enumerate(conv):
            if m.get("role") != "assistant":
                continue
            prev = conv[i - 1]["content"] if i > 0 and conv[i - 1].get("role") == "user" else ""
            if _norm(prev) in _CONFIRMS:
                cands.append(i)

        if len(execs) != len(cands):
            skipped.append((sid, len(execs), len(cands)))
            continue

        dirty = False
        for (eid, rc, trunc), midx in zip(execs, cands):
            m = conv[midx]
            if rc <= 0:
                continue
            if "execution" in m:
                continue
            m["execution"] = {
                "execution_id": int(eid),
                "row_count": int(rc),
                "truncated": bool(trunc),
                "csv_url": f"/api/execution/{int(eid)}/csv",
            }
            total_stamped += 1
            dirty = True
            print(f"  stamp sid={sid[:12]} msg[{midx}] exec_id={eid} rc={rc}")

        if dirty:
            total_sessions_touched += 1
            if apply:
                sessions_repo.save_messages(sid, user_id, conv)

    conn.close()

    print()
    print(f"sessions examined: {len(sessions)}")
    print(f"sessions touched:  {total_sessions_touched}")
    print(f"messages stamped:  {total_stamped}")
    if skipped:
        print(f"skipped (count mismatch): {len(skipped)}")
        for sid, ne, nc in skipped:
            print(f"  {sid[:12]}: execs={ne} cands={nc}")
    print(f"mode: {'APPLIED' if apply else 'DRY-RUN (pass --apply to write)'}")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
