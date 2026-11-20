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

# The browser and Windows icons must preserve the approved artwork, including alpha.
try:
    from PIL import Image, ImageOps
except ImportError:
    print("SKIP branding pixel checks (Pillow is not installed)")
else:
    import base64, hashlib, re
    import xml.etree.ElementTree as ET
    source_path = Path("public/App_Icon.png")
    source = Image.open(source_path).convert("RGBA")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    check("the supplied source logo is unchanged", digest,
          "dc767801ba86b5f53eb34cdd9edf5c9885737e5c5c510a55be53fd5febf6376b")

    def frame(size):
        scaled = ImageOps.contain(source, (size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size))
        canvas.paste(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
        return canvas

    for size in (192, 512):
        with Image.open(f"web/brand/icon-{size}.png") as icon:
            check(f"web {size}px logo preserves the full source canvas and transparency",
                  icon.size == (size, size) and icon.convert("RGBA").tobytes() == frame(size).tobytes())
    sizes = {(s, s) for s in (16, 24, 32, 48, 64, 128, 256)}
    for icon_path in ("web/brand/favicon.ico", "desktop/qlipit.ico"):
        with Image.open(icon_path) as icon:
            check(f"{icon_path} supplies all seven Windows/browser sizes", icon.ico.sizes(), sizes)
            for width, height in sorted(icon.ico.sizes()):
                check(f"{icon_path} {width}px frame preserves the source",
                      icon.ico.getimage((width, height)).convert("RGBA").tobytes() == frame(width).tobytes())
    svg = ET.fromstring(Path("web/brand/favicon.svg").read_text(encoding="utf-8"))
    image = svg.find("{http://www.w3.org/2000/svg}image")
    check("the SVG is a PNG-backed logo, not redrawn vector artwork", image is not None and len(svg) == 1)
    if image is not None:
        embedded = base64.b64decode(image.attrib["href"].split(",", 1)[1])
        check("the SVG embeds the matching 512px PNG", embedded, Path("web/brand/icon-512.png").read_bytes())
    manifest = json.loads(Path("web/manifest.json").read_text(encoding="utf-8"))
    check("all manifest icon URLs invalidate the old cached artwork",
          all(i["src"].endswith("?v=" + digest[:8]) for i in manifest["icons"]))
    check("the supplied edge-to-edge artwork is not advertised as maskable",
          all("maskable" not in i.get("purpose", "") for i in manifest["icons"]))
    for template in Path("web").glob("*.html"):
        refs = re.findall(r'(?:href|src)="(/brand/(?:favicon\.(?:svg|ico)|icon-(?:192|512)\.png)[^"]*)"',
                          template.read_text(encoding="utf-8"))
        check(f"{template.name} logo/favicon references invalidate the old cache",
              all(ref.endswith("?v=" + digest[:8]) for ref in refs))

print("\nFAILED: " + ", ".join(fails) if fails else "\nAll checks passed.")
raise SystemExit(1 if fails else 0)
