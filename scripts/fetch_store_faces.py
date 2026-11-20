#!/usr/bin/env python3
"""List and download Galaxy Fit3 watch faces from Samsung's Galaxy Store API.

Samsung's Galaxy Store API at https://vas.samsungapps.com/ needs no account:

  product/getContentCategoryProductList.as  -> catalogue (category 0000004252)
  stub/gearAppDownload.as                   -> signed, short-lived download URL

Each download is an APK holding assets/SM-R390_<id>_256x402.bin (what goes to the
watch over route 1e1e + L2CAP), assets/bandface_info.json and preview PNGs.

Usage:
  fetch_store_faces.py list [--out faces.json]
  fetch_store_faces.py download <face-number>... --dir DIR
  fetch_store_faces.py previews [<face-number>...] --dir DIR

`previews` saves each face's 256x402 preview as DIR/face_<number>.png, the layout the
integration serves from config/www/galaxy_fit3/faces/. (The integration can fetch them itself: the
galaxy_fit3.fetch_face_previews action.)
The store's screenshot links are dead, so the previews come out of the packages; photo
faces have none and the dashboard uses their store icon instead.

Downloaded packages are Samsung's content: keep them out of git.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://vas.samsungapps.com/"
CALLER = "com.samsung.wearable.fit3plugin"  # the caller id the store API expects
CATEGORY = "0000004252"
MODEL = "SM-R390"
APP_PREFIX = "com.samsung.fit3watchface.sm_r390_"
PAGE = 100


def _get(path: str, params: dict[str, str]) -> str:
    url = BASE + path + ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode()


def _field(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", block, re.S)
    return m.group(1).strip() if m else ""


def list_faces() -> list[dict]:
    faces, start = [], 1
    while True:
        xml = _get(
            "product/getContentCategoryProductList.as",
            {
                "imgWidth": "216", "imgHeight": "432", "startNum": str(start),
                "endNum": str(start + PAGE - 1), "status": "1", "cc": "KOR",
                "extraInfo": "screenshot", "callerId": CALLER, "locale": "en_US",
                "alignOrder": "recent", "contentCategoryID": CATEGORY, "mcc": "310",
                "mnc": "260", "csc": "NONE", "deviceId": MODEL, "sdkVer": "33", "pd": "0",
            },
        )
        if start > 1 and _field(xml, "resultMsg") == "No Items":
            return faces  # isEndOfList stays false on an exactly full page
        if _field(xml, "resultCode") != "0":
            raise RuntimeError(f"catalogue request failed: {_field(xml, 'resultMsg')}")
        for block in re.findall(r"<appInfo>(.*?)</appInfo>", xml, re.S):
            app_id = _field(block, "appId")
            faces.append({
                "face": int(app_id.rsplit("_", 1)[-1]),
                "name": _field(block, "productName"),
                "app_id": app_id,
                "product_id": _field(block, "productID"),
                "version_code": int(_field(block, "versionCode") or 0),
                "price": _field(block, "price"),
                "size": int(_field(block, "realContentSize") or 0),
                "icon": _field(block, "iconImgURL"),
                "screenshot": _field(block, "screenShotImgURL"),
            })
        if _field(xml, "isEndOfList") == "true":
            return faces
        start += PAGE


def download_url(app_id: str) -> str:
    digest = hashlib.sha1((app_id + "GALAXYAPPSAPI").encode("iso-8859-1")).digest()
    xml = _get(
        "stub/gearAppDownload.as",
        {
            "appInfo": app_id, "hashValue": base64.b64encode(digest).decode(),
            "csc": "NONE", "sdkVer": "33", "callerId": CALLER, "versionCode": "1",
            "mcc": "310", "mnc": "260", "systemId": str(int(time.time() * 1000)),
            "extuk": "0000000000000000", "abiType": "64", "deviceId": MODEL,
            "loginType": "N", "oneUiVersion": "0", "cc": "KOR", "pd": "0",
        },
    )
    if _field(xml, "resultCode") != "1":
        raise RuntimeError(f"{app_id}: {_field(xml, 'resultMsg')}")
    return _field(xml, "downloadURI")


def download(face: int, out_dir: Path) -> Path:
    app_id = f"{APP_PREFIX}{face:04d}"
    face_dir = out_dir / f"{face:05d}"
    face_dir.mkdir(parents=True, exist_ok=True)
    apk = face_dir / f"{app_id}.apk"
    with urllib.request.urlopen(download_url(app_id), timeout=120) as resp:
        apk.write_bytes(resp.read())
    with zipfile.ZipFile(apk) as zf:
        for name in zf.namelist():
            base = name.removeprefix("assets/")
            if name.startswith("assets/") and "/" not in base and (
                base.endswith(".bin") or base.endswith(".png") or base == "bandface_info.json"
            ):
                (face_dir / base).write_bytes(zf.read(name))
    return face_dir


def save_preview(face: int, out_dir: Path) -> Path | None:
    # The style-0 preview in assets/ itself; assets/<locale>/ holds translated copies.
    pattern = re.compile(rf"assets/{MODEL}_{face:05d}_\d+_0\.png")
    with tempfile.TemporaryDirectory() as tmp:
        apk = Path(tmp) / "face.apk"
        with urllib.request.urlopen(download_url(f"{APP_PREFIX}{face:04d}"), timeout=120) as resp:
            apk.write_bytes(resp.read())
        with zipfile.ZipFile(apk) as zf:
            member = next((n for n in sorted(zf.namelist()) if pattern.fullmatch(n)), None)
            if member is None:
                return None
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"face_{face}.png"
            out.write_bytes(zf.read(member))
            return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--out", type=Path)
    dl = sub.add_parser("download")
    dl.add_argument("faces", type=int, nargs="+")
    dl.add_argument("--dir", type=Path, required=True)
    pv = sub.add_parser("previews")
    pv.add_argument("faces", type=int, nargs="*", help="face numbers (default: every face in the store)")
    pv.add_argument("--dir", type=Path, required=True)
    args = ap.parse_args()

    if args.cmd == "list":
        faces = sorted(list_faces(), key=lambda f: f["face"])
        if args.out:
            args.out.write_text(json.dumps(faces, indent=1))
        for f in faces:
            print(f"{f['face']:>4}  {f['name']:<28} v{f['version_code']:<6} {f['price']}")
        print(f"{len(faces)} faces", file=sys.stderr)
    elif args.cmd == "previews":
        for face in args.faces or sorted(f["face"] for f in list_faces()):
            out = save_preview(face, args.dir)
            print(out or f"{face}: no preview in the package (the dashboard uses the store icon)")
    else:
        for face in args.faces:
            print(download(face, args.dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
