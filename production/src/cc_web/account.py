"""サインイン中のアカウント情報 (設計書 §4 の /api/me)。

R1 は個人モードのシングルユーザーなので認証基盤を持たない。ログイン済みの
Azure CLI (`az account show`) から UPN を借りて表示するだけにする。

azure-identity は使えない (arm64 環境で cryptography の wheel が壊れている)
ため、SDK ではなく az CLI を subprocess で叩く。az 自体が数秒かかることが
あるので 10 分キャッシュする。az が無い / 未ログインでも UI は動かす
(signed_in=False で「ローカル ユーザー」表示)。
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

from cc_core.logging_util import get_logger

logger = get_logger("cc_web.account")

CACHE_TTL_SEC = 600  # 10 分
AZ_TIMEOUT_SEC = 15

FALLBACK = {
    "name": "ローカル ユーザー",
    "upn": "",
    "initials": "ロユ",
    "signed_in": False,
    "mode": "personal",
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cached_at: float = 0.0


def _az_upn() -> str | None:
    """az CLI から UPN を取得する。失敗は None (UI は動かし続ける)。"""
    try:
        proc = subprocess.run(
            ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
            capture_output=True, text=True, timeout=AZ_TIMEOUT_SEC, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("az account show unavailable: %s", type(exc).__name__)
        return None
    if proc.returncode != 0:
        logger.info("az account show failed rc=%d", proc.returncode)
        return None
    upn = proc.stdout.strip()
    return upn or None


def _initials(name: str) -> str:
    """氏名から 2 文字のイニシャルを作る (アバター表示用)。"""
    parts = [p for p in name.replace("　", " ").split(" ") if p]
    if not parts:
        return "CC"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def profile_from_upn(upn: str) -> dict[str, Any]:
    """UPN から表示用プロフィールを組み立てる。

    `nakamura.zen@example.ac.jp` → 「Nakamura Zen」。区切りは . _ - を想定し、
    それ以外の形 (日本語ローカル部など) はそのまま表示する。
    """
    local = upn.split("@", 1)[0]
    words = [w for w in local.replace("_", ".").replace("-", ".").split(".") if w]
    name = " ".join(w[:1].upper() + w[1:] for w in words) if words else upn
    return {
        "name": name,
        "upn": upn,
        "initials": _initials(name),
        "signed_in": True,
        "mode": "personal",
    }


def clear_cache() -> None:
    """キャッシュを捨てる (サインイン切替・テスト用)。"""
    global _cache, _cached_at
    with _lock:
        _cache = None
        _cached_at = 0.0


def me(*, force: bool = False) -> dict[str, Any]:
    """/api/me の中身。10 分キャッシュ付き。"""
    global _cache, _cached_at
    with _lock:
        fresh = _cache is not None and (time.time() - _cached_at) < CACHE_TTL_SEC
        if fresh and not force:
            return dict(_cache)  # type: ignore[arg-type]
    upn = _az_upn()
    info = profile_from_upn(upn) if upn else dict(FALLBACK)
    with _lock:
        _cache = info
        _cached_at = time.time()
    return dict(info)


def current_user_id() -> str:
    """ギャップ確定・評価記録に残す user_id。未サインインでも空にしない。"""
    info = me()
    return info.get("upn") or info.get("name") or "local-user"
