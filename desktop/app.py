"""QlipIt Studio as an ordinary Windows program: double-click it, the app opens; close the window, it stops.

There is nothing to install and no terminal to keep open. The first start makes its own folder under
%LOCALAPPDATA%\\QlipIt (videos, clips, the database and the speech model), starts the QlipIt server on a free
port, and shows it in a window of its own: Edge without tabs, an address bar or a Start menu of its own.

    QlipIt.exe --selftest            check that everything the bundle needs is really there, then stop
    QlipIt.exe --pick-folder FILE    show the computer's "choose a folder" window, write the answer to FILE
                                      (the settings page asks for it this way: a bundled app cannot run "python -c")
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NAME = "QlipIt Studio"
LOCAL = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
HOME = LOCAL / "QlipIt"
LOG = HOME / "qlipit.log"
FIRST_PORT = int(os.environ.get("QLIPIT_PORT") or os.environ.get("PEAKCUT_PORT") or 8765)
WIDTH, HEIGHT = 1360, 900
READY_SECONDS = 180


def _migrate_legacy_data():
    r"""Move any PeakCut library into %LOCALAPPDATA%\QlipIt once, then remove the old folder.

    The old folder is only removed after the copy has gone through: a full disk or a file in use must never cost
    the only copy. The old settings file goes with it, so this runs again next start if anything was left behind."""
    old_dir = LOCAL / "PeakCut"
    if not old_dir.exists():
        return
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        old_data = old_dir / "data"
        new_data = HOME / "data"
        clean = True
        if old_data.exists():
            for item in old_data.iterdir():
                target = new_data / item.name
                if target.exists():
                    clean = False             # never replace newer data with the older copy, and never delete it
                    continue
                try:
                    if item.is_dir():
                        shutil.copytree(str(item), str(target))
                    else:
                        shutil.copy2(str(item), str(target))
                except OSError as e:
                    clean = False
                    log(f"migration: could not copy {item.name}: {e!r}")
        old_storage = old_dir / "storage.json"
        new_storage = HOME / "storage.json"
        if old_storage.exists() and not new_storage.exists():
            shutil.copy2(str(old_storage), str(new_storage))
        if new_storage.exists():          # an old settings file may still point at the folder we are about to remove
            try:
                chosen = json.loads(new_storage.read_text(encoding="utf-8"))
            except ValueError:
                chosen = {}
            p = Path(str(chosen.get("path") or ""))
            if p and (p == old_data or p == old_dir or old_dir in p.parents):
                new_storage.write_text(json.dumps({"path": str(new_data)}, indent=1), encoding="utf-8")
        if clean:                         # anything not copied keeps the old folder, so nothing is ever lost
            shutil.rmtree(str(old_dir), ignore_errors=True)
    except Exception as e:
        log(f"migration warning: {e!r}")


def _clean_stale_edge_cache():
    """Removes stale Chromium favicon and app caches so Edge fetches fresh QlipIt Studio brand icons."""
    default_dir = HOME / "window" / "Default"
    if not default_dir.exists():
        return
    stale_items = [
        "Favicons",
        "Favicons-journal",
        "HubApps Icons",
        "HubApps Icons-journal",
        "Web Applications",
    ]
    for item in stale_items:
        target = default_dir / item
        try:
            if target.is_dir():
                shutil.rmtree(str(target), ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)
        except Exception:
            pass


def log(text: str):
    """A line for qlipit.log. It is the only way to see what went wrong when no window opened."""
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except OSError:
        pass


def say(text: str):
    """A message box. A double-click that does nothing tells nobody anything, so failures say why."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, NAME, 0x10)
    except Exception:  # noqa: BLE001 - a message box that fails is not worth a second error
        pass


