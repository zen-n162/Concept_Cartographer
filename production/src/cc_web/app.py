"""FastAPI アプリ本体 (設計書 §4 の API 仕様)。

方針:
- 状態は持たない。地図は `graphs/*.json`、評価は `logs/evaluation.jsonl`、
  履歴は `logs/web_history.jsonl` が正。プロセスを落としても失われない。
- 重い処理 (パイプライン) は JobManager のワーカースレッドへ。ここでは
  投入と状態参照だけを行う。
- 相対パスは **呼び出し時の cwd** で解決する (テストが tmp_path へ chdir して
  同じコードを使えるようにするため)。モジュール読込時に cwd を固定しない。
- エラーは `{"error": {"message": ...}}` に統一する。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from cc_core.community import LEVEL_ORDER
from cc_core.editing import EditConflict, EditError, EditTargetNotFound
from cc_core.evaluation import EvaluationSession, EvaluationStore
from cc_core import gap_report as gap_report_mod
from cc_core.gaps import GapDecisionError
from cc_core.learning import cue_warnings, load_learned, summarize as summarize_learned
from cc_core.logging_util import get_logger
from cc_core.offline_eval import GOLD_DIR, run_offline_eval
from cc_orchestrator.pipeline import offline_needs_kg_file
from cc_web import account, jobs as jobs_mod, sessions as sessions_mod
from cc_web.jobs import JobManager

logger = get_logger("cc_web.app")

STATIC_DIR = Path(__file__).parent / "static"

_STARTED_AT = dt.datetime.now().replace(microsecond=0).isoformat()


def _code_revision() -> str:
    """動作中コードの git 版数 (取れなければ "unknown")。

    古いサーバが残っていると修正が効かないまま応答し続けるため、
    /healthz から外形的に確認できるようにする。
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"
INBOX_DIR = "inbox"
EVAL_LOG = "logs/evaluation.jsonl"
ALLOWED_UPLOAD_EXT = {".pdf", ".docx", ".txt", ".md"}
TARGETS = ("local", "file")

# 設計書 §6.4。UI のテンプレートカード 4 枚。
TEMPLATES: list[dict[str, str]] = [
    {
        "id": "weekly", "icon": "map-2", "bg": "#EEEDFE", "fg": "#534AB7",
        "title": "今週の研究を概念地図として整理して",
        "description": "今週の研究内容を概念地図にまとめ、主要な概念と関係性を可視化。",
        "message": "今週の研究を概念地図として整理して",
    },
    {
        "id": "prior", "icon": "hierarchy-2", "bg": "#E1F5EE", "fg": "#0F6E56",
        "title": "先行研究の関係性を概念地図にして",
        "description": "アップロードした論文から先行研究のつながりを整理。",
        "message": "アップロードした資料から先行研究の関係性を概念地図にして",
    },
    {
        "id": "ideas", "icon": "bulb", "bg": "#FBEAF0", "fg": "#993556",
        "title": "研究アイデアを広げて整理して",
        "description": "テーマに関連する概念を広げ、構造的に整理。",
        "message": "研究アイデアを広げて概念地図として整理して",
    },
    {
        "id": "causal", "icon": "chart-dots-3", "bg": "#FAECE7", "fg": "#993C1D",
        "title": "実験結果の因果関係を整理して",
        "description": "実験結果から読み取れる因果関係を地図化し示唆を導出。",
        "message": "実験結果の因果関係を概念地図として整理して",
    },
]


# ------------------------------------------------------------ リクエスト型


class JobRequest(BaseModel):
    message: str
    level: str | None = None
    local_only: bool = False
    causal_verify: bool = True
    kg_file: str | None = None
    target: str = "local"
    offline: bool = False
    learned: bool = True     # 過去の修正からの学習を適用するか (§8.1)
    layers: bool = True      # 多層分析 (R2a 設計書 §10)。M7 で既定 ON
    # テストモード (裁定 X)。**既定 OFF** — 明示的に入れたときだけ再利用する
    test_cache: bool = False


class GapDecisionRequest(BaseModel):
    decision: str


