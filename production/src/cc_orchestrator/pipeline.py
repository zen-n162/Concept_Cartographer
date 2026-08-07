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
from pathlib import Path
from typing import Any, Callable

from cc_core import layer_assign, layers_store, test_cache, token_usage, verifiers
from cc_core.causal import apply_relation_policy
from cc_core.detail import build_multilevel_plan, check_level_bands, project
from cc_core.evaluation import summarize
from cc_core.gaps import GAP_KINDS, GAP_TYPES, detect_gaps
from cc_core.layer_assign import assign_layer_tags
from cc_core.layers import CAUSAL_GLYPH, apply_meta, verifier_id
from cc_core.learning import (
    apply_learned,
    build_prompt_hints,
    load_learned,
    note_cues_kept,
)
from cc_core.logging_util import get_logger
from cc_core.mcp_client import extract_json
from cc_core.normalize import normalize_kg
from cc_core.overlap import check_overlaps
from cc_core.svg_export import write_svg
from cc_core.validate import validate_layout_plan
from cc_orchestrator import analysis, qa
from cc_orchestrator.agents_def import AGENT_SPECS, MODELS
from cc_orchestrator.foundry_v2 import FoundryAgentsV2
from cc_orchestrator.ingest import Doc, bundle, ingest, parse_window
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

        prompt = (
            f"依頼: {message}\n"
            f"今日は {dt.datetime.now():%Y-%m-%d (%a)} です。対象期間: {window}。"
            f"{lang_note}{tag_note}\n"
        )
        if local_only:
            prompt += "\nWork IQ ツールは使わず、以下の添付資料のみから抽出してください。\n"
        else:
            prompt += ("\nWork IQ ツールで OneDrive / SharePoint から対象期間の研究資料を"
                       "収集し、下の添付資料と併せて knowledge_graph を抽出してください。\n")
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

        logger.info("extraction start local_docs=%d workiq=%s", len(docs), not local_only)
        _notify(progress, "extract")
        kg = extract_json(client.run("cc-extraction", prompt))
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
    summary["layout"] = {"saved": str(plan_path)}

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
    if offline:
        # エージェントを介さず実行系を直接叩く。往復が無いので再試行も不要。
        render_status = executor("render_layout_plan", {"plan": view})
        if not render_status.get("success"):
            raise RuntimeError(f"projection failed: {render_status.get('errors')}")
        summary["projection"] = {
            "status": "RENDER_OK",
            "created": len(render_status.get("created", [])),
            "mode": render_status.get("mode", target),
        }
        _notify(progress, "verify")
        report = executor("verify_scene", {})
        verdict = {
            "verdict": "PASS" if report.get("passed") else "FAIL",
            "summary": (f"要素 {report.get('canvas_element_count', 0)} / 期待 "
                        f"{report.get('expected_element_count', 0)}"
                        f" (欠落 {len(report.get('missing_elements', []))} / "
                        f"ラベル不一致 {len(report.get('label_mismatches', []))})"),
        }
        summary["verification"] = verdict
    else:
        for attempt in (1, 2):
            render_status = extract_json(client.run(
                "cc-projection", RENDER_PROMPT, tool_executor=executor))
            summary["projection"] = render_status
            if render_status.get("status") != "RENDER_OK":
                raise RuntimeError(f"projection failed: {render_status}")

            _notify(progress, "verify")
            verdict = extract_json(client.run(
                "cc-verification", VERIFY_PROMPT, tool_executor=executor))
            summary["verification"] = verdict
            if verdict.get("verdict") == "PASS":
                break
            logger.warning("verification FAIL (attempt %d)", attempt)

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
