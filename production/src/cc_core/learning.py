"""編集フィードバックからの「学習」 (編集/学習設計書 §5)。

**「学習」の正直な定義** (§1): モデルの重みは変えない。実体は 3 機構だけである。

  (a) 決定的な自動適用 — 用語辞書 (改名) / 除外リスト / 因果の上書き
  (b) 抽出プロンプトへの少数事例注入 — 「過去にこう直された」を注意書きで渡す
  (c) 因果検証語彙の統計調整 — cue ごとの kept / downgraded_by_user を記録する

UI・文書でもこの言葉で説明する (誇張しない)。そして **黙って直さない**:
自動適用は必ず実行サマリに件数と内訳を出し、`--no-learned` / Web の設定トグルで
無効化できる。cc_core.normalize と同じ思想である。

照合キーは**正規化ラベル** (NFKC + trim + casefold)。id はセッションローカルなので
セッションを跨ぐ学習には使えない。`logs/feedback/learned.json` はあくまで
**キャッシュ**であり、`relearn()` で全セッションの編集ログから再構成できる
(整合性の最終根拠は常に編集ログ)。
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

from cc_core.causal import _edge_text, find_causal_cues
from cc_core.layers import CAUSAL_GLYPH
from cc_core.editing import (
    GRAPHS_DIR,
    effective_edits,
    list_edited_sessions,
    load_edits,
    normalize_label,
)
from cc_core.logging_util import get_logger

logger = get_logger("cc_core.learning")

LEARNED_PATH = "logs/feedback/learned.json"
STORE_VERSION = 1

# 自動適用の閾値 (保守的に。§5.2)
STOPLIST_AUTO_MIN = 2      # 同一ラベルの削除が 2 回以上で機械適用
FEW_SHOT_MAX = 40          # プロンプトヒントの母集団としてこの件数まで保持
HINTS_MAX_CHARS = 600      # 抽出プロンプトへ足すヒントの全体上限 (§5.3)

DECISIONS = ("allow", "deny", "reverse")


def empty_store() -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "scope": "personal",   # R2 のチーム学習は consent 前提 (計画 §11)
        "updated_at": None,
        "lexicon": [],
        "stoplist": [],
        "causal_overrides": [],
        "cue_stats": {},
        "few_shot": [],
    }


# ------------------------------------------------------------ 入出力


def load_learned(path: str | Path = LEARNED_PATH) -> dict[str, Any]:
    """学習ストアを読む。壊れていても空ストアで動き続ける (生成を止めない)。"""
    p = Path(path)
    if not p.exists():
        return empty_store()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("learned.json unreadable (%s); using empty store",
                       type(exc).__name__)
        return empty_store()
    if not isinstance(data, dict):
        return empty_store()
    store = empty_store()
    store.update({k: v for k, v in data.items() if k in store})
    return store


def save_learned(learned: dict[str, Any],
                 path: str | Path = LEARNED_PATH) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 呼び出し元が受け取ったストアと保存内容がずれないよう、その場で更新する
    learned["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(learned, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ------------------------------------------------------------ 導出 (§5.2)


def _label_of(before: Any) -> str:
    if isinstance(before, dict):
        return str(before.get("label") or "")
    return ""


def _cues_of(edge: dict[str, Any]) -> list[str]:
    """降格されたエッジが根拠にしていた causal cue の語を取り出す。

    causal_check.lexicon_hit は "category:語" の形なので語だけを取る。
    記録が無い場合は根拠テキストから引き直す。
    """
    hits = ((edge.get("causal_check") or {}).get("lexicon_hit")
            or find_causal_cues(_edge_text(edge)))
    words = []
    for hit in hits:
        word = str(hit).split(":", 1)[-1].strip()
        if word:
            words.append(word)
    return sorted(set(words))


def _date_of(edit: dict[str, Any]) -> str:
    return str(edit.get("ts") or "")[:10]


def derive_learned(sessions: dict[str, list[dict[str, Any]]], *,
                   base: dict[str, Any] | None = None) -> dict[str, Any]:
    """全セッションの編集ログから学習ストアを組み立てる (§5.2 の規則表)。

    base を渡すと、編集ログからは復元できない統計 (cue_stats の kept は生成側で
    数えるもの) を引き継ぐ。編集由来のエントリはすべて作り直す。
    """
    store = empty_store()
    if base:
        store["scope"] = base.get("scope", "personal")

    # --- 収集 ---
    renames: dict[str, dict[str, dict[str, Any]]] = {}   # norm(from) -> norm(to) -> entry
    stops: dict[str, dict[str, Any]] = {}                # norm(label) -> entry
    overrides: dict[tuple[str, str], dict[str, Any]] = {}
    cue_down: dict[str, int] = {}
    few_shot: list[dict[str, Any]] = []

    def add_few_shot(kind: str, text: str, ts: str) -> None:
        few_shot.append({"kind": kind, "text": text, "ts": ts})

    def set_override(a: str, b: str, decision: str, source: str,
                     session: str, ts: str, n: int = 1) -> None:
        key = (normalize_label(a), normalize_label(b))
        if not key[0] or not key[1]:
            return
        prev = overrides.get(key)
        overrides[key] = {
            "from_label": a, "to_label": b, "decision": decision,
            "source": source, "session": session, "ts": ts,
            "n": (prev.get("n", 0) if prev else 0) + n,
        }

    for session in sorted(sessions):
        for edit in effective_edits(sessions[session]):
            op = str(edit.get("op") or "")
            payload = edit.get("payload") or {}
            before = edit.get("before") or {}
            ts = str(edit.get("ts") or "")

            if op == "rename_node":
                src, dst = _label_of(before), str(payload.get("label") or "")
                if not src or not dst or normalize_label(src) == normalize_label(dst):
                    continue
                bucket = renames.setdefault(normalize_label(src), {})
                entry = bucket.setdefault(normalize_label(dst), {
                    "from": src, "to": dst, "n": 0, "sessions": [], "ts": ts})
                entry["n"] += 1
                entry["ts"] = ts
                if session not in entry["sessions"]:
                    entry["sessions"].append(session)

            elif op == "delete_node":
                label = _label_of((before or {}).get("node", before))
                if not label:
                    continue
                entry = stops.setdefault(normalize_label(label), {
                    "label": label, "n": 0, "sessions": [], "ts": ts})
                entry["n"] += 1
                entry["ts"] = ts
                if session not in entry["sessions"]:
                    entry["sessions"].append(session)

            elif op == "retype_edge":
                old_glyph = str(before.get("glyph") or "")
                new_glyph = str(payload.get("glyph") or "")
                a, b = before.get("from_label"), before.get("to_label")
                if not a or not b:
                    continue
                # 記号が 10 種に増えても「因果へ / 因果から」の判定は
                # CAUSAL_GLYPH との異同だけで正しい (deny/allow の意味は不変)。
                if old_glyph == CAUSAL_GLYPH and new_glyph != CAUSAL_GLYPH:
                    set_override(a, b, "deny", "user_retype", session, ts)
                    for cue in _cues_of(before):
                        cue_down[cue] = cue_down.get(cue, 0) + 1
                elif new_glyph == CAUSAL_GLYPH and old_glyph != CAUSAL_GLYPH:
                    set_override(a, b, "allow", "user_retype", session, ts)

            elif op == "reverse_edge":
                a, b = before.get("from_label"), before.get("to_label")
                if a and b and str(before.get("glyph") or "") == CAUSAL_GLYPH:
                    set_override(a, b, "reverse", "user_reverse", session, ts)

            elif op == "delete_edge":
                a, b = before.get("from_label"), before.get("to_label")
                if not a or not b:
                    continue
                if str(before.get("glyph") or "") == CAUSAL_GLYPH:
                    set_override(a, b, "deny", "user_delete", session, ts)
                    for cue in _cues_of(before):
                        cue_down[cue] = cue_down.get(cue, 0) + 1
                add_few_shot("wrong_edge",
                             f"「{a}」と「{b}」の関係は誤りとして削除されました"
                             f" ({_date_of(edit)})", ts)

            elif op == "add_edge":
                a = before.get("from_label") or payload.get("from")
                b = before.get("to_label") or payload.get("to")
                add_few_shot("missed_edge",
                             f"「{a}」と「{b}」の関係が見落とされがちです"
                             f" (ユーザーが {_date_of(edit)} に追加)", ts)

            elif op == "add_node":
                label = str(payload.get("label") or "")
                if label:
                    add_few_shot("missed_node",
                                 f"「{label}」が概念として抽出されていませんでした"
                                 f" (ユーザーが {_date_of(edit)} に追加)", ts)

    # --- 自動適用の可否を決める (§5.2) ---
    lexicon: list[dict[str, Any]] = []
    for src_key in sorted(renames):
        bucket = renames[src_key]
        # 同じ from に複数の to があるなら文脈依存の改名。機械適用しない
        unique = len(bucket) == 1
        for dst_key in sorted(bucket):
            entry = dict(bucket[dst_key])
            entry["auto"] = unique
            lexicon.append(entry)
        if not unique:
            targets = "・".join(f"「{bucket[k]['to']}」" for k in sorted(bucket))
            add_few_shot(
                "ambiguous_rename",
                f"「{bucket[sorted(bucket)[0]]['from']}」の言い換えは文脈により"
                f"異なります ({targets})。文脈に合う表記を選んでください",
                max((bucket[k]["ts"] for k in bucket), default=""))

    stoplist: list[dict[str, Any]] = []
    for key in sorted(stops):
        entry = dict(stops[key])
        entry["auto"] = entry["n"] >= STOPLIST_AUTO_MIN
        stoplist.append(entry)
        if not entry["auto"]:
            add_few_shot("noisy_node",
                         f"「{entry['label']}」はノイズかもしれません"
                         "(ユーザーが 1 回削除しています)", entry["ts"])

    # --- cue_stats: kept は生成側の統計なので base から引き継ぐ ---
    cue_stats: dict[str, dict[str, int]] = {}
    for cue, stats in (base or {}).get("cue_stats", {}).items():
        kept = int(stats.get("kept", 0)) if isinstance(stats, dict) else 0
        if kept:
            cue_stats[cue] = {"kept": kept, "downgraded_by_user": 0}
    for cue, n in cue_down.items():
        cue_stats.setdefault(cue, {"kept": 0, "downgraded_by_user": 0})
        cue_stats[cue]["downgraded_by_user"] = n

    # few_shot は重複を除いて新しい順に上限まで
    seen: set[str] = set()
    unique_few: list[dict[str, Any]] = []
    for item in sorted(few_shot, key=lambda x: str(x.get("ts") or ""), reverse=True):
        if item["text"] in seen:
            continue
        seen.add(item["text"])
        unique_few.append(item)

    store["lexicon"] = lexicon
    store["stoplist"] = stoplist
    store["causal_overrides"] = [overrides[k] for k in sorted(overrides)]
    store["cue_stats"] = dict(sorted(cue_stats.items()))
    store["few_shot"] = unique_few[:FEW_SHOT_MAX]
    return store


def relearn(*, graphs_dir: str | Path = GRAPHS_DIR,
            path: str | Path = LEARNED_PATH,
            save: bool = True) -> dict[str, Any]:
    """全セッションの編集ログから learned.json を作り直す (§5.2)。

    取り消し (revert) は effective_edits が落とすので、undo すれば対応する
    学習エントリも自然に減る。増分更新のバグより「毎回作り直す」方が安全で、
    編集ログは十分小さい。
    """
    sessions = {s: load_edits(s, graphs_dir=graphs_dir)
                for s in list_edited_sessions(graphs_dir=graphs_dir)}
    learned = derive_learned(sessions, base=load_learned(path))
    if save:
        save_learned(learned, path)
    logger.info("relearned sessions=%d lexicon=%d stoplist=%d overrides=%d",
                len(sessions), len(learned["lexicon"]), len(learned["stoplist"]),
                len(learned["causal_overrides"]))
    return learned


def update_from_edit(edit: dict[str, Any], session: str, *,
                     graphs_dir: str | Path = GRAPHS_DIR,
                     path: str | Path = LEARNED_PATH) -> dict[str, Any]:
    """append_edit の後に呼ぶ更新 (§5.2)。戻り値は増減の差分。

    実装は「編集ログ全体から relearn を回し直す」で単純化する (設計書が
    許容する簡略化)。戻り値の差分は Web の `learned_delta` に使う。
    """
    before = summarize(load_learned(path))
    after = summarize(relearn(graphs_dir=graphs_dir, path=path))
    changed = {k: after[k] - before[k] for k in
               ("lexicon", "lexicon_auto", "stoplist", "stoplist_auto",
                "causal_overrides", "few_shot")
               if after[k] != before[k]}
    logger.info("learned updated by %s/%s changed=%s",
                session, edit.get("edit_id"), changed)
    return {"before": before, "after": after, "changed": changed}


def note_cues_kept(cues: Iterable[str], *,
                   path: str | Path = LEARNED_PATH) -> None:
    """因果として維持された cue を数える (§5.1 cue_stats の kept)。

    R1 は**記録と報告のみ**。閾値を超えた語彙の自動降格は R2 (§12) で、
    変更の判断は人が行う。

    エッジ単位で数える (重複を潰さない)。downgraded_by_user もエッジ単位なので、
    そろえないと「維持 1 : 降格 5」のような比率が読めなくなる。
    """
    words = [str(c).split(":", 1)[-1].strip() for c in cues if str(c).strip()]
    if not words:
        return
    learned = load_learned(path)
    stats = learned.setdefault("cue_stats", {})
    for word in words:
        entry = stats.setdefault(word, {"kept": 0, "downgraded_by_user": 0})
        entry["kept"] = int(entry.get("kept", 0)) + 1
    save_learned(learned, path)


# ------------------------------------------------------------ 適用 (§5.3)


def apply_learned(kg: dict[str, Any], learned: dict[str, Any] | None, *,
                  enabled: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """抽出直後の KG へ学習を適用する。**必ず report を返す** (黙って直さない)。

    - lexicon (auto) → ラベルの改名
    - stoplist (auto) → 概念の除外 (接続エッジもカスケード)
    - causal_overrides → `causal_override` を刻み、因果検証をスキップさせる
      (reverse は向きを直したうえで allow 扱い)

    因果の**昇格は行わない**。allow は「抽出側が因果と言った対を LLM 検証なしで
    通す」という意味であって、相関として抽出されたものを機械が因果へ格上げする
    ことではない (資料に無い主張を作らないため)。
    """
    report: dict[str, Any] = {
        "enabled": bool(enabled), "renames": 0, "stoplisted": 0,
        "causal_allow": 0, "causal_deny": 0, "reversed": 0, "details": [],
    }
    if not enabled or not learned:
        return kg, report

    out = copy.deepcopy(kg)

    # --- 1) 改名 ---
    lexicon = {normalize_label(e.get("from")): e
               for e in learned.get("lexicon", []) if e.get("auto") and e.get("to")}
    for node in out.get("nodes", []):
        entry = lexicon.get(normalize_label(node.get("label")))
        if not entry or normalize_label(entry["to"]) == normalize_label(node.get("label")):
            continue
        report["details"].append({"kind": "rename", "from": node.get("label"),
                                  "to": entry["to"], "node_id": node.get("id")})
        node["label"] = entry["to"]
        report["renames"] += 1

    # --- 2) 除外 ---
    stop = {normalize_label(e.get("label")) for e in learned.get("stoplist", [])
            if e.get("auto") and e.get("label")}
    if stop:
        drop = {n["id"] for n in out.get("nodes", [])
                if normalize_label(n.get("label")) in stop}
        for node in out.get("nodes", []):
            if node["id"] in drop:
                report["details"].append({"kind": "stoplist", "label": node.get("label"),
                                          "node_id": node["id"]})
        if drop:
            out["nodes"] = [n for n in out.get("nodes", []) if n["id"] not in drop]
            out["edges"] = [e for e in out.get("edges", [])
                            if e.get("from") not in drop and e.get("to") not in drop]
            report["stoplisted"] = len(drop)

    # --- 3) 因果の上書き ---
    overrides: dict[tuple[str, str], dict[str, Any]] = {}
    for o in learned.get("causal_overrides", []):
        if o.get("decision") in DECISIONS:
            overrides[(normalize_label(o.get("from_label")),
                       normalize_label(o.get("to_label")))] = o
    if overrides:
        labels = {n["id"]: n.get("label") for n in out.get("nodes", [])}
        for edge in out.get("edges", []):
            key = (normalize_label(labels.get(edge.get("from"))),
                   normalize_label(labels.get(edge.get("to"))))
            o = overrides.get(key)
            if not o:
                continue
            decision = o["decision"]
            detail = {"kind": decision, "edge_id": edge.get("id"),
                      "from": labels.get(edge.get("from")),
                      "to": labels.get(edge.get("to")),
                      "source": o.get("source")}
            if decision == "reverse":
                edge["from"], edge["to"] = edge["to"], edge["from"]
                edge["causal_override"] = "allow"
                report["reversed"] += 1
                report["causal_allow"] += 1
            elif decision == "allow":
                edge["causal_override"] = "allow"
                report["causal_allow"] += 1
            else:
                edge["causal_override"] = "deny"
                report["causal_deny"] += 1
            report["details"].append(detail)

    if any(report[k] for k in ("renames", "stoplisted", "causal_allow",
                               "causal_deny", "reversed")):
        logger.info("learned applied renames=%d stoplisted=%d allow=%d deny=%d rev=%d",
                    report["renames"], report["stoplisted"], report["causal_allow"],
                    report["causal_deny"], report["reversed"])
    return out, report


def report_line(report: dict[str, Any]) -> str:
    """実行サマリ用の 1 行 (CLI の標準出力・Web の学習チップに使う)。"""
    if not report or not report.get("enabled"):
        return "学習の適用: なし (無効)"
    parts = []
    if report.get("renames"):
        parts.append(f"改名 {report['renames']}")
    if report.get("stoplisted"):
        parts.append(f"除外 {report['stoplisted']}")
    causal = report.get("causal_allow", 0) + report.get("causal_deny", 0)
    if causal:
        parts.append(f"因果上書き {causal}")
    if report.get("reversed"):
        parts.append(f"向き修正 {report['reversed']}")
    if not parts:
        return "学習を適用: 該当なし"
    return "学習を適用: " + "・".join(parts)


# ------------------------------------------- プロンプトヒント (§5.3 の 1)


def _hint_lines(learned: dict[str, Any]) -> list[tuple[str, str, int]]:
    """(本文, ts, 頻度) の候補一覧を作る。"""
    out: list[tuple[str, str, int]] = []
    for e in learned.get("lexicon", []):
        if e.get("auto"):
            out.append((f"「{e['from']}」は「{e['to']}」と表記してください",
                        str(e.get("ts") or ""), int(e.get("n", 1))))
        else:
            out.append((f"「{e['from']}」は文脈により「{e['to']}」と呼ばれます",
                        str(e.get("ts") or ""), int(e.get("n", 1))))
    for e in learned.get("stoplist", []):
        if e.get("auto"):
            out.append((f"「{e['label']}」は概念として抽出しないでください"
                        f" (過去に {e['n']} 回削除されています)",
                        str(e.get("ts") or ""), int(e.get("n", 1))))
    for o in learned.get("causal_overrides", []):
        a, b = o.get("from_label"), o.get("to_label")
        if o["decision"] == "allow":
            text = f"「{a}」→「{b}」は因果として扱ってかまいません (ユーザー確定)"
        elif o["decision"] == "deny":
            text = f"「{a}」と「{b}」は因果ではなく相関として扱ってください"
        else:
            text = f"「{a}」と「{b}」の因果の向きは「{b}」→「{a}」です"
        out.append((text, str(o.get("ts") or ""), int(o.get("n", 1))))
    for f in learned.get("few_shot", []):
        out.append((str(f.get("text") or ""), str(f.get("ts") or ""), 1))
    return [x for x in out if x[0]]


def build_prompt_hints(learned: dict[str, Any] | None, *,
                       max_items: int = 10,
                       max_chars: int = HINTS_MAX_CHARS) -> str:
    """抽出プロンプト末尾に足す「過去の修正からの注意」を組み立てる (§5.3)。

    直近 5 件 + 頻度上位 5 件を混ぜ、全体 max_chars 字で打ち切る。
    エージェント定義は変えない — バージョンを増殖させず、実行ごとに最新の
    ヒントを使うため。
    """
    if not learned:
        return ""
    candidates = _hint_lines(learned)
    if not candidates:
        return ""

    half = max(1, max_items // 2)
    recent = sorted(candidates, key=lambda x: (x[1], x[0]), reverse=True)[:half]
    frequent = sorted(candidates, key=lambda x: (-x[2], x[0]))[:half]

    picked: list[str] = []
    for text, _ts, _n in recent + frequent:
        if text not in picked:
            picked.append(text)
        if len(picked) >= max_items:
            break

    header = "\n=== 過去の修正からの注意 (この利用者の編集履歴より) ===\n"
    body = ""
    for text in picked:
        line = f"- {text}\n"
        if len(header) + len(body) + len(line) > max_chars:
            break
        body += line
    if not body:
        return ""
    return header + body


# ------------------------------------------------------------ 要約


def summarize(learned: dict[str, Any] | None) -> dict[str, Any]:
    """learned.json の要約 (CLI の --show-learned / Web の GET /api/learned)。"""
    learned = learned or empty_store()
    overrides = learned.get("causal_overrides", [])
    return {
        "scope": learned.get("scope", "personal"),
        "updated_at": learned.get("updated_at"),
        "lexicon": len(learned.get("lexicon", [])),
        "lexicon_auto": sum(1 for e in learned.get("lexicon", []) if e.get("auto")),
        "stoplist": len(learned.get("stoplist", [])),
        "stoplist_auto": sum(1 for e in learned.get("stoplist", []) if e.get("auto")),
        "causal_overrides": len(overrides),
        "by_decision": {d: sum(1 for o in overrides if o.get("decision") == d)
                        for d in DECISIONS},
        "few_shot": len(learned.get("few_shot", [])),
        "cue_stats": len(learned.get("cue_stats", {})),
    }


def cue_warnings(learned: dict[str, Any] | None, *, ratio: float = 0.5,
                 min_n: int = 3) -> list[str]:
    """ユーザーに多く降格されている cue を警告する (§12: 変更は人が判断)。"""
    out: list[str] = []
    for cue, stats in sorted((learned or {}).get("cue_stats", {}).items()):
        down = int(stats.get("downgraded_by_user", 0))
        kept = int(stats.get("kept", 0))
        total = down + kept
        if down >= min_n and total and down / total >= ratio:
            out.append(f"語彙「{cue}」はユーザーに {down}/{total} 回降格されています"
                       " — 因果語彙から外すか検討してください")
    return out
