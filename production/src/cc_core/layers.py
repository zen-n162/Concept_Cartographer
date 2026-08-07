"""4 層 30 種の関係語彙と、層タグ → glyph の決定的投影 (R2a 設計書 §1 / §4)。

概念地図の「意味」は 4 層 30 種の関係語彙で持ち、**画面に出す記号は 10 種**に
畳む。この落差を埋めるのがこのモジュールの投影 (projection) で、次の 2 点を
同時に満たすためにある:

  - UI は 8〜10 記号で読めるまま (記号が増えすぎると地図が読めなくなる)
  - 内部の 30 種は失わない (layer_tags として保持し、クリック展開で説明できる)

**投影は生成パイプラインだけで走る** (裁定 C)。fold / rebuild_session /
reconcile_relation_policy は保存済み glyph の世界で完結し、このモジュールを
呼ばない — 編集済みの地図が再構成のたびに機械の都合で塗り替わらないため。

依存を持たないモジュールにしてあるのは意図的で、normalize.py がこちらを
import する (逆向きの依存を作らない)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

# --- 4 層 30 種 (設計書 §1 / v4実§4.1-4.4) ---
# 内部タグは snake_case。ハイフン・大文字は normalize_layer_tags が吸収する。
LAYER_A: tuple[str, ...] = (
    "is_a", "part_of", "participates_in", "has_property", "located_in", "has_role",
)  # オントロジー
LAYER_B: tuple[str, ...] = (
    "hypothesis_of", "method_of", "evidence_for", "result_of",
    "motivation_of", "limitation_of", "background_of", "conclusion_of",
)  # 言説・構造 (AZ/CoreSC 転用)
LAYER_C: tuple[str, ...] = (
    "causes", "correlates_with", "precedes", "co_occurs_with",
    "depends_on", "example_of", "precondition_of", "mechanism_of",
)  # 意味・因果
LAYER_D: tuple[str, ...] = (
    "corroborates", "refutes", "refines", "extends",
    "questions", "agrees_with", "disagrees_with", "cites_as_source",
)  # 認識論・修辞

LAYER_VOCAB: dict[str, tuple[str, ...]] = {
    "layer_A": LAYER_A, "layer_B": LAYER_B, "layer_C": LAYER_C, "layer_D": LAYER_D,
}
LAYER_KEYS: tuple[str, ...] = ("layer_A", "layer_B", "layer_C", "layer_D")

# 因果を表す glyph。learning.py / evaluation.py / pipeline.py が
# "arrow" を直書きしていた箇所はすべてこの定数を参照する (設計書 §4)。
CAUSAL_GLYPH = "arrow"

# 層 D の裏付けとみなす検証スコアの下限 (設計書 §4 corroborated / §7 の validated 閾値)
CORROBORATION_THRESHOLD = 0.75

# kg 直下に刻む知識モデル世代 (旧セッションには無い = R1.5 以前と区別できる)
LAYER_MODEL = "r2a"

# polarity の 3 値 (v4実§4.5)。⑦meta が未設定を "neutral" で充填する。
POLARITY = ("positive", "negative", "neutral")
DEFAULT_POLARITY = "neutral"

# 層キーの表記ゆれ ("A" / "a" / "layer-a" / "layerA" …) を正規形へ寄せる
_LAYER_KEY_ALIASES: dict[str, str] = {}
for _key in LAYER_KEYS:
    _letter = _key[-1]
    for _alias in (_key, _key.lower(), _letter, _letter.lower(),
                   f"layer-{_letter}", f"layer-{_letter.lower()}",
                   f"layer{_letter}", f"layer{_letter.lower()}"):
        _LAYER_KEY_ALIASES[_alias] = _key
del _key, _letter, _alias


def normalize_tag(raw: Any) -> str:
    """関係タグ 1 個を内部表記 (snake_case・小文字) へ揃える。"""
    return str(raw or "").strip().replace("-", "_").replace(" ", "_").lower()


def normalize_layer_tags(raw: Any) -> tuple[dict[str, list[str]], list[str]]:
    """layer_tags を正規形へ揃え、(正規化済みタグ, 捨てたタグ) を返す。

    LLM は語彙表どおりのタグを返すとは限らない (normalize.py と同じ思想:
    プロンプトで縛るだけに頼らず、受け取り側で必ず直す)。ここでの規則:

      - 層キーの表記ゆれを吸収 ("A" / "layer-a" / "layerA" -> "layer_A")
      - タグは snake_case・小文字へ ("is-a" / "Is_A" -> "is_a")
      - **語彙表に無いタグは捨てる**。捨てた分は第 2 戻り値で返し、
        呼び出し側 (normalize.py) が NormalizeReport へ積む
      - 層をまたいだ誤配置 (layer_A に "causes") も未知タグとして捨てる
      - 重複は落とし、語彙表の順序で並べ直す (決定的な出力にするため)

    戻り値の 1 つ目は **常に 4 層すべてのキーを持つ** (中身は空配列でよい)。
    形を固定しておくと、下流が `tags["layer_C"]` を素で書けて事故が減る。

    NormalizeReport をここで受け取らないのは、layers.py を依存ゼロに保って
    normalize.py -> layers.py の一方向 import だけにするため。
    """
    tags: dict[str, list[str]] = {k: [] for k in LAYER_KEYS}
    dropped: list[str] = []
    if not isinstance(raw, dict):
        if raw is not None:
            dropped.append(f"layer_tags: {type(raw).__name__} は dict ではない")
        return tags, dropped

    for raw_key, raw_values in raw.items():
        key = _LAYER_KEY_ALIASES.get(str(raw_key).strip())
        if key is None:
            dropped.append(f"未知の層キー '{raw_key}'")
            continue
        if isinstance(raw_values, str):        # 単一タグを裸で返された場合
            raw_values = [raw_values]
        elif not isinstance(raw_values, (list, tuple)):
            if raw_values is not None:
                dropped.append(f"{key}: 配列でない値を破棄")
            continue
        vocabulary = LAYER_VOCAB[key]
        seen: set[str] = set()
        for value in raw_values:
            tag = normalize_tag(value)
            if not tag:
                continue
            if tag not in vocabulary:
                dropped.append(f"{key}: 未知のタグ '{value}'")
                continue
            if tag not in seen:
                seen.add(tag)
        tags[key] = [t for t in vocabulary if t in seen]   # 語彙表の順に整列
    return tags, dropped


def has_layer_tags(edge: dict[str, Any]) -> bool:
    """投影の材料になる層タグが 1 つでも付いているか。"""
    tags = edge.get("layer_tags")
    if not isinstance(tags, dict):
        return False
    return any(tags.get(k) for k in LAYER_KEYS)


def is_user_origin(edge: dict[str, Any]) -> bool:
    """ユーザーが編集・追加した要素か (編集/学習設計書 §2 の origin)。"""
    return str(edge.get("origin") or "").startswith("user")


def corroborated(edge: dict[str, Any]) -> bool:
    """因果として点灯させてよいだけの裏付けがあるか (設計書 §4)。

    優先順:
      1. `causal_override == "allow"` — 過去の修正でユーザーが因果と確定した対。
         人間が最終権威なので検証結果より強い
      2. `validation.combined >= 0.75` — ⑤validate が走った run
      3. 互換モード — 検証段が走らなかった run (offline 等) では R1.5 の
         `causal_check` を見る。保存形に `passed` キーは無く、降格したときだけ
         `demoted_from` が入る仕様なので「causal_check があって demoted_from が
         無い」= 3 点セット通過、と読む
    """
    if edge.get("causal_override") == "allow":
        return True
    validation = edge.get("validation")
    if isinstance(validation, dict) and validation.get("combined") is not None:
        try:
            return float(validation["combined"]) >= CORROBORATION_THRESHOLD
        except (TypeError, ValueError):
            pass
    check = edge.get("causal_check")
    if isinstance(check, dict):
        return not check.get("demoted_from")
    return False


def project_glyph(edge: dict[str, Any]) -> str:
    """層タグから表示 glyph を決める純関数。**順序そのものが仕様** (設計書 §4)。

    純関数なので edge は変更しない。規則⑩ (裏付け不足の causes) で
    `causal_check.demoted_from` を記録するのは `apply_glyph_projection`。
    """
    glyph = str(edge.get("glyph") or "wave")

    # ① 人間が最終権威 — ユーザーが選んだ記号を機械が塗り替えない (裁定 D)
    if is_user_origin(edge):
        return glyph
    # ② 層タグが無い = R1.5 世代のエッジ。そのまま通す (挙動不変の要)
    if not has_layer_tags(edge):
        return glyph

    tags = edge["layer_tags"]
    layer_a = set(tags.get("layer_A") or ())
    layer_c = set(tags.get("layer_C") or ())
    layer_d = set(tags.get("layer_D") or ())

    if "refutes" in layer_d:                              # ③ 矛盾は層 D でのみ点灯
        return "zigzag"
    if "causes" in layer_c and corroborated(edge):        # ④ 因果 + 裏付け
        return CAUSAL_GLYPH
    if "questions" in layer_d:                            # ⑤ 疑問
        return "question"
    if "precedes" in layer_c:                             # ⑥ 時系列先行
        return "precedes"
    if "is_a" in layer_a:                                 # ⑦ 分類
        return "isa"
    if "part_of" in layer_a:                              # ⑧ 構成
        return "partof"
    if layer_d & {"corroborates", "agrees_with"}:         # ⑨ 補強 (裁定 F)
        return "double"
    # ⑩ causes だが裏付け不足 → 相関へ降格 / ⑪ それ以外の既定も相関
    return "wave"


def demoted_by_projection(edge: dict[str, Any], projected: str) -> bool:
    """規則⑩ (causes の裏付け不足による降格) に当たるか。"""
    return (projected == "wave"
            and not is_user_origin(edge)
            and has_layer_tags(edge)
            and "causes" in set(edge["layer_tags"].get("layer_C") or ()))


def verifier_id(model: str) -> str:
    """モデル名から検証器 ID を作る ("gpt-5.6-terra" -> "llm-verifier:terra")。

    設計書 §7 の verifier_id 表記に合わせる。provenance.validator_ids には
    **実際に走った**検証器だけを入れる — 走っていない検証器の ID が残ると、
    後から「この関係は 3 種で検証済み」と誤読されるため。
    """
    short = str(model or "").rsplit("-", 1)[-1] or str(model or "unknown")
    return f"llm-verifier:{short}"


def apply_meta(
    kg: dict[str, Any],
    *,
    extractor_model: str,
    validator_ids: list[str] | tuple[str, ...] = (),
    timestamp: str | None = None,
) -> dict[str, int]:
    """⑦meta — 生成した kg に決定的なメタ情報を書き込む (設計書 §9)。

    独立した STAGE にはしない (LLM 呼び出しが無く、進捗として見せる意味が
    ないため)。KG 保存の直前に 1 回だけ走る:

      1. polarity 未設定 -> "neutral" (3 値のうち「向きなし」を既定に置く)
      2. provenance を充填 — validator_ids は実際に走った検証器のみ。
         human_reviewed は既存値を尊重する (人が見た事実を機械が消さない)
      3. project_glyph を実行 — layer_tags が無ければ R1.5 のパススルー
      4. kg["layer_model"] = "r2a" を刻印 (旧世代と読み分けるため)

    kg は **その場で書き換える**。戻り値は集計 (summary へ載せる)。
    """
    ts = timestamp or dt.datetime.now().isoformat(timespec="seconds")
    stats = {"edges": 0, "polarity_filled": 0, "provenance_written": 0,
             "glyph_projected": 0, "glyph_changed": 0, "demoted_from_causal": 0}

    for edge in kg.get("edges", []):
        if not isinstance(edge, dict):
            continue
        stats["edges"] += 1

        if edge.get("polarity") not in POLARITY:
            edge["polarity"] = DEFAULT_POLARITY
            stats["polarity_filled"] += 1

        prov = edge.get("provenance")
        prov = dict(prov) if isinstance(prov, dict) else {}
        prov["extractor_model"] = extractor_model
        prov["timestamp"] = ts
        prov["validator_ids"] = list(validator_ids)
        prov.setdefault("human_reviewed", False)
        edge["provenance"] = prov
        stats["provenance_written"] += 1

        before = edge.get("glyph")
        had_demotion = bool((edge.get("causal_check") or {}).get("demoted_from"))
        after = apply_glyph_projection(edge)
        stats["glyph_projected"] += 1
        if after != before:
            stats["glyph_changed"] += 1
        if not had_demotion and (edge.get("causal_check") or {}).get("demoted_from"):
            stats["demoted_from_causal"] += 1

    kg["layer_model"] = LAYER_MODEL
    return stats


def apply_glyph_projection(edge: dict[str, Any]) -> str:
    """project_glyph を 1 本のエッジへ適用する (edge を書き換える)。

    規則⑩ で降格したときは `causal_check.demoted_from = "arrow"` を残す。
    evaluation.causal_precision_log がこの印を数えているので、層タグ経由の
    降格でも KPI の連続性が保たれる (設計書 §4 / §9)。

    逆に規則③ で ⚡ に**戻った**ときは、R1 の「矛盾は非断定へ降格」の記録を
    畳む — 投影が最終的な記号を決める段なので、そこと食い違う説明を残さない。
    """
    projected = project_glyph(edge)
    if demoted_by_projection(edge, projected):
        check = edge.get("causal_check")
        if not isinstance(check, dict):
            check = {}
        check.setdefault("verifier_verdict", "skipped")
        check["demoted_from"] = CAUSAL_GLYPH
        check.setdefault("reason", "層タグは causes だが裏付けが不足 (投影で相関へ降格)")
        edge["causal_check"] = check
    elif projected == "zigzag":
        # ④relate は矛盾候補を毎回 tension へ落として「矛盾判定は L8 (R2) で
        # 行う」と書く (裁定 7)。層 D に refutes を持つエッジを再実行すると、
        # その記録が ⚡ の説明として残ってしまう — クリック展開に
        # 「候補として非断定表示」と出て、表示している ⚡ と食い違う
        # 【実測: layers 再利用の offline 再実行で発生】。
        check = edge.get("causal_check")
        if isinstance(check, dict) and check.get("demoted_from") == "zigzag":
            check.pop("demoted_from", None)
            check["reason"] = "層 D の refutes により矛盾として断定 (投影規則③)"
    edge["glyph"] = projected
    return projected
