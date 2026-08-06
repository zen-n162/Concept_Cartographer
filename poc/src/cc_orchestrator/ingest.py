"""今週の研究資料の取込 (Layer 1 Ingest の最小実装)。

ソース優先順:
  1. Microsoft Graph (OneDrive /me/drive/recent + insights) — 動作にはテナント側で
     (a) OneDrive のプロビジョニング と (b) Files.Read 系スコープ が必要。
     権限が無い場合は自動でスキップしてログに理由を残す。
  2. ローカルフォルダ: --path 指定 / $CC_INBOX_DIRS / ./inbox
     (OneDrive/SharePoint 同期フォルダを指定すれば実質同じデータソースになる)

いずれも更新日時が対象期間内のファイルのみ。pdf/docx/txt/md からテキスト抽出。
本文はメモリ内でのみ扱い、ログにはファイル名と文字数だけを残す。
"""

from __future__ import annotations

import datetime as dt
import io
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from cc_core.logging_util import get_logger
from cc_orchestrator.token_provider import TOKENS

logger = get_logger("cc_orchestrator.ingest")

SUPPORTED = {".pdf", ".txt", ".md", ".docx"}
PER_DOC_CHARS = 15000
TOTAL_CHARS = 48000


@dataclass
class Doc:
    name: str
    source: str          # "graph" | "local"
    modified: dt.datetime
    text: str


# ---------- 期間解釈 ----------
def parse_window(message: str, now: dt.datetime | None = None) -> tuple[dt.datetime, str]:
    now = now or dt.datetime.now()
    m = re.search(r"(?:直近|過去)\s*(\d+)\s*日", message)
    if m:
        days = int(m.group(1))
        return now - dt.timedelta(days=days), f"直近{days}日"
    if "先週" in message:
        monday = (now - dt.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return monday - dt.timedelta(days=7), "先週以降"
    if "今月" in message:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "今月"
    if "今週" in message:
        monday = (now - dt.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return monday, "今週 (月曜以降)"
    return now - dt.timedelta(days=7), "直近7日 (既定)"


# ---------- テキスト抽出 ----------
def extract_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        if ext == ".docx":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            return re.sub(r"<[^>]+>", "", xml)
        return data.decode("utf-8", "ignore")
    except Exception as exc:
        logger.warning("text extraction failed name=%s err=%s", name, type(exc).__name__)
        return ""


# ---------- Microsoft Graph ----------
def _graph_get(path: str) -> dict | None:
    try:
        token = TOKENS.token("ms-graph")
    except RuntimeError as exc:
        logger.warning("graph token unavailable: %s", exc)
        return None
    resp = httpx.get(f"https://graph.microsoft.com/v1.0{path}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if resp.status_code != 200:
        logger.info("graph %s -> %d (skip)", path.split("?")[0], resp.status_code)
        return None
    return resp.json()


def ingest_graph(since: dt.datetime) -> list[Doc]:
    """OneDrive/SharePoint の最近のファイル。権限が無ければ空リスト。"""
    docs: list[Doc] = []
    recent = _graph_get("/me/drive/recent?$top=50")
    if recent is None:
        logger.info("graph ingest unavailable (OneDrive 未提供 or 権限不足) — ローカル取込へ")
        return docs
    token = TOKENS.token("ms-graph")
    for item in recent.get("value", []):
        name = item.get("name", "")
        if Path(name).suffix.lower() not in SUPPORTED:
            continue
        mod = dt.datetime.fromisoformat(
            item["lastModifiedDateTime"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        if mod < since:
            continue
        remote = item.get("remoteItem") or {}
        drive_id = (remote.get("parentReference") or item.get("parentReference") or {}).get("driveId")
        item_id = remote.get("id") or item.get("id")
        if not (drive_id and item_id):
            continue
        resp = httpx.get(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content",
            headers={"Authorization": f"Bearer {token}"},
            timeout=120, follow_redirects=True)
        if resp.status_code != 200:
            continue
        text = extract_text(name, resp.content)[:PER_DOC_CHARS]
        if text.strip():
            docs.append(Doc(name=name, source="graph", modified=mod, text=text))
            logger.info("graph doc name=%s chars=%d", name, len(text))
    return docs


# ---------- ローカルフォルダ ----------
def ingest_local(paths: list[str], since: dt.datetime) -> list[Doc]:
    docs: list[Doc] = []
    search: list[Path] = [Path(p).expanduser() for p in paths]
    env_dirs = os.environ.get("CC_INBOX_DIRS", "")
    search += [Path(p).expanduser() for p in env_dirs.split(":") if p]
    if not search:
        search = [Path("inbox")]
    for base in search:
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in SUPPORTED:
                continue
            mod = dt.datetime.fromtimestamp(f.stat().st_mtime)
            if mod < since:
                continue
            text = extract_text(f.name, f.read_bytes())[:PER_DOC_CHARS]
            if text.strip():
                docs.append(Doc(name=f.name, source="local", modified=mod, text=text))
                logger.info("local doc name=%s chars=%d", f.name, len(text))
    return docs


def ingest(message: str, paths: list[str]) -> tuple[list[Doc], str]:
    since, label = parse_window(message)
    docs = ingest_graph(since) + ingest_local(paths, since)
    # 合計文字数を丸める (新しい順に優先)
    docs.sort(key=lambda d: d.modified, reverse=True)
    total = 0
    kept: list[Doc] = []
    for d in docs:
        room = TOTAL_CHARS - total
        if room <= 0:
            logger.info("char budget reached; dropping name=%s", d.name)
            continue
        d.text = d.text[:room]
        total += len(d.text)
        kept.append(d)
    logger.info("ingest window=%s docs=%d chars=%d", label, len(kept), total)
    return kept, label


def bundle(docs: list[Doc]) -> str:
    parts = []
    for d in docs:
        parts.append(f"===== FILE: {d.name} (updated {d.modified:%Y-%m-%d}) =====\n{d.text}")
    return "\n\n".join(parts)
