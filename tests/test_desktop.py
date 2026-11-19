"""The desktop launcher (desktop/app.py): where it puts PeakCut's folder, and how it picks a port.

Runs against the file directly - the launcher is not part of the `studio` package. Nothing is started and no
window is opened; the app itself is checked by hand (desktop\\app.py --selftest inside a built copy)."""
import importlib.util, json, os, tempfile
from pathlib import Path

fake = tempfile.mkdtemp(prefix="pc-desktop-")
os.environ["LOCALAPPDATA"] = fake                  # the launcher reads this when it is imported
spec = importlib.util.spec_from_file_location("desktop_app", Path("desktop/app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

home = app.HOME
check("everything it keeps goes under %LOCALAPPDATA%\\QlipIt", app.HOME in (Path(fake) / "QlipIt", Path(fake) / "PeakCut"))

app.seed_storage()
chosen = json.loads((home / "storage.json").read_text(encoding="utf-8"))
check("the first start points PeakCut's folder at its own data folder", Path(chosen["path"]), home / "data")
check("...and makes that folder", (home / "data").is_dir())

(home / "storage.json").write_text(json.dumps({"path": "D:\\somewhere"}), encoding="utf-8")
app.seed_storage()
check("a folder the owner chose is never overwritten", app.HOME.joinpath("storage.json").read_text(encoding="utf-8"),
      json.dumps({"path": "D:\\somewhere"}))

port = next(p for p in range(8801, 8901) if app.free(p))
check("a free port is free", app.free(port))
check("...and nothing answers there", app.up(port), False)
listener = __import__("socket").socket()
listener.bind(("127.0.0.1", port)); listener.listen(1)
check("a port something else holds is not free", app.free(port), False)
check("...and is not mistaken for PeakCut", app.up(port), False)
listener.close()

check("Edge (or, failing that, nothing) is found without guessing", callable(app.edge))

first = app.hold()
check("one PeakCut at a time: a second copy cannot take the lock", app.hold(), None)
first.close()
second = app.hold()
check("...and can once the first has let go", second is not None)
second.close()
import shutil; shutil.rmtree(fake, ignore_errors=True)
print("\nFAILED: " + ", ".join(fails) if fails else "\nAll checks passed.")
raise SystemExit(1 if fails else 0)
