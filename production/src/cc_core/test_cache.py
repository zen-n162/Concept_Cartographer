"""テストモード — 同じ依頼の結果を再利用する (裁定 X / Y。コスト設計 §1・§2)。

**既定は OFF**。`--test-cache` / `CC_TEST_MODE=1` / Web の設定モーダルで明示的に
入れたときだけ働く。通常実行はこのモジュールを一切読まない・書かない
(索引ファイルすら作らない) ので、本番の挙動は変わらない。

狙いは「テスト段階の反復実行」だけを安くすること。同じ文言で 2 回目を回すと、
1 回目に確定した結果 (summary / 答え) をそのまま返し、**LLM を 1 回も呼ばない**。
資料の取込 (裁定 Y) も同じ TTL で再利用するので、文言を変えて試すときは
抽出以降だけが課金される。

## 黙って再利用しない

再利用したことは必ず呼び出し側へ返す (`hit.age_min` / `hit.session`)。CLI と Web は
これを「♻ 前回の結果を再利用」として**冒頭に**出す。黙って古い結果を返すと、
直したはずの挙動が直っていないように見えて、キャッシュの存在ごと信用を失う。

## キー

  sha256( normalize(依頼文) + level + target + layers + learned + local_only
          + kg_file + sorted(paths) )

normalize は NFKC + trim + 連続空白の畳み込み。完全一致より少しだけ寛容で、
意味は変えない (全角空白や末尾改行の違いで測り直しになるのを防ぐ)。

kg_file は設計書のキー一覧には無いが**足している**: 同じ文言・同じ設定で
`--kg` だけ違う 2 本は別の地図になるので、鍵が同じだと 2 本目に 1 本目の地図が
返る。索引が嘘をつくくらいならキーを 1 つ増やすほうが安い。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from cc_core.logging_util import get_logger

logger = get_logger("cc_core.test_cache")

CACHE_DIR = "logs/test_cache"
INDEX_NAME = "index.json"
DEFAULT_TTL_MIN = 360           # 6 時間 (CC_TEST_CACHE_TTL_MIN で変更可)
ENV_FLAG = "CC_TEST_MODE"
ENV_TTL = "CC_TEST_CACHE_TTL_MIN"

_SPACE_RE = re.compile(r"\s+")


# ------------------------------------------------------------------ 有効化


def env_enabled() -> bool:
    """環境変数によるテストモード (CC_TEST_MODE=1)。

    "0" / "" / 未設定はすべて OFF。既定 OFF を崩さないため、明示的に真と
    読める値のときだけ True にする。
    """
    value = (os.environ.get(ENV_FLAG) or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def enabled(flag: bool = False) -> bool:
    """CLI/Web のフラグと環境変数の論理和。"""
    return bool(flag) or env_enabled()


def ttl_minutes() -> int:
    """期限 (分)。壊れた値は既定へ落とす (実行を止める理由にはしない)。"""
    raw = os.environ.get(ENV_TTL)
    if raw is None:
        return DEFAULT_TTL_MIN
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s の値が数値ではありません: 既定 %d 分を使います",
                       ENV_TTL, DEFAULT_TTL_MIN)
        return DEFAULT_TTL_MIN
    return value if value > 0 else DEFAULT_TTL_MIN


# ------------------------------------------------------------------ キー


def normalize_message(message: str) -> str:
    """依頼文の正規化 (NFKC + trim + 連続空白の畳み込み)。

    表記ゆれを吸収するのはここまで。語順や助詞の違いまで同一視すると
    「別の依頼が同じ扱いになる」ほうの事故になるため、意味は変えない。
    """
    text = unicodedata.normalize("NFKC", str(message or ""))
    return _SPACE_RE.sub(" ", text).strip()


def make_key(message: str, *, level: str | None = None, target: str = "local",
             layers: bool = True, learned: bool = True, local_only: bool = False,
             kg_file: str | None = None,
             paths: Iterable[str] | None = None) -> str:
    """キャッシュキー (設計 §1「キャッシュキーと保存」)。

    level は None (依頼文から自動判定) と "standard" を**区別しない** —
    どちらも同じ地図になるので、呼び出し側で解決済みの値を渡すこと。
    """
    parts = [
        normalize_message(message),
        str(level or ""),
        str(target or ""),
        f"layers={bool(layers)}",
        f"learned={bool(learned)}",
        f"local_only={bool(local_only)}",
        f"kg={kg_file or ''}",
        "paths=" + "|".join(sorted(str(p) for p in (paths or ()))),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def ingest_key(window: str, *, paths: Iterable[str] | None = None,
               local_only: bool = False) -> str:
    """取込キャッシュのキー (裁定 Y: 期間指定 + sorted(paths) + local_only)。"""
    parts = [
        str(window or ""),
        f"local_only={bool(local_only)}",
        "paths=" + "|".join(sorted(str(p) for p in (paths or ()))),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 索引


@dataclass
class CacheHit:
    """再利用できる 1 件。表示に必要な材料をすべて持たせる。"""

    kind: str                       # "map" | "qa"
    entry: dict[str, Any]
    age_min: int

    @property
    def session(self) -> str | None:
        return self.entry.get("session")

    def to_dict(self) -> dict[str, Any]:
        """summary["cache"] に載る形。

        `note` は CLI・Web が**そのまま出す**告知文 (設計 §1「表示」)。文面を
        ここで 1 本に決めておくことで、2 つの画面で言い回しがずれない。
        """
        where = f" / session {self.session}" if self.session else ""
        info: dict[str, Any] = {
            "hit": True, "age_min": self.age_min,
            "note": (f"♻ 前回の結果を再利用 (テストモード / "
                     f"{self.age_min} 分前{where})")}
        if self.session:
            info["from"] = self.session
        return info


def cache_dir(base: str | Path = CACHE_DIR) -> Path:
    return Path(base)


def index_path(base: str | Path = CACHE_DIR) -> Path:
    return cache_dir(base) / INDEX_NAME


def load_index(base: str | Path = CACHE_DIR) -> dict[str, Any]:
    """索引を読む。**壊れていても実行を止めない** (空とみなして作り直す)。

    キャッシュは派生物であって原本ではないので、読めないことは異常ではあっても
    致命ではない。ここで例外を上げると、節約のための機能が実行を殺すことになる。
    """
    path = index_path(base)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("テストキャッシュの索引を読めません (%s) — 作り直します",
                       type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(index: dict[str, Any], base: str | Path = CACHE_DIR) -> None:
    """索引を原子的に書く (途中で落ちても半端な JSON を残さない)。"""
    path = index_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _age_minutes(ts: str, now: dt.datetime | None = None) -> int | None:
    try:
        stamp = dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    delta = (now or dt.datetime.now()) - stamp
    return max(0, int(delta.total_seconds() // 60))


def lookup(key: str, *, base: str | Path = CACHE_DIR,
           ttl_min: int | None = None,
           now: dt.datetime | None = None) -> CacheHit | None:
    """期限内の登録を引く。期限切れ・壊れた行は「無い」として扱う。"""
    entry = load_index(base).get(key)
    if not isinstance(entry, dict):
        return None
    age = _age_minutes(entry.get("ts", ""), now)
    if age is None:
        return None
    limit = ttl_minutes() if ttl_min is None else ttl_min
    if age > limit:
        logger.info("テストキャッシュは期限切れ (%d 分 > %d 分) — 通常実行します",
                    age, limit)
        return None
    kind = str(entry.get("kind") or "")
    if kind not in ("map", "qa"):
        return None
    return CacheHit(kind=kind, entry=entry, age_min=age)


def _prune(index: dict[str, Any], *, ttl_min: int,
           now: dt.datetime | None = None) -> dict[str, Any]:
    """期限切れの登録を落とす。

    `lookup` は期限切れを読み飛ばすだけなので、これが無いと索引は**永久に
    増え続ける**。二度と当たらない行のために、以後の実行が毎回それを読む
    ことになる (登録のたびに全体を読み書きするため 1 実行 2 回)。
    """
    kept: dict[str, Any] = {}
    for key, entry in index.items():
        if not isinstance(entry, dict):
            continue
        age = _age_minutes(entry.get("ts", ""), now)
        if age is not None and age <= ttl_min:
            kept[key] = entry
    return kept


def record(key: str, kind: str, *, message: str, session: str | None = None,
           answer: str | None = None, sources: Any = None,
           qa: Any = None, base: str | Path = CACHE_DIR,
           now: dt.datetime | None = None) -> dict[str, Any] | None:
    """成功した実行を登録する。**失敗した実行は呼ばれない** (設計 §1「ミス時」)。

    書けなくても実行そのものは成功しているので、例外は投げずに None を返す。
    """
    entry: dict[str, Any] = {
        "kind": kind,
        "ts": (now or dt.datetime.now()).isoformat(timespec="seconds"),
        "message": str(message or "")[:400],
    }
    if session:
        entry["session"] = session
    if answer is not None:
        entry["answer"] = answer
    if sources is not None:
        entry["sources"] = sources
    if qa is not None:
        entry["qa"] = qa
    try:
        # 書くついでに期限切れを掃除する (索引を無限に太らせない)
        index = _prune(load_index(base), ttl_min=ttl_minutes(), now=now)
        index[key] = entry
        _write_index(index, base)
    except OSError as exc:
        logger.warning("テストキャッシュに登録できません: %s", type(exc).__name__)
        return None
    logger.info("テストキャッシュに登録 kind=%s session=%s", kind, session or "-")
    return entry


# ------------------------------------------------- 取込キャッシュ (裁定 Y)


def ingest_path(key: str, base: str | Path = CACHE_DIR) -> Path:
    return cache_dir(base) / f"ingest_{key[:16]}.json"


def load_ingest(key: str, *, base: str | Path = CACHE_DIR,
                ttl_min: int | None = None,
                now: dt.datetime | None = None) -> tuple[dict[str, Any], int] | None:
    """取り込み済みの資料バンドルを引く。期限内なら (payload, 経過分)。"""
    path = ingest_path(key, base)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("取込キャッシュを読めません (%s) — 取り直します",
                       type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    age = _age_minutes(payload.get("ts", ""), now)
    if age is None:
        return None
    limit = ttl_minutes() if ttl_min is None else ttl_min
    if age > limit:
        return None
    return payload, age


def save_ingest(key: str, payload: dict[str, Any], *,
                base: str | Path = CACHE_DIR,
                now: dt.datetime | None = None) -> Path | None:
    """取り込んだ資料を保存する。書けなくても取込は成功している。"""
    body = dict(payload)
    body["ts"] = (now or dt.datetime.now()).isoformat(timespec="seconds")
    path = ingest_path(key, base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("取込キャッシュを保存できません: %s", type(exc).__name__)
        return None
    return path


__all__ = ["CACHE_DIR", "CacheHit", "DEFAULT_TTL_MIN", "enabled", "env_enabled",
           "index_path", "ingest_key", "ingest_path", "load_index", "load_ingest",
           "lookup", "make_key", "normalize_message", "record", "save_ingest",
           "ttl_minutes"]
