"""実運用版パイプライン (R1)。

  ⓪Routing → ①Ingest → ③Concept(抽出) → ④Relate(因果3点セット/矛盾非断定)
  → 可変詳細度(3レベル同梱) → ギャップ検出 → ⑧Project(描画) → 検証 → 評価

PoC からの主な変更 (実運用計画):
- ⓪ Query Routing を入口に置く (§6)。地図生成でない要求はフルパイプラインを回さない
- 可変詳細度: 生成は 1 回、3 レベルを同梱して切替は再計算なし (§4)
- 因果は 3 点セット通過時のみ、矛盾は R1 では非断定へ降格 (裁定 7)
- ギャップは 4 点メタデータ付き候補 + confirm/dismiss (裁定 8)
- run-command 中継は廃止。描画先は MCP か .excalidraw/SVG 直接生成 (§3-2)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cc_core import layer_assign, layers_store, test_cache, token_usage, verifiers
from cc_core.causal import apply_relation_policy
from cc_core.detail import build_multilevel_plan, check_level_bands, project
from cc_core.evaluation import summarize
from cc_core.gaps import GAP_KINDS, GAP_TYPES, detect_gaps
from cc_core.layer_assign import assign_layer_tags
from cc_core.layers import CAUSAL_GLYPH, apply_meta, verifier_id
from cc_core.layout_v3 import layout_summary
from cc_core.learning import (
    apply_learned,
    build_prompt_hints,
    load_learned,
    note_cues_kept,
)
from cc_core.logging_util import get_logger
from cc_core.mcp_client import extract_json, gateway_healthy
from cc_core.normalize import extract_max, merge_extraction, normalize_kg
from cc_core.overlap import check_overlaps
from cc_core.svg_export import write_svg
from cc_core.validate import validate_layout_plan
from cc_orchestrator import analysis, qa
from cc_orchestrator.agents_def import AGENT_SPECS, MODELS
from cc_orchestrator.foundry_v2 import FoundryAgentsV2
from cc_orchestrator.ingest import PER_DOC_CHARS, Doc, bundle, ingest, parse_window
from cc_orchestrator.routing import RouteDecision, route
from cc_orchestrator.tool_exec import ToolExecutor
from cc_store import SessionStore

logger = get_logger("cc_orchestrator.pipeline")

LOCAL_BUDGET = 40000
GRAPHS_DIR = "graphs"
SUMMARY_PREFIX = "summary_session_"

# ⑧Project / 検証のプロンプト (裁定 W)。**plan 本文を載せない**のが要点で、
# 描画対象は ToolExecutor.authoritative_plan が持っている。ここに view_json を
# 戻すと、往復ぶんの入力トークンがそのまま費用へ返ってくる。
RENDER_PROMPT = (
    "描画対象の layout_plan はツール側で確定済みです。"
    "render_layout_plan を引数 {} で呼び、結果を "
    '{"status": "RENDER_OK"|"RENDER_FAILED", "created": <要素数>} の '
    "JSON で返してください。"
)
VERIFY_PROMPT = (
    "直前に描画した layout_plan を検証してください。検証対象はツール側で"
    "確定済みです。verify_scene を引数 {} で呼び、結果を "
    '{"verdict": "PASS"|"FAIL", "missing": <数>, "mismatched": <数>, '
    '"summary": "<50字以内>"} の JSON で返してください。'
)

# ---- 描画の逃げ道 (描画ハング恒久対処 計画 C/D) ----------------------------
# ライブキャンバス (target=local) は外部プロセス 2 つ (canvas :3000 / MCP
# gateway :8000) に依存する。落ちていたり半死だったりしたときに待ち続けず、
# **ファイル生成へ倒して完走させる**。黙って倒すと「ライブに描いたつもりが
# 描かれていない」になるので、必ず summary に理由を残して CLI/Web に出す。
RENDER_DEADLINE_ENV = "CC_RENDER_DEADLINE_S"
DEFAULT_RENDER_DEADLINE_S = 600.0

GATEWAY_DOWN_NOTE = (
    "⚠ ライブキャンバスが応答しないため、ファイル生成に切り替えました。"
    "canvas 起動後に --render で描き直せます。")
RENDER_DEADLINE_NOTE = (
    "⚠ ライブキャンバスへの描画が時間内に終わらなかったため、"
    "ファイル生成に切り替えました。canvas 起動後に --render で描き直せます。")


def _render_deadline_s() -> float:
    """描画段の壁時計上限 (秒)。env CC_RENDER_DEADLINE_S で上書き可。

    実行のたびに読む — import 時に固めると、テストや運用での上書きが効かない。
    """
    raw = os.environ.get(RENDER_DEADLINE_ENV)
    if not raw:
        return DEFAULT_RENDER_DEADLINE_S
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s が数値ではありません (%r); 既定 %.0f 秒を使います",
                       RENDER_DEADLINE_ENV, raw, DEFAULT_RENDER_DEADLINE_S)
        return DEFAULT_RENDER_DEADLINE_S


class _RenderDeadlineExceeded(RuntimeError):
    """描画段が壁時計デッドラインを超えた (内部用; 呼び出し元へは出ない)。"""


ProgressFn = Callable[[str, str], None]
"""進捗フック: (stage_key, 日本語ラベル)。Web UI の進捗チェックリスト用。"""

# 進捗ステージ。UI 側が「未着手/実行中/完了」を描くための固定順序でもあるため、
# 並びを変えるときは cc_web/static/app.js の STAGES も揃えること。
STAGES: tuple[tuple[str, str], ...] = (
    ("routing", "経路判定"),
    ("ingest", "資料収集"),
    ("extract", "概念抽出"),
    ("zone", "文脈ラベル付け"),
    ("claims", "主張の抽出"),
    ("relate", "関係の検証"),
    ("validate", "主張の検証"),
    ("rhetoric", "論証と矛盾の検出"),
    ("detail", "詳細度の計算"),
    ("gaps", "ギャップ検出"),
    ("render", "描画"),
    ("verify", "独立検証"),
    ("export", "出力"),
)
STAGE_LABELS: dict[str, str] = dict(STAGES)


def _notify(progress: ProgressFn | None, key: str) -> None:
    """進捗を通知する。表示都合の失敗で本処理を止めない (例外は握りつぶす)。"""
    if progress is None:
        return
    try:
        progress(key, STAGE_LABELS[key])
    except Exception as exc:  # pragma: no cover - 通知側の事故は本処理に無関係
        logger.debug("progress hook error: %s", type(exc).__name__)


# ---------------------------------------------- 経路ディスパッチ表 (R2b §2)
#
# 「map 以外 → 直答」という 1 本の if 分岐を表に開いたもの。basic / vector は
# R1 の実装をそのまま関数へ移しただけで、使うエージェントもプロンプト文字列も
# 変えていない (受け入れ基準 1「map/basic/vector の挙動完全不変」)。
# QA 3 経路だけが新しく、材料集めと出典の組み立ては cc_orchestrator.qa にある。


OFFLINE_DIRECT_ANSWER = ("この問いに答えるには Foundry への接続が要ります "
                         "(offline 実行では LLM を呼べません)。")


def _answer_basic(message: str, *, client: FoundryAgentsV2 | None,
                  **_: Any) -> dict[str, Any]:
    """雑談・ヘルプ (R1 と同一: 依頼文をそのまま投げる)。"""
    if client is None:
        return {"answer": OFFLINE_DIRECT_ANSWER}
    return {"answer": client.run("cc-extraction", message)}


def _answer_vector(message: str, *, client: FoundryAgentsV2 | None,
                   **_: Any) -> dict[str, Any]:
    """事実照会 (R1 と同一: KB を使ってよいと添えて投げる)。"""
    if client is None:
        return {"answer": OFFLINE_DIRECT_ANSWER}
    return {"answer": client.run(
        "cc-extraction",
        f"次の質問に、必要なら Work IQ / KB で調べて簡潔に答えてください:\n{message}")}


def _qa_handler(route_name: str) -> Callable[..., dict[str, Any]]:
    """local / global / hybrid の入口 (材料は保存済みの索引から作る)。"""
    def handler(message: str, *, client: FoundryAgentsV2 | None,
                offline: bool = False, **_: Any) -> dict[str, Any]:
        return qa.ANSWERERS[route_name](
            message, SessionStore(), client, offline=offline)
    return handler


ROUTE_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "basic": _answer_basic,
    "vector": _answer_vector,
    **{name: _qa_handler(name) for name in qa.QA_ROUTES},
}


def offline_needs_kg_file(message: str) -> bool:
    """offline 実行で kg_file が要るか = **地図生成の経路かどうか** (R2b §2)。

    判定を 1 か所に置くための関数。Web の入口 (cc_web.app) も同じ規則で弾く —
    「CLI では答えるのに Web では 400」のような食い違いを作らないため。
    """
    return route(message).route not in ROUTE_HANDLERS


def ensure_agents(client: FoundryAgentsV2) -> dict[str, str]:
    names = {}
    for name, spec in AGENT_SPECS.items():
        names[name] = client.ensure_agent(
            name, spec["model"], spec["instructions"], spec["tools"],
            effort=spec.get("effort", "medium"),
            description=spec.get("description", ""),
            welcome=spec.get("welcome"))
    return names


# 因果の独立検証で「契約違反の応答」を受けたエッジに立てる一時印 (R1.5 の潜在不具合)。
# apply_relation_policy が返した後に _mark_verifier_errors が回収して消す。
VERIFIER_ERROR_FLAG = "_causal_verifier_error"
VERIFIER_ERROR_CODE = "verifier_error"


def _causal_verifier(client: FoundryAgentsV2):
    """独立検証器 (裁定 7 の 3 点目)。描画検証と同じ「別モデル判定」パターン。

    cc-verification (gpt-5.6-terra) に因果の可否だけを判定させる。抽出側
    (gpt-5.6-sol) とは別モデルなので、同一モデルの自己確認にならない。

    **応答に "causal" キーが無い場合の扱い** (R1.5 からの潜在不具合の修正):
    `bool(res.get("causal"))` だと「答えていない」が「因果ではない」と同じ
    結論になり、エージェントの結線ミスが静かに全件降格へ化ける
    (verifiers.LLMNLIVerifier.repair と同じ事故)。

    **結論は変えない** — 検証器が答えられなかった因果を通すほうが危険なので、
    安全側 (fail-closed) で降格させる。ただし `causal_check` に
    `reason_code: "verifier_error"` を残し、**本物の否定と区別できる**ように
    する。区別が要るのは、KPI で「検証器が否定した」と「検証器が壊れていた」を
    混ぜると、モデル障害が「因果の抽出精度が低い」に見えてしまうため。
    """
    def verify(edge: dict[str, Any], evidence_text: str) -> bool:
        prompt = (
            "次の JSON を処理し、JSON のみで応答してください。\n"
            + json.dumps({"task": "causal_check",
                          "relation": f"{edge.get('from')} → {edge.get('to')}"
                                      f" 「{edge.get('label', '')}」",
                          "evidence": evidence_text[:600]}, ensure_ascii=False)
        )
        try:
            res = extract_json(client.run("cc-verification", prompt))
        except Exception as exc:
            logger.warning("causal verifier error: %s", type(exc).__name__)
            raise
        if not isinstance(res, dict) or "causal" not in res:
            # edge は apply_relation_policy が作った複製で、そのまま kg に載る。
            # ここに印を付けておけば呼び出し側が後から回収できる
            edge[VERIFIER_ERROR_FLAG] = VERIFIER_ERROR_CODE
            logger.warning("causal verifier contract violation (no 'causal' key)")
            return False
        return bool(res.get("causal"))
    return verify


def _mark_verifier_errors(kg: dict[str, Any]) -> int:
    """`_causal_verifier` が立てた印を causal_check の理由へ畳む。

    印そのものは kg に残さない (保存形に `_` 始まりのキーを増やさない)。
    """
    marked = 0
    for edge in kg.get("edges", []) or ():
        if not isinstance(edge, dict) or not edge.pop(VERIFIER_ERROR_FLAG, None):
            continue
        check = edge.get("causal_check")
        if not isinstance(check, dict):
            continue
        check["reason_code"] = VERIFIER_ERROR_CODE
        check["reason"] = ("独立検証器が契約どおりに応答しなかった "
                           "(causal キー無し) — 安全側で相関へ降格")
        marked += 1
    if marked:
        logger.warning("causal verifier contract violations: %d edges", marked)
    return marked


# ------------------------------ 資料ごとの追加抽出ループ (裁定 AM = 抽出 v2)
#
# v1 は「概念 30〜80 個」と**指示するだけ**だった。指示は守られないことがあり、
# 同じ依頼で 64 のときと 20 のときがある (実測)。粒度そのものより
# **再現しないこと**が問題で、指示を強くしても再現性は買えない。
#
# v2 は仕組みで保証する: 初回抽出のあと概念が目標に届かなければ、**資料を
# 1 件ずつ回して**追加抽出する。1 call = 1 資料に絞ると、
#   - ローカル本文がある資料は本文をそのまま渡せる (連結した束への単一スライス
#     LOCAL_BUDGET では、資料が多いと後半が黙って切れていた)
#   - Work IQ 資料は「この 1 件だけ読み直して」と指示できる。本文はこちらへ
#     届かないので、資料名で指すしか手が無い (調査で判明した事実 1)
# どちらの経路でも既存ラベル一覧を毎回渡して重複を抑え、
# **資料に無いものは足さない**を毎回書く (創作禁止が上位原則)。

DETAILED_TARGET_DEFAULT = 45
ENV_DETAILED_TARGET = "CC_DETAILED_TARGET"
EXPAND_MAX_CALLS_DEFAULT = 5
ENV_EXPAND_MAX_CALLS = "CC_EXPAND_MAX_CALLS"
DRY_LIMIT = 2       # 新規ゼロが 2 資料続いたら、この資料束からはもう出ない


def _env_int(name: str, default: int, *, floor: int = 0) -> int:
    """環境変数を整数で読む (**呼び出しのたびに**読む)。

    Web は常駐プロセスなので、import 時に固定すると再起動なしに変えられない。
    読めない値は既定へ倒して警告を出す (黙って 0 にすると挙動が消える)。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, int(raw))
    except ValueError:
        logger.warning("%s=%r を整数として読めません: 既定 %d を使います",
                       name, raw, default)
        return default