def up(port: int) -> bool:
    """Is QlipIt already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
            return r.status == 200 and b'"ok"' in r.read(200)
    except Exception:  # noqa: BLE001 - not running, not ours, or still starting
        return False


def free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def seed_storage():
    """Point QlipIt's folder at %LOCALAPPDATA%\\QlipIt\\data on the first start.

    It has to be written down rather than passed as QLIPIT_DATA: that setting wins over the settings page, and
    then "Storage, move my folder" would stop working. Written this way, everything works as it does from source."""
    f = HOME / "storage.json"
    if not f.exists():
        (HOME / "data").mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"path": str(HOME / "data")}, indent=1), encoding="utf-8")


_MUTEX = None
_KILL_WITH = None


def kill_tree_on_exit():
    """Make Windows end everything QlipIt started - ffmpeg, yt-dlp, Edge - when QlipIt ends, even after a crash.

    They all inherit this job object, and Windows kills a job's processes when the last handle to it closes.
    Without it, closing the window mid-render leaves ffmpeg running, holding the clip file and a processor core."""
    global _KILL_WITH
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000           # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return                                               # JobObjectExtendedLimitInformation = 9
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            return
        _KILL_WITH = job          # kept for the life of the program: closing it is what kills the children
    except Exception as e:
        log(f"could not set up process cleanup: {e!r}")


def hold():
    """One QlipIt at a time: a second double-click shows the copy that is already open, instead of starting a
    second server on the same database. Windows releases the lock when the program ends, even after a crash, so
    nothing can be left stuck. Returns the open file (keep it open), or None when another copy has it."""
    global _MUTEX
    if sys.platform == "win32":
        try:
            import ctypes
            # Named Win32 mutex so Inno Setup (AppMutex=QlipItAppMutex) detects running instances on upgrade/re-install
            _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "QlipItAppMutex")
        except Exception:
            pass
    import msvcrt
    f = open(HOME / "qlipit.lock", "a+")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        f.close()
        return None


def edge():
    """The Edge that is already on every Windows computer, in the places it is normally installed."""
    found = shutil.which("msedge")
    if found:
        return Path(found)
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.is_file():
                return p
    return None


def ask_folder() -> str:
    """The computer's own "choose a folder" window. Shown by this program itself when it is bundled."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askdirectory(title="Choose where QlipIt keeps its videos and clips") or ""
        finally:
            root.destroy()
    except Exception as e:  # noqa: BLE001
        log(f"choose a folder failed: {e!r}")
        return ""


def _find_ico() -> str:
    candidates = [
        Path(__file__).resolve().parent / "qlipit.ico",
        Path(sys.executable).resolve().parent / "_internal" / "desktop" / "qlipit.ico",
        Path(sys.executable).resolve().parent / "desktop" / "qlipit.ico",
        Path(sys.executable).resolve().parent / "_internal" / "web" / "brand" / "favicon.ico",
        Path(__file__).resolve().parent.parent / "web" / "brand" / "favicon.ico",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return ""


def _set_window_aumid(hwnd: int, app_id: str, ico_path: str):
    """Set System.AppUserModel.ID and Relaunch properties on the window so Windows Taskbar pins and groups it as QlipIt."""
    import ctypes
    from ctypes import wintypes
    import uuid

    FMTID_AppUserModel = uuid.UUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")
    IID_IPropertyStore = uuid.UUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("wReserved1", wintypes.WORD),
            ("wReserved2", wintypes.WORD),
            ("wReserved3", wintypes.WORD),
            ("pwszVal", wintypes.LPWSTR),
            ("padding", ctypes.c_ubyte * 8),
        ]

    def to_guid(u: uuid.UUID) -> GUID:
        b = u.bytes_le
        return GUID(
            int.from_bytes(b[0:4], "little"),
            int.from_bytes(b[4:6], "little"),
            int.from_bytes(b[6:8], "little"),
            (ctypes.c_ubyte * 8)(*b[8:16]),
        )

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long

    riid = to_guid(IID_IPropertyStore)
    ppv = ctypes.c_void_p()
    hr = shell32.SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(riid), ctypes.byref(ppv))
    if hr != 0 or not ppv.value:
        return

    SetValueProto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    CommitProto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    ReleaseProto = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)

    vtable = ctypes.cast(ppv, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    set_value = SetValueProto(vtable[6])
    commit = CommitProto(vtable[7])
    release = ReleaseProto(vtable[2])

    pv = PROPVARIANT(vt=31, pwszVal=app_id)
    set_value(ppv, ctypes.byref(PROPERTYKEY(to_guid(FMTID_AppUserModel), 5)), ctypes.byref(pv))

    if ico_path:
        pv_ico = PROPVARIANT(vt=31, pwszVal=ico_path)
        set_value(ppv, ctypes.byref(PROPERTYKEY(to_guid(FMTID_AppUserModel), 2)), ctypes.byref(pv_ico))
        pv_name = PROPVARIANT(vt=31, pwszVal=NAME)
        set_value(ppv, ctypes.byref(PROPERTYKEY(to_guid(FMTID_AppUserModel), 4)), ctypes.byref(pv_name))
        pv_cmd = PROPVARIANT(vt=31, pwszVal=sys.executable)
        set_value(ppv, ctypes.byref(PROPERTYKEY(to_guid(FMTID_AppUserModel), 3)), ctypes.byref(pv_cmd))

    commit(ppv)
    release(ppv)


def _apply_dark_title_bar(hwnd: int):
    """Apply modern dark mode styling to the Windows title bar."""
    import ctypes
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        c_true = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE (20 on Win10 build 18985+ and Win11; 19 on older Win10)
        dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(c_true), ctypes.sizeof(c_true))
        dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(c_true), ctypes.sizeof(c_true))
        # DWMWA_CAPTION_COLOR = 35: Background color for caption bar (#0B0D13 -> 0x00130D0B in COLORREF format)
        caption_color = ctypes.c_int(0x00130D0B)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))
        # DWMWA_TEXT_COLOR = 36: Caption title text color (#E4E0DA -> 0x00DAE0E4)
        text_color = ctypes.c_int(0x00DAE0E4)
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
        # DWMWA_BORDER_COLOR = 34: Thin modern dark border (#1E2333 -> 0x0033231E)
        border_color = ctypes.c_int(0x0033231E)
        dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border_color), ctypes.sizeof(border_color))
    except Exception as e:
        log(f"dark title bar error: {e!r}")


