"""ポータル完結エージェント `cc-cartographer-portal` の定義と登録。

Foundry ポータル (Playground) から単独で使えるよう、外部ツールに依存しない構成:
  Work IQ MCP (OneDrive/SharePoint/Copilot)  … 資料収集      = Foundry 内
  code_interpreter                            … 座標計算と作図 = Foundry 内
  → .excalidraw ファイルを添付として返す

CLI 版 (cc_orchestrator.chat) との違い:
  CLI 版は VM-Excalidraw-MCP へライブ描画する。ポータル版は VM が private で
  Foundry から到達できないため、同じ規則で .excalidraw を生成して渡す。
  レイアウト規則・glyph 配色・ギャップ表現は cc_core と同一に保つ。

登録:  python -m cc_orchestrator.portal_agent
"""

from __future__ import annotations

from pathlib import Path

from cc_orchestrator.agents_def import WORKIQ_TOOLS
from cc_orchestrator.foundry_v2 import FoundryAgentsV2

AGENT_NAME = "cc-cartographer-portal"
MODEL = "gpt-5.6-sol"

# code_interpreter に貼り付けさせる決定的スクリプト (cc_core.layout +
# cc_core.excalidraw_file の規則をポータブルにまとめたもの)
BUILDER_SCRIPT = r'''
import json, math, zlib
NODE_W, CELL_W, CELL_H, PAD, GAP_X, GAP_Y, OX, OY = 180, 260, 140, 60, 120, 120, 60, 80
HR, GAP_OP, FONT = 0.55, 40, 1
G = {
 "arrow":  ("#c92a2a","solid",2,"arrow",100,""),
 "wave":   ("#1971c2","dotted",2,None,100,"〜 "),
 "zigzag": ("#e8590c","solid",2,"bar",100,"⚡ "),
 "double": ("#2f9e44","solid",3,"triangle",100,"⇒ "),
 "hole":   ("#868e96","dashed",2,"dot",GAP_OP,"? "),
}
def seed(k): return zlib.crc32(k.encode()) % 2000000000
def base(i,t,x,y,w,h,**o):
    e={"id":i,"type":t,"x":x,"y":y,"width":w,"height":h,"angle":0,"strokeColor":"#1e1e1e",
       "backgroundColor":"transparent","fillStyle":"solid","strokeWidth":1,"strokeStyle":"solid",
       "roughness":2,"opacity":100,"groupIds":[],"frameId":None,"roundness":None,"seed":seed(i),
       "version":1,"versionNonce":seed(i+"n"),"isDeleted":False,"boundElements":[],"updated":1,
       "link":None,"locked":False}
    e.update(o); return e
def text(i,x,y,s,size=16,color="#1e1e1e",op=100,cont=None):
    e=base(i,"text",x,y,max(20,len(s)*size*0.62),size*1.25,strokeColor=color,opacity=op)
    e.update({"text":s,"originalText":s,"fontSize":size,"fontFamily":FONT,
              "textAlign":"center" if cont else "left","verticalAlign":"middle" if cont else "top",
              "containerId":cont,"lineHeight":1.25,"baseline":size,"autoResize":True})
    return e
def layout(kg, detail="standard"):
    groups={}
    for n in kg["nodes"]: groups.setdefault(n.get("community_id","comm_default"),[]).append(n)
    comms={c["id"]:c for c in kg.get("communities",[])}
    per_row=max(1,math.ceil(math.sqrt(len(groups)))); cx,cy,rh=OX,OY,0
    nodes,islands=[],[]
    for idx,(cid,mem) in enumerate(groups.items()):
        if idx and idx%per_row==0: cx,cy,rh=OX,cy+rh+GAP_Y,0
        cols=max(1,math.ceil(math.sqrt(len(mem)))); rows=math.ceil(len(mem)/cols)
        w,h=2*PAD+cols*CELL_W,2*PAD+rows*CELL_H; rh=max(rh,h)
        for j,n in enumerate(mem):
            nodes.append({"id":n["id"],"label":n["label"],"x":cx+PAD+(j%cols)*CELL_W,
                          "y":cy+PAD+(j//cols)*CELL_H,"size":NODE_W,"community_id":cid,
                          "style":{"rough":True}})
        m=comms.get(cid,{})
        islands.append({"community_id":cid,"name":m.get("name",cid),
                        "bbox":[cx,cy,cx+w,cy+h],"is_gap":bool(m.get("is_gap",False))})
        cx+=w+GAP_X
    edges=[{"id":e.get("id") or f"r{i+1:03d}","from":e["from"],"to":e["to"],
            "label":e.get("label",""),"glyph":e["glyph"] if e.get("glyph") in G else "arrow"}
           for i,e in enumerate(kg.get("edges",[]))]
    return {"detail_level":detail,"nodes":nodes,"edges":edges,"islands":islands,
            "provenance":{"graph_version":kg.get("graph_version","kg"),
                          "generated_for":"foundry_portal"}}
def validate(p):
    err=[]; ids=[n["id"] for n in p["nodes"]]
    if len(ids)!=len(set(ids)): err.append("duplicate node id")
    s=set(ids); isl={i["community_id"] for i in p["islands"]}
    for e in p["edges"]:
        for k in ("from","to"):
            if e[k] not in s: err.append(f"edge {e['id']}: {k}={e[k]} missing")
        if e["from"]==e["to"]: err.append(f"edge {e['id']}: self-loop")
    for n in p["nodes"]:
        if n["community_id"] not in isl: err.append(f"node {n['id']}: no island")
    return err
def build(p):
    els=[]; gaps={i["community_id"] for i in p["islands"] if i.get("is_gap")}
    for i in p["islands"]:
        x0,y0,x1,y1=i["bbox"]; g=i.get("is_gap"); c="#868e96" if g else "#495057"
        op=GAP_OP if g else 100
        els.append(base(f"isl-{i['community_id']}","rectangle",x0,y0,x1-x0,y1-y0,
                        strokeColor=c,strokeStyle="dashed" if g else "solid",opacity=op))
        els.append(text(f"isl-{i['community_id']}-label",x0+10,y0+8,
                        ("❓ " if g else "")+i["name"],16,c,op))
    for n in p["nodes"]:
        ing=n["community_id"] in gaps; nid=f"node-{n['id']}"; tid=nid+"-text"
        w=n["size"]; h=max(60,n["size"]*HR)
        els.append(base(nid,"ellipse",n["x"],n["y"],w,h,backgroundColor="#fff9db",
                        strokeStyle="dashed" if ing else "solid",
                        opacity=GAP_OP if ing else 100,
                        boundElements=[{"id":tid,"type":"text"}]))
        t=text(tid,n["x"]+8,n["y"]+h/2-10,n["label"],14,"#1e1e1e",GAP_OP if ing else 100,nid)
        t["width"]=w-16; els.append(t)
    ctr={n["id"]:(n["x"]+n["size"]/2,n["y"]+max(60,n["size"]*HR)/2) for n in p["nodes"]}
    by={e["id"]:e for e in els}
    for e in p["edges"]:
        col,ss,sw,ah,op,pre=G[e["glyph"]]; eid=f"edge-{e['id']}"
        sx,sy=ctr[e["from"]]; ex,ey=ctr[e["to"]]
        a=base(eid,"arrow",sx,sy,ex-sx,ey-sy,strokeColor=col,strokeStyle=ss,strokeWidth=sw,
               opacity=op,roundness={"type":2})
        a.update({"points":[[0,0],[ex-sx,ey-sy]],"lastCommittedPoint":None,
                  "startBinding":{"elementId":f"node-{e['from']}","focus":0,"gap":4},
                  "endBinding":{"elementId":f"node-{e['to']}","focus":0,"gap":4},
                  "startArrowhead":None,"endArrowhead":ah,"elbowed":False})
        lab=(pre+e.get("label","")).strip()
        if lab: a["boundElements"]=[{"id":eid+"-text","type":"text"}]
        els.append(a)
        if lab: els.append(text(eid+"-text",(sx+ex)/2,(sy+ey)/2,lab,12,col,op,eid))
        for ep in (e["from"],e["to"]):
            if f"node-{ep}" in by: by[f"node-{ep}"]["boundElements"].append({"id":eid,"type":"arrow"})
    return {"type":"excalidraw","version":2,"source":"concept-cartographer",
            "elements":els,"appState":{"viewBackgroundColor":"#ffffff","gridSize":None},"files":{}}
'''