def detailed_target() -> int:
    """追加抽出ループの目標概念数 (`CC_DETAILED_TARGET`、既定 45)。

    Standard の帯 (20-50) の中ほど。ここを超えると Standard に全量が収まらず、
    Detailed が本当に別の地図になる — 「三段に分化する」の実務的な下限線。
    """
    return _env_int(ENV_DETAILED_TARGET, DETAILED_TARGET_DEFAULT, floor=1)


def expand_max_calls() -> int:
    """追加抽出に使ってよい LLM call の上限 (`CC_EXPAND_MAX_CALLS`、既定 5)。

    抽出系は初回 1 + 追加 5 = 最大 6 call (裁定 AP)。0 を入れれば追加抽出を
    完全に止められる (費用の緊急ブレーキ)。
    """
    return _env_int(ENV_EXPAND_MAX_CALLS, EXPAND_MAX_CALLS_DEFAULT, floor=0)


@dataclass
class _Source:
    """追加抽出で 1 call を割り当てる資料 1 件。"""

    name: str
    text: str       # ローカル本文。Work IQ 資料は空 (向こう側で読み直させる)

    @property
    def kind(self) -> str:
        return "local" if self.text else "workiq"


def _document_roster(kg: dict[str, Any], docs: list[Any], *,
                     local_only: bool) -> list[_Source]:
    """巡回する資料の一覧 (ローカル Doc + `kg["source_files"]` の和集合)。

    並びは「本文を持つ資料 → Work IQ 資料」で、それぞれ元の順のまま
    (**決定的** — 同じ入力なら必ず同じ巡回順になる)。本文を持つ資料を先に
    回すのは 1 call あたりの取り分が大きいから: Work IQ 資料は向こうで
    読み直す往復が要り、空振りも起きうる。

    重複は**正規化ラベル**で除去する。`source_files` にはローカル添付資料も
    載るので、素の文字列一致だと同じ資料に 2 call 使うことがある。
    local_only のときは本文の無い資料を落とす — 読む手段が無いのに call を
    投げても空振りするだけ。
    """
    from cc_core.editing import normalize_label

    roster: list[_Source] = []
    seen: set[str] = set()

    def add(name: str, text: str) -> None:
        name = str(name or "").strip()
        key = normalize_label(name)
        if not name or key in seen:
            return
        seen.add(key)
        roster.append(_Source(name=name, text=text))

    for d in docs:
        add(str(getattr(d, "name", "") or ""), str(getattr(d, "text", "") or ""))
    if not local_only:
        for f in kg.get("source_files") or ():
            if isinstance(f, str):
                add(f, "")
    return [s for s in roster if s.text or not local_only]


