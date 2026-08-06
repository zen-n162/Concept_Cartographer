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
import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from cc_core.community import LEVEL_ORDER
from cc_core.evaluation import EvaluationSession, EvaluationStore
from cc_core.gaps import GapDecisionError
from cc_core.logging_util import get_logger
from cc_web import account, jobs as jobs_mod, sessions as sessions_mod
from cc_web.jobs import JobManager

logger = get_logger("cc_web.app")

STATIC_DIR = Path(__file__).parent / "static"
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


class GapDecisionRequest(BaseModel):
    decision: str


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
        return {"ok": True}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/me")
    def api_me() -> dict[str, Any]:
        return account.me()

    @app.get("/api/templates")
    def api_templates() -> dict[str, Any]:
        return {"templates": TEMPLATES}

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
            "kg_file": _resolve_kg_file(req.kg_file) if req.kg_file else None,
        }
        if req.offline and not params["kg_file"]:
            raise _bad("offline モードは kg_file が必須です")
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

    @app.get("/api/sessions/{session}/view")
    def api_session_view(session: str, level: str = Query("standard")) -> dict[str, Any]:
        _session_or_404(session)
        _check_level(level)
        return sessions_mod.view_of(session, level)

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

    # -------------------------------------------------- 履歴
    @app.get("/api/history")
    def api_history() -> dict[str, Any]:
        return {"items": jobs_mod.read_history()}

    # 静的配信は最後に (API パスを食わないよう /static 配下に限定)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
