"""Concept Cartographer のローカル Web アプリ (R1)。

`cc_orchestrator` / `cc_core` の実装済みパイプラインを、ブラウザから使える
ローカル UI として提供する層。ビジネスロジックはここに置かない — 地図の
生成・詳細度・ギャップ確定・評価はすべて既存モジュールを呼ぶだけにして、
CLI (cc_orchestrator.chat) と挙動が食い違わないようにする。

バインドは 127.0.0.1 のみ (引き継ぎメモ §4)。研究本文はサーバログへ出さない
(cc_core.logging_util の方針)。依頼文は閉域前提で logs/web_history.jsonl に
のみ残す。
"""

__all__ = ["create_app"]


def create_app():  # 遅延 import: FastAPI 未導入でも cc_web を import できるように
    from cc_web.app import create_app as _create_app

    return _create_app()
