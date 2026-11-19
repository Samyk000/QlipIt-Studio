# Build QlipIt.exe and the folder it needs. Run from the project folder:
#
#   .venv\Scripts\python.exe -m PyInstaller desktop\qlipit.spec --noconfirm
#
# The result is desktop\dist\QlipIt\ : QlipIt.exe plus an _internal folder holding Python, the pages, ffmpeg
# and the speech libraries. That whole folder is the app; desktop\build.ps1 also wraps it in an installer.
import sys  # noqa: F401 - PyInstaller puts SPECPATH in this namespace
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent                       # the project folder (this file lives in desktop\)
FFMPEG = ROOT / "desktop" / "ffmpeg"               # ffmpeg.exe and ffprobe.exe, put there by desktop\build.ps1
ICO = ROOT / "desktop" / "qlipit.ico"

datas = [
    (str(ROOT / "web"), "web"),                    # every page, script and logo (server.WEB = <app>\web)
    (str(ROOT / "studio" / "fonts"), "studio/fonts"),   # the caption fonts, copied into the data folder
    (str(FFMPEG), "ffmpeg/bin"),                   # studio/tools.py looks in <app>\ffmpeg\bin before anywhere else
    (str(ICO), "desktop"),                         # application icon for window decoration
]
if (ROOT / "desktop" / "models").is_dir():
    datas.append((str(ROOT / "desktop" / "models"), "models"))

binaries = (collect_dynamic_libs("sherpa_onnx")       # onnxruntime.dll and the sherpa libraries next to their module
            + collect_dynamic_libs("_soundfile_data"))  # libsndfile_x64.dll: reading and writing audio

hiddenimports = (collect_submodules("studio")      # every part of the app, including the ones imported on demand
                 + collect_submodules("yt_dlp")    # the site list is loaded by name at runtime, not imported
                 + collect_submodules("edge_tts")  # neural voiceover synthesis
                 + ["sqlalchemy.dialects.sqlite", "uvicorn.logging", "uvicorn.loops.auto",
                    "uvicorn.loops.asyncio", "uvicorn.protocols.http.auto",
                    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto",
                    "uvicorn.lifespan.on"])

a = Analysis([str(ROOT / "desktop" / "app.py")], pathex=[str(ROOT)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=["psycopg", "psycopg_binary", "psycopg2", "tests"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="QlipIt", console=False, upx=False,
          icon=str(ICO), version=str(ROOT / "desktop" / "version.txt"))
COLLECT(exe, a.binaries, a.datas, name="QlipIt", upx=False)
