"""狭いウィンドウでの表示 (レスポンシブ)。

ユーザー報告の症状: 幅を詰めるとヘッダーの 3 カードが押し潰れ、
「モード: 個人モード」が 1 文字ずつ縦に折れ、アバターがセグメントに重なる。
原因は .hdr が 3 カードを 1 行に固定していて、カードの中身が min-content まで
潰されること (同じ潰れ方は body.sidebar-hidden のカラム定義でも一度起きている)。

固定するのは 2 種類:
  ① **状態機械** — サイドバーは狭幅で「重なるドロワー」になる。ここで
     ユーザー設定 (collapsed) を書き換えてしまうと、広い画面へ戻したときに
     勝手に閉じたままになる。app.js の純関数を Node から直接叩いて固定する
     (スクリーンショットでは往復の挙動を捕まえられないため)
  ② **CSS の契約** — 折り返しの段 (1180 / 960 / 700px) と、JS のブレークポイント
     が一致していること。片方だけ動かすとドロワーと下敷きがずれる
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "cc_web" / "static"
APP_JS = STATIC / "app.js"
APP_CSS = (STATIC / "app.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node が無い環境では JS を評価できない")

# 状態機械だけを Node から呼ぶ。app.js は素の IIFE なので、末尾の
# module.exports (ブラウザでは無効) 経由で 3 つの純関数だけが取り出せる。
SCENARIO_JS = r"""
const sb = require(process.argv[1]);
const wideOpen   = { collapsed: false, narrow: false, drawerOpen: false };
const wideClosed = { collapsed: true,  narrow: false, drawerOpen: false };

// 「開いたまま使っていた人」が狭幅へ入り、ドロワーを開閉して、広幅へ戻る
const enter   = sb.sidebarResize(wideOpen, true);
const opened  = sb.sidebarToggle(enter, false);
const closed  = sb.sidebarToggle(opened, true);
const back    = sb.sidebarResize(closed, false);

// 「閉じておく派」が狭幅でドロワーを開いたまま広幅へ戻る
const enter2  = sb.sidebarResize(wideClosed, true);
const opened2 = sb.sidebarToggle(enter2, false);
const back2   = sb.sidebarResize(opened2, false);

