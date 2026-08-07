"""Web からのサインイン/アウト (web-auth 設計書) の回帰テスト。

**本物の az は 1 回も起動しない**。`subprocess.Popen` / `subprocess.run` を
差し替え、開発機の az セッションを絶対に壊さないようにしてある
(このテストが実 az を叩くと、走らせた人が突然サインアウトされる)。

az の出力は「stdout に出る版」「stderr に出る版」「全角が混じる端末」の
3 通りを想定する — 設計書のとおり、az はバージョンで出力先が違う。
"""

from __future__ import annotations

import io
import subprocess
import threading
import time

import pytest

from cc_web import account
from cc_web.app import create_app

CODE = "AB1CD2EF3"
DEVICE_LINE = (
    "To sign in, use a web browser to open the page "
    f"https://microsoft.com/devicelogin and enter the code {CODE} to authenticate.\n"
)
# 全角混じり (NFKC で畳めば同じコードになるはず)
DEVICE_LINE_WIDE = (
    "To sign in, use a web browser to open the page "
    "https://microsoft.com/devicelogin and enter the code "
    "ＡＢ１ＣＤ２ＥＦ３ to authenticate.\n"
)
TENANT_LINE = "Retrieving tenants and subscriptions for the selection...\n"


class FakePopen:
    """az login の子プロセスの替え玉。

    `block=True` で「ユーザーがまだブラウザでコードを入れていない」状態を
    再現する。`ignore_terminate=True` は SIGTERM を無視する子 (kill 経路)。
    """

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0,
                 block: bool = False, ignore_terminate: bool = False):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.args = list(account.AZ_LOGIN_CMD)
        self._rc = returncode
        self._ignore_terminate = ignore_terminate
        self._exited = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        if not block:
            self._exited.set()

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
        return self._rc

    def poll(self):
        return self._rc if self._exited.is_set() else None

    def terminate(self):
        self.terminate_calls += 1
        if not self._ignore_terminate:
            self._rc = -15
            self._exited.set()

    def kill(self):
        self.kill_calls += 1
        self._rc = -9
        self._exited.set()


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """毎回まっさらな idle から始め、実 az も実 UPN も見に行かせない。"""
    monkeypatch.chdir(tmp_path)
    upn = {"value": "nakamura.zen@example.ac.jp"}
    monkeypatch.setattr(account, "_az_upn", lambda: upn["value"])
    account.reset_login_state()
    account.clear_cache()
    yield upn
    account.reset_login_state()
    account.clear_cache()


@pytest.fixture
def client(auth_env):
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def fake_popen(**kwargs):
    """Popen の差し替え本体を作り、生成された偽プロセスを覚えておく。"""
    made: list[FakePopen] = []

    def factory(*_args, **_kwargs):
        proc = FakePopen(**kwargs)
        made.append(proc)
        return proc

    factory.made = made  # type: ignore[attr-defined]
    return factory


