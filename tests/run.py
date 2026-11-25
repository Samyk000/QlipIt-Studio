"""Run the test suite against a throwaway copy of the app.

    python tests/run.py

Starts PeakCut on a spare port with an empty, temporary data folder, runs the tests against it, then stops it and
deletes the folder. Your real data and your running server are never touched. Provider responses and voice audio
are mocked; non-loopback Python networking is blocked, including in nested test subprocesses."""
import argparse
import ipaddress
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PEAKCUT_TEST_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"


def _block_external_network(log_path=None):
    """Fail even when application fallback code catches an accidental provider call."""
    def guard(event, args):
        if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            host = args[0]
        elif event in ("socket.connect", "socket.sendto"):
            address = args[1]
            if not isinstance(address, tuple):  # local Unix-domain sockets
                return
            host = address[0]
        else:
            return
        if host is None:
            return
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="replace")
        if str(host).lower().rstrip(".") == "localhost":
            return
        try:
            address = ipaddress.ip_address(host)
            if address.is_loopback or (getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback):
                return
        except ValueError:
            pass
        message = f"FAIL offline network access blocked: {event} {host!r}"
        if log_path:
            with open(log_path, "a", encoding="utf-8") as out:
                out.write(message + "\n")
        print(message, file=sys.stderr, flush=True)
        raise OSError(message)
    sys.addaudithook(guard)


def _isolated_env(data: Path, port: int, base=None) -> dict:
    """A test environment with no inherited app settings and no project .env loading."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith(("QLIPIT_", "PEAKCUT_"))
           and k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "PYTHONPATH")}
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
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "TEMP": str(data),
        "TMP": str(data),
        "TMPDIR": str(data),
        "PEAKCUT_AI_PROVIDER": "gemini",
        "PEAKCUT_AI_BASE_URL": "",
        "PEAKCUT_AI_KEY": "",
        "PEAKCUT_AI_KEY_FILE": "",
        "PEAKCUT_AI_MODELS": "",
        "PEAKCUT_UPLOAD_PART_MB": "5",
        "PEAKCUT_MAX_UPLOAD_MB": "64",
    })
    return env


def _stop_process(process):
    """Stop only a subprocess tree this runner created, including the Windows venv launcher child."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def _run_checks(command, env):
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
    try:
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        stdout, stderr = process.communicate(timeout=15)
        return subprocess.CompletedProcess(command, 124, stdout, stderr + "\nFAIL test group timed out after 180s\n")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline regressions on a temporary server and data folder.")
    parser.add_argument("--only", action="append", default=[], metavar="TEST_FILE", help="Run only this test file basename; repeat to select more groups")
    options = parser.parse_args()
    began = time.perf_counter()
    data = Path(tempfile.mkdtemp(prefix="peakcut-test-"))
    # These tests are about people signing in; tests/test_local.py covers the default. Nothing outside this
    # machine is contacted: no AI, email, Google sign-in, updates or model downloads.
    env = _isolated_env(data, PORT)
    # Python loads this before any app/test imports; nested Python processes inherit it as well.
    offline = data / "offline"
    offline.mkdir()
    network_log = data / "network.log"
    (offline / "sitecustomize.py").write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
        "from tests.run import _block_external_network\n"
        f"_block_external_network({str(network_log)!r})\n", encoding="utf-8")
    env["PYTHONPATH"] = os.pathsep.join((str(offline), str(ROOT)))
    log = data / "server.log"
    failed = []
    total_passed = 0
    with log.open("w", encoding="utf-8") as out:
        server = subprocess.Popen([sys.executable, "-B", "-m", "studio.server"], cwd=ROOT, env=env,
                                  stdout=out, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
    try:
        for _ in range(90):
            try:
                with urllib.request.urlopen(BASE + "/healthz", timeout=2):
                    pass
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
                ("immutable editor attempts and safe publication", ["tests/test_editor_attempts.py"]),
                ("visual framing, headlines and measured encodes", ["tests/test_render_visual.py"]),
                ("grounded selection and English ASR safety", ["tests/test_selection_asr.py"]),
                ("offline English benchmark scoring gate", ["tests/test_benchmark_english.py"]),
                ("the Windows launcher: where it keeps things, picking a port", ["tests/test_desktop.py"]),
                ("Homepage layout and AI warnings", ["tests/test_homepage.js"]),
                ("editor drafts, attempts and stable progress", ["tests/test_editor.js"])]
        if options.only:
            requested = set(options.only)
            unknown = requested - {Path(args[0]).name for _, args in runs}
            if unknown:
                raise ValueError("Unknown test file: " + ", ".join(sorted(unknown)))
            runs = [(name,args) for name,args in runs if Path(args[0]).name in requested]
            print("Selected test groups: " + ", ".join(sorted(requested)), flush=True)
        for name, args in runs:
            print(f"\n=== {name} ===", flush=True)
            runner = shutil.which("node") if args[0].endswith(".js") else sys.executable
            if not runner:
                print(f"  SKIP {name} (Node.js is not installed)", flush=True)
                continue
            command = [runner, *args] if args[0].endswith(".js") else [runner, "-B", *args]
            r = _run_checks(command, env)
            lines = (r.stdout + r.stderr).splitlines()
            passed = sum(1 for x in lines if x.startswith("PASS"))
            if not passed:
                count = re.search(r"Ran (\d+) tests? in", r.stderr)
                if count:
                    unsuccessful = sum(int(n) for n in re.findall(r"(?:failures|errors|skipped)=(\d+)", r.stderr))
                    passed = max(0, int(count[1]) - unsuccessful)
            total_passed += passed
            for x in lines:
                if not x.startswith("PASS") and x.strip():
                    print("  " + x)
            print(f"  {passed} checks passed")
            if r.returncode != 0 or any(x.startswith("FAIL") for x in lines):
                failed.append(name)
    finally:
        _stop_process(server)
        violations = network_log.read_text(encoding="utf-8").splitlines() if network_log.exists() else []
        for violation in violations:
            print("  " + violation)
        if violations:
            failed.append("offline network isolation")
        print(f"\nExternal network attempts blocked: {len(violations)}")
        shutil.rmtree(data, ignore_errors=True)
        print(f"Baseline duration: {time.perf_counter() - began:.2f}s; {total_passed} checks passed")
    print("\nFAILED: " + ", ".join(failed) if failed else "\nSelected tests passed." if options.only else "\nAll tests passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
