"""One-time authentication links stay one-time under concurrent requests."""
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import sqlalchemy as sa

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-auth-token-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import auth, db  # noqa: E402

db.init()
fails = []


def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


user, _ = auth._create("token@example.com", "Token", password_hash="x")
token = auth._make_link(user.id, "verify", 3600)
gate = threading.Barrier(2)


def meet_after_reading_token(_conn, _cursor, statement, _parameters, _context, _many):
    if "email_tokens" not in statement.lower() or not statement.lstrip().lower().startswith("select"):
        return
    try:
        gate.wait(timeout=0.5)
    except threading.BrokenBarrierError:
        pass  # the atomic path only reads after the losing update, so no second reader is expected


def consume_token():
    try:
        return "used", auth._use_link(token, "verify")
    except auth.AuthError:
        return "refused", None
    except Exception as e:  # noqa: BLE001 - report unsafe database races by exception name
        return type(e).__name__, None


sa.event.listen(db.engine, "after_cursor_execute", meet_after_reading_token)
try:
    with ThreadPoolExecutor(2) as pool:
        results = [f.result(timeout=10) for f in (pool.submit(consume_token), pool.submit(consume_token))]
finally:
    sa.event.remove(db.engine, "after_cursor_execute", meet_after_reading_token)

check("a verification link can be consumed exactly once under concurrency",
      (sorted(x[0] for x in results), [x[1] for x in results if x[0] == "used"]),
      (["refused", "used"], [user.id]))

print("ALL PASSED" if not fails else f"{len(fails)} failed")
db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
