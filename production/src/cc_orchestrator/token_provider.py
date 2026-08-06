"""Entra トークン取得 (az CLI 経由)。

azure-identity は cryptography のネイティブ wheel がこの環境で壊れているため
使わず、ログイン済みの az CLI からトークンを取得する。トークンは期限 5 分前
までキャッシュする。トークン自体はログ・ファイルへ出力しない。
"""

from __future__ import annotations

import json
import subprocess
import time


class AzTokenProvider:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}

    def token(self, resource: str) -> str:
        cached = self._cache.get(resource)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        if resource == "ms-graph":
            cmd = ["az", "account", "get-access-token", "--resource-type", "ms-graph",
                   "--output", "json"]
        else:
            cmd = ["az", "account", "get-access-token", "--resource", resource,
                   "--output", "json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(
                f"az token acquisition failed for {resource}: {proc.stderr.strip()[:200]}"
            )
        data = json.loads(proc.stdout)
        expires = data.get("expires_on") or time.time() + 1800
        self._cache[resource] = (data["accessToken"], float(expires))
        return data["accessToken"]


TOKENS = AzTokenProvider()