def _expand_prompt(kg: dict[str, Any], source: _Source, *,
                   window: str, local_only: bool) -> str:
    """1 資料ぶんの追加抽出プロンプト (裁定 AM)。

    要点は 3 つ: **対象を 1 資料に限る** / **既存ラベルを全部渡す** /
    **新しく足す概念だけを返させる**。全量を作り直させると同じ概念が別 id で
    返り、統合が「ほぼ全部が重複」になって 1 call ぶんが無駄になる。
    """
    labels = [str(n.get("label") or "") for n in kg.get("nodes") or ()]
    prompt = (
        f"概念地図の粒度を上げます (現在 {len(labels)} 概念)。"
        f"**資料「{source.name}」1 件だけ**を対象に、まだ挙がっていない概念と"
        f"関係を追加抽出してください (対象期間: {window})。\n\n"
        "すでに抽出済みの概念 (これらは出力に含めないでください):\n"
        + "\n".join(f"- {label}" for label in labels)
        + "\n\n追加してほしいもの: 手法の構成要素・実験条件・個別の結果・"
        "具体的な数値指標といった**下位概念**と、それらの関係 "
        "(新概念どうし・新概念と既存概念の両方)。\n"
        "出力は knowledge_graph JSON のみ (前置き・後置き・コードフェンス禁止)。\n"
        "- nodes には**新しく足す概念だけ**を入れる (既出の概念は入れない)。\n"
        "- edges の from / to には、この出力の新しい概念 id か、"
        "**既存概念のラベルをそのまま**書く。\n"
        "- 各エッジに evidence_span を配列で付け、surface に原文をそのまま入れる。\n"
        "**この資料に無いものを足さないでください** — 増やすのは粒度であって、"
        "資料に無いものを足すことではありません。新しい下位概念がこの資料から"
        '読み取れなければ、数を合わせずに {"nodes": [], "edges": []} を'
        "返して構いません。\n"
    )
    if source.text:
        prompt += (f"\n=== 資料「{source.name}」の本文 ===\n"
                   f"{source.text[:PER_DOC_CHARS]}")
    elif not local_only:
        prompt += (f"\n本文は添付していません。Work IQ ツールで"
                   f"**「{source.name}」だけ**を読み直し、その資料から抽出して"
                   "ください (ほかの資料は読まないでください)。\n")
    return prompt


