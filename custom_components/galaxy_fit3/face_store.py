"""Fetch watch-face files from Samsung's Galaxy Store (no account needed).

Same calls as ``scripts/fetch_store_faces.py``: ``stub/gearAppDownload.as`` returns a short-lived download
link to an APK whose ``assets/SM-R390_<id>_256x402.bin`` is the file the watch takes. Files are cached
under ``<config>/galaxy_fit3/faces/``. The package's style-0 preview (``assets/SM-R390_<id>_<n>_0.png``,
256x402) goes to ``<config>/www/galaxy_fit3/faces/face_<id>.png`` so dashboards can show it as
``/local/galaxy_fit3/faces/face_<id>.png``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import time
import zipfile
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import store_catalog
from .sap import faceinstall

_LOGGER = logging.getLogger(__name__)

BASE = "https://vas.samsungapps.com/"
CALLER = "com.samsung.wearable.fit3plugin"  # the caller id the store API expects
APP_PREFIX = "com.samsung.fit3watchface.sm_r390_"


class FaceStoreError(Exception):
    """The store has no such face, or the download failed."""


def _field(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _extract_bin(apk: bytes, face_id: int) -> bytes:
    name = "assets/" + faceinstall.face_filename(face_id)
    with zipfile.ZipFile(io.BytesIO(apk)) as zf:
        try:
            return zf.read(name)
        except KeyError as err:
            raise FaceStoreError(f"the store package has no {name}") from err


def _extract_preview(apk: bytes, face_id: int) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(apk)) as zf:
        name = store_catalog.preview_member(zf.namelist(), face_id)
        return zf.read(name) if name else None


def cache_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("galaxy_fit3", "faces"))


def preview_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("www", "galaxy_fit3", "faces"))


def preview_url(face_id: int) -> str:
    return f"/local/galaxy_fit3/faces/face_{face_id}.png"


async def async_local_previews(hass: HomeAssistant) -> set[int]:
    """Faces that have a preview image under ``www`` (from an earlier download, or copied there by hand)."""

    def _scan() -> set[int]:
        found = set()
        for path in preview_dir(hass).glob("face_*.png"):
            if path.stem[5:].isdigit():
                found.add(int(path.stem[5:]))
        return found

    return await hass.async_add_executor_job(_scan)


async def async_get_face_file(hass: HomeAssistant, face_id: int, *, refresh: bool = False) -> bytes:
    """The face's ``.bin``, from the cache or downloaded from the store."""
    path = cache_dir(hass) / faceinstall.face_filename(face_id)
    if not refresh:
        cached = await hass.async_add_executor_job(lambda: path.read_bytes() if path.is_file() else None)
        if cached:
            return cached
    app_id = f"{APP_PREFIX}{face_id:04d}"
    digest = hashlib.sha1((app_id + "GALAXYAPPSAPI").encode("iso-8859-1")).digest()
    params = {
        "appInfo": app_id, "hashValue": base64.b64encode(digest).decode(), "csc": "NONE", "sdkVer": "33",
        "callerId": CALLER, "versionCode": "1", "mcc": "310", "mnc": "260", "systemId": str(int(time.time() * 1000)),
        "extuk": "0000000000000000", "abiType": "64", "deviceId": "SM-R390", "loginType": "N",
        "oneUiVersion": "0", "cc": "KOR", "pd": "0",
    }
    session = async_get_clientsession(hass)
    async with session.get(BASE + "stub/gearAppDownload.as", params=params, timeout=30) as resp:
        xml = await resp.text()
    if _field(xml, "resultCode") != "1":
        raise FaceStoreError(f"the store has no face {face_id}: {_field(xml, 'resultMsg') or 'no answer'}")
    async with session.get(_field(xml, "downloadURI"), timeout=120) as resp:
        resp.raise_for_status()
        apk = await resp.read()
    data = await hass.async_add_executor_job(_extract_bin, apk, face_id)
    preview = await hass.async_add_executor_job(_extract_preview, apk, face_id)

    def _save() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if preview:
            target = preview_dir(hass) / f"face_{face_id}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(preview)

    await hass.async_add_executor_job(_save)
    return data


async def async_list_faces(hass: HomeAssistant) -> dict[int, store_catalog.StoreFace]:
    """Every face in the store (name, preview image), cached in ``catalogue.json`` for when the store is unreachable."""
    path = cache_dir(hass) / "catalogue.json"
    session = async_get_clientsession(hass)
    faces: list[store_catalog.StoreFace] = []
    try:
        start = 1
        for _ in range(10):  # 100 faces per page; 10 pages is far more than the store has ever listed
            async with session.get(BASE + store_catalog.CATALOGUE_PATH, params=store_catalog.catalogue_params(start), timeout=30) as resp:
                page, last = store_catalog.parse_page(await resp.text())
            faces += page
            if last or not page:
                break
            start += store_catalog.PAGE
    except Exception as err:  # noqa: BLE001 - fall back to the cached catalogue
        _LOGGER.debug("Samsung store catalogue not reachable: %s", err)
        faces = []
    if faces:
        data = json.dumps([f.as_dict() for f in faces])

        def _save() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)

        await hass.async_add_executor_job(_save)
        return {f.face_id: f for f in faces}
    cached = await hass.async_add_executor_job(lambda: path.read_text() if path.is_file() else None)
    return store_catalog.from_dicts(json.loads(cached)) if cached else {}
