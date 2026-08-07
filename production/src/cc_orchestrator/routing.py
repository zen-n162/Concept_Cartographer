"""段階⓪ Query Routing (v3 §4.1 / v4実§2.3)。

「全クエリを概念地図生成へ流す」設計は v1→v2 で否定された (v3 §0.2)。
時間・コスト・過剰構造化を同時に抑えるため、要求の性質で経路を分ける。

R1 の 3 経路 (実運用計画 §6):
  basic   雑談・範囲外の単純質問       -> 直答 (LLM のみ、資料収集なし)
  vector  事実照会 (who/what/when)     -> AI Search KB + 直答
  map     概念地図の生成・更新          -> フルパイプライン
R2b で local / global / hybrid を追加した (R2b 設計書 §2)。R3 で ontology-guided。

  local   「X と Y の関係は?」        -> 索引検索 + 2-hop 近傍から直答 (出典つき)
  global  「全体像は?」               -> コーパスコミュニティの要約から直答
  hybrid  local と global の複合       -> 近傍 + 要約を統合して直答

判定はまず決定的なルール (日本語・英語のキーワードと文型) で行い、
確信が持てないときだけ LLM 分類器へ委ねる。ルールで済むものに LLM を
使わないこと自体がコスト対策 (計画 §13-4)。

**裁定 N (経路判定の保守性)**: 新経路へ倒すのは明示的な手がかり語があるときだけ。
手がかりが無い入力の既定は従来どおり map で、既存 3 経路の判定は 1 件も変えない。
そのため新しい cue の照合は MAP_CUES と BASIC_CUES の**後ろ**に置いてある —
「概念地図として整理して」のような地図生成の依頼が、文中に「なぜ」が入って
いるだけで QA へ流れることが無いようにするため。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from cc_orchestrator.ingest import parse_window
from cc_core.logging_util import get_logger

logger = get_logger("cc_orchestrator.routing")

ROUTES = ("basic", "vector", "map", "local", "global", "hybrid")
# 地図を作らず直答する経路 (pipeline のディスパッチ表と対応させること)
ANSWER_ROUTES = ("basic", "vector", "local", "global", "hybrid")

# --- 地図生成を明示する語 ---
MAP_CUES = [
    "概念地図", "概念マップ", "コンセプトマップ", "地図にして", "図にして",
    "整理して", "可視化", "マッピング", "concept map", "visuali", "map my",
    "描いて", "作図",
]
# --- 事実照会 (単発の問い) ---
VECTOR_CUES = [
    "とは", "何ですか", "なんですか", "教えて", "いつ", "どこ", "誰が", "何件",
    "どのくらい", "定義", "意味は", "what is", "who ", "when ", "where ",
    "how many", "explain",
]
# --- 雑談・範囲外 ---
BASIC_CUES = [
    "こんにちは", "ありがとう", "おはよう", "hello", "hi ", "thanks",
    "使い方", "help", "ヘルプ", "できること",
]
# --- 局所 QA: 特定の概念どうしの繋がりを問う (R2b 設計書 §2) ---
# 設計書の列挙に「の関係」を足してある。設計は「との関係」だけを挙げていたが、
# 日本語では「A と B の関係は?」と書くほうが普通で (「との関係」は現れない)、
# 受け入れ基準 2 の例文そのものが素通りしてしまうため。地図依頼の
# 「概念の関係を図にして」は MAP_CUES が先に当たるので影響しない。
LOCAL_CUES = [
    "との関係", "の関係", "との繋がり", "とのつながり",
    "どう繋が", "どうつなが", "どう関係",
    "なぜ", "どうして", "経緯", "原因", "影響",
]
# --- 大域 QA: コーパス全体を見渡す問い (R2b 設計書 §2) ---
GLOBAL_CUES = [
    "全体像", "俯瞰", "全体をまとめ", "全体を通して", "テーマは",
    "何がわかって", "何が分かって", "横断して", "横断で",
]
# --- 複合: local の材料と global の要約を両方使う (R2b 設計書 §2) ---
# local と global の cue が両方当たった場合もここへ倒す。
HYBRID_CUES = ["比較して", "比べて", "整理した上で", "踏まえた上で"]
# 詳細度の指定
LEVEL_CUES = {
    "overview": ["概観", "俯瞰", "ざっくり", "全体像", "overview", "大まか", "簡単に"],
    "detailed": ["詳細", "細かく", "詳しく", "detailed", "精査", "深く"],
    "standard": ["標準", "standard", "通常"],
}
# 言語オプション (v3 §2.2)
LANG_CUES = {
    "en": ["英語で", "in english", "english"],
    "ja": ["日本語で", "in japanese", "japanese"],
}


@dataclass
class RouteDecision:
    route: str
    detail_level: str | None
    language: str | None
    window_label: str
    tags: list[str]
    rationale: str
    used_llm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "detail_level": self.detail_level,
            "language": self.language,
            "window": self.window_label,
            "tags": self.tags,
            "rationale": self.rationale,
            "used_llm": self.used_llm,
        }


def _hit(text: str, cues: list[str]) -> str | None:
    low = text.lower()
    for c in cues:
        if c in low or c in text:
            return c
    return None


def parse_detail_level(message: str) -> str | None:
    """依頼文から詳細度の指定を読み取る (無指定なら None = 既定 standard)。"""
    for level in ("detailed", "overview", "standard"):
        if _hit(message, LEVEL_CUES[level]):
            return level
    return None


def parse_language(message: str) -> str | None:
    for lang, cues in LANG_CUES.items():
        if _hit(message, cues):
            return lang
    return None


def parse_tags(message: str) -> list[str]:
    """#タグ 形式のスコープ指定を取り出す (v3 §2.2)。"""
    return sorted(set(re.findall(r"#([^\s#、,]+)", message)))


