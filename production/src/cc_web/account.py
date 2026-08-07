"""サインイン中のアカウント情報 (設計書 §4 の /api/me)。

R1 は個人モードのシングルユーザーなので認証基盤を持たない。ログイン済みの
Azure CLI (`az account show`) から UPN を借りて表示するだけにする。

azure-identity は使えない (arm64 環境で cryptography の wheel が壊れている)
ため、SDK ではなく az CLI を subprocess で叩く。az 自体が数秒かかることが
あるので 10 分キャッシュする。az が無い / 未ログインでも UI は動かす
(signed_in=False で「ローカル ユーザー」表示)。

後半はサインイン/アウト (web-auth 設計書)。認証の実体は従来どおり
**この Mac の az CLI のセッション**で (裁定 AF)、ここがやるのは
`az login --use-device-code` の起動・デバイスコードの可視化・`az logout`
だけ。状態はメモリにしか置かない — サーバを落とせば idle に戻るのが正しい
(受け入れ基準 3)。
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
import unicodedata
from typing import Any

from cc_core.logging_util import get_logger

logger = get_logger("cc_web.account")

CACHE_TTL_SEC = 600  # 10 分
AZ_TIMEOUT_SEC = 15

# 裁定 AH: デバイスコードの有効期限 (既定 15 分) に合わせて子プロセスを畳む
LOGIN_TIMEOUT_SEC = 900
# terminate してから kill に切り替えるまでの猶予
TERMINATE_GRACE_SEC = 5
DEVICE_LOGIN_URL = "https://microsoft.com/devicelogin"

# `-o none` は成功時のサブスクリプション一覧を黙らせるだけ。デバイスコードの
# 案内は警告として stderr に出るので消えない (`--only-show-errors` は
# **付けてはいけない** — az のバージョンによってはコードの案内ごと消える)。
AZ_LOGIN_CMD = ("az", "login", "--use-device-code", "-o", "none")
AZ_LOGOUT_CMD = ("az", "logout")

AZ_MISSING_MSG = (
    "az コマンドを実行できませんでした。Azure CLI がこの Mac に入っているか"
    "確認してください (brew install azure-cli)"
)

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


# ============================================================ サインイン
# 以降は az login --use-device-code の起動と可視化 (web-auth 設計書)。

# az の案内文:
#   "To sign in, use a web browser to open the page https://microsoft.com/devicelogin
#    and enter the code ABCD1234 to authenticate."
# az のバージョンで stdout / stderr のどちらに出るか違うので両方を見る。
# 全角が混ざる端末があるので NFKC で畳んでから当てる。
_CODE_RE = re.compile(r"enter\s+the\s+code\s+([A-Za-z0-9][A-Za-z0-9-]{3,15})")
# 文言が変わったとき用の保険 (「code XXXXXXXX」だけ拾う)
_CODE_FALLBACK_RE = re.compile(r"\bcode\s+([A-Za-z0-9]{6,16})\b")
_URL_RE = re.compile(r"https?://\S+devicelogin\S*")
# コード入力後、az がテナント/サブスクリプションを取りに行くときの出力。
# 「ブラウザ待ち」と「認証後の処理中」を区別できる唯一の手掛かり。
_AUTHENTICATING_HINTS = ("retrieving", "subscription", "tenant")


class LoginInProgress(RuntimeError):
    """裁定 AH: ログインフローは同時に 1 つだけ。実行中の再開始はこれ。"""


_login_lock = threading.Lock()
_login: dict[str, Any] = {
    "status": "idle",       # idle | waiting_code | authenticating | done | error
    "code": None,
    "url": DEVICE_LOGIN_URL,
    "message": None,
}
_login_proc: subprocess.Popen[str] | None = None
_ACTIVE = ("waiting_code", "authenticating")
# 本物の Popen クラスを掴んでおく。テストは subprocess.Popen を替え玉の関数に
# 差し替えるので、_signal_group の型判定はこちらで行う (差し替え後の
# subprocess.Popen は関数であって型ではなく、isinstance に渡せない)。
_REAL_POPEN = subprocess.Popen


def _norm(line: str) -> str:
    """全角混じりの出力を半角へ畳む (コード抽出の前処理)。"""
    return unicodedata.normalize("NFKC", line).strip()


def _scan_login_line(line: str) -> None:
    """az の出力 1 行から、デバイスコードと進行状況を読み取る。"""
    with _login_lock:
        if _login["status"] not in _ACTIVE:
            return  # 中止済み / 終了済みのフローの残り火は捨てる
        if _login["code"] is None:
            match = _CODE_RE.search(line) or _CODE_FALLBACK_RE.search(line)
            if match:
                _login["code"] = match.group(1).upper()
                url = _URL_RE.search(line)
                if url:
                    _login["url"] = url.group(0).rstrip(".,、。)]")
                # コードは短命の公開値で秘密ではないのでログに残してよい。
                # **トークン類は一切出さない** (設計書)。
                logger.info("device login code issued: %s", _login["code"])
                return
        # コードを出す前の行で先走らない (「コードは出ていないのに認証中」を防ぐ)
        if _login["code"] and any(h in line.lower() for h in _AUTHENTICATING_HINTS):
            _login["status"] = "authenticating"


def _read_stream(stream: Any, buf: list[str]) -> None:
    """子プロセスの出力を 1 行ずつ食べる (stdout/stderr で 1 本ずつ動かす)。"""
    try:
        for raw in stream:
            line = _norm(raw)
            if not line:
                continue
            buf.append(line)
            _scan_login_line(line)
    except (OSError, ValueError):  # 途中で kill されると読み取りが壊れる
        pass
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _signal_group(proc: Any, sig: int) -> None:
    """子の**プロセスグループごと**シグナルを送る。

    Homebrew の `az` は python 本体を呼ぶだけの bash ラッパなので、ラッパ
    だけに SIGTERM を送ると python がデバイスコードを polling したまま孤児
    (PPID 1) として残る — 実測で確認済み。そのため Popen は
    `start_new_session=True` で独立したセッションに置き、ここでグループごと
    畳む。

    グループが自分と同じなら **絶対に撃たない** (start_new_session が効いて
    いない環境で uvicorn ごと道連れにするのを防ぐ最後の砦)。その場合と
    テストの替え玉は、通常の terminate/kill にフォールバックする。
    """
    pgid = None
    if isinstance(proc, _REAL_POPEN):
        with contextlib.suppress(OSError, ValueError):
            pgid = os.getpgid(proc.pid)
    if pgid is not None and pgid != os.getpgid(0):
        with contextlib.suppress(OSError):
            os.killpg(pgid, sig)
            return
    with contextlib.suppress(OSError):
        proc.kill() if sig == signal.SIGKILL else proc.terminate()


def _terminate(proc: Any) -> None:
    """terminate → 効かなければ kill (裁定 AH)。"""
    if proc.poll() is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=TERMINATE_GRACE_SEC)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=TERMINATE_GRACE_SEC)


def _watch_login(proc: Any) -> None:
    """子プロセスを看取り、終了コードで done / error を決める。"""
    buf: list[str] = []
    readers = [
        threading.Thread(target=_read_stream, args=(s, buf), daemon=True)
        for s in (proc.stdout, proc.stderr) if s is not None
    ]
    for thread in readers:
        thread.start()

    timed_out = False
    try:
        code = proc.wait(timeout=LOGIN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(proc)
        code = proc.poll()
    for thread in readers:
        thread.join(timeout=TERMINATE_GRACE_SEC)

    tail = " ".join(buf)[-200:]
    with _login_lock:
        if _login_proc is not proc:
            return  # cancel 済み / 別のフローに置き換わった。状態を触らない
        if timed_out:
            _login.update(status="error", code=None, message=(
                "時間切れです (15 分)。もう一度サインインをやり直してください"))
        elif code == 0:
            _login.update(status="done", message=None)
        else:
            _login.update(status="error", code=None,
                          message=tail or f"az login が失敗しました (rc={code})")
        final = _login["status"]
    if final == "done":
        # 設計書: 終了コード 0 → /api/me のキャッシュを即無効化する。
        # 次の GET /api/me が新しい UPN を引き直す。
        clear_cache()
    logger.info("device login finished status=%s", final)


def start_device_login() -> dict[str, Any]:
    """`az login --use-device-code` を起動する (裁定 AH: 同時 1 本)。

    実行中に呼ばれたら LoginInProgress (呼び出し側で 409)。az が無い環境でも
    例外にはせず status=error + 案内文で返す — UI は動き続けるべきなので。
    """
    global _login_proc
    with _login_lock:
        if _login["status"] in _ACTIVE:
            raise LoginInProgress("サインインの手続きが既に実行中です")
        _login.update(status="waiting_code", code=None,
                      url=DEVICE_LOGIN_URL, message=None)
        _login_proc = None

    try:
        proc = subprocess.Popen(  # noqa: S603 — 固定コマンド
            list(AZ_LOGIN_CMD), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            # 中止時にラッパごと畳めるよう独立セッションへ (_signal_group 参照)
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("az login unavailable: %s", type(exc).__name__)
        with _login_lock:
            _login.update(status="error", code=None, message=AZ_MISSING_MSG)
            return dict(_login)

    with _login_lock:
        _login_proc = proc
        snapshot = dict(_login)
    threading.Thread(target=_watch_login, args=(proc,), daemon=True).start()
    return snapshot


def login_status() -> dict[str, Any]:
    """今の状態。UI が 2 秒間隔で叩く。"""
    with _login_lock:
        return dict(_login)


def cancel_login() -> dict[str, Any]:
    """実行中のフローを畳む。終わっていれば何もしない (冪等)。"""
    global _login_proc
    with _login_lock:
        proc = _login_proc
        _login_proc = None
        if _login["status"] in _ACTIVE:
            _login.update(status="idle", code=None,
                          message="サインインを中止しました")
        snapshot = dict(_login)
    if proc is not None:
        _terminate(proc)   # ロックの外で待つ (最大 10 秒かかりうる)
    return snapshot


def logout() -> dict[str, Any]:
    """`az logout`。**この Mac の az CLI 全体**からサインアウトする (裁定 AG)。"""
    cancel_login()  # 進行中のフローが後から done になるのを防ぐ
    try:
        proc = subprocess.run(  # noqa: S603 — 固定コマンド
            list(AZ_LOGOUT_CMD), capture_output=True, text=True,
            timeout=AZ_TIMEOUT_SEC, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("az logout unavailable: %s", type(exc).__name__)
        clear_cache()
        return {"ok": False, "message": AZ_MISSING_MSG}

    clear_cache()  # 成否によらず捨てる (中途半端に消えていることがある)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-200:]
        logger.info("az logout failed rc=%d", proc.returncode)
        return {"ok": False,
                "message": tail or f"az logout が失敗しました (rc={proc.returncode})"}
    logger.info("az logout done")
    return {"ok": True, "message": None}


def reset_login_state() -> None:
    """テスト用。プロセスは残さず idle へ戻す。"""
    global _login_proc
    with _login_lock:
        proc = _login_proc
        _login_proc = None
        _login.update(status="idle", code=None, url=DEVICE_LOGIN_URL, message=None)
    if proc is not None:
        _terminate(proc)