def _expand_once(client: FoundryAgentsV2, kg: dict[str, Any], source: _Source, *,
                 window: str, local_only: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """資料 1 件ぶんの追加抽出 (1 call) と統合。戻りは (KG, per_document の 1 行)。

    **失敗しても次の資料へ進む** — 粒度の底上げは地図そのものの前提ではない
    ので、1 資料の事故で run を落とさない。落ちた事実は行に残す。
    """
    entry: dict[str, Any] = {"document": source.name, "source": source.kind,
                             "added_nodes": 0, "added_edges": 0,
                             "nodes_after": len(kg.get("nodes") or ())}
    try:
        fragment = extract_json(client.run(
            "cc-extraction",
            _expand_prompt(kg, source, window=window, local_only=local_only)))
        if not isinstance(fragment, dict):
            raise ValueError("追加抽出が JSON オブジェクトを返しませんでした")
        if not fragment.get("nodes") and not fragment.get("edges"):
            # 「この資料にはもう無い」は**正常な答え**。数合わせを禁じている
            # 以上、空を返してくるのは指示どおりの振る舞いで、失敗ではない。
            entry["note"] = "新規なし"
            return kg, entry
        merged, report = merge_extraction(kg, fragment)
    except Exception as exc:      # 追加抽出の事故で地図を失わせない
        logger.warning("expand skipped doc=%s: %s: %s",
                       source.name, type(exc).__name__, exc)
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return kg, entry

    entry["added_nodes"] = report.added_nodes
    entry["added_edges"] = report.added_edges
    entry["nodes_after"] = len(merged.get("nodes") or ())
    entry["merge"] = report.to_dict()
    logger.info("expand doc=%s (%s): +%d nodes +%d edges -> %d concepts",
                source.name, source.kind, report.added_nodes,
                report.added_edges, entry["nodes_after"])
    return merged, entry


def _expand_extraction(client: FoundryAgentsV2 | None, kg: dict[str, Any], *,
                       window: str, docs: list[Any],
                       local_only: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """目標に届くまで資料を 1 件ずつ回して追加抽出する (裁定 AM / AP)。

    戻り値は (KG, summary["extraction"])。**何をしたかを必ず記録する** —
    黙ってノードを増やすと、同じ依頼で数が変わった理由を後から説明できない。

    停止条件は 4 つ (どれで止まったかは `stopped_by` に残す):
      target     目標概念数に到達した (成功)
      dry        新規ゼロが 2 資料続いた = この資料束からはもう出ない
      max_calls  追加抽出の call 上限
      cap        CC_EXTRACT_MAX (統合後のノード上限)
    """
    target, max_calls, limit = detailed_target(), expand_max_calls(), extract_max()
    before = len(kg.get("nodes") or ())
    info: dict[str, Any] = {
        "mode": "llm", "before": before, "nodes": before, "target": target,
        "max_calls": max_calls, "rounds": 0, "calls": 0,
        "added_nodes": 0, "added_edges": 0, "expanded": False,
        "stopped_by": "target", "per_document": [], "documents": [],
    }
    if client is None:                        # offline はここへ来ない (保険)
        info["stopped_by"] = "no_client"
        return kg, info
    roster = _document_roster(kg, docs, local_only=local_only)
    info["documents"] = [f"{s.name} ({s.kind})" for s in roster]
    if before >= target:
        return kg, info                       # stopped_by = "target"
    if before >= limit:
        info["stopped_by"] = "cap"
        return kg, info
    if not roster:
        info["stopped_by"] = "no_documents"
        return kg, info
    if max_calls <= 0:
        info["stopped_by"] = "max_calls"
        return kg, info

    logger.info("expansion loop start: %d concepts < target %d, %d documents, "
                "budget %d calls", before, target, len(roster), max_calls)
    dry = 0
    stopped = ""
    round_no = 0
    while not stopped:
        round_no += 1
        for source in roster:                 # 同じ資料は 1 周につき 1 回まで
            if info["calls"] >= max_calls:
                stopped = "max_calls"
                break
            nodes_now = len(kg.get("nodes") or ())
            if nodes_now >= target:
                stopped = "target"
                break
            if nodes_now >= limit:
                stopped = "cap"
                break
            kg, entry = _expand_once(client, kg, source, window=window,
                                     local_only=local_only)
            entry["round"] = round_no
            info["per_document"].append(entry)
            info["calls"] += 1
            info["added_nodes"] += entry["added_nodes"]
            info["added_edges"] += entry["added_edges"]
            if entry["added_nodes"]:
                dry = 0
            else:
                dry += 1
                if dry >= DRY_LIMIT:
                    stopped = "dry"
                    break

    info["stopped_by"] = stopped
    info["rounds"] = max((e["round"] for e in info["per_document"]), default=0)
    info["nodes"] = len(kg.get("nodes") or ())
    info["expanded"] = info["nodes"] > before
    logger.info("expansion loop done: %d -> %d concepts in %d calls "
                "(%d rounds, stopped_by=%s)", before, info["nodes"],
                info["calls"], info["rounds"], stopped)
    return kg, info


# ------------------------------------------------ 正直な上限表示 (裁定 AO)

DETAIL_NOTE = "資料から抽出できる概念が上限です (水増ししていません)"

# 「これ以上は増やせない」と言い切ってよい停止理由。max_calls / cap は
# **予算を使い切っただけ**で、資料にはまだ概念が残っているかもしれない。
# そこで「上限」と書けば嘘になるので、黙る。
EXHAUSTED_STOPS = {"dry", "no_documents", "kg_file"}


def _detail_note(summary: dict[str, Any], plan: dict[str, Any]) -> str | None:
    """Standard と Detailed が同数で、かつ**もう増やせない**ときだけ注記を返す。

    水増ししない (創作禁止が上位原則) 以上、同数になること自体は正しい結果で、
    ユーザーに要るのは「これはバグではない」の一行。逆に予算切れで止まった
    ときは注記を出さない — 事実でないことを書かないのが裁定 AO の趣旨。
    """
    info = summary.get("extraction") or {}
    if info.get("stopped_by") not in EXHAUSTED_STOPS:
        return None
    levels = plan.get("levels") or {}
    standard = (levels.get("standard") or {}).get("nodes")
    detailed = (levels.get("detailed") or {}).get("nodes")
    if standard is None or detailed is None or standard != detailed:
        return None
    return DETAIL_NOTE


def _layers_stage(
    client: FoundryAgentsV2 | None,
    *,
    session: str,
    kg: dict[str, Any],
    docs: list[Any],
    kg_file: str | None,
    layers: bool,
    offline: bool,
    progress: ProgressFn | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """R2a の ①文分割 ②zone ③claims (設計書 §5/§6/§9)。

    戻り値は (layers サイドカー, summary["layers"])。status の語彙は 4 つ:

      generated       LLM を呼んで新規に作った
      reused          offline で元セッションのサイドカーを再利用した
      skipped_offline offline で再利用できるサイドカーが無かった
      disabled        layers=False (既定)

    **層を作らない場合も進捗は必ず発火させる** (瞬時完了扱い)。進捗
    チェックリストが途中で止まって見えると「固まった」と読まれるため。
    """
    def skip_progress() -> None:
        _notify(progress, "zone")
        _notify(progress, "claims")

    def store(doc: dict[str, Any]) -> str | None:
        """サイドカーを書く。**書けなくても地図の生成は続ける**。

        層は付加情報なので、ディスク側の事故で地図そのものを失わせない
        (書けなかった事実は summary に残る)。
        """
        try:
            return str(layers_store.save(session, doc))
        except OSError as exc:
            logger.warning("layers sidecar not saved: %s", type(exc).__name__)
            return None

    if not layers:
        skip_progress()
        return None, {"status": "disabled"}

    if offline or client is None:
        # offline は LLM を呼べない。元セッション (kg_file の名前から辿る) の
        # サイドカーがあれば再利用する — 層の情報は不変なので、同じ KG から
        # 作り直す必要がない (§9)。
        skip_progress()
        source = layers_store.session_of_kg_file(kg_file)
        if source and layers_store.exists(source):
            doc = layers_store.load(source)
            doc["session"] = session
            logger.info("layers reused from session=%s", source)
            return doc, {"status": "reused", "source_session": source,
                         "stats": doc.get("stats", {}),
                         # 新セッションでも自己完結させる (サイドカーを複製)
                         "saved": store(doc)}
        return None, {"status": "skipped_offline",
                      "reason": "offline 実行で再利用できる layers_session がない"}

    doc, report = analysis.analyze(
        lambda prompt: client.run(analysis.AGENT, prompt),
        session=session, kg=kg, docs=docs,
        progress=lambda key: _notify(progress, key))
    info: dict[str, Any] = {"status": "generated", "stats": doc.get("stats", {}),
                            "saved": store(doc)}
    info.update(report.to_dict())
    return doc, info


def _validation_stages(
    client: FoundryAgentsV2 | None,
    *,
    session: str,
    kg: dict[str, Any],
    layers_doc: dict[str, Any] | None,
    offline: bool,
    progress: ProgressFn | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """R2a の ⑤validate と ⑥rhetoric (設計書 §7/§6/§8(5))。

    戻り値は (summary["validation"], summary["rhetoric"], 走った検証器の ID)。
    status の語彙は _layers_stage と揃える:

      done            3 検証器を走らせた / 論証と矛盾を判定した
      skipped_offline offline (LLM を呼べない) — 再利用したサイドカーの
                      検証結果はそのまま残る
      disabled        layers=False、または層が作れなかった run

    **層を作らない run でも進捗は必ず発火**させる (_layers_stage と同じ理由)。
    layers_doc はその場で書き換わる (claims に validation、arguments/refutes
    を足す)。保存は呼び出し側がまとめて 1 回行う。
    """
    _notify(progress, "validate")
    _notify(progress, "rhetoric")
    if layers_doc is None:
        return {"status": "disabled"}, {"status": "disabled"}, []
    if offline or client is None:
        reason = "offline 実行では検証器 (別モデル) を呼べない"
        return ({"status": "skipped_offline", "reason": reason},
                {"status": "skipped_offline", "reason": reason}, [])

    run = lambda prompt: client.run("cc-verification", prompt)   # noqa: E731
    notes: list[str] = []
    model = MODELS["verification"]
    nli = verifiers.make_nli_verifier(run, model=model, notes=notes)
    checker = verifiers.OntologyChecker(
        (layers_doc.get("ontology") or {}).get("relations") or ())

    # ---- ⑤validate: 主張全件 + causes 候補エッジ (§7) ----
    edge_results, report = verifiers.run_validation(
        kg, layers_doc.get("claims") or [],
        zones=layers_doc.get("zones") or (), nli=nli,
        llm=verifiers.LLMClaimVerifier(run, model=model),
        ontology=checker, session=session)
    report.notes.extend(notes)
    validation_info: dict[str, Any] = {"status": "done"}
    validation_info.update(report.to_dict())
    validation_info["applied"] = layer_assign.apply_validation(
        kg, edge_results, claims=layers_doc.get("claims") or ())

    # ---- ⑥rhetoric: 論証と内部矛盾 (§6) ----
    arguments, refutes, rhetoric_report = analysis.analyze_rhetoric(
        lambda prompt: client.run(analysis.AGENT, prompt),
        claims=layers_doc.get("claims") or [],
        zones=layers_doc.get("zones") or ())
    layers_doc["arguments"] = arguments
    layers_doc["refutes"] = refutes
    rhetoric_info: dict[str, Any] = {"status": "done"}
    rhetoric_info.update(rhetoric_report.to_dict())
    # sentence_source は ①文分割 の記録。⑥rhetoric の summary では意味がない
    rhetoric_info.pop("sentence_source", None)
    # 矛盾の刻印は layer_assign 側で行う (層 D の刻印を 1 か所に集める)。
    # 対応するエッジが無ければサイドカーの記録だけが残る (エッジは作らない)。
    rhetoric_info["stamped"] = layer_assign.stamp_refutes(
        kg, refutes, layers_doc.get("claims") or ())

    # 検証段ぶんの LLM 呼び出しを stats へ積む (受け入れ基準 5 の実測値)
    stats = dict(layers_doc.get("stats") or {})
    layers_doc["stats"] = layers_store.compute_stats(
        layers_doc, sentences=int(stats.get("sentences") or 0),
        llm_calls=int(stats.get("llm_calls") or 0)
        + report.llm_calls + rhetoric_report.llm_calls)
    return validation_info, rhetoric_info, list(report.verifier_ids)


# ---------------------------------------- summary の永続化 + 再利用 (裁定 X)


def summary_path(session: str, graphs_dir: str | Path = GRAPHS_DIR) -> Path:
    return Path(graphs_dir) / f"{SUMMARY_PREFIX}{session}.json"


def save_summary(session: str, summary: dict[str, Any],
                 graphs_dir: str | Path = GRAPHS_DIR) -> str | None:
    """生成結果の summary を保存する (設計 §1)。**モードに依らず常時**。

    軽い JSON (KG も plan も含まない) なので毎回書いてよい。テストモードの
    ヒット時はこれをそのまま返す素材になり、通常実行でも「あのときの地図は
    どんな数字だったか」を後から引ける。**書けなくても生成は成功**扱い —
    地図そのものは既に graphs/ にあるので、控えが取れないことで失わせない。

    **原子的に書く** (tmp + replace)。毎回書かれるファイルであり、かつ
    `_replay_map` が「索引にあるなら地図もある」を担保する根拠でもあるので、
    途中で落ちて半端な JSON が残ると、再利用が黙って 1 回ぶん失われる。
    """
    path = summary_path(session, graphs_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("summary not saved: %s", type(exc).__name__)
        return None
    return str(path)


def load_summary(session: str,
                 graphs_dir: str | Path = GRAPHS_DIR) -> dict[str, Any] | None:
    path = summary_path(session, graphs_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("summary を読めません (%s)", type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None


def _replay_map(hit: test_cache.CacheHit, *, target: str,
                progress: ProgressFn | None) -> dict[str, Any] | None:
    """テストキャッシュのヒットから地図を復元する (**LLM ゼロ**・設計 §1)。

    素材 (summary / plan) が消えていたら None を返し、呼び出し側は通常実行へ
    落ちる。索引に残っているというだけで「無い地図」を返さないため。

    描画は target に依らず `--render` と**同じ決定的経路**を通す。local では
    それが canvas への描き直しそのもので、file では plan がいまも描ける形か
    どうかの確認になる (書き出し済みのファイルは前回のものを使う)。
    """
    session = hit.session
    if not session:
        return None
    summary = load_summary(session)
    if summary is None:
        logger.info("summary_session_%s.json が無いため再利用できません", session)
        return None

    # 進捗は全ステージを即座に完了させる。UI のチェックリストが途中で
    # 止まったままだと「固まった」と読まれるため (_layers_stage と同じ理由)。
    for key, _ in STAGES:
        _notify(progress, key)

    summary = dict(summary)
    summary["cache"] = hit.to_dict()
    level = str(summary.get("detail_level") or "standard")

    plan_file = Path((summary.get("layout") or {}).get("saved")
                     or Path(GRAPHS_DIR) / f"layout_plan_session_{session}.json")
    if not plan_file.exists():
        logger.info("plan が無いため再利用できません: %s", plan_file)
        return None

    summary["cache"]["render"] = _replay_render(plan_file, level, target)
    _record_tokens(summary, None, route_name="map", session=session, cached=True)
    logger.info("♻ テストキャッシュから地図を再利用 session=%s age=%d分",
                session, hit.age_min)
    return summary


# 再描画の結末。**表示の分岐はここで決め切る** — CLI と Web が生フィールドから
# それぞれ判定すると、同じ summary に違う文言が出る (実際 ok と reused_files の
# 見方が 2 画面でずれていた)。front-end は state で 1 回分岐するだけにする。
RENDER_REDRAWN = "redrawn"        # canvas へ描き直した
RENDER_REUSED = "reused_files"    # 書き出し済みファイルをそのまま使う
RENDER_FAILED = "failed"          # 描き直せなかった (再利用自体は成立)


def _replay_render(plan_file: Path, level: str, target: str) -> dict[str, Any]:
    """保存済み plan を描き直す (`--render` と同じ決定的経路)。

    local ではこれが canvas への描き直しそのもので、file では plan がいまも
    描ける形かどうかの確認になる (書き出し済みのファイルは前回のものを使う)。
    canvas が落ちていても**例外にしない** — 再利用そのものは成立しており、
    描けなかったことは state=failed として残せば読み手が区別できる。
    """
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        result = ToolExecutor(target=target).tool_render_layout_plan(
            {"plan": project(plan, level)})
    except Exception as exc:      # canvas 未起動・plan が壊れている等
        logger.warning("再描画に失敗: %s", type(exc).__name__)
        return {"state": RENDER_FAILED, "level": level, "target": target,
                "error": f"{type(exc).__name__}: {exc}"}
    if not result.get("success"):
        return {"state": RENDER_FAILED, "level": level, "target": target,
                "error": "; ".join(result.get("errors", []) or [])}
    return {"state": RENDER_REUSED if target == "file" else RENDER_REDRAWN,
            "level": level, "target": target,
            "elements": len(result.get("created", []) or [])}


def _replay_answer(hit: test_cache.CacheHit) -> dict[str, Any] | None:
    """QA / 雑談の答えを再利用する (設計 §1)。"""
    answer = hit.entry.get("answer")
    if not answer:
        return None
    summary: dict[str, Any] = {"status": "answered", "answer": answer,
                               "cache": hit.to_dict()}
    if hit.entry.get("sources") is not None:
        summary["sources"] = hit.entry["sources"]
    if hit.entry.get("qa") is not None:
        summary["qa"] = hit.entry["qa"]
    _record_tokens(summary, None, cached=True,
                   route_name=str((hit.entry.get("qa") or {}).get("route") or "qa"))
    logger.info("♻ テストキャッシュから答えを再利用 age=%d分", hit.age_min)
    return summary


def _answer_is_cacheable(summary: dict[str, Any], *, offline: bool) -> bool:
    """答えを索引へ登録してよいか。**LLM を実際に使った答えだけ**を入れる。

    材料不足 (`_no_material`) や offline の答えはもともと LLM 0 call なので、
    登録しても節約にならない。それどころか `--reindex` して材料を足した直後も
    TTL のあいだ古い「見つかりません」を返し続けることになる。
    """
    if offline or not summary.get("answer"):
        return False
    info = summary.get("qa")
    if info is None:            # basic / vector は依頼文を直接 LLM へ投げている
        return True
    return int(info.get("llm_calls") or 0) > 0


def _record_tokens(summary: dict[str, Any], client: FoundryAgentsV2 | None, *,
                   route_name: str, session: str | None = None,
                   cached: bool = False) -> None:
    """使用量を summary へ載せ、jsonl へ 1 行積む (裁定 Z)。

    client が無い実行 (offline / キャッシュ再利用) は**本当に 1 回も呼んで
    いない**ので 0 call。usage を持たないクライアントは実 API を叩かない
    テストの代役だけで、これも実費は 0 なので同じ扱いでよい。summary["tokens"]
    は必ず埋める — キーを省くと、表示側がどちらの語彙 (0 call / 不明) でも
    説明できない第 3 の状態ができてしまうため。
    """
    tokens = dict(getattr(client, "usage", None) or token_usage.blank())
    summary["tokens"] = tokens
    token_usage.append_log(route_name, tokens, session=session, cached=cached)


# ------------------------------------------------- 取込キャッシュ (裁定 Y)


def _ingest_stage(message: str, paths: list[str], *, local_only: bool,
                  use_cache: bool) -> tuple[list[Doc], str, dict[str, Any] | None]:
    """資料の取込。テストモードのときだけ結果を使い回す (設計 §2)。

    戻り値は (docs, 期間ラベル, キャッシュ情報 or None)。**通常モードでは
    ファイルを読みも書きもしない** — 索引が存在すること自体が本番の挙動を
    変えないようにするため。
    """
    if not use_cache:
        docs, window = ingest(message, paths)
        return docs, window, None

    _, window_label = parse_window(message)
    key = test_cache.ingest_key(window_label, paths=paths, local_only=local_only)
    found = test_cache.load_ingest(key)
    if found is not None:
        payload, age = found
        docs = [Doc(name=str(row.get("name") or ""),
                    source=str(row.get("source") or "local"),
                    modified=dt.datetime.fromisoformat(str(row["modified"])),
                    text=str(row.get("text") or ""))
                for row in payload.get("docs") or ()
                if row.get("modified")]
        note = f"♻ 資料は {age} 分前の取込を再利用 ({len(docs)} 件)"
        logger.info("%s", note)
        return (docs, str(payload.get("window") or window_label),
                {"hit": True, "age_min": age, "docs": len(docs), "note": note})

    docs, window = ingest(message, paths)
    test_cache.save_ingest(key, {
        "window": window,
        "docs": [{"name": d.name, "source": d.source,
                  "modified": d.modified.isoformat(), "text": d.text}
                 for d in docs]})
    # ミスは「再利用しなかった」= 通常実行と同じなので、何も足さない
    # (第 3 のキーを作らないほうが呼び出し側の分岐が 1 本で済む)。
    return docs, window, None


def run_pipeline(
    message: str,
    *,
    target: str = "local",
    paths: list[str] | None = None,
    kg_file: str | None = None,
    local_only: bool = False,
    detail_level: str | None = None,
    verify_causal: bool = True,
    export_svg: bool = True,
    progress: ProgressFn | None = None,
    offline: bool = False,
    learned: bool = True,
    layers: bool = True,
    test_cache_mode: bool = False,
) -> dict[str, Any]:
    """概念地図生成の全経路。

    progress: 各ステージ開始時に (key, 日本語ラベル) で呼ばれるフック。
    offline:  Foundry を一切呼ばない実行モード (Web の再描画・テスト用)。
              保存済み KG から詳細度計算以降だけを回すため**地図生成では
              kg_file が必須**。R2b の QA 経路 (local/global/hybrid) は保存済みの
              索引だけで答えられるので kg_file 無しでも通り、LLM の要る部分だけ
              劣化した形 (「LLM なし要約」「オンライン実行が必要」) で返る。
              LLM 抽出も因果の独立検証も無いので、結果は語彙証拠のみに基づく。
    learned:  過去の修正からの学習を適用するか (編集/学習設計書 §5.3)。
              False で ①抽出ヒント ②自動適用 ③因果上書き のすべてを止める。
              適用した場合は必ず summary["learned"] に内訳が出る (黙って直さない)。
    layers:   R2a の知識モデル多層化 (文分割 → zone → claims → 検証 → 論証) を
              走らせるか (R2a 設計書 §9)。**M7 で既定 True へフリップ済み**
              (CLI は `--no-layers`、Web は設定モーダルの「多層分析」で切れる)。
              True にすると ①資料を文へ切り ②cc-analysis で文脈ラベルと主張を
              取り ③層タグを刻み ⑤3 検証器で主張と因果候補を検証し ⑥論証と
              内部矛盾を判定して layers サイドカーを書く。offline では
              LLM を呼べないので、元セッションの
              サイドカーがあれば再利用し、無ければ層抽出を飛ばして完走する。
              ⑦meta (polarity 充填・provenance・投影・layer_model 刻印) はこの
              フラグに依らず常に走る — 層タグが無ければ投影は素通しなので、
              R1.5 と同じ地図が出る。
    test_cache_mode:
              テストモード (裁定 X)。**既定 OFF**。同じ依頼 (正規化した文言 +
              同じ設定) の結果が TTL 内に残っていれば、LLM を 1 回も呼ばずに
              前回の結果を返す。ヒットしたことは summary["cache"] に必ず出る
              (黙って再利用しない)。環境変数 CC_TEST_MODE=1 でも入る。
    """
    # ---- テストモード: 前回の結果を再利用できるか (裁定 X) ----
    # **FoundryAgentsV2 を作る前**に判定する。生成した時点で Azure 認証と
    # ensure_agents (ネットワーク) が走るので、「ヒット時は LLM ゼロ」を
    # 保証できる場所はここしかない。既定 OFF なので通常実行は素通りする
    # (索引ファイルすら作らない)。
    use_cache = test_cache.enabled(test_cache_mode)
    cache_key: str | None = None
    if use_cache:
        pre = route(message)
        cache_key = test_cache.make_key(
            message, level=detail_level or pre.detail_level or "standard",
            target=target, layers=layers, learned=learned,
            local_only=local_only, kg_file=kg_file, paths=paths)
        hit = test_cache.lookup(cache_key)
        if hit is not None:
            replayed = (_replay_map(hit, target=target, progress=progress)
                        if hit.kind == "map" else _replay_answer(hit))
            if replayed is not None:
                return replayed
            logger.info("再利用の素材が揃わないため通常実行します")

    # offline では FoundryAgentsV2 を生成しない。生成だけで Azure 認証と
    # エージェント確保 (ensure_agents) が走り、閉域・テストで失敗するため。
    client = None if offline else FoundryAgentsV2()
    if client is not None:
        ensure_agents(client)
    executor = ToolExecutor(target=target)
    session = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    summary: dict[str, Any] = {"session": session, "target": target}
    if offline:
        summary["offline"] = True

    # ---- ⓪ Query Routing (v3 §4.1 / 計画 §6) ----
    _notify(progress, "routing")
    decision: RouteDecision = route(message)
    summary["routing"] = decision.to_dict()
    level = detail_level or decision.detail_level or "standard"
    summary["detail_level"] = level

    handler = ROUTE_HANDLERS.get(decision.route)
    if handler is not None and not kg_file:
        # 地図生成でない要求にフルパイプラインを回さない (コスト・時間の一次緩和)。
        # QA 経路は answer に加えて sources (出典) と qa (内訳) を返す。
        summary.update(handler(message, client=client, offline=offline))
        summary["status"] = "answered"
        logger.info("routed to %s (no map generation)", decision.route)
        _record_tokens(summary, client, route_name=decision.route)
        if cache_key and _answer_is_cacheable(summary, offline=offline):
            test_cache.record(cache_key, "qa", message=message,
                              answer=summary.get("answer"),
                              sources=summary.get("sources"),
                              qa=summary.get("qa"))
        return summary

    # offline の kg_file 必須は**地図生成の話**。QA 経路は保存済みの索引だけで
    # (劣化した形で) 答えられるので、経路が決まってから判定する。
    if offline and not kg_file:
        raise ValueError("offline モードは kg_file が必須です (LLM 抽出を行わないため)")

    # ---- ①② Ingest + Extraction ----
    _notify(progress, "ingest")
    learned_store = load_learned() if learned else None
    # ⑦meta の provenance に載せる抽出元。kg_file 経由は抽出 LLM を通って
    # いないので、モデル名を書くと出所を偽ることになる (§9)。
    extractor_model = "kg_file"
    docs: list[Any] = []      # 文分割 (§5) の入力。kg_file 経由では手元に本文が無い
    if kg_file:
        kg, norm = normalize_kg(json.loads(Path(kg_file).read_text(encoding="utf-8")))
        summary["ingest"] = {"mode": "kg_file", "file": kg_file}
        if norm.repairs:
            summary["ingest"]["normalized"] = norm.to_dict()
        # 追加抽出ループ (裁定 AM) は**行わない** (裁定 AQ)。kg_file は保存済み
        # の抽出結果を読み直す経路で、資料そのものが手元に無いため追加抽出の
        # 根拠が無い。stopped_by に "kg_file" を置くのは、「これ以上は増やせ
        # ない」が事実だから — 裁定 AO の注記はこの状態でも正しい。
        summary["extraction"] = {"mode": "kg_file",
                                 "nodes": len(kg.get("nodes") or ()),
                                 "expanded": False, "calls": 0, "rounds": 0,
                                 "added_nodes": 0, "added_edges": 0,
                                 "stopped_by": "kg_file", "per_document": []}
        # 抽出済みの KG を読んだ時点で「概念抽出」は完了している (UI の
        # チェックリストが途中で止まって見えないよう、ここで通知する)
        _notify(progress, "extract")
    else:
        docs, window, ingest_cache = _ingest_stage(
            message, paths or [], local_only=local_only, use_cache=use_cache)
        summary["ingest"] = {
            "window": window,
            "local_files": [{"name": d.name, "modified": d.modified.strftime("%Y-%m-%d")}
                            for d in docs],
            "workiq": "disabled" if local_only else "enabled",
        }
        if ingest_cache is not None:
            summary["ingest"]["cache"] = ingest_cache
        lang_note = ""
        if decision.language == "en":
            lang_note = "\nラベルは英語で出力してください。"
        elif decision.language == "ja":
            lang_note = "\nラベルは日本語で出力してください。"
        tag_note = (f"\n対象を次のタグに絞ってください: {', '.join(decision.tags)}"
                    if decision.tags else "")

        def _build_prompt(use_workiq: bool) -> str:
            prompt = (
                f"依頼: {message}\n"
                f"今日は {dt.datetime.now():%Y-%m-%d (%a)} です。対象期間: {window}。"
                f"{lang_note}{tag_note}\n"
            )
            if not use_workiq:
                prompt += "\nWork IQ ツールは使わず、以下の添付資料のみから抽出してください。\n"
            else:
                prompt += ("\nWork IQ ツールで OneDrive / SharePoint から対象期間の研究資料を"
                           "収集し、下の添付資料と併せて knowledge_graph を抽出してください。\n")
            return prompt

        prompt = _build_prompt(use_workiq=not local_only)
        prompt += (
            "\n重要: 各エッジには evidence_span を **配列** で付けてください。\n"
            '  "evidence_span": [{"document_id": "<ファイルID>", '
            '"surface": "<原文のままの引用>"}]\n'
            "surface は要約せず原文のまま入れてください (後段で因果の語彙証拠を"
            "検査するため)。文字位置が分かる場合のみ char_start / char_end を"
            "整数で 追加してください。分からなければ省略して構いません。\n"
        )
        # フック 1: 過去の修正からの注意を抽出プロンプト末尾に足す (§5.3)。
        # エージェント定義 (agents_def) は変えない — バージョンを増殖させず、
        # 実行ごとに最新のヒントを使うため。
        hints = build_prompt_hints(learned_store)
        if hints:
            prompt += hints
            summary["learned_hints"] = hints.count("\n- ")

        if docs:
            prompt += f"\n=== 添付資料 ({len(docs)} 件) ===\n{bundle(docs)[:LOCAL_BUDGET]}"
        else:
            prompt += "\n(ローカル添付資料はありません)"

        # プロンプト尾部 (evidence 指示・学習ヒント・添付) は上で prompt に付加済み。
        # フォールバック用に「尾部だけ」を切り出しておく (先頭の依頼部と差し替えるため)
        _tail = prompt[len(_build_prompt(use_workiq=not local_only)):]

        logger.info("extraction start local_docs=%d workiq=%s", len(docs), not local_only)
        _notify(progress, "extract")
        try:
            kg = extract_json(client.run("cc-extraction", prompt))
        except RuntimeError as exc:
            # Work IQ (Remote MCP) は Foundry 側 HttpClient の 100 秒制限で
            # TaskCanceledException になる【実測 2026-08-07。foundry_v2.run が
            # 3 回試して全滅した場合にここへ来る】。広い検索は毎回 100 秒を
            # 超えることがあり、再試行では救えない。ローカル資料があるなら
            # Work IQ 抜きで 1 回だけ作り直す (全滅よりは狭い地図を返す)。
            msg = str(exc)
            workiq_timeout = any(mark in msg for mark in (
                "TaskCanceled", "HttpClient.Timeout",
                "did not complete the request"))
            if not workiq_timeout or local_only:
                raise
            if not docs:
                raise RuntimeError(
                    "Work IQ (M365 読み取り) が 100 秒制限に繰り返しかかり、"
                    "応答を得られませんでした。時間を置いて再試行するか、"
                    "資料を inbox/ に置いてローカルのみで再実行してください。"
                    f" (詳細: {msg[:200]})") from exc
            logger.warning("Work IQ timed out repeatedly; falling back to local-only")
            _notify(progress, "extract")
            kg = extract_json(client.run(
                "cc-extraction", _build_prompt(use_workiq=False) + _tail))
            summary["ingest"]["workiq"] = "timeout_fallback"
            summary["ingest"]["note"] = (
                "⚠ Work IQ (M365) が時間内に応答しなかったため、"
                "ローカル資料のみで生成しました。M365 の資料を含めるには"
                "時間を置いて再実行してください。")
        extractor_model = MODELS["extraction"]
        if kg.get("error") == "no_documents":
            summary["status"] = "no_documents"
            summary["hint"] = (
                f"対象期間の研究資料が見つかりません ({kg.get('detail', '')})。"
                "inbox/ に資料を置くか --path で指定してください。")
            return summary
        if not kg.get("nodes"):
            raise RuntimeError(f"extraction returned no nodes: {str(kg)[:200]}")

        # LLM 出力は指示どおりの形とは限らない。契約形へ正規化してから先へ渡す
        # (実測: evidence_span を単一オブジェクトで返す / char offset が null)
        kg, norm = normalize_kg(kg)
        if norm.repairs or norm.warnings:
            summary["normalized"] = norm.to_dict()
            logger.info("kg normalized: %s", norm.repairs)

        # ---- 裁定 AM: 目標に届くまで資料を 1 件ずつ回して追加抽出 ----
        # online の map 経路だけ (裁定 AQ)。offline は LLM を呼べず、kg_file は
        # 資料が手元に無いので、どちらもここへ来ない。進捗は "extract" のまま
        # にする — ユーザーから見れば追加抽出も概念抽出の続きで、段を増やすと
        # 「同じ段が 2 回出る」ように見えるだけ。
        kg, summary["extraction"] = _expand_extraction(
            client, kg, window=window, docs=docs, local_only=local_only)

    # ---- フック 2: 学習の自動適用 (§5.3) ----
    # 改名辞書・除外リストを当て、因果上書きの印を付ける。**必ず内訳を返す**
    # ので、何を機械が直したかは常に summary から追える (黙って直さない)。
    kg, learned_report = apply_learned(kg, learned_store, enabled=bool(learned))
    summary["learned"] = learned_report

    # ---- R2a: 文脈ラベル付け + 主張の抽出 (設計書 §5/§6/§9) ----
    # 層タグの刻印は ④relate の**後**に行う (降格後の glyph を見るため)。
    # ここでは LLM 呼び出しと layers サイドカーの用意だけを済ませる。
    layers_doc, summary["layers"] = _layers_stage(
        client, session=session, kg=kg, docs=docs, kg_file=kg_file,
        layers=layers, offline=offline, progress=progress)

    # ---- ④ Relate: 因果3点セット + 矛盾の非断定化 (裁定 7) ----
    # フック 3: causal_override が付いた対は 3 点セットを走らせず確定させる
    # (apply_relation_policy が edge["causal_override"] を見る)。
    # offline は独立検証器 (別モデル判定) を持てないため verifier=None。
    # 3 点セットの 3 点目が欠けるので、通る因果は語彙証拠のみの根拠になる。
    _notify(progress, "relate")
    verifier = _causal_verifier(client) if (verify_causal and client) else None
    kg, causal_stats = apply_relation_policy(kg, verifier=verifier)
    # 検証器の契約違反を「本物の否定」と区別できる形にする (降格の結論は維持)
    verifier_errors = _mark_verifier_errors(kg)
    if verifier_errors:
        causal_stats["verifier_errors"] = verifier_errors
    summary["relation_policy"] = causal_stats
    # provenance.validator_ids は**実際に走った**検証器だけを並べる (§9)。
    # offline / verify_causal=False では空 = 「何も検証していない」が正しい記録。
    validator_ids = ([verifier_id(MODELS["verification"])]
                     if verifier is not None else [])

    # ---- ⑦meta: 決定的なメタ情報の書き込み (R2a 設計書 §9) ----
    # 独立した STAGE にはしない — LLM 呼び出しが無く、進捗に出す意味がない。
    # polarity 充填 / provenance / 層タグ→glyph 投影 / layer_model 刻印 を
    # **KG 保存の直前**に 1 回だけ行う。層タグがまだ無い世代 (R1.5 と
    # layers=False の run) では投影は素通しなので、glyph も座標も変わらない。
    # 層タグの刻印 (§8 の (1)(3)(4)) は ④relate の後・⑦meta の前。降格後の
    # glyph を初期タグにするので、LLM が何も足さなければ記号は動かない。
    if layers_doc is not None:
        summary["layers"]["assigned"] = assign_layer_tags(
            kg, zones=layers_doc.get("zones", ()),
            claims=layers_doc.get("claims", ()),
            ontology=layers_doc.get("ontology"))

    # ---- ⑤validate + ⑥rhetoric (設計書 §7/§6) ----
    # 層タグを刻んだ**後**に置く: causes 候補の判定を ④relate 後の glyph で
    # 揃えるため。ここで書いた edge["validation"] を ⑦meta の投影が読み、
    # 裏付けの足りない causes 候補は矢印にならない (規則④/⑩)。
    summary["validation"], summary["rhetoric"], checked_ids = _validation_stages(
        client, session=session, kg=kg, layers_doc=layers_doc,
        offline=offline, progress=progress)
    for vid in checked_ids:                  # 実際に走った検証器だけを並べる
        if vid not in validator_ids:
            validator_ids.append(vid)
    if layers_doc is not None and summary["layers"].get("saved"):
        # 検証結果と論証をサイドカーへ書き戻す (生成時 1 回書きの原則は保つ —
        # 同じ run の中での確定であって、後から書き換えているわけではない)
        try:
            summary["layers"]["saved"] = str(
                layers_store.save(session, layers_doc))
            summary["layers"]["stats"] = layers_doc.get("stats", {})
        except OSError as exc:
            logger.warning("layers sidecar not updated: %s", type(exc).__name__)

    summary["meta"] = apply_meta(kg, extractor_model=extractor_model,
                                 validator_ids=validator_ids)

    # 因果として維持された語彙証拠を数える (§5.1 cue_stats)。R1 は記録のみで、
    # 閾値を超えた語彙の扱いは人が判断する (§12)。
    # **⑦meta の後**に置く (裁定 H)。投影が終わった後の glyph で数えるので、
    # 層タグ経由で降格した causes 候補の語彙が「因果として維持された」に
    # 混ざらない。layers=False の run では投影が素通しなので集計は変わらない。
    if learned:
        try:
            note_cues_kept([hit for e in kg.get("edges", [])
                            if e.get("glyph") == CAUSAL_GLYPH
                            for hit in (e.get("causal_check") or {}).get("lexicon_hit", [])])
        except OSError as exc:  # 統計が書けなくても生成は続ける
            logger.warning("cue_stats not recorded: %s", type(exc).__name__)

    # ---- 原本 KG の保存 (編集の base) ----
    # **関係ポリシー適用後**を保存するのが要点。ここが利用者に見えている状態で
    # あり、編集はこの上に積まれる。ポリシー適用**前**を base にすると、
    # 1 か所の編集で rebuild したときに降格済みの相関が生の因果矢印へ戻り、
    # 3 点セット (裁定 7) が黙って無効化されてしまう。
    # kg_file 経由でもセッション固有の原本を残す — 原本が無いセッションは
    # cc_core.editing が「原本 + 追記ログ」で再構成できず、編集できないため。
    kg_path = Path("graphs") / f"kg_session_{session}.json"
    kg_path.parent.mkdir(exist_ok=True)
    kg_path.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["knowledge_graph"] = {
        "nodes": len(kg["nodes"]), "edges": len(kg.get("edges", [])),
        "communities": len(kg.get("communities", [])),
        "source_files": kg.get("source_files", []),
        "saved": str(kg_path),
    }

    # ---- 可変詳細度: 3 レベル同梱を 1 回で生成 (§4) ----
    _notify(progress, "detail")
    plan = build_multilevel_plan(kg, default_level=level,
                                 language=decision.language)
    band_problems = check_level_bands(plan)
    if band_problems:
        logger.warning("detail level band problems: %s", band_problems)
    summary["levels"] = plan["levels"]
    summary["band_check"] = band_problems or "OK"
    # レイアウト v3 §6: どのエンジンで組んだか / 島がグリッドへ退避した件数。
    # 退避は「読めない図」の予兆なので summary に必ず残す (CLI も 1 行出す)。
    summary["layout"] = layout_summary(plan)

    # ---- 裁定 AO: 正直な上限表示 ----
    # plan にも載せる (Web は plan から作った view しか見ない)。
    note = _detail_note(summary, plan)
    if note:
        summary["detail_note"] = plan["detail_note"] = note
        logger.info("detail_note: %s", note)

    # ---- ギャップ候補 (裁定 8) ----
    _notify(progress, "gaps")
    # rejection_log は「なぜ矢印にならなかったか」の原文。因果ギャップの出典に
    # 添えるだけで、検出そのものは kg 内の validation から決まる (§9)。
    gap_list = detect_gaps(
        kg, rejection_log=(summary.get("validation") or {}).get("rejection_log"))
    plan["gaps"] = [g.to_dict() for g in gap_list]
    summary["gaps"] = {
        "candidates": len(gap_list),
        "by_type": {t: sum(1 for g in gap_list if g.presumed_type == t)
                    for t in GAP_TYPES},
        "by_gap_type": {k: sum(1 for g in gap_list if g.gap_type == k)
                        for k in GAP_KINDS},
    }

    # ---- 可読性: 実際に描かれる位置での重なり検査 (レイアウト重なり設計書 裁定 AC) ----
    # ラベルは cc_core.overlap の一括プランナーが逃がすが、逃げ場が無いことも
    # ある。黙って重ねるとユーザーには「壊れた図」としか見えないので、
    # plan と summary の両方に残す。
    overlap_levels: dict[str, dict[str, int]] = {}
    unresolved: list[dict[str, Any]] = []
    for lvl in plan.get("levels", {}) or {level: None}:
        report = check_overlaps(project(plan, lvl))
        overlap_levels[lvl] = {
            "label_on_node": len(report.label_on_node),
            "label_on_label": len(report.label_on_label),
        }
        for item in report.unresolved_labels:
            unresolved.append({"level": lvl, **item})
    if unresolved:
        plan["unresolved_labels"] = unresolved
        logger.warning("unresolved edge labels: %s",
                       [u["edge"] for u in unresolved][:10])
    summary["overlaps"] = {
        "clean": not unresolved and all(
            v["label_on_node"] == 0 and v["label_on_label"] == 0
            for v in overlap_levels.values()),
        "by_level": overlap_levels,
        "unresolved_labels": unresolved,
    }

    check = validate_layout_plan(plan)
    if not check.valid:
        raise RuntimeError(f"layout plan invalid: {check.errors[:3]}")
    plan_path = Path("graphs") / f"layout_plan_session_{session}.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    summary.setdefault("layout", {})["saved"] = str(plan_path)

    # ---- ⑧ Project: 既定レベルを描画 + 検証 (FAIL 時 1 回再試行) ----
    _notify(progress, "render")
    view = project(plan, level)
    # 描画・検証の対象はここで確定させる。エージェント経路では LLM が plan を
    # 復唱してツールへ渡すが、復唱は静かに壊れる【実測: 島の欠落で
    # RENDER_FAILED】ため、ツール側はこの確定 plan だけを使う。
    #
    # 確定させた以上、**プロンプトに plan 本文を載せる理由が無い** (裁定 W)。
    # 載せると往路 (プロンプト) と復路 (ツール引数への復唱) で同じ JSON を
    # 2 回課金することになり、それが再試行のぶんだけ積み上がる。エージェントに
    # 要るのは「ツールを呼べ」という指示だけで、何を描くかはツール側が知っている。
    executor.authoritative_plan = view
    verdict: dict[str, Any] = {}

    def _render_verify_direct(ex: ToolExecutor) -> dict[str, Any]:
        """エージェントを介さず実行系を直接叩く。往復が無いので再試行も不要。

        offline と、ライブキャンバスからの file フォールバック (計画 C/D) が
        共有する経路。どちらも「LLM に描かせない」点で同じ。
        """
        render_status = ex("render_layout_plan", {"plan": view})
        if not render_status.get("success"):
            raise RuntimeError(f"projection failed: {render_status.get('errors')}")
        summary["projection"] = {
            "status": "RENDER_OK",
            "created": len(render_status.get("created", [])),
            "mode": render_status.get("mode", ex.target),
        }
        _notify(progress, "verify")
        report = ex("verify_scene", {})
        result = {
            "verdict": "PASS" if report.get("passed") else "FAIL",
            "summary": (f"要素 {report.get('canvas_element_count', 0)} / 期待 "
                        f"{report.get('expected_element_count', 0)}"
                        f" (欠落 {len(report.get('missing_elements', []))} / "
                        f"ラベル不一致 {len(report.get('label_mismatches', []))})"),
        }
        summary["verification"] = result
        return result

    def _fallback_to_file(note: str) -> dict[str, Any]:
        """ライブキャンバスへ描けないときの逃げ道 (計画 C/D)。

        target=file の ToolExecutor を新たに立て、MCP を一切使わずに描画・検証・
        書き出しを済ませる。以降の export もこの executor を使うため、ライブ
        ゲートウェイに触れないまま完走する。
        """
        nonlocal executor
        logger.warning("render fallback to file: %s", note)
        executor = ToolExecutor(target="file")
        executor.authoritative_plan = view
        summary["render_fallback"] = True
        summary["render_note"] = note
        _notify(progress, "render")
        return _render_verify_direct(executor)

    if offline:
        verdict = _render_verify_direct(executor)
    elif target == "local" and not gateway_healthy(timeout=3.0):
        # プリフライト: 描きに行く前に 3 秒でゲートウェイの生死を見る。
        # 落ちていると分かっているものに向かってエージェントを走らせない。
        verdict = _fallback_to_file(GATEWAY_DOWN_NOTE)
    else:
        deadline = time.monotonic() + _render_deadline_s()

        def _check_deadline() -> None:
            if time.monotonic() > deadline:
                raise _RenderDeadlineExceeded()

        try:
            for attempt in (1, 2):
                _check_deadline()
                render_status = extract_json(client.run(
                    "cc-projection", RENDER_PROMPT, tool_executor=executor))
                summary["projection"] = render_status
                if render_status.get("status") != "RENDER_OK":
                    raise RuntimeError(f"projection failed: {render_status}")

                _notify(progress, "verify")
                _check_deadline()
                verdict = extract_json(client.run(
                    "cc-verification", VERIFY_PROMPT, tool_executor=executor))
                summary["verification"] = verdict
                if verdict.get("verdict") == "PASS":
                    break
                logger.warning("verification FAIL (attempt %d)", attempt)
        except _RenderDeadlineExceeded:
            # 最後の網。プリフライトをすり抜けた半死のゲートウェイでも、
            # ここで必ず打ち切ってファイル生成へ倒す (数時間固まらせない)。
            verdict = _fallback_to_file(RENDER_DEADLINE_NOTE)

    # ---- 出力 ----
    _notify(progress, "export")
    if offline and target == "file":
        # file 経路の offline はローカルキャンバスへ描いていない。live canvas を
        # export すると別セッションの内容を書き出してしまうため plan から直接作る。
        from cc_core.excalidraw_file import write_scene
        Path("exports").mkdir(parents=True, exist_ok=True)
        summary["export"] = {"excalidraw": write_scene(
            view, f"exports/session_{session}.excalidraw")}
    else:
        summary["export"] = {"excalidraw": executor.export_excalidraw(
            f"exports/session_{session}.excalidraw")}
    if export_svg:
        svgs = {}
        for lv in ("overview", "standard", "detailed"):
            svgs[lv] = str(write_svg(project(plan, lv),
                                     f"exports/session_{session}_{lv}.svg"))
        summary["export"]["svg"] = svgs

    summary["kpi"] = summarize(view, [])
    summary["status"] = "success" if verdict.get("verdict") == "PASS" else "verify_failed"
    summary["view"] = {"local_canvas": "http://127.0.0.1:3000"}
    _record_tokens(summary, client, route_name="map", session=session)

    # summary の永続化は**常時** (設計 §1)。テストモードの素材であると同時に、
    # 「あのときの地図の数字」を後から引ける控えでもある。
    save_summary(session, summary)
    if cache_key and summary["status"] == "success":
        # 検証まで通った実行だけを登録する。FAIL の地図を再利用させると、
        # 直したはずの不具合が「再利用」で復活して見える (設計 §1「ミス時」)。
        test_cache.record(cache_key, "map", message=message, session=session)
    return summary


def switch_level(plan_path: str, level: str) -> dict[str, Any]:
    """保存済み plan の詳細度を切り替える (LLM 呼び出しゼロ・再レイアウトなし)。

    v3 §2.4 の「切替は再生成を伴わずクライアント側で完結」に対応する入口。
    """
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    view = project(plan, level)
    return view
