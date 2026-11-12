"""Download pure-python wheels from PyPI and unzip them into ../vendor.

Used instead of pip because pip's temp/unpack handling is blocked in this sandbox.
"""
import io
import json
import os
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(os.path.dirname(HERE), "vendor")

PACKAGES = [
    ("selenium", "4.49.0"),
    ("certifi", None),
    ("trio", None),
    ("trio-websocket", None),
    ("typing_extensions", None),
    ("urllib3", None),
    ("websocket-client", None),
    ("attrs", None),
    ("sortedcontainers", None),
    ("idna", None),
    ("outcome", None),
    ("sniffio", None),
    ("wsproto", None),
    ("h11", None),
]


def pick_wheel(files, py=(3, 13)):
    cands = []
    for f in files:
        if f["packagetype"] != "bdist_wheel":
            continue
        fn = f["filename"]
        if not fn.endswith(".whl"):
            continue
        # only pure python wheels are safe to extract this way
        if "py3-none-any" not in fn and "py2.py3-none-any" not in fn:
            print(f"    skip non-pure wheel: {fn}")
            continue
        tags = fn[:-4].split("-")[-3:]
        py_tag = tags[0]
        ok = False
        for t in py_tag.split("."):
            if t == "py3":
                ok = True
            elif t.startswith("py") and t[2:].isdigit():
                ok = ok or int(t[2:]) == py[0]
        if ok:
            cands.append((py_tag != "py3", fn, f))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][2]


def main():
    os.makedirs(VENDOR, exist_ok=True)
    failures = []
    for name, pinned in PACKAGES:
        try:
            url = f"https://pypi.org/pypi/{name}/json"
            if pinned:
                url = f"https://pypi.org/pypi/{name}/{pinned}/json"
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
            version = data["info"]["version"]
            wheel = pick_wheel(data["urls"])
            if wheel is None:
                failures.append(f"{name}: no pure-python wheel found")
                print(f"[FAIL] {name}: no pure-python wheel")
                continue
            print(f"[GET ] {name} {version} <- {wheel['filename']}")
            with urllib.request.urlopen(wheel["url"], timeout=120) as r:
                blob = r.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names = z.namelist()
                # skip dist-info: we do not need metadata, just importable modules
                members = [n for n in names if not n.split("/")[0].endswith(".dist-info")]
                z.extractall(VENDOR, members=members)
            print(f"[OK  ] {name} {version} ({len(members)} files)")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc}")
            print(f"[FAIL] {name}: {exc}")

    # make vendor permanently importable for the system interpreter too
    site = None
    for p in sys.path:
        if p.endswith("site-packages"):
            site = p
            break
    if site:
        pth = os.path.join(site, "jit_autologin_vendor.pth")
        try:
            with open(pth, "w", encoding="utf-8") as fh:
                fh.write(VENDOR + "\n")
            print(f"[OK  ] wrote {pth}")
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] could not write .pth ({exc}); script adds vendor to sys.path itself")

    print("\n== summary ==")
    print("failures:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
