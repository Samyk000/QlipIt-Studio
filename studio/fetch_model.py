"""The speech-to-text model: downloaded once, by itself, the first time it is needed.

Nothing has to be run by hand. (python -m studio.fetch_model still works, for getting it ahead of time.) It is
about 480 MB. It is unpacked next to where it will live and moved into place only when it is whole, so a download
that was cut off can never leave half a model behind."""
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
from pathlib import Path

from . import config, states

URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{config.ASR_MODEL_NAME}.tar.bz2"
_lock = threading.Lock()
state = {"doing": False, "percent": 0, "downloaded_bytes": 0, "total_bytes": 0, "stage": "", "error": "", "status": states.ModelStatus.NOT_DOWNLOADED}


def ready() -> bool:
    """Is the whole model there: the word list and all three parts?"""
    d = config.ASR_MODEL_DIR
    if not (d / "tokens.txt").is_file():
        return False
    return all(any(d.glob(f"{part}*.onnx")) for part in ("encoder", "decoder", "joiner"))


def status() -> dict:
    is_ready = ready()
    curr_status = states.ModelStatus.READY if is_ready else state.get("status", states.ModelStatus.NOT_DOWNLOADED)
    return {
        "ready": is_ready,
        "status": curr_status,
        "doing": state["doing"],
        "percent": state["percent"],
        "downloaded_mb": round(state.get("downloaded_bytes", 0) / (1024 * 1024), 1),
        "total_mb": round((state.get("total_bytes") or 482440461) / (1024 * 1024), 1),
        "stage": state.get("stage", ""),
        "error": state.get("error", ""),
    }


def _bundled() -> Path | None:
    """A model already placed in the project or shipped inside the app bundle."""
    places = [
        config.ROOT / "models",
        config.ROOT / "desktop" / "models",
    ]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        places = [exe_dir / "_internal" / "models", exe_dir / "models"] + places
    for p in places:
        d = p / config.ASR_MODEL_NAME
        if (d / "tokens.txt").is_file():
            return d
    return None


def ensure(on_progress=None) -> None:
    """Make sure the model is on this computer, downloading it if it is not. One download at a time: anything else
    that needs the model meanwhile waits for it here."""
    if ready():
        return
    with _lock:
        if ready():
            return
        state.update(doing=True, percent=0, downloaded_bytes=0, total_bytes=0, stage="Starting download...", error="")

        def _rep(blocks, size, total):
            dl = blocks * size
            tot = total if total > 0 else 482440461
            state["downloaded_bytes"] = min(tot, dl)
            state["total_bytes"] = tot
            pct = min(99, int(dl * 100 / tot)) if tot > 0 else 0
            state["percent"] = pct
            state["stage"] = f"Downloading ({pct}%)"
            if on_progress:
                try:
                    on_progress(pct, state["stage"], state["downloaded_bytes"], state["total_bytes"])
                except Exception:
                    pass

        found = _bundled()
        if found:
            print(f"\n  Copying speech model from {found}...\n", flush=True)
            state.update(stage="Copying local speech model...", status=states.ModelStatus.COPYING_BUNDLED)
            config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copytree(found, config.ASR_MODEL_DIR, dirs_exist_ok=True)
            if ready():
                state["doing"] = False
                state.update(stage="Ready", status=states.ModelStatus.READY)
                print("  Speech model ready.\n", flush=True)
                return

        print("\n  Downloading the speech model (one time, about 480 MB)...\n", flush=True)
        state["status"] = states.ModelStatus.DOWNLOADING
        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.MODELS_DIR / "speech-model.tar.bz2"
        part = config.MODELS_DIR / "speech-model.unpacking"
        try:
            urllib.request.urlretrieve(URL, tmp, _rep)
            shutil.rmtree(part, ignore_errors=True)
            part.mkdir(parents=True, exist_ok=True)

            state.update(stage="Unpacking speech model...", status=states.ModelStatus.EXTRACTING)
            if on_progress:
                try:
                    on_progress(99, "Unpacking speech model...", state["downloaded_bytes"], state["total_bytes"])
                except Exception:
                    pass

            unpacked = False
            if shutil.which("tar"):
                try:
                    cmd = ["tar", "-xjf", str(tmp), "-C", str(part)]
                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    subprocess.run(cmd, check=True, creationflags=creationflags)
                    unpacked = True
                except Exception:
                    unpacked = False

            if not unpacked:
                with tarfile.open(tmp, "r:bz2") as t:
                    for m in t:
                        if (m.isfile() or m.isdir()) and not m.name.startswith(("/", "..")) and ".." not in m.name.split("/"):
                            t.extract(m, part)

            got = part / config.ASR_MODEL_NAME
            if not got.is_dir():
                raise OSError("the download did not contain the model")
            shutil.rmtree(config.ASR_MODEL_DIR, ignore_errors=True)        # a half-finished one from before
            got.rename(config.ASR_MODEL_DIR)
            print("  Speech model ready.\n", flush=True)
            state.update(stage="Ready", status=states.ModelStatus.READY)
        except Exception as e:  # noqa: BLE001
            state.update(error=str(e)[-200:], status=states.ModelStatus.ERROR)
            raise RuntimeError("QlipIt Studio couldn't download its speech model (about 480 MB, needed once). Check the "
                               "internet connection and free disk space, then try again.") from e
        finally:
            state["doing"] = False
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            shutil.rmtree(part, ignore_errors=True)


def start_background_download(on_progress=None):
    if state["doing"] or ready():
        return
    t = threading.Thread(target=lambda: ensure(on_progress=on_progress), daemon=True)
    t.start()


def delete_model() -> bool:
    if ready():
        shutil.rmtree(config.ASR_MODEL_DIR, ignore_errors=True)
        return True
    return False


def main():
    if ready():
        print("Speech model already present.")
        return
    ensure()


if __name__ == "__main__":
    main()
