"""Big files sent in pieces: they arrive whole, and nobody can misuse the pieces address.
Usage: test_upload_parts.py <base url of a running test server>"""
import asyncio
import hashlib
import os
import sys
import threading
import time
from pathlib import Path

import httpx

B = sys.argv[1]
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"   {extra}" if extra else ""))
    if not ok:
        fails.append(name)


def user(email):
    c = httpx.Client(base_url=B, headers={"Origin": B}, timeout=120)
    c.post("/api/auth/signup", json={"email": email, "password": "test-pass-123", "name": "U"})
    return c


a, b = user("pieces-a@example.com"), user("pieces-b@example.com")
# uploading needs a confirmed email; the test server sends no email, so the two accounts are confirmed in its database
sys.path.insert(0, os.getcwd())
import time                                    # noqa: E402

import sqlalchemy as sa                        # noqa: E402

from studio import db                          # noqa: E402

db.init()
with db.engine.begin() as cn:
    users = dict(cn.execute(sa.select(db.users.c.email, db.users.c.id).where(db.users.c.email.like("pieces-%"))).all())
    for uid in users.values():
        cn.execute(db.email_ok.insert().values(user_id=uid, verified_at=time.time()))
st = a.get("/api/status").json()
part_mb = st["upload_part_mb"]
data = os.urandom(int(part_mb * 1024 * 1024 * 2.4))            # two full pieces and a shorter last one
H = {"Content-Type": "application/octet-stream"}

s = a.post("/api/upload/start", json={"name": "big video.mp4", "size": len(data), "options": {"max_clips": 2}, "creator": ""}).json()
check("a big file is split into pieces", s.get("parts") == 3, str(s))
part = s["part"]
piece = lambda i: data[i * part:(i + 1) * part]                # noqa: E731
check("someone else cannot add to my upload", b.put(f"/api/upload/{s['id']}/0", content=piece(0), headers=H).status_code == 404)
check("a piece of the wrong size is refused", a.put(f"/api/upload/{s['id']}/0", content=piece(0)[:-5], headers=H).status_code == 400)
check("a piece number that does not exist is refused", a.put(f"/api/upload/{s['id']}/9", content=b"x", headers=H).status_code == 400)
for i in (2, 0):
    check(f"piece {i + 1} is accepted", a.put(f"/api/upload/{s['id']}/{i}", content=piece(i), headers=H).status_code == 200)
r = a.post(f"/api/upload/{s['id']}/finish", json={})
check("joining before every piece has arrived is refused", r.status_code == 409)
a.put(f"/api/upload/{s['id']}/1", content=piece(1), headers=H)
a.put(f"/api/upload/{s['id']}/1", content=piece(1), headers=H)   # sent again after a retry: still one piece
check("someone else cannot finish my upload", b.post(f"/api/upload/{s['id']}/finish", json={}).status_code == 404)
r = a.post(f"/api/upload/{s['id']}/finish", json={})
job = r.json()
check("the pieces are joined into a video", r.status_code == 200 and bool(job.get("id")), r.text[:120])
check("finishing twice does not make a second video", a.post(f"/api/upload/{s['id']}/finish", json={}).status_code == 404)
data_dir = os.environ.get("PEAKCUT_DATA")
if data_dir and job.get("id"):
    src = list(Path(data_dir).glob(f"users/*/jobs/{job['id']}/source.mp4"))
    check("the file arrived byte for byte", bool(src) and hashlib.sha256(src[0].read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest())
    check("no pieces are left behind", not list(Path(data_dir).glob("tmp/uploads/up-*")))
check("someone else does not see my video", all(j["id"] != job.get("id") for j in b.get("/api/jobs").json()))
limit = st["max_upload_mb"] * 1024 * 1024
check("a file over the limit is refused before anything is sent", a.post("/api/upload/start", json={"name": "x.mp4", "size": limit + 1}).status_code == 413)
check("a file that is not a video is refused", a.post("/api/upload/start", json={"name": "x.exe", "size": 5000}).status_code == 400)
check("a made-up size is refused", a.post("/api/upload/start", json={"name": "x.mp4", "size": -4}).status_code == 400)
check("without signing in nothing can be started", httpx.post(B + "/api/upload/start", json={"name": "x.mp4", "size": 10}, headers={"Origin": B}).status_code == 401)
big = a.put(f"/api/upload/{'0' * 24}/0", content=b"x" * ((part_mb + 9) * 1024 * 1024), headers=H)
check("an oversized request to the pieces address is cut off", big.status_code == 413, str(big.status_code))
c = a.post("/api/upload/start", json={"name": "y.mp4", "size": 1000}).json()
check("an upload can be cancelled", a.delete(f"/api/upload/{c['id']}").status_code == 200 and a.put(f"/api/upload/{c['id']}/0", content=b"x" * 1000, headers=H).status_code == 404)

# A piece may arrive while another worker is assembling that upload. Waiting for its lock must not freeze the
# event loop (and therefore every unrelated request handled by that worker).
waiting = a.post("/api/upload/start", json={"name": "waiting.mp4", "size": 1000}).json()
from studio import ctx, server  # noqa: E402
class OnePiece:
    async def stream(self):
        yield b"z" * 1000
lock = server._up_lock_for(waiting["id"])
lock.acquire()
loop_responded = threading.Event()
def release_after_probe():
    loop_responded.wait(0.8)
    lock.release()
release = threading.Thread(target=release_after_probe)
release.start()
async def lock_wait_time():
    token = ctx._uid.set(users["pieces-a@example.com"])
    try:
        started = time.perf_counter()
        task = asyncio.create_task(server.upload_part(OnePiece(), waiting["id"], 0))
        await asyncio.sleep(0.05)
        responsive_after = time.perf_counter() - started
        loop_responded.set()
        await task
        return responsive_after
    finally:
        ctx._uid.reset(token)
elapsed = asyncio.run(lock_wait_time())
release.join()
check("waiting for an upload lock does not block the event loop", elapsed < 0.4, f"event loop paused for {elapsed:.3f}s")
check("the directly tested upload can be cancelled", a.delete(f"/api/upload/{waiting['id']}").status_code == 200)

print("\n%d failed" % len(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
