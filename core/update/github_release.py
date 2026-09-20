"""Anonymous GitHub Releases lookup for the AMBER installer.

This module is pure logic: no Tk, no threading.  All network access goes
through an injectable ``fetch`` callable so tests can run without touching
the network.

Version comparison note
------------------------
GitHub release tags follow ``vMAJOR.MINOR.PATCH`` (three parts, e.g.
``v1.5.20``).  The local runtime version comes from
``core.install.versioning.resolve_version()`` which is always four parts,
``MAJOR.MINOR.PATCH.BUILD`` (e.g. ``1.5.20.825``) — the fourth part is a
build/commit counter that has no equivalent on the release tag.  Comparing
all four parts would therefore always report the local build as "newer"
because the tag never carries a fourth number.  Instead, comparison is
truncated to the *shorter* of the two version tuples: when the tag is three
parts, only Major.Minor.Patch are compared and the local Build number is
ignored.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence

RELEASES_API_URL = (
    "https://api.github.com/repos/JJHbrams/Project-AMBER/releases/latest"
)
USER_AGENT = "ProjectIntelContinuum-UpdateCheck/1.0"
REQUEST_TIMEOUT_SECONDS = 10

ASSET_NAME_PREFIX = "AMBER_"
ASSET_NAME_SUFFIX = "_x64-setup.exe"


class UpdateCheckError(Exception):
    """Raised whenever the update check cannot produce a usable answer.

    ``reason`` is always a Korean sentence suitable for showing to the user
    verbatim (e.g. in a messagebox).  Nothing in this module returns ``None``
    on failure — every failure path raises this with an explicit reason.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int
    created_at: str


@dataclass(frozen=True)
class UpdateResult:
    is_update_available: bool
    current_version: str
    latest_version: str
    asset: "ReleaseAsset | None"


FetchFunc = Callable[[urllib.request.Request], object]


def default_fetch(request: urllib.request.Request):
    return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)


def fetch_latest_release(fetch: FetchFunc | None = None) -> dict:
    """Fetch the latest release JSON. Raises UpdateCheckError on any failure."""

    fetcher = fetch or default_fetch
    request = urllib.request.Request(
        RELEASES_API_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        response = fetcher(request)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateCheckError(
                "GitHub 익명 조회 한도를 초과했습니다(시간당 60회 제한). "
                "잠시 후 다시 시도해주세요."
            ) from exc
        raise UpdateCheckError(
            f"업데이트 정보를 가져오지 못했습니다 (HTTP {exc.code})."
        ) from exc
    except TimeoutError as exc:
        raise UpdateCheckError(
            "업데이트 서버 응답이 지연되어 확인에 실패했습니다 (시간 초과)."
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise UpdateCheckError(
                "업데이트 서버 응답이 지연되어 확인에 실패했습니다 (시간 초과)."
            ) from exc
        raise UpdateCheckError(
            "네트워크에 연결할 수 없어 업데이트를 확인하지 못했습니다."
        ) from exc
    except OSError as exc:
        raise UpdateCheckError(
            "네트워크에 연결할 수 없어 업데이트를 확인하지 못했습니다."
        ) from exc

    try:
        with response:
            raw = response.read()
    except AttributeError:
        raw = response.read()

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateCheckError(
            "업데이트 정보를 해석하지 못했습니다 (응답 형식 오류)."
        ) from exc

    if not isinstance(payload, dict):
        raise UpdateCheckError(
            "업데이트 정보를 해석하지 못했습니다 (응답 형식 오류)."
        )
    return payload


def normalize_tag(tag: str) -> tuple[int, ...]:
    """Normalize a release tag or local version string to a tuple of ints.

    Accepts ``v1.5.20``, ``1.5.20`` and ``1.5.20.825``.
    """

    cleaned = tag.strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:]
    parts = cleaned.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise UpdateCheckError(
            f"버전 문자열을 해석하지 못했습니다: '{tag}'"
        )
    return tuple(int(part) for part in parts)


def compare_versions(current_version: str, latest_tag: str) -> int:
    """Compare current (local, usually 4-part) vs latest (tag, usually 3-part).

    Only compares up to the shorter tuple's length — see module docstring.
    Returns negative if current < latest, 0 if equal (in the compared
    prefix), positive if current > latest.
    """

    current_parts = normalize_tag(current_version)
    latest_parts = normalize_tag(latest_tag)
    length = min(len(current_parts), len(latest_parts))
    current_prefix = current_parts[:length]
    latest_prefix = latest_parts[:length]
    if current_prefix < latest_prefix:
        return -1
    if current_prefix > latest_prefix:
        return 1
    return 0


def select_asset(assets: Sequence[dict]) -> ReleaseAsset:
    """Pick the installer asset from a release's asset list.

    Raises UpdateCheckError with a Korean reason when no suitable asset is
    found.
    """

    candidates = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and asset["name"].startswith(ASSET_NAME_PREFIX)
        and asset["name"].endswith(ASSET_NAME_SUFFIX)
    ]
    if not candidates:
        raise UpdateCheckError("릴리스에 설치 파일이 없습니다.")

    def _created_at(asset: dict) -> str:
        return str(asset.get("created_at") or "")

    chosen = max(candidates, key=_created_at)
    download_url = chosen.get("browser_download_url")
    if not isinstance(download_url, str) or not download_url:
        raise UpdateCheckError("릴리스에 설치 파일 다운로드 주소가 없습니다.")

    return ReleaseAsset(
        name=str(chosen.get("name")),
        download_url=download_url,
        size=int(chosen.get("size") or 0),
        created_at=_created_at(chosen),
    )


def check_update(current_version: str, fetch: FetchFunc | None = None) -> UpdateResult:
    """Full update check: fetch latest release, compare, pick asset.

    Raises UpdateCheckError (Korean reason) on any failure. If the current
    version is already up to date (or newer), returns an UpdateResult with
    ``is_update_available=False`` and ``asset=None`` — the asset lookup is
    skipped in that case since nothing needs to be downloaded.
    """

    payload = fetch_latest_release(fetch)
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise UpdateCheckError("릴리스 버전 정보를 해석하지 못했습니다.")

    comparison = compare_versions(current_version, tag)
    if comparison >= 0:
        return UpdateResult(
            is_update_available=False,
            current_version=current_version,
            latest_version=tag,
            asset=None,
        )

    assets = payload.get("assets")
    if not isinstance(assets, list):
        assets = []
    asset = select_asset(assets)

    return UpdateResult(
        is_update_available=True,
        current_version=current_version,
        latest_version=tag,
        asset=asset,
    )
