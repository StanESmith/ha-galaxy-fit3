"""Samsung store catalogue parsing (format from a live response on 2026-11-19)."""

from __future__ import annotations

import pytest

import store_catalog as sc

PAGE = """<result>
	<resultCode>0</resultCode>
	<resultMsg>Success</resultMsg>
	<isEndOfList>false</isEndOfList>
	<appInfo>
		<productID>000007255362</productID>
		<productName><![CDATA[Black and white]]></productName>
		<appId>com.samsung.fit3watchface.sm_r390_0112</appId>
		<iconImgURL>https://img.samsungapps.com/productNew/000007255362/IconImage_1_NEW_WAP_ICON_146_146.png</iconImgURL>
		<screenShotImgURL>https://img.samsungapps.com/productNew/000007255362/ENG/ScreenShot_1.png</screenShotImgURL>
			<screenShotCount>4</screenShotCount>
		<versionCode>40001</versionCode>
	</appInfo>
	<appInfo>
		<productName><![CDATA[Progress gauge 1]]></productName>
		<appId>com.samsung.fit3watchface.sm_r390_0016</appId>
		<screenShotImgURL>https://img.samsungapps.com/x.png</screenShotImgURL>
		<screenShotCount>3</screenShotCount>
		<versionCode>40000</versionCode>
	</appInfo>
</result>"""


def test_parse_page_reads_ids_names_previews() -> None:
    faces, last = sc.parse_page(PAGE)
    assert not last
    assert [(f.face_id, f.name, f.styles, f.version) for f in faces] == [
        (112, "Black and white", 4, 40001),
        (16, "Progress gauge 1", 3, 40000),
    ]
    assert faces[0].preview.endswith("ICON_146_146.png")  # the icon; screenshot links are dead
    assert faces[1].preview == "https://img.samsungapps.com/x.png"  # no icon: fall back to the screenshot
    assert sc.from_dicts([f.as_dict() for f in faces])[16].name == "Progress gauge 1"


def test_no_items_past_the_end_is_the_last_page() -> None:
    assert sc.parse_page("<result><resultCode>1</resultCode><resultMsg>No Items</resultMsg></result>") == ([], True)


def test_errors_raise() -> None:
    with pytest.raises(ValueError):
        sc.parse_page("<result><resultCode>9</resultCode><resultMsg>Bad</resultMsg></result>")


def test_cached_catalogue_ignores_unknown_keys() -> None:
    faces = sc.from_dicts([{"face_id": 16, "name": "Progress gauge 1", "preview": "p", "extra": 1}])
    assert faces[16].name == "Progress gauge 1"


def test_preview_member_picks_the_style_preview_not_a_locale_copy() -> None:
    names = ["assets/fr_FR/SM-R390_00016_2_0.png", "assets/SM-R390_00016_2_1.png", "assets/SM-R390_00016_2_0.png",
             "assets/SM-R390_00016_256x402.bin"]
    assert sc.preview_member(names, 16) == "assets/SM-R390_00016_2_0.png"
    assert sc.preview_member(names, 16, style=1) == "assets/SM-R390_00016_2_1.png"
    assert sc.preview_member(names, 79) is None