def wait_for(predicate, timeout: float = 5.0) -> dict:
    """監視スレッドが状態を書き換えるのを待つ (ポーリングと同じ見え方)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = account.login_status()
        if predicate(status):
            return status
        time.sleep(0.01)
    raise AssertionError(f"状態が変わりませんでした: {account.login_status()}")


# ------------------------------------------------------------ コード抽出

def test_code_from_stdout(auth_env, monkeypatch):
    """案内文が stdout に出る az でコードを拾える。"""
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stdout=DEVICE_LINE, block=True))
    started = account.start_device_login()
    assert started["status"] == "waiting_code"

    status = wait_for(lambda s: s["code"] is not None)
    assert status["code"] == CODE
    assert status["url"] == "https://microsoft.com/devicelogin"
    assert status["status"] == "waiting_code"


def test_code_from_stderr(auth_env, monkeypatch):
    """案内文が stderr (警告扱い) に出る az でも拾える。"""
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stderr=DEVICE_LINE, block=True))
    account.start_device_login()
    assert wait_for(lambda s: s["code"] is not None)["code"] == CODE


def test_code_with_fullwidth_chars(auth_env, monkeypatch):
    """全角が混ざった出力でも半角のコードとして取り出す。"""
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stderr=DEVICE_LINE_WIDE, block=True))
    account.start_device_login()
    assert wait_for(lambda s: s["code"] is not None)["code"] == CODE


# ------------------------------------------------------------ 状態遷移

def test_transition_idle_waiting_authenticating_done(auth_env, monkeypatch):
    """idle → waiting_code → authenticating → done と進み、キャッシュが落ちる。"""
    assert account.login_status()["status"] == "idle"
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stderr=DEVICE_LINE + TENANT_LINE, returncode=0))

    account.me()  # 古い UPN でキャッシュを温めておく
    auth_env["value"] = "new.person@example.ac.jp"

    account.start_device_login()
    done = wait_for(lambda s: s["status"] == "done")
    assert done["code"] == CODE
    # 終了コード 0 でキャッシュが無効化され、次の /api/me が引き直される
    assert account.me()["upn"] == "new.person@example.ac.jp"


def test_authenticating_needs_code_first(auth_env, monkeypatch):
    """コードを出す前にテナント行が来ても authenticating へ先走らない。"""
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stderr=TENANT_LINE, block=True))
    account.start_device_login()
    time.sleep(0.15)
    assert account.login_status()["status"] == "waiting_code"


def test_transition_to_error_keeps_tail(auth_env, monkeypatch):
    """非 0 終了は error。出力の末尾をそのまま理由として見せる。"""
    monkeypatch.setattr(subprocess, "Popen", fake_popen(
        stderr="ERROR: AADSTS70016 authorization_pending expired\n", returncode=1))
    account.start_device_login()
    status = wait_for(lambda s: s["status"] == "error")
    assert "AADSTS70016" in status["message"]
    assert status["code"] is None


def test_single_flow_conflict(auth_env, monkeypatch):
    """裁定 AH: 実行中の再開始は LoginInProgress (= 409)。"""
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stderr=DEVICE_LINE, block=True))
    account.start_device_login()
    wait_for(lambda s: s["code"] is not None)
    with pytest.raises(account.LoginInProgress):
        account.start_device_login()


def test_cancel_terminates_child(auth_env, monkeypatch):
    """中止で子プロセスを terminate し、状態は idle へ戻す。"""
    factory = fake_popen(stderr=DEVICE_LINE, block=True)
    monkeypatch.setattr(subprocess, "Popen", factory)
    account.start_device_login()
    wait_for(lambda s: s["code"] is not None)

    status = account.cancel_login()
    assert status["status"] == "idle"
    assert status["code"] is None
    assert factory.made[0].terminate_calls == 1
    # 監視スレッドが後から error を書き込んで idle を汚さないこと
    time.sleep(0.2)
    assert account.login_status()["status"] == "idle"


def test_login_child_gets_its_own_session(auth_env, monkeypatch):
    """`az` は python を呼ぶ bash ラッパ。独立セッションで起動しないと、
    中止してもラッパだけ死んで python が孤児 (PPID 1) のまま残る (実測)。"""
    seen: list[dict] = []

    def factory(*_args, **kwargs):
        seen.append(kwargs)
        return FakePopen(block=True)

    monkeypatch.setattr(subprocess, "Popen", factory)
    account.start_device_login()
    assert seen[0]["start_new_session"] is True
    assert seen[0]["stdin"] is subprocess.DEVNULL   # 対話プロンプトで固まらせない


def test_timeout_kills_child(auth_env, monkeypatch):
    """裁定 AH: 900 秒 (ここでは短縮) で terminate → 効かなければ kill。"""
    monkeypatch.setattr(account, "LOGIN_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(account, "TERMINATE_GRACE_SEC", 0.05)
    factory = fake_popen(stderr=DEVICE_LINE, block=True, ignore_terminate=True)
    monkeypatch.setattr(subprocess, "Popen", factory)

    account.start_device_login()
    status = wait_for(lambda s: s["status"] == "error")
    assert "時間切れ" in status["message"]
    assert factory.made[0].terminate_calls == 1
    assert factory.made[0].kill_calls == 1


# ------------------------------------------------------------ ログアウト

def test_logout_invalidates_cache(auth_env, monkeypatch):
    """az logout でキャッシュを捨て、次の me() が「ローカル ユーザー」になる。"""
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert account.me()["signed_in"] is True

    auth_env["value"] = None  # az logout 後は UPN が引けなくなる
    result = account.logout()
    assert result["ok"] is True
    assert calls == [list(account.AZ_LOGOUT_CMD)]
    assert account.me()["name"] == "ローカル ユーザー"


def test_logout_failure_is_reported_not_raised(auth_env, monkeypatch):
    """未サインインで az logout が非 0 でも例外にせず理由を返す。"""
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="ERROR: There are no active accounts.\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = account.logout()
    assert result["ok"] is False
    assert "no active accounts" in result["message"]


# ------------------------------------------------------------ az 不在

def test_missing_az_is_error_not_crash(auth_env, monkeypatch):
    """az が入っていない Mac でも例外にせず案内文を返す (UI は動かし続ける)。"""
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("az")

    monkeypatch.setattr(subprocess, "Popen", boom)
    status = account.start_device_login()
    assert status["status"] == "error"
    assert "Azure CLI" in status["message"]

    monkeypatch.setattr(subprocess, "run", boom)
    assert account.logout()["ok"] is False


# ------------------------------------------------------------ エンドポイント

def test_endpoint_login_and_status(client, monkeypatch):
    """POST は 202 + {status, code?, url}、GET は 200 でポーリングできる。"""
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stderr=DEVICE_LINE, block=True))
    res = client.post("/api/auth/login")
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "waiting_code"
    assert body["url"] == "https://microsoft.com/devicelogin"

    wait_for(lambda s: s["code"] is not None)
    poll = client.get("/api/auth/login")
    assert poll.status_code == 200
    assert poll.json()["code"] == CODE


def test_endpoint_login_conflict_and_cancel(client, monkeypatch):
    """2 本目の POST は 409 (エラー体つき)、cancel すれば再び開始できる。"""
    monkeypatch.setattr(subprocess, "Popen",
                        fake_popen(stderr=DEVICE_LINE, block=True))
    assert client.post("/api/auth/login").status_code == 202
    wait_for(lambda s: s["code"] is not None)

    conflict = client.post("/api/auth/login")
    assert conflict.status_code == 409
    assert "実行中" in conflict.json()["error"]["message"]

    cancelled = client.post("/api/auth/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "idle"
    assert client.post("/api/auth/login").status_code == 202


def test_endpoint_logout_returns_fresh_me(client, auth_env, monkeypatch):
    """POST /api/auth/logout の応答で /api/me が即差し替わる。"""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    assert client.get("/api/me").json()["signed_in"] is True

    auth_env["value"] = None
    res = client.post("/api/auth/logout")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["me"]["signed_in"] is False
    assert body["me"]["name"] == "ローカル ユーザー"
    # 続けて叩く /api/me も (キャッシュではなく) 新しい方を返す
    assert client.get("/api/me").json()["signed_in"] is False


def test_endpoint_login_without_az_is_not_500(client, monkeypatch):
    """az 不在でも 500 にしない — 202 + status=error で案内する。"""
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("az")

    monkeypatch.setattr(subprocess, "Popen", boom)
    res = client.post("/api/auth/login")
    assert res.status_code == 202
    assert res.json()["status"] == "error"
    assert "Azure CLI" in res.json()["message"]
