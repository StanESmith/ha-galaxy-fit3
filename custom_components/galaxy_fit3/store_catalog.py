"""Samsung Galaxy Store catalogue of Fit3 watch faces: request parameters and the XML parser (no Home Assistant).

``product/getContentCategoryProductList.as`` (content category 0000004252, device SM-R390) lists every face
with its name and preview image.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CATALOGUE_PATH = "product/getContentCategoryProductList.as"
PAGE = 100
_APP_ID = re.compile(r"sm_r390_(\d+)$")


@dataclass(frozen=True, slots=True)
class StoreFace:
    face_id: int
    name: str
    preview: str  # store icon (146x146 PNG of the face on a band); the screenshot links answer 404 since 2026-11
    styles: int = 1
    version: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def catalogue_params(start: int) -> dict[str, str]:
    return {
        "imgWidth": "216", "imgHeight": "432", "startNum": str(start), "endNum": str(start + PAGE - 1),
        "status": "1", "cc": "KOR", "extraInfo": "screenshot", "callerId": "com.samsung.wearable.fit3plugin",
        "locale": "en_US", "alignOrder": "recent", "contentCategoryID": "0000004252", "mcc": "310", "mnc": "260",
        "csc": "NONE", "deviceId": "SM-R390", "sdkVer": "33", "pd": "0",
    }


def _field(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", block, re.S)
    return m.group(1).strip() if m else ""


def parse_page(xml: str) -> tuple[list[StoreFace], bool]:
    """Faces on one catalogue page and whether it was the last page.

    The store answers ``No Items`` past the end; ``isEndOfList`` stays false on an exactly full page.
    """
    if _field(xml, "resultMsg") == "No Items":
        return [], True
    if _field(xml, "resultCode") != "0":
        raise ValueError(f"catalogue request failed: {_field(xml, 'resultMsg') or 'no answer'}")
    faces = []
    for block in re.findall(r"<appInfo>(.*?)</appInfo>", xml, re.S):
        m = _APP_ID.search(_field(block, "appId"))
        if not m:
            continue
        faces.append(
            StoreFace(
                face_id=int(m.group(1)),
                name=_field(block, "productName"),
                preview=_field(block, "iconImgURL") or _field(block, "screenShotImgURL"),
                styles=int(_field(block, "screenShotCount") or 1),
                version=int(_field(block, "versionCode") or 0),
            )
        )
    return faces, _field(xml, "isEndOfList") == "true"


def preview_member(names: list[str], face_id: int, style: int = 0) -> str | None:
    """The package entry with the face's own preview for ``style`` (``assets/SM-R390_00016_2_0.png``, 256x402).

    Localised copies under ``assets/<locale>/`` are skipped."""
    pattern = re.compile(rf"assets/SM-R390_{face_id:05d}_\d+_{style}\.png")
    return next((n for n in sorted(names) if pattern.fullmatch(n)), None)


def from_dicts(items: list[dict]) -> dict[int, StoreFace]:
    fields = set(StoreFace.__dataclass_fields__)
    return {int(d["face_id"]): StoreFace(**{k: v for k, v in d.items() if k in fields}) for d in items}