INSTRUCTIONS = f"""\
あなたは Concept Cartographer のポータル版エージェントです。研究者の M365 データから
今週(等)の研究資料を集め、Novak 流概念地図を .excalidraw ファイルとして生成します。

# 手順
## 1. 資料収集 (Work IQ)
依頼文の期間 (今週/先週/今月/直近N日) の研究関連ファイルを探して内容を読む。
- `copilot_chat` (WorkIQCopilot): M365 全体の意味検索・内容要約に最も有効。まずこれを使う。
- OneDrive: `findFileOrFolderInMyDrive` / `getFolderChildrenInMyOnedrive` /
  `readSmallTextFileFromMyOnedrive`
- SharePoint: `findSite` / `findFileOrFolder` / `readSmallTextFile`
会議メモ・予算資料・事務書類など研究内容でないものは除外する。
資料が 1 件も見つからなければ、その旨だけを報告して終了する。

## 2. 概念抽出
集めた内容から knowledge_graph を組み立てる (自分の頭の中で作り、次の手順で使う):
{{"graph_version":"kg_<短いID>",
  "nodes":[{{"id":"c001","label":"<概念名 25字以内>","community_id":"comm_001"}}],
  "edges":[{{"id":"r001","from":"c001","to":"c002","label":"<関係 20字以内>","glyph":"arrow"}}],
  "communities":[{{"id":"comm_001","name":"<テーマ名>","is_gap":false}}]}}
- glyph: arrow=因果 (機序・介入・反事実の語彙証拠がある場合のみ) / wave=相関・関連 /
  zigzag=矛盾・対立 / double=補強・支持・具体例 / hole=情報不足のギャップ候補
- 因果の語彙証拠がなければ wave にする。相関を因果へ昇格させない
- 言及が薄い・未検証のテーマは is_gap:true のコミュニティにまとめ、そこへの関係は hole
- コミュニティ 3〜7 個、ノード 8〜20 個。資料にない概念を創作しない
- id は c001../r001.. の連番。edges の from/to は必ず存在する node id
- 資料横断で同じ概念は 1 ノードに統合。ラベルは日本語で簡潔に

## 3. 作図 (code_interpreter)
**必ず** code_interpreter を使い、次のスクリプトを**一字一句そのまま**貼り付けてから、
末尾に kg 変数と実行部を足して .excalidraw を生成する。座標を自分で計算してはいけない。

```python
{BUILDER_SCRIPT}
# --- ここから下を自分で書く ---
kg = {{ ...手順2で作った knowledge_graph... }}
plan = layout(kg)
errs = validate(plan)
assert not errs, errs
scene = build(plan)
with open("concept_map.excalidraw", "w", encoding="utf-8") as f:
    json.dump(scene, f, ensure_ascii=False, indent=2)
print("elements:", len(scene["elements"]), "nodes:", len(plan["nodes"]),
      "edges:", len(plan["edges"]), "islands:", len(plan["islands"]))
```

## 4. 報告
生成した `concept_map.excalidraw` を添付し、次を日本語で簡潔に伝える:
- 使った資料名の一覧
- 島 (テーマ) の一覧。ギャップ候補の島は「❓ギャップ候補」と明示
- 概念数 / 関係数 / 要素数
- 「ファイルを excalidraw.com または手元の Excalidraw で開いてください」

# 禁止
- 座標・bbox を自分で計算すること (必ず上のスクリプトに計算させる)
- code_interpreter を使わずに .excalidraw を手書きすること
- 資料の生文・個人情報・秘密情報を回答本文へ転記すること
- ギャップ候補を確定事項として述べること
"""


def register(client: FoundryAgentsV2 | None = None) -> str:
    client = client or FoundryAgentsV2()
    return client.ensure_agent(
        AGENT_NAME, MODEL, INSTRUCTIONS,
        tools=[*WORKIQ_TOOLS, {"type": "code_interpreter"}],
        effort="medium",
        description="ポータル完結版: Work IQ で資料収集 → code_interpreter で作図 → .excalidraw を添付",
        welcome="今週の研究を概念地図として整理します",
    )


if __name__ == "__main__":
    print("registered:", register())
    print("Foundry ポータル > エージェント >", AGENT_NAME, "を開いて")
    print('「今週の研究を概念地図として整理して」と入力してください')
    Path("docs").mkdir(exist_ok=True)