def _decorate_window(stop_ev: threading.Event, target_pid: int = 0):
    """Watches for the QlipIt window to appear, applies native icon, title bar styling, and taskbar grouping instantly."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ico = _find_ico()
    hicon_big, hicon_small = None, None
    if ico:
        try:
            hicon_big = user32.LoadImageW(0, ico, 1, 48, 48, 0x00000010)
            if not hicon_big:
                hicon_big = user32.LoadImageW(0, ico, 1, 32, 32, 0x00000010)
            hicon_small = user32.LoadImageW(0, ico, 1, 16, 16, 0x00000010)
        except Exception as e:
            log(f"LoadImageW error: {e!r}")

    decorated = set()
    found_named_window = False

    # Poll rapidly (15ms) for the first 120 cycles (1.8s) so decoration occurs before or on first paint
    for cycle in range(160):
        if stop_ev.is_set():
            break

        def cb(hwnd, _):
            nonlocal found_named_window
            if user32.GetWindow(hwnd, 4) != 0:  # GW_OWNER != 0 (skip popup/child frames)
                return True
            cls = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, cls, 512)
            if "Chrome_WidgetWin_1" not in cls.value:
                return True

            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            t_lower = title.value.lower()

            if target_pid:
                wpid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                is_our_pid = (wpid.value == target_pid)
                is_our_title = "qlipit" in t_lower
                if not (is_our_pid or is_our_title):
                    return True
            else:
                if "qlipit" not in t_lower and title.value:
                    return True

            if "qlipit" in t_lower:
                found_named_window = True

            if hwnd not in decorated:
                try:
                    if hicon_big:
                        user32.SendMessageW(hwnd, 0x0080, 1, hicon_big)
                    if hicon_small:
                        user32.SendMessageW(hwnd, 0x0080, 0, hicon_small)
                    _set_window_aumid(hwnd, "QlipIt.Studio", ico)
                    _apply_dark_title_bar(hwnd)
                    user32.SetWindowTextW(hwnd, "QlipIt Studio")
                    decorated.add(hwnd)
                    log(f"decorated window {hwnd} (title: '{title.value}')")
                except Exception as e:
                    log(f"decorate window error: {e!r}")
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        if found_named_window and len(decorated) > 0:
            time.sleep(0.2)
            for hwnd in list(decorated):
                try:
                    if hicon_big:
                        user32.SendMessageW(hwnd, 0x0080, 1, hicon_big)
                    if hicon_small:
                        user32.SendMessageW(hwnd, 0x0080, 0, hicon_small)
                    _set_window_aumid(hwnd, "QlipIt.Studio", ico)
                    _apply_dark_title_bar(hwnd)
                except Exception:
                    pass
            break
        time.sleep(0.015 if cycle < 100 else 0.05)


def show(url: str) -> int:
    """Open QlipIt in a window of its own, and return when that window is closed.

    Edge is given its own profile folder under QlipIt's home, which is what makes one window one QlipIt: the
    process we start is the one that closes, so closing the window really is quitting."""
    exe = edge()
    if not exe:
        # shortcut: without a browser of its own there is no window to wait for, so QlipIt keeps running until
        # it is ended from Task Manager; upgrade when Windows without Edge turns up.
        import webbrowser
        webbrowser.open(url)
        log(f"no Edge found: opened {url} in the default browser")
        threading.Event().wait()
        return 0

    _clean_stale_edge_cache()
    flags = [
        str(exe),
        f"--app={url}",
        f"--user-data-dir={HOME / 'window'}",
        f"--window-size={WIDTH},{HEIGHT}",
        "--force-dark-mode",
        "--enable-features=DarkLightMode",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    p = subprocess.Popen(flags, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stop_ev = threading.Event()
    th = threading.Thread(target=_decorate_window, args=(stop_ev, p.pid), daemon=True)
    th.start()
    try:
        p.wait()
    finally:
        stop_ev.set()
    return 0


def main() -> int:
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QlipIt.Studio")
    except Exception:
        pass
    _migrate_legacy_data()
    HOME.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 2_000_000:
        LOG.unlink()
    out = open(LOG, "a", buffering=1, encoding="utf-8", errors="replace")   # nothing here has a console to print to
    sys.stdout = sys.stderr = out
    log(f"starting: {sys.executable} {' '.join(sys.argv[1:])}")

    if "--pick-folder" in sys.argv:
        i = sys.argv.index("--pick-folder")
        if i + 1 >= len(sys.argv):
            log("--pick-folder needs a file to write the answer to")
            return 2
        Path(sys.argv[i + 1]).write_text(ask_folder(), encoding="utf-8")
        return 0

    # settings that must be in place before studio.config reads them
    os.environ["QLIPIT_STORAGE_FILE"] = os.environ["PEAKCUT_STORAGE_FILE"] = str(HOME / "storage.json")
    os.environ.setdefault("QLIPIT_UPDATE_CHECK", "0")
    os.environ.setdefault("PEAKCUT_UPDATE_CHECK", "0")     # a packaged copy cannot update itself from GitHub
    os.environ.setdefault("QLIPIT_AUTO_RELOAD", "0")
    os.environ.setdefault("PEAKCUT_AUTO_RELOAD", "0")
    seed_storage()

    if "--selftest" in sys.argv:
        import studio.server  # noqa: F401 - the whole server, its pages and its AI settings
        import sherpa_onnx  # noqa: F401 - the speech library and its own DLLs
        import soundfile  # noqa: F401
        import yt_dlp  # noqa: F401
        import edge_tts  # noqa: F401 - neural voice synthesis
        from studio import config, tools
        # A bundle is only good if everything it will need is inside it. Saying so with an exit code is what lets
        # the build refuse to publish an app that would fail on someone's computer.
        wanted = [config.ROOT / "web" / "index.html", config.ROOT / "web" / "landing.html",
                  config.ROOT / "web" / "brand" / "favicon.svg", config.ROOT / "studio" / "fonts" / "Poppins-ExtraBold.ttf",
                  config.ROOT / "desktop" / "qlipit.ico"]
        missing = [str(p) for p in wanted if not p.is_file()]
        bad = bool(tools.missing()) or bool(missing)
        log(f"selftest {'FAILED' if bad else 'ok'}: {config.DATA_DIR} | ffmpeg missing={tools.missing()} | files missing={missing}")
        return 1 if bad else 0

    lock = hold()                          # held for as long as this program runs
    kill_tree_on_exit()                    # ...and everything it starts ends with it, even after a crash
    if lock is None:
        for _ in range(40):                # the copy that is open may still be starting
            for p in range(FIRST_PORT, FIRST_PORT + 20):
                if up(p):
                    log(f"already open on {p}: showing that one")
                    return show(f"http://127.0.0.1:{p}")
            time.sleep(0.5)
        say("QlipIt is already open. Close it first, then start it again.")
        return 0

    port = None
    for p in range(FIRST_PORT, FIRST_PORT + 20):
        if up(p):
            log(f"QlipIt is already running on {p}: showing that one")
            return show(f"http://127.0.0.1:{p}")
        if free(p):
            port = p
            break
    if port is None:
        say("QlipIt could not find a free port to start on. Restart the computer and try again.")
        return 1
    os.environ["QLIPIT_PORT"] = os.environ["PEAKCUT_PORT"] = str(port)
    os.environ.setdefault("QLIPIT_PUBLIC_URL", f"http://127.0.0.1:{port}")
    os.environ.setdefault("PEAKCUT_PUBLIC_URL", f"http://127.0.0.1:{port}")
    url = f"http://127.0.0.1:{port}"

    import uvicorn
    from studio.server import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", backlog=4096))
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    for _ in range(READY_SECONDS * 2):
        if up(port):
            break
        time.sleep(0.5)
    else:
        log("the server did not come up")
        say("QlipIt could not start. The reason is in this file:\n\n" + str(LOG))
        return 1

    log(f"open on {url}")
    try:
        return show(url)
    finally:
        server.should_exit = True      # renders still running are stopped with the window, as they are with Ctrl+C
        serving.join(timeout=15)       # let the database and the videos close properly before the program ends
        try:
            lock.close()
        except Exception:
            pass
        log("stopped")


if __name__ == "__main__":
    sys.exit(main())