class EditRequest(BaseModel):
    """編集 1 操作 (編集/学習設計書 §2)。

    edit_id / ts / user / before はサーバが充填するので受け取らない
    (クライアントに採番させると重複や偽装の余地ができる)。
    """

    op: str
    target: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------ ヘルパ


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _check_level(level: str) -> str:
    if level not in LEVEL_ORDER:
        raise _bad(f"詳細度は {'/'.join(LEVEL_ORDER)} のいずれかです: {level}")
    return level


def _safe_name(filename: str | None) -> str:
    """アップロード名を basename 化する (パストラバーサル対策)。

    `../evil.md` や `C:\\tmp\\x.md` のような入力でも inbox 直下にしか
    書かせない。隠しファイル (先頭ドット) も拒否する。
    """
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    if not name or name.startswith("."):
        raise _bad("ファイル名が不正です")
    return name


def _resolve_kg_file(value: str) -> str:
    """kg_file を graphs/ 配下へ固定する (任意パスの読み出しを禁じる)。"""
    name = _safe_name(value)
    rel = Path(sessions_mod.GRAPHS_DIR) / name
    if not rel.exists():
        raise _bad(f"kg_file が見つかりません: {name}")
    return str(rel)


def _session_or_404(session: str) -> None:
    try:
        sessions_mod.plan_path(session)
    except sessions_mod.SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _edit_http_error(exc: EditError) -> HTTPException:
    """編集エラーを HTTP へ写す (§8.1: validate 400 / 対象なし 404 / 競合 409)。"""
    if isinstance(exc, EditTargetNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, EditConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ------------------------------------------------------------ アプリ生成


def create_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.jobs.shutdown()

    app = FastAPI(title="Concept Cartographer", version="1.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.jobs = JobManager()

    # -------------------------------------------------- エラー形式の統一
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": {"message": str(exc.detail)}})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError):
        # 入力の形違いは 400 で返す (422 だと UI 側の分岐が増えるだけ)
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", ())[1:])
        why = first.get("msg", "リクエストの形式が不正です")
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"{where}: {why}".strip(": ")}})

    # -------------------------------------------------- 基本
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        # code は「いま動いているサーバのコード版数」。古いサーバが残っていると
        # 修正が効かないまま応答し続けるため、外から確認できるようにする
        # 【実測 2026-08-07: 3 時間前起動のサーバが旧仕様で生成していた】
        return {"ok": True, "code": _code_revision(), "started_at": _STARTED_AT}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/me")
    def api_me() -> dict[str, Any]:
        return account.me()

    @app.get("/api/templates")
    def api_templates() -> dict[str, Any]:
        return {"templates": TEMPLATES}

    # -------------------------------------------------- サインイン (web-auth)
    # 認証の実体は az CLI のセッション (裁定 AF)。ここは起動と可視化だけ。
    @app.post("/api/auth/login", status_code=202)
    def api_auth_login() -> dict[str, Any]:
        """デバイスコードフローを開始する。実行中なら 409 (裁定 AH)。

        az が無い / 起動できない場合も **500 にはしない** — status=error と
        案内文を 202 で返し、UI 側でそのまま出す。サインインできないことは
        このアプリの異常ではなく、伝えるべき事実だから。
        """
        try:
            return account.start_device_login()
        except account.LoginInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/auth/login")
    def api_auth_login_status() -> dict[str, Any]:
        return account.login_status()

    @app.post("/api/auth/cancel")
    def api_auth_cancel() -> dict[str, Any]:
        return account.cancel_login()

    @app.post("/api/auth/logout")
    def api_auth_logout() -> dict[str, Any]:
        """az logout。新しい /api/me を同じ応答に載せる (画面を即差し替える)。"""
        result = account.logout()
        return {"ok": result["ok"], "message": result.get("message"),
                "me": account.me(force=True)}

    # -------------------------------------------------- ファイル (inbox)
    @app.get("/api/files")
    def api_files() -> dict[str, Any]:
        base = Path(INBOX_DIR)
        if not base.exists():
            return {"files": []}
        items = []
        for p in sorted((f for f in base.iterdir() if f.is_file()),
                        key=lambda f: f.stat().st_mtime, reverse=True):
            items.append({"name": p.name, "size": p.stat().st_size,
                          "ext": p.suffix.lower().lstrip(".")})
        return {"files": items}

    @app.post("/api/files")
    async def api_upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        base = Path(INBOX_DIR)
        base.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for item in files:
            name = _safe_name(item.filename)
            if Path(name).suffix.lower() not in ALLOWED_UPLOAD_EXT:
                raise _bad(
                    "対応する拡張子は "
                    f"{', '.join(sorted(ALLOWED_UPLOAD_EXT))} のみです: {name}")
            (base / name).write_bytes(await item.read())
            saved.append(name)
        logger.info("upload saved n=%d", len(saved))
        return {"saved": saved}

    @app.delete("/api/files/{name}")
    def api_delete_file(name: str) -> dict[str, Any]:
        """添付チップの × から inbox/ の資料を実削除する。

        アップロードと同じ basename 検証を通す (パス要素を含む名前で
        inbox の外を消せないようにする)。
        """
        safe = _safe_name(name)
        path = Path(INBOX_DIR) / safe
        if not path.is_file():
            raise HTTPException(status_code=404,
                                detail=f"ファイルが見つかりません: {safe}")
        try:
            path.unlink()
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"削除できませんでした: {safe}") from exc
        logger.info("upload deleted")
        return {"deleted": safe}

    # -------------------------------------------------- ジョブ
    @app.post("/api/jobs", status_code=202)
    def api_create_job(req: JobRequest) -> dict[str, Any]:
        if not req.message.strip():
            raise _bad("依頼文が空です")
        if req.target not in TARGETS:
            raise _bad(f"target は {'/'.join(TARGETS)} のいずれかです: {req.target}")
        params: dict[str, Any] = {
            "message": req.message.strip(),
            "level": _check_level(req.level) if req.level else None,
            "local_only": req.local_only,
            "causal_verify": req.causal_verify,
            "target": req.target,
            "offline": req.offline,
            "learned": req.learned,
            "layers": req.layers,
            "test_cache": req.test_cache,
            "kg_file": _resolve_kg_file(req.kg_file) if req.kg_file else None,
        }
        # offline の kg_file 必須は**地図生成の話**。R2b の QA 経路は保存済みの
        # 索引だけで (劣化した形で) 答えられるので通す (pipeline と同じ規則)。
        if (req.offline and not params["kg_file"]
                and offline_needs_kg_file(params["message"])):
            raise _bad("offline モードは kg_file が必須です (地図生成の場合)")
        job = app.state.jobs.submit(params)
        return {"job_id": job.job_id}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict[str, Any]:
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"未知のジョブ: {job_id}")
        return job.to_dict()

    # -------------------------------------------------- セッション
    @app.get("/api/sessions")
    def api_sessions() -> dict[str, Any]:
        titles = jobs_mod.history_titles()
        return {"sessions": sessions_mod.list_sessions(titles)}

    @app.get("/api/sessions/{session}")
    def api_session(session: str) -> dict[str, Any]:
        _session_or_404(session)
        return sessions_mod.session_detail(session)

    @app.get("/api/sessions/{session}/svg")
    def api_session_svg(session: str, level: str = Query("standard")) -> FileResponse:
        _session_or_404(session)
        _check_level(level)
        path = sessions_mod.svg_file(session, level)
        # 詳細度を切り替えるたび取り直すのでキャッシュさせない
        return FileResponse(path, media_type="image/svg+xml",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session}/excalidraw")
    def api_session_excalidraw(session: str) -> FileResponse:
        """生成済みシーンのダウンロード (設計書 §5.3 の地図ツールバー)。

        パイプラインが `exports/session_{s}.excalidraw` に書いたものを返すだけ。
        """
        _session_or_404(session)
        path = Path("exports") / f"session_{session}.excalidraw"
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail="このセッションの .excalidraw は生成されていません")
        return FileResponse(path, media_type="application/json",
                            filename=f"session_{session}.excalidraw")

    @app.post("/api/sessions/{session}/render")
    def api_session_render(session: str, level: str = Query("standard")) -> dict[str, Any]:
        """今の詳細度をローカル Excalidraw canvas へ描画する (設計書 §2.1)。

        canvas は 1 面しかないため、生成ジョブ・編集と同じ JobManager の
        ロックで直列化する (run_exclusive は既存の _pool.submit(...).result()
        ヘルパ)。MCP/canvas に繋がらない場合は 503。
        """
        _session_or_404(session)
        _check_level(level)
        try:
            return app.state.jobs.run_exclusive(
                sessions_mod.render_to_canvas, session, level)
        except sessions_mod.RenderConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/sessions/{session}/view")
    def api_session_view(session: str, level: str = Query("standard")) -> dict[str, Any]:
        _session_or_404(session)
        _check_level(level)
        return sessions_mod.view_of(session, level)

    @app.get("/api/sessions/{session}/layers")
    def api_session_layers(session: str) -> dict[str, Any]:
        """多層分析のサイドカー (R2a 設計書 §10)。

        R2a 以前に作った地図には存在しないので、その場合は 404 + 理由。
        エラーではなく「この地図は古い世代」であることを本文で伝える。
        """
        _session_or_404(session)
        try:
            return sessions_mod.layers_of(session)
        except sessions_mod.LayersNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/sessions/{session}/gaps/{gap_id}")
    def api_gap_decision(session: str, gap_id: str,
                         req: GapDecisionRequest) -> dict[str, Any]:
        _session_or_404(session)
        if req.decision not in ("confirm", "dismiss"):
            raise _bad(f"decision は confirm / dismiss のみ: {req.decision}")
        try:
            return sessions_mod.decide_gap(session, gap_id, req.decision,
                                           user_id=account.current_user_id())
        except sessions_mod.GapNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GapDecisionError as exc:  # 確定済みの再確定 (上書きしない)
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/sessions/{session}/gap-report")
    def api_gap_report(session: str) -> dict[str, Any]:
        """ギャップの「次の一手」を作る (R2c 設計書 §2.1)。

        `run_exclusive` に載せるのは、セッションを横断して KG を読み込むため
        生成ジョブと同時に走らせたくないから。CPU ではなく**読む対象が動く**
        のが理由で、生成中の中途半端な plan を材料にすると finding が嘘になる。

        LLM は付いていれば使うが、無くても finding だけで成立する
        (受け入れ基準 3)。既定では外部ネットワークへは一切出ない (裁定 R)。
        """
        _session_or_404(session)
        return app.state.jobs.run_exclusive(sessions_mod.build_gap_report, session)

    @app.get("/api/sessions/{session}/gap-report")
    def api_gap_report_file(session: str) -> FileResponse:
        """保存済みレポートの JSON をそのまま返す (画面のダウンロード用)。

        POST の戻り値と同じ中身。ダウンロードはこのアプリの流儀どおり
        `<a download>` + GET で行うので、その受け口がいる。
        """
        _session_or_404(session)
        path = Path(gap_report_mod.EXPORT_DIR) / f"gap_report_{session}.json"
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail="ギャップレポートがまだありません。先に作成してください")
        return FileResponse(path, media_type="application/json",
                            filename=path.name)

    # -------------------------------------------------- 編集 (§8.1)
    @app.get("/api/sessions/{session}/edits")
    def api_edits(session: str) -> dict[str, Any]:
        _session_or_404(session)
        return sessions_mod.list_edits(session)

    @app.post("/api/sessions/{session}/edits")
    def api_add_edit(session: str, req: EditRequest,
                     level: str | None = Query(None)) -> dict[str, Any]:
        _session_or_404(session)
        if level:
            _check_level(level)
        op = {"op": req.op, "target": req.target, "payload": req.payload}
        try:
            # 生成ジョブと同じワーカーで直列化する (plan の二重書き込み防止)
            return app.state.jobs.run_exclusive(
                sessions_mod.apply_edit, session, op,
                user=account.current_user_id(), level=level)
        except EditError as exc:
            raise _edit_http_error(exc) from exc

    @app.post("/api/sessions/{session}/edits/{edit_id}/revert")
    def api_revert_edit(session: str, edit_id: str,
                        level: str | None = Query(None)) -> dict[str, Any]:
        _session_or_404(session)
        if level:
            _check_level(level)
        try:
            return app.state.jobs.run_exclusive(
                sessions_mod.revert_edit, session, edit_id,
                user=account.current_user_id(), level=level)
        except EditError as exc:
            raise _edit_http_error(exc) from exc

    @app.get("/api/learned")
    def api_learned() -> dict[str, Any]:
        """学習ストアの中身 (設定画面の透明性表示用。§8.1)。

        「何を機械が自動適用するのか」を利用者がいつでも確認できることが
        「黙って直さない」原則 (§1) の担保になるので、要約だけでなく
        エントリ本体も返す。
        """
        store = load_learned()
        return {
            "summary": summarize_learned(store),
            "lexicon": store.get("lexicon", []),
            "stoplist": store.get("stoplist", []),
            "causal_overrides": store.get("causal_overrides", []),
            "few_shot": store.get("few_shot", []),
            "cue_stats": store.get("cue_stats", {}),
            "warnings": cue_warnings(store),
        }

    @app.post("/api/sessions/{session}/expand/{aggregate_id}")
    def api_expand(session: str, aggregate_id: str) -> dict[str, Any]:
        _session_or_404(session)
        try:
            return sessions_mod.expand(session, aggregate_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"集約ノードが見つかりません: {aggregate_id}") from exc

    @app.post("/api/sessions/{session}/evaluation")
    def api_evaluation(session: str,
                       body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _session_or_404(session)
        ev = EvaluationSession(map_id=session, user_id=account.current_user_id())
        try:
            if "satisfaction" in body:
                ev.rate(int(body["satisfaction"]))
            elif "edge_id" in body and "verdict" in body:
                ev.judge_relation(str(body["edge_id"]), str(body["verdict"]))
            elif "operation" in body:
                detail = {k: v for k, v in body.items() if k != "operation"}
                ev.log_operation(str(body["operation"]), **detail)
            else:
                raise ValueError(
                    "satisfaction / edge_id+verdict / operation のいずれかが必要です")
        except (TypeError, ValueError) as exc:
            raise _bad(str(exc)) from exc
        EvaluationStore(EVAL_LOG).append(ev)
        return {"ok": True}

    @app.get("/api/evaluation/offline")
    def api_offline_evaluation() -> dict[str, Any]:
        """オフライン評価 (R2c 設計書 §1.2)。CLI `--offline-eval` と同じ JSON。

        **LLM を 1 回も呼ばない** (裁定 O) ので JobManager へは回さず、その場で
        読んで返す。材料は溜まった判定と graphs/ のファイルだけで、重いのは
        セッション数ぶんの JSON 読み込みに限られる。

        判定が 0 件でも 200 で返す (`empty: true` + `hint`)。「まだ測っていない」
        は異常ではないので、404 や 500 にすると使い始めが壊れて見える
        (受け入れ基準 2)。
        """
        return run_offline_eval(eval_log=EVAL_LOG, gold_dir=GOLD_DIR,
                                graphs_dir=sessions_mod.GRAPHS_DIR)

    # -------------------------------------------------- 履歴
    @app.get("/api/history")
    def api_history() -> dict[str, Any]:
        return {"items": jobs_mod.read_history()}

    # 静的配信は最後に (API パスを食わないよう /static 配下に限定)。
    # Cache-Control を付けないとブラウザのヒューリスティックキャッシュが
    # 古い app.js をサーバに聞かずに使い続け、**アプリを更新しても
    # 再読み込みで新機能が出ない**【実測 2026-08-08: 地図ズーム追加後も
    # 旧 JS が動き続け「昔の図はズームが効かない」ように見えた】。
    # no-cache = 毎回 ETag で再検証 (ローカル配信なのでコストは無視できる)
    app.mount("/static", _NoCacheStatic(directory=STATIC_DIR), name="static")
    return app


class _NoCacheStatic(StaticFiles):
    """更新が即座に届く静的配信 (ETag 再検証は残るので転送は 304 で済む)。"""

    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = create_app()
