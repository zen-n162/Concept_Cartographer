"""3 検証器と合成 — 主張と因果候補を検証する (R2a 設計書 §7)。

抽出した主張をそのまま地図に載せない、というのがこの段の役目。**種類の違う
検証器を 3 つ走らせ、重み付き平均で 1 つのスコアにまとめる**:

  nli       0.40  根拠文 (premise) から主張 (hypothesis) が導けるか
  llm       0.35  抽出とは**別モデル**による独立判定 (裁定 7 の 3 点目と同じ流儀)
  ontology  0.25  決定的な整合性規則 (LLM を使わない / 裁定 B)

重みは「走れた検証器だけ」で再正規化する (§7)。検証器が落ちた run で
スコアが勝手に下がると、モデルの障害が「主張が弱い」に化けてしまうため。

判定は 3 分岐 (§7):

  >= 0.75  validated  そのまま採用
  >= 0.50  uncertain  `requires_human_review: true` を付けて**登録は継続**
  <  0.50  rejected   rejection_log (§3.3) へ記録

**rejected でもサイドカーからは消さない**。§3.2 の validation.status に
"rejected" があるのは、何を落としたかを後から追えるようにするため
(normalize が捨てたタグを報告するのと同じ思想)。地図の側 —
KB 登録に当たる claim_refs — からは外す。

安全側の規則を 1 つ足してある: **ontology だけしか走らなかった対象は
validated にしない**。決定的な整合性検査は「オントロジーとして矛盾しない」
としか言っておらず、主張が正しいことの裏付けではないため。offline で
LLM 検証器が居ない run が黙って「検証済み」を量産するのを防ぐ。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, TypedDict

import networkx as nx

from cc_core.editing import normalize_label
from cc_core.layers import verifier_id as llm_verifier_id
from cc_core.logging_util import get_logger
from cc_core.mcp_client import extract_json

logger = get_logger("cc_core.verifiers")

RunFn = Callable[[str], str]
"""プロンプトを投げて本文を受け取る関数。テストはここをモックに差し替える。"""


# ------------------------------------------------------------ 定数 (§7)

# verifier_id は 3 種。provenance.validator_ids に載るのは**実際に走った**もの
# だけ (layers.apply_meta と同じ約束)。
ONTOLOGY_VERIFIER_ID = "ontology-rules"
NLI_VERIFIER_PREFIX = "llm-nli:"
LOCAL_NLI_VERIFIER_ID = "local-nli:mdeberta-xnli"

# 合成の重み (§7)。キーは §3.2 の validation.scores と同じ語彙
WEIGHTS: dict[str, float] = {"nli": 0.40, "llm": 0.35, "ontology": 0.25}

# 判定の閾値 (§7)。layers.CORROBORATION_THRESHOLD と同じ 0.75 が validated
VALIDATED_THRESHOLD = 0.75
UNCERTAIN_THRESHOLD = 0.50

STATUS_VALIDATED = "validated"
STATUS_UNCERTAIN = "uncertain"
STATUS_REJECTED = "rejected"

# rejection_log (§3.3) の kind
KIND_CLAIM = "claim"
KIND_CAUSAL_EDGE = "causal_edge"
KIND_REFUTES = "refutes"

LOGS_DIR = "logs"
REJECTIONS_SUBDIR = "rejections"

# NLI ラベルと、それを 0〜1 のスコアへ落とすときの基準値。
# confidence は「基準値」と「情報なし (0.5)」の間を補間する重みとして使う —
# 自信の無い contradicts が 0.0 として効いてしまうのを避けるため。
NLI_LABELS: tuple[str, ...] = ("entails", "neutral", "contradicts")
NLI_BASE: dict[str, float] = {"entails": 1.0, "neutral": 0.4, "contradicts": 0.0}
NO_INFORMATION = 0.5
DEFAULT_CONFIDENCE = 0.5

MAX_PREMISE_CHARS = 900
MAX_HYPOTHESIS_CHARS = 300
NEIGHBOUR_SENTENCES = 3

# cc-verification は描画検証用の instructions と tools を持つ (裁定 E で流用)。
# 何も言わないと `verify_scene` を呼びに行くので、先に釘を刺す【実測 2026-08-07】。
#
# M7 (裁定 I) で instructions 側にも task 分岐を入れた — 入力 JSON に
# `"task": "nli" | "claim_check"` があればツールを呼ばず純 JSON で答える契約
# (agents_def.VERIFICATION_INSTRUCTIONS)。この 1 行は**契約が積まれていない
# 旧バージョンのエージェントに当たったときの保険**として残す。プロンプト側と
# instructions 側の二重掛けにしておくと、ensure_agents が新版を積む前の run
# でも verify_scene を呼びに行かない。
NO_TOOLS_NOTE = "ツールは呼ばず、以下のテキストだけを見て判断してください。\n"


def _payload(task: str, **body: Any) -> str:
    """cc-verification へ渡す入力 (裁定 I)。JSON 1 個だけを送る。

    形は cc-analysis の呼び出し (analysis._payload) と揃えてある。task 名で
    分岐させるので、検証器を足すときも instructions に節を 1 つ増やすだけで済む。
    """
    return (NO_TOOLS_NOTE
            + "次の JSON を処理し、JSON のみで応答してください。\n"
            + json.dumps({"task": task, **body}, ensure_ascii=False))


class VerifierError(RuntimeError):
    """検証器が結果を出せなかった。**「走らなかった」扱い**にして再正規化する。"""


class LocalNLIUnavailable(VerifierError):
    """CC_NLI_BACKEND=local を選んだが、ローカル NLI が使えない。"""


class VerifierResult(TypedDict):
    """1 つの検証器の返り値 (§7)。"""

    label: str
    score: float
    verifier_id: str
    detail: str


class EntailmentVerifier(Protocol):
    """検証器の差し替え口 (裁定 A)。LLM でもローカルモデルでも同じ形で呼ぶ。"""

    verifier_id: str

    def check(self, premise: str, hypothesis: str) -> VerifierResult:
        ...


def _knob(name: str, default: int) -> int:
    """環境変数の上限を読む。壊れた値は既定へ倒す (上限が外れるより安全)。"""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("%s の値を数値化できないので既定 %d を使う", name, default)
        return default
    return value if value > 0 else default


def validate_max() -> int:
    """検証対象の上限 (§7 の CC_VALIDATE_MAX)。claims + causes 候補の合計。"""
    return _knob("CC_VALIDATE_MAX", 60)


def validate_max_calls() -> int:
    """検証段が使ってよい LLM 呼び出し数の上限。

    §7 の CC_VALIDATE_MAX=60 は「対象の上限」で、1 対象あたり NLI と独立 LLM の
    2 call が要る。上限いっぱいだと 120 call になり、受け入れ基準 5 の
    「30 call/run 以下」と噛み合わない。そこで**呼び出し数の側にも栓**を付けた。
    使い切った後の対象は決定的検証だけを受け、uncertain (要レビュー) になる —
    黙って validated にしないのが要点。

    既定 16 は裁定 G の逆算 (zone 8 + claims 2 + validate 16 + cgw 2 +
    refutes 1 = 29 ≤ 30)。1 対象 2 call なので 8 対象ぶんが LLM 検証を受ける。
    """
    return _knob("CC_VALIDATE_MAX_CALLS", 16)


def nli_verifier_id(model: str) -> str:
    """モデル名から NLI 検証器の ID を作る ("gpt-5.6-terra" -> "llm-nli:terra")。"""
    short = str(model or "").rsplit("-", 1)[-1] or str(model or "unknown")
    return f"{NLI_VERIFIER_PREFIX}{short}"


def _clamp(raw: Any, default: float = DEFAULT_CONFIDENCE) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return default


def _blend(base: float, confidence: float) -> float:
    """基準値と「情報なし (0.5)」を confidence で補間する。

    confidence=1.0 なら基準値そのもの、0.0 なら 0.5 (何も分からなかった)。
    """
    return round(NO_INFORMATION + (base - NO_INFORMATION) * _clamp(confidence), 4)


def _trim(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


# ------------------------------------------------------------ 検証器 3 種


class LLMNLIVerifier:
    """既定の NLI 検証器 (裁定 A) — cc-verification に含意関係を判定させる。

    抽出 (cc-extraction / cc-analysis = sol) とは**別モデル** (terra) を使う。
    同じモデルに自分の出力を確認させても独立した検証にならないため。
    """

    def __init__(self, run: RunFn, *, model: str = "unknown") -> None:
        self._run = run
        self.verifier_id = nli_verifier_id(model)

    def prompt(self, premise: str, hypothesis: str) -> str:
        return _payload("nli",
                        premise=_trim(premise, MAX_PREMISE_CHARS),
                        hypothesis=_trim(hypothesis, MAX_HYPOTHESIS_CHARS))

    def check(self, premise: str, hypothesis: str) -> VerifierResult:
        try:
            raw = extract_json(self._run(self.prompt(premise, hypothesis)))
        except Exception as exc:
            logger.warning("nli verifier error: %s", type(exc).__name__)
            raise VerifierError(f"NLI 検証器エラー ({type(exc).__name__})") from exc
        return self.repair(raw)

    def repair(self, raw: Any) -> VerifierResult:
        """LLM の返り値を VerifierResult へ直す (analysis.py と同じ思想)。

        **未知ラベルと「別の契約で返ってきた」を区別する**のが要点:

          - `label` はあるが語彙外 ("たぶん含意")  -> neutral へ丸める。
            問いには答えているので、判定として合成に入れる
          - `label` キーが無い                     -> `VerifierError`。
            別の質問に答えている (= 検証器として走っていない) ので、
            再正規化で他の検証器へ重みを寄せる

        後者を neutral (0.45) として扱うと、エージェントの結線ミスが「主張が
        弱い」に化けて静かに全件を rejected 側へ押し下げる。実測でこれが起きた:
        cc-verification は instructions で verify_scene の呼び出しと
        `{"verdict": "PASS"|"FAIL"}` を義務づけられており、NLI の問いにも
        その形で答えた【実測 2026-08-07】。
        """
        if not isinstance(raw, dict):
            raise VerifierError("NLI 検証器が JSON オブジェクトを返さなかった")
        if "label" not in raw:
            raise VerifierError(
                "NLI 検証器が label を返さなかった (別の契約で応答した可能性)")
        label = str(raw.get("label") or "").strip().lower()
        detail = _trim(raw.get("rationale") or raw.get("detail"), 80)
        if label not in NLI_LABELS:
            label = "neutral"
            detail = (detail + " / 未知ラベルを neutral へ丸め").strip()
        return {"label": label,
                "score": _blend(NLI_BASE[label], _clamp(raw.get("score"))),
                "verifier_id": self.verifier_id, "detail": detail}


class LocalNLIVerifier:
    """ローカル NLI (mDeBERTa-xnli) — **M5 ではスタブ** (裁定 A)。

    `CC_NLI_BACKEND=local` で選ばれる。torch / transformers は extra `[nli]`
    でのみ入るので、本体の依存は増やさない (関数内 import)。実装が入るまでは
    「何を入れれば動くか」を伝えて `LocalNLIUnavailable` を送出する — 黙って
    LLM へ落ちると、閉域で動かしたつもりが外に出ていた、が起こりうるため。
    """

    verifier_id = LOCAL_NLI_VERIFIER_ID
    model_name = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"

    INSTALL_HINT = (
        "ローカル NLI には追加の依存が要ります: "
        "`pip install -e '.[nli]'` (torch + transformers) を実行し、"
        f"モデル {model_name} を取得してください。"
    )
    NOT_IMPLEMENTED = (
        "ローカル NLI backend は R2a M5 時点ではスタブです "
        "(インタフェースのみ確定)。CC_NLI_BACKEND を外して LLM NLI "
        "(cc-verification) をお使いください。"
    )

    def __init__(self) -> None:
        try:                                   # 関数内 import (本体依存を増やさない)
            import transformers  # noqa: F401
        except ImportError as exc:
            raise LocalNLIUnavailable(self.INSTALL_HINT) from exc
        raise LocalNLIUnavailable(self.NOT_IMPLEMENTED)

    def check(self, premise: str, hypothesis: str) -> VerifierResult:  # pragma: no cover
        raise LocalNLIUnavailable(self.NOT_IMPLEMENTED)


class LLMClaimVerifier:
    """独立 LLM 評価 (§7) — `validate_causal_edge` の verifier 呼び出しの流用。

    裁定 7 の 3 点目「別モデルによる独立判定」を、因果エッジだけでなく主張にも
    当てる。問いを NLI とずらしてある (含意ではなく「根拠に明示されているか」)
    ので、2 つの検証器が同じ誤りを共有しにくい。
    """

    def __init__(self, run: RunFn, *, model: str = "unknown") -> None:
        self._run = run
        self.verifier_id = llm_verifier_id(model)

    def prompt(self, premise: str, hypothesis: str) -> str:
        return _payload("claim_check",
                        claim=_trim(hypothesis, MAX_HYPOTHESIS_CHARS),
                        evidence=_trim(premise, MAX_PREMISE_CHARS))

    def check(self, premise: str, hypothesis: str) -> VerifierResult:
        try:
            raw = extract_json(self._run(self.prompt(premise, hypothesis)))
        except Exception as exc:
            logger.warning("llm verifier error: %s", type(exc).__name__)
            raise VerifierError(f"独立 LLM 検証器エラー ({type(exc).__name__})") from exc
        if not isinstance(raw, dict):
            raise VerifierError("独立 LLM 検証器が JSON オブジェクトを返さなかった")
        if "supported" not in raw:
            # `bool(raw.get("supported"))` だと「答えていない」が「支持しない」に
            # なり、全件を静かに rejected へ押し下げる (LLMNLIVerifier.repair 参照)
            raise VerifierError(
                "独立 LLM 検証器が supported を返さなかった (別の契約で応答した可能性)")
        supported = bool(raw.get("supported"))
        # 裁定 I の契約は {"supported", "score", "rationale"}。M6 までの
        # {"confidence", "why"} でも読めるようにしておく (どちらも同じ意味)
        confidence = raw.get("score")
        if confidence is None:
            confidence = raw.get("confidence")
        return {"label": "supported" if supported else "unsupported",
                "score": _blend(1.0 if supported else 0.0, _clamp(confidence)),
                "verifier_id": self.verifier_id,
                "detail": _trim(raw.get("rationale") or raw.get("why")
                                or raw.get("detail"), 80)}


class OntologyChecker:
    """決定的なオントロジー整合性検査 (裁定 B / §7)。**LLM を呼ばない**。

    入力は layers サイドカーの `ontology.relations` (is_a / part_of の候補)。
    規則は 3 つだけで、OWL 推論はしない:

      1. is_a に循環がある            -> 0.0  (分類が閉じており、上下が決まらない)
      2. 同じ対に is_a と part_of 併存 -> 0.0  (「一種」と「一部」は両立しない)
      3. is_a 兄弟の間の causes        -> 0.5  (同じ親を持つ概念どうしの因果は
                                               並置の誤読が多い。警告に留める)
      4. 該当なし                     -> 1.0

    3 が 0.0 でなく 0.5 なのは、兄弟間の因果が**あり得ない**わけではないため。
    警告として重み 0.25 ぶんだけ combined を押し下げる。
    """

    verifier_id = ONTOLOGY_VERIFIER_ID

    def __init__(self, relations: Iterable[Mapping[str, Any]] = ()) -> None:
        self.pairs: dict[tuple[str, str], set[str]] = {}
        self.parents: dict[str, set[str]] = {}
        graph = nx.DiGraph()
        for relation in relations or ():
            if not isinstance(relation, Mapping):
                continue
            src = normalize_label(relation.get("from"))
            dst = normalize_label(relation.get("to"))
            tag = str(relation.get("relation") or "").strip().replace("-", "_").lower()
            if not src or not dst or tag not in ("is_a", "part_of"):
                continue
            self.pairs.setdefault((src, dst), set()).add(tag)
            if tag == "is_a":
                self.parents.setdefault(src, set()).add(dst)
                graph.add_edge(src, dst)
        # 循環に居るノード。simple_cycles は列挙が高くつきうるので 1 回だけ回す
        cycled: set[str] = set()
        try:
            for cycle in nx.simple_cycles(graph):
                cycled.update(cycle)
        except Exception as exc:                     # pragma: no cover - 保険
            logger.warning("is_a cycle scan failed: %s", type(exc).__name__)
        self.cycles: frozenset[str] = frozenset(cycled)

    def _result(self, label: str, score: float, detail: str) -> VerifierResult:
        return {"label": label, "score": score,
                "verifier_id": self.verifier_id, "detail": detail}

    def siblings(self, a: str, b: str) -> bool:
        """同じ親 (is_a の上位) を共有するか。"""
        if a == b:
            return False
        return bool(self.parents.get(a, set()) & self.parents.get(b, set()))

    def check_relation(self, src: Any, dst: Any,
                       relation: str = "causes") -> VerifierResult:
        """概念の対と関係名を受けて 3 規則を当てる。"""
        a, b = normalize_label(src), normalize_label(dst)
        if a in self.cycles or b in self.cycles:
            return self._result("inconsistent", 0.0,
                                "is_a に循環があり上下関係が決まらない")
        for pair in ((a, b), (b, a)):
            tags = self.pairs.get(pair, set())
            if {"is_a", "part_of"} <= tags:
                return self._result("inconsistent", 0.0,
                                    "同じ対に is_a と part_of が併存している")
        if relation == "causes" and self.siblings(a, b):
            return self._result("warning", 0.5,
                                "is_a の兄弟どうしの因果 (並置の誤読に注意)")
        return self._result("consistent", 1.0, "オントロジー規則に抵触しない")

    def check(self, premise: str, hypothesis: str) -> VerifierResult:
        """Protocol 適合の入口。premise/hypothesis を関係の両端として読む。"""
        return self.check_relation(premise, hypothesis, "causes")


def make_nli_verifier(run: RunFn, *, model: str = "unknown",
                      backend: str | None = None,
                      notes: list[str] | None = None) -> EntailmentVerifier:
    """CC_NLI_BACKEND を見て NLI 検証器を選ぶ (裁定 A)。

    local を選んでスタブに当たった場合は**警告して LLM へ落とす** — 環境変数の
    綴り間違いで run 全体を止めない。落ちた事実は notes に残す (黙って別の
    検証器を使わない)。
    """
    choice = str(backend if backend is not None
                 else os.environ.get("CC_NLI_BACKEND", "")).strip().lower()
    if choice == "local":
        try:
            return LocalNLIVerifier()
        except LocalNLIUnavailable as exc:
            logger.warning("local NLI unavailable: %s", exc)
            if notes is not None:
                notes.append(f"CC_NLI_BACKEND=local は使えないので LLM NLI を使う: {exc}")
    return LLMNLIVerifier(run, model=model)


# ------------------------------------------------------------ 合成と判定


def combine(scores: Mapping[str, float]) -> float | None:
    """重み付き平均。**走れた検証器だけ**で再正規化する (§7)。

    1 つも走らなかった場合は None (= 0.0 ではない)。0.0 を返すと
    「検証して否定された」と読めてしまい、rejected 扱いになるため。
    """
    total = 0.0
    weighted = 0.0
    for key, value in scores.items():
        weight = WEIGHTS.get(key)
        if weight is None:
            continue
        total += weight
        weighted += weight * _clamp(value, 0.0)
    if total <= 0.0:
        return None
    return round(weighted / total, 4)


def judge(scores: Mapping[str, float], *,
          force_review: bool = False) -> dict[str, Any]:
    """スコア束から §3.2 の validation レコードを作る。

    安全側の 2 規則:
      - 検証器が 1 つも走らなかった -> uncertain (要レビュー)。**rejected に
        しない** — 検証できなかったことと否定されたことは違う
      - 決定的な ontology だけしか走らなかった -> validated にはしない。
        整合性は正しさの裏付けではない
    """
    combined = combine(scores)
    kinds = {k for k in scores if k in WEIGHTS}
    if combined is None:
        return {"status": STATUS_UNCERTAIN, "combined": None,
                "scores": {}, "requires_human_review": True}
    if combined >= VALIDATED_THRESHOLD:
        status = STATUS_VALIDATED
    elif combined >= UNCERTAIN_THRESHOLD:
        status = STATUS_UNCERTAIN
    else:
        status = STATUS_REJECTED
    if status == STATUS_VALIDATED and kinds <= {"ontology"}:
        status = STATUS_UNCERTAIN                # 決定的検査だけでは validated にしない
        force_review = True
    return {"status": status, "combined": combined,
            "scores": {k: round(_clamp(v, 0.0), 4) for k, v in scores.items()
                       if k in WEIGHTS},
            "requires_human_review": bool(
                force_review or status == STATUS_UNCERTAIN)}


# ------------------------------------------------------------ rejection_log


def rejection_path(session: str, *, logs_dir: str | Path = LOGS_DIR) -> Path:
    """`logs/rejections/rejections_{session}.jsonl` (§3.3)。"""
    return Path(logs_dir) / REJECTIONS_SUBDIR / f"rejections_{session}.jsonl"


def log_rejection(session: str, *, kind: str, target_id: str, text: str,
                  validation: Mapping[str, Any],
                  verdicts: Sequence[VerifierResult] = (),
                  evidence_span: Sequence[Any] = (),
                  reason: str = "",
                  logs_dir: str | Path = LOGS_DIR,
                  timestamp: str | None = None) -> Path | None:
    """§3.3 の 1 行を追記する。**書けなくても本処理は止めない**。

    地図そのものは作れているのに、ログが書けないだけで生成を落とすのは割に
    合わない (layers サイドカーの save と同じ方針)。
    """
    row = {
        "ts": timestamp or dt.datetime.now().isoformat(timespec="seconds"),
        "session": session,
        "kind": kind,
        "target_id": target_id,
        "text": _trim(text, MAX_HYPOTHESIS_CHARS),
        "scores": dict(validation.get("scores") or {}),
        "combined": validation.get("combined"),
        "verdicts": [dict(v) for v in verdicts or ()],
        "evidence_span": list(evidence_span or ()),
        "reason": reason,
    }
    file = rejection_path(session, logs_dir=logs_dir)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("rejection not logged: %s", type(exc).__name__)
        return None
    return file


# ------------------------------------------------------------ 検証の実行


@dataclass
class ValidationReport:
    """何を何件検証し、どう判定したか。summary["validation"] にそのまま載る。"""

    llm_calls: int = 0
    targets: int = 0
    claims: dict[str, int] = field(default_factory=dict)
    edges: dict[str, int] = field(default_factory=dict)
    verifier_ids: list[str] = field(default_factory=list)
    rejections: int = 0
    log_path: str | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def count(self, bucket: str, status: str) -> None:
        target = self.claims if bucket == "claims" else self.edges
        target[status] = target.get(status, 0) + 1

    def used(self, verifier_id: str) -> None:
        if verifier_id and verifier_id not in self.verifier_ids:
            self.verifier_ids.append(verifier_id)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "llm_calls": self.llm_calls, "targets": self.targets,
            "claims": dict(self.claims), "edges": dict(self.edges),
            "verifier_ids": list(self.verifier_ids),
            "rejections": self.rejections,
        }
        if self.log_path:
            out["rejection_log"] = self.log_path
        if self.notes:
            out["notes"] = list(self.notes)
        if self.errors:
            out["errors"] = list(self.errors)
        return out


def causal_candidates(kg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """検証対象の causes 候補エッジ (§7「全エッジではない」)。

    ④relate を通った後の姿で見る:
      - glyph = arrow             3 点セットを通った因果
      - glyph = wave + demoted_from=arrow   語彙証拠はあるが降格した候補

    layer_assign._initial_tags が layer_C に causes を置く条件と同じにしてある
    (投影の規則④/⑩ と判断がずれないため)。
    """
    out: list[dict[str, Any]] = []
    for edge in kg.get("edges", []) or ():
        if not isinstance(edge, dict):
            continue
        glyph = str(edge.get("glyph") or "")
        check = edge.get("causal_check") or {}
        if glyph == "arrow" or (glyph == "wave"
                                and isinstance(check, dict)
                                and check.get("demoted_from") == "arrow"):
            out.append(edge)
    return out


class _SentenceIndex:
    """zones を「文 ID -> 本文」「概念ラベル -> 近傍文」で引くための索引。"""

    def __init__(self, zones: Iterable[Mapping[str, Any]] = ()) -> None:
        self.text: dict[str, str] = {}
        self.ordered: list[str] = []
        for zone in zones or ():
            if not isinstance(zone, Mapping):
                continue
            sid = str(zone.get("sentence_id") or "")
            body = _trim(zone.get("text"), MAX_PREMISE_CHARS)
            if not body:
                continue
            if sid:
                self.text[sid] = body
            self.ordered.append(body)

    def premise_of(self, sentence_ids: Iterable[Any]) -> str:
        parts = [self.text[str(s)] for s in sentence_ids or ()
                 if str(s) in self.text]
        return _trim(" ".join(parts), MAX_PREMISE_CHARS)

    def neighbours(self, *labels: Any) -> str:
        """根拠スパンが無い対象の premise。ラベルを含む文を出現順に拾う (§7)。"""
        wanted = [str(v).strip() for v in labels if str(v or "").strip()]
        if not wanted:
            return ""
        hits = [body for body in self.ordered
                if any(word in body for word in wanted)]
        return _trim(" ".join(hits[:NEIGHBOUR_SENTENCES]), MAX_PREMISE_CHARS)


def _edge_premise(edge: Mapping[str, Any]) -> str:
    parts = [str(s["surface"]) for s in edge.get("evidence_span") or ()
             if isinstance(s, Mapping) and s.get("surface")]
    return _trim(" ".join(parts), MAX_PREMISE_CHARS)


def _edge_hypothesis(edge: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    src = labels.get(str(edge.get("from")), str(edge.get("from") or ""))
    dst = labels.get(str(edge.get("to")), str(edge.get("to") or ""))
    label = str(edge.get("label") or "").strip()
    core = f"{src} は {dst} を引き起こす"
    return _trim(f"{core} ({label})" if label else core, MAX_HYPOTHESIS_CHARS)


def _run_verifiers(
    premise: str, hypothesis: str, *,
    nli: EntailmentVerifier | None,
    llm: EntailmentVerifier | None,
    ontology_result: VerifierResult | None,
    report: ValidationReport,
    budget: list[int],
) -> tuple[dict[str, float], list[VerifierResult]]:
    """走れる検証器を走らせ、(スコア束, 記録) を返す。

    落ちた検証器は**スコア束に入れない** — combine が残りで再正規化する。
    budget はリスト 1 要素の可変カウンタ (残り LLM 呼び出し数)。
    """
    scores: dict[str, float] = {}
    verdicts: list[VerifierResult] = []
    if ontology_result is not None:
        scores["ontology"] = ontology_result["score"]
        verdicts.append(ontology_result)
        report.used(ontology_result["verifier_id"])

    if not premise:
        return scores, verdicts                # 根拠が無ければ LLM に問う材料がない

    for key, verifier in (("nli", nli), ("llm", llm)):
        if verifier is None:
            continue
        if budget[0] <= 0:
            note = ("LLM 検証の呼び出し上限に達したため、以降の対象は決定的検証"
                    "のみで判定した (CC_VALIDATE_MAX_CALLS)")
            if note not in report.notes:
                report.notes.append(note)
            break
        try:
            result = verifier.check(premise, hypothesis)
        except VerifierError as exc:
            report.errors.append(str(exc))
            continue
        except Exception as exc:               # 想定外も「走らなかった」に倒す
            report.errors.append(f"{key}: {type(exc).__name__}")
            continue
        finally:
            budget[0] -= 1
            report.llm_calls += 1
        scores[key] = result["score"]
        verdicts.append(result)
        report.used(result["verifier_id"])
    return scores, verdicts


def run_validation(
    kg: dict[str, Any],
    claims: Sequence[dict[str, Any]],
    *,
    zones: Sequence[Mapping[str, Any]] = (),
    nli: EntailmentVerifier | None = None,
    llm: EntailmentVerifier | None = None,
    ontology: OntologyChecker | None = None,
    session: str = "",
    limit: int | None = None,
    max_calls: int | None = None,
    logs_dir: str | Path = LOGS_DIR,
    timestamp: str | None = None,
) -> tuple[dict[str, dict[str, Any]], ValidationReport]:
    """⑤validate — claims 全件 + causes 候補エッジを検証する (§7)。

    `claims` は **その場で書き換える** (サイドカーの要素に validation を足す)。
    エッジ側は書き換えず、`{edge_id: validation}` を返す — kg への刻印は
    layer_assign.apply_validation の仕事にして、層の刻印を 1 か所に集める。
    """
    report = ValidationReport()
    index = _SentenceIndex(zones)
    checker = ontology or OntologyChecker()
    budget = [max_calls if max_calls is not None else validate_max_calls()]
    cap = limit if limit is not None else validate_max()

    labels = {str(n.get("id")): str(n.get("label") or "")
              for n in kg.get("nodes", []) or () if isinstance(n, dict)}
    edges = causal_candidates(kg)

    claim_list = [c for c in claims or () if isinstance(c, dict)]
    total = len(claim_list) + len(edges)
    if total > cap:
        report.notes.append(
            f"検証対象が {total} 件あるため先頭 {cap} 件に絞った "
            f"(CC_VALIDATE_MAX={cap}、主張を優先)")
    claim_list = claim_list[:cap]
    edges = edges[:max(0, cap - len(claim_list))]
    report.targets = len(claim_list) + len(edges)

    # --- 主張 (§7: claims 全件) ---
    for claim in claim_list:
        assertion = claim.get("assertion") or {}
        text = str(assertion.get("claim_text") or "")
        spans = (claim.get("provenance") or {}).get("source_span") or ()
        premise = index.premise_of(spans)
        force = not premise
        if force:
            premise = index.neighbours(*(assertion.get("related_concepts") or ()))
        scores, verdicts = _run_verifiers(
            premise, text, nli=nli, llm=llm, ontology_result=None,
            report=report, budget=budget)
        validation = judge(scores, force_review=force)
        claim["validation"] = validation
        report.count("claims", validation["status"])
        if validation["status"] == STATUS_REJECTED:
            report.rejections += 1
            path = log_rejection(
                session, kind=KIND_CLAIM, target_id=str(claim.get("nanopub_id") or ""),
                text=text, validation=validation, verdicts=verdicts,
                evidence_span=list(spans), logs_dir=logs_dir, timestamp=timestamp,
                reason="検証スコアが 0.5 未満のため KB へ登録しない")
            if path is not None:
                report.log_path = str(path)

    # --- causes 候補エッジ (§7: 全エッジではない) ---
    results: dict[str, dict[str, Any]] = {}
    for edge in edges:
        hypothesis = _edge_hypothesis(edge, labels)
        premise = _edge_premise(edge)
        force = not premise
        if force:
            premise = index.neighbours(labels.get(str(edge.get("from"))),
                                       labels.get(str(edge.get("to"))))
        onto = checker.check_relation(labels.get(str(edge.get("from"))),
                                      labels.get(str(edge.get("to"))), "causes")
        scores, verdicts = _run_verifiers(
            premise, hypothesis, nli=nli, llm=llm, ontology_result=onto,
            report=report, budget=budget)
        validation = judge(scores, force_review=force)
        results[str(edge.get("id"))] = validation
        report.count("edges", validation["status"])
        if validation["status"] == STATUS_REJECTED:
            report.rejections += 1
            path = log_rejection(
                session, kind=KIND_CAUSAL_EDGE, target_id=str(edge.get("id") or ""),
                text=hypothesis, validation=validation, verdicts=verdicts,
                evidence_span=list(edge.get("evidence_span") or ()),
                logs_dir=logs_dir, timestamp=timestamp,
                reason="検証スコアが 0.5 未満のため因果の矢印にしない (相関のまま)")
            if path is not None:
                report.log_path = str(path)

    logger.info("validation done targets=%d claims=%s edges=%s calls=%d",
                report.targets, report.claims, report.edges, report.llm_calls)
    return results, report
