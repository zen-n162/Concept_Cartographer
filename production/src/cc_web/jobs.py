"""地図生成ジョブの管理 (設計書 §4 の JobManager)。

パイプラインは内部で `asyncio.run` を使う同期関数なので、イベントループ内で
直接呼べない。必ずワーカースレッドへ逃がす。

**直列実行 (max_workers=1)** にしているのは、描画先の Excalidraw キャンバスが
共有状態で、2 本同時に描くと互いのシーンを壊すため (clear_before=True)。
2 件目以降は queued のまま待つ。

進捗は run_pipeline の progress フックで受け取り、Job.stage / stages_done を
更新する。完了・失敗時に logs/web_history.jsonl へ 1 行追記する
(依頼文はここにだけ残す。サーバログには出さない)。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cc_core.logging_util import get_logger
from cc_orchestrator.pipeline import run_pipeline

logger = get_logger("cc_web.jobs")

HISTORY_PATH = "logs/web_history.jsonl"
HISTORY_LIMIT = 50


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    """1 回の地図生成。UI はこの内容をポーリングして進捗を描く。"""

    job_id: str
    message: str
    params: dict[str, Any]
    status: str = "queued"                     # queued | running | done | error
    stage: dict[str, str] | None = None
    stages_done: list[str] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "stages_done": list(self.stages_done),
            "summary": self.summary,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }

    # ---- 履歴用の派生値 ----
    @property
    def session(self) -> str | None:
        """地図が保存されたセッション ID。地図なし応答 (basic/vector) では None。"""
        if not self.summary or not self.summary.get("layout"):
            return None
        return self.summary.get("session")

    @property
    def route(self) -> str | None:
        if not self.summary:
            return None
        return (self.summary.get("routing") or {}).get("route")


class JobManager:
    """ジョブの投入・状態参照・履歴追記。"""

    def __init__(self, *, history_path: str | Path = HISTORY_PATH) -> None:
        self.history_path = Path(history_path)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cc-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 投入
    def submit(self, params: dict[str, Any]) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12],
                  message=str(params.get("message", "")),
                  params=dict(params))
        with self._lock:
            self._jobs[job.job_id] = job
        self._pool.submit(self._run, job)
        logger.info("job submitted id=%s offline=%s target=%s", job.job_id,
                    bool(params.get("offline")), params.get("target"))
        return job

    def run_exclusive(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """生成ジョブと**同じ 1 本のワーカー**で即時実行し、結果を待つ。

        編集も rebuild (plan の書き換え) を伴うため、生成中に割り込むと
        同じ plan ファイルを 2 系統が書いて壊れる。JobManager の
        ThreadPoolExecutor(max_workers=1) を共有することで、キューに並べる
        だけで直列化できる (編集/学習設計書 §8.1)。

        呼び出し元は FastAPI の同期エンドポイント (= 別スレッド) なので、
        ここで結果を待ってもイベントループは止まらない。
        """
        return self._pool.submit(fn, *args, **kwargs).result()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ 実行
    def _run(self, job: Job) -> None:
        with self._lock:
            job.status = "running"

        def progress(key: str, label: str) -> None:
            with self._lock:
                job.stage = {"key": key, "label": label}
                if key not in job.stages_done:
                    job.stages_done.append(key)

        p = job.params
        try:
            summary = run_pipeline(
                job.message,
                target=p.get("target") or "local",
                paths=p.get("paths") or None,
                kg_file=p.get("kg_file") or None,
                local_only=bool(p.get("local_only")),
                detail_level=p.get("level") or None,
                verify_causal=bool(p.get("causal_verify", True)),
                progress=progress,
                offline=bool(p.get("offline")),
                learned=bool(p.get("learned", True)),
                layers=bool(p.get("layers", True)),
            )
            with self._lock:
                job.summary = summary
                job.status = "done"
                job.stage = None
        except Exception as exc:  # パイプラインの失敗は UI へ返す (落とさない)
            with self._lock:
                job.status = "error"
                job.stage = None
                job.error = f"{type(exc).__name__}: {exc}"
            logger.warning("job failed id=%s %s", job.job_id, type(exc).__name__)
        finally:
            with self._lock:
                job.finished_at = _now()
            self._append_history(job)

    # ------------------------------------------------------------ 履歴
    def _append_history(self, job: Job) -> None:
        entry: dict[str, Any] = {
            "ts": job.finished_at or _now(),
            "message": job.message,
            "job_id": job.job_id,
            "status": job.status,
        }
        if job.session:
            entry["session"] = job.session
        if job.route:
            entry["route"] = job.route
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # 履歴が書けなくても本処理は成功扱いにする
            logger.warning("history append failed: %s", type(exc).__name__)


def read_history(path: str | Path = HISTORY_PATH,
                 limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    """web_history.jsonl を新しい順に読む (壊れた行は飛ばす)。"""
    p = Path(path)
    if not p.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(items))[:limit]


def history_titles(path: str | Path = HISTORY_PATH) -> dict[str, str]:
    """session ID -> 依頼文。セッション一覧のタイトルに使う。"""
    titles: dict[str, str] = {}
    for item in reversed(read_history(path, limit=10_000)):
        session = item.get("session")
        if session and item.get("message"):
            titles[session] = item["message"]
    return titles
