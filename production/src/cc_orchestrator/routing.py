"""段階⓪ Query Routing (v3 §4.1 / v4実§2.3)。

「全クエリを概念地図生成へ流す」設計は v1→v2 で否定された (v3 §0.2)。
時間・コスト・過剰構造化を同時に抑えるため、要求の性質で経路を分ける。

R1 の 3 経路 (実運用計画 §6):
  basic   雑談・範囲外の単純質問       -> 直答 (LLM のみ、資料収集なし)
  vector  事実照会 (who/what/when)     -> AI Search KB + 直答
  map     概念地図の生成・更新          -> フルパイプライン
R2 で local / global / hybrid、R3 で ontology-guided を追加する。

判定はまず決定的なルール (日本語・英語のキーワードと文型) で行い、
確信が持てないときだけ LLM 分類器へ委ねる。ルールで済むものに LLM を
使わないこと自体がコスト対策 (計画 §13-4)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from cc_orchestrator.ingest import parse_window
from cc_core.logging_util import get_logger

logger = get_logger("cc_orchestrator.routing")

ROUTES = ("basic", "vector", "map")

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

    ルール判定の優先順位:
      1. 地図生成の明示語があれば map (最も確実な合図)
      2. 雑談・ヘルプ語だけなら basic
      3. 事実照会の文型で、かつ地図語が無ければ vector
      4. 期間指定 (今週・今月等) があれば map (資料横断の意図)
      5. 決まらなければ classifier (あれば) → 無ければ default_route
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