LLMClassifier = Callable[[str], str]
"""LLM 分類器: message -> route ('basic'|'vector'|'map')。ルールで決まらない時のみ使う。"""


def route(
    message: str,
    *,
    classifier: LLMClassifier | None = None,
    default_route: str = "map",
) -> RouteDecision:
    """依頼文から経路と生成オプションを決める。

    ルール判定の優先順位 (R2b 設計書 §2):
      1. 地図生成の明示語があれば map (最も確実な合図)
      2. 雑談・ヘルプ語だけなら basic
      3. local / global の手がかりがあれば QA 経路 (両方当たれば hybrid)
      4. 事実照会の文型で、かつ地図語が無ければ vector
      5. 期間指定 (今週・今月等) があれば map (資料横断の意図)
      6. 決まらなければ classifier (あれば) → 無ければ default_route

    3 を 1・2 の**後ろ**、4 の**前**に置くのが裁定 N の実装そのもの。既存 3 経路の
    入口 (地図語・雑談語) は先に確定させ、そのどれでもなかった問いだけを新経路が
    受ける。「NV中心とは何ですか」(vector) に local/global の手がかりは無いので、
    既存の判定は 1 件も動かない。
    """
    detail = parse_detail_level(message)
    language = parse_language(message)
    tags = parse_tags(message)
    _, window_label = parse_window(message)

    map_hit = _hit(message, MAP_CUES)
    basic_hit = _hit(message, BASIC_CUES)
    vector_hit = _hit(message, VECTOR_CUES)
    has_window = "既定" not in window_label

    if map_hit:
        return RouteDecision("map", detail, language, window_label, tags,
                             f"地図生成の明示語「{map_hit}」")
    if basic_hit and not vector_hit and len(message) <= 40:
        return RouteDecision("basic", detail, language, window_label, tags,
                             f"雑談・ヘルプ語「{basic_hit}」で短文")

    # ---- R2b: QA 経路 (裁定 N: 明示的な手がかりがあるときだけ) ----
    local_hit = _hit(message, LOCAL_CUES)
    global_hit = _hit(message, GLOBAL_CUES)
    hybrid_hit = _hit(message, HYBRID_CUES)
    if hybrid_hit or (local_hit and global_hit):
        why = (f"複合の語「{hybrid_hit}」" if hybrid_hit
               else f"局所「{local_hit}」と大域「{global_hit}」の両方")
        return RouteDecision("hybrid", detail, language, window_label, tags,
                             f"{why}があり近傍と要約を統合")
    if local_hit:
        return RouteDecision("local", detail, language, window_label, tags,
                             f"特定の概念どうしを問う語「{local_hit}」")
    if global_hit:
        return RouteDecision("global", detail, language, window_label, tags,
                             f"全体を見渡す語「{global_hit}」")

    if vector_hit:
        return RouteDecision("vector", detail, language, window_label, tags,
                             f"事実照会の文型「{vector_hit}」")
    if has_window:
        return RouteDecision("map", detail, language, window_label, tags,
                             f"期間指定「{window_label}」があり資料横断の意図")

    if classifier is not None:
        try:
            decided = classifier(message)
            if decided in ROUTES:
                return RouteDecision(decided, detail, language, window_label, tags,
                                     "ルールで判定できずLLM分類器を使用", used_llm=True)
        except Exception as exc:
            logger.warning("route classifier failed: %s", type(exc).__name__)

    return RouteDecision(default_route, detail, language, window_label, tags,
                         "ルール・分類器いずれも決定打なし → 既定経路")