console.log(JSON.stringify({
  mq: sb.NARROW_MQ,
  view: {
    wideOpen: sb.sidebarView(wideOpen),
    wideClosed: sb.sidebarView(wideClosed),
    narrowClosed: sb.sidebarView(enter),
    narrowOpen: sb.sidebarView(opened),
  },
  states: { enter, opened, closed, back, enter2, opened2, back2 },
}));
"""


@pytest.fixture(scope="module")
def js() -> dict:
    proc = subprocess.run(["node", "-e", SCENARIO_JS, str(APP_JS)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ================================================== ① 状態機械 (app.js)


@needs_node
def test_wide_screen_follows_the_saved_preference(js) -> None:
    """広い画面では従来どおり: 設定がそのまま表示になる (下敷きは出ない)。"""
    assert js["view"]["wideOpen"] == {"hidden": False, "reopen": False,
                                      "scrim": False}
    assert js["view"]["wideClosed"] == {"hidden": True, "reopen": True,
                                        "scrim": False}


@needs_node
def test_narrow_screen_starts_closed_and_opens_as_a_drawer(js) -> None:
    """狭幅では閉じた状態から始まり、開くと下敷き付きのドロワーになる。"""
    assert js["view"]["narrowClosed"] == {"hidden": True, "reopen": True,
                                          "scrim": False}
    assert js["view"]["narrowOpen"] == {"hidden": False, "reopen": False,
                                        "scrim": True}


@needs_node
def test_narrow_toggling_never_touches_the_saved_preference(js) -> None:
    """狭幅の開閉は**一時的な状態**で、ユーザー設定を書き換えない。"""
    states = js["states"]
    assert states["enter"] == {"collapsed": False, "narrow": True,
                               "drawerOpen": False}
    assert states["opened"]["collapsed"] is False       # 開いても既定は不変
    assert states["closed"]["collapsed"] is False       # 閉じても既定は不変
    assert states["opened2"]["collapsed"] is True       # 「閉じておく派」も同じ


@needs_node
def test_widening_restores_the_users_own_setting(js) -> None:
    """広げ直したら**元の設定に戻る** (これが狭幅対応の一番の落とし穴)。"""
    states = js["states"]
    # 開いて使っていた人 -> 狭幅でドロワーを閉じた -> 広げ直す = 開いたまま
    assert states["back"] == {"collapsed": False, "narrow": False,
                              "drawerOpen": False}
    # 閉じておく派 -> 狭幅でドロワーを開いた -> 広げ直す = 閉じたまま
    assert states["back2"] == {"collapsed": True, "narrow": False,
                               "drawerOpen": False}


@needs_node
def test_the_js_breakpoint_matches_the_stylesheet(js) -> None:
    """JS のブレークポイントと CSS の段が同じであること。

    片方だけ動かすと「CSS はドロワー、JS は押し出し」のような半端な状態に
    なり、下敷きだけが残る/出ないといった見た目の壊れ方をする。
    """
    assert js["mq"] == "(max-width:960px)"
    assert "@media (max-width:960px)" in APP_CSS


# ==================================================== ② CSS / HTML の契約


def test_template_grid_reflows_without_extra_breakpoints() -> None:
    """テンプレートは列数を決め打ちしない (広い画面では従来どおり 4 列)。"""
    assert "repeat(auto-fit,minmax(220px,1fr))" in APP_CSS
    assert "repeat(4,minmax(0,1fr))" not in APP_CSS


def test_header_cards_wrap_before_they_are_crushed() -> None:
    """1180px 以下でヘッダーを折り返す + カードに潰れの下限を与える。"""
    block = APP_CSS.split("@media (max-width:1180px)")[1].split("}\n}")[0]
    assert ".hdr { flex-wrap:wrap; }" in block
    assert "flex:1 1 260px" in block and "min-width:240px" in block


def test_narrow_screen_turns_the_sidebar_into_an_overlay_drawer() -> None:
    """960px 以下ではサイドバーを重ねる (押し出すと本文がまた潰れる)。"""
    block = APP_CSS.split("@media (max-width:960px)")[1].split("\n}\n\n")[0]
    assert ".hdr-sub { display:none; }" in block
    assert "body:not(.sidebar-hidden) { grid-template-columns:minmax(0,1fr); }" in block
    assert "position:fixed" in block and "width:250px" in block
    assert "z-index:55" in block


def test_the_scrim_exists_and_reuses_the_modal_overlay_colour() -> None:
    """下敷きは既存トークンの流用 (新しい色を足さない)。"""
    assert '<div id="drawer-scrim" hidden></div>' in INDEX_HTML
    assert ("#drawer-scrim { position:fixed; inset:0; z-index:54; "
            "background:rgba(28,25,58,.28); }") in APP_CSS
    assert "#drawer-scrim[hidden] { display:none; }" in APP_CSS
    # モーダルの下敷きと同じ色・ドロワー (55) より下・ヘッダーより上
    assert APP_CSS.count("rgba(28,25,58,.28)") == 2


def test_very_narrow_screen_stacks_the_header_cards() -> None:
    """700px 以下は 1 列。見出しも viewport 追従にして横スクロールを出さない。"""
    block = APP_CSS.split("@media (max-width:700px)")[1].split("\n}")[0]
    assert "flex-basis:100%" in block
    assert "font-size:clamp(22px,6vw,30px)" in block
    assert "padding-left:16px" in block and "padding-right:16px" in block


def test_the_reopen_button_still_gets_out_of_the_header() -> None:
    """既存の逃がし (再展開ボタンとヘッダーの重なり回避) を消していない。"""
    assert "body.sidebar-hidden .hdr { padding-left:48px; }" in APP_CSS
