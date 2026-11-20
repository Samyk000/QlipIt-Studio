"""Run the test suite against a throwaway copy of the app.

    python tests/run.py

Starts PeakCut on a spare port with an empty, temporary data folder, runs the tests against it, then stops it and
deletes the folder. Your real data and your running server are never touched. No AI key is needed: the tests cover accounts, permissions, limits and the queue, not clip quality."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PEAKCUT_TEST_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"


def _isolated_env(data: Path, port: int, base=None) -> dict:
    """A test environment with no inherited app settings and no project .env loading."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith(("QLIPIT_", "PEAKCUT_"))}
    env.update({
        "QLIPIT_LOAD_DOTENV": "0",
        "PEAKCUT_DATA": str(data),
        "PEAKCUT_PORT": str(port),
        "PEAKCUT_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "PEAKCUT_HOST": "127.0.0.1",
        "PEAKCUT_AUTO_RELOAD": "0",
        "DATABASE_URL": "",
        "PEAKCUT_ACCOUNTS": "1",
        "PEAKCUT_FETCH_MODEL": "0",
        "PEAKCUT_UPDATE_CHECK": "0",
        "PEAKCUT_SMTP_HOST": "",
        "PEAKCUT_VERIFY_EMAIL": "1",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "GOOGLE_CLIENT_FILE": "",
        "PYTHONUNBUFFERED": "1",
        "PEAKCUT_AI_KEY": "",
        "PEAKCUT_AI_KEY_FILE": "",
        "PEAKCUT_AI_MODELS": "",
        "PEAKCUT_UPLOAD_PART_MB": "5",
        "PEAKCUT_MAX_UPLOAD_MB": "64",
    })
    return env


def main() -> int:
    data = Path(tempfile.mkdtemp(prefix="peakcut-test-"))
    # These tests are about people signing in; tests/test_local.py covers the default. Nothing outside this
    # machine is contacted: no AI, email, Google sign-in, updates or model downloads.
    env = _isolated_env(data, PORT)
    log = data / "server.log"
    failed = []
    with log.open("w", encoding="utf-8") as out:
        server = subprocess.Popen([sys.executable, "-m", "studio.server"], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=2)
                break
            except Exception:  # noqa: BLE001
                if server.poll() is not None:
                    print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
                    return 1
                time.sleep(1)
        else:
            print("The test server did not start.")
            return 1
        runs = [("permissions", ["tests/test_permissions.py", BASE, str(ROOT), sys.executable]),
                ("abuse and limits", ["tests/test_abuse.py", BASE, str(ROOT)]),
                ("queue under load", ["tests/test_load_queue.py", str(ROOT)]),
                ("deleting and bulk actions", ["tests/test_delete.py"]),
                ("big uploads in pieces", ["tests/test_upload_parts.py", BASE]),
                ("AI services", ["tests/test_ai.py"]),
                ("no sign-in, and the AI key from the dashboard", ["tests/test_local.py"]),
                ("Hindi and Urdu captions in Roman letters", ["tests/test_roman.py"]),
                ("one-time authentication links", ["tests/test_auth_tokens.py"]),
                ("the new-version notice", ["tests/test_updates.py"]),
                ("no limits by default", ["tests/test_limits.py"]),
                ("captions never overlap, stuck clips, sentence endings", ["tests/test_stuck.py"]),
                ("fresh installs: ffmpeg, the speech model, AI errors", ["tests/test_tools.py"]),
                ("the storage folder: full drive, open, move", ["tests/test_storage.py"]),
                ("state machines and transitions", ["tests/test_states.py"]),
                ("voiceover and AI script studio", ["tests/test_voice.py"]),
                ("the Windows launcher: where it keeps things, picking a port", ["tests/test_desktop.py"]),
                ("Homepage layout and AI warnings", ["tests/test_homepage.js"])]
        for name, args in runs:
            print(f"\n=== {name} ===", flush=True)
            runner = shutil.which("node") if args[0].endswith(".js") else sys.executable
            if not runner:
                print("  SKIP Homepage JavaScript checks (Node.js is not installed)", flush=True)
                continue
            r = subprocess.run([runner, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            lines = (r.stdout + r.stderr).splitlines()
            passed = sum(1 for x in lines if x.startswith("PASS"))
            for x in lines:
                if not x.startswith("PASS") and x.strip():
                    print("  " + x)
            print(f"  {passed} checks passed")
            if r.returncode != 0 or any(x.startswith("FAIL") for x in lines):
                failed.append(name)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data, ignore_errors=True)
    print("\nFAILED: " + ", ".join(failed) if failed else "\nAll tests passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
