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
import json, math, zlib, unicodedata
PAD, GAP_X, GAP_Y, OX, OY = 56, 120, 130, 60, 80
NODE_FONT, EDGE_FONT, USABLE, LINE_H = 14, 12, 0.58, 1.25
NW_MIN, NW_MAX, NH_MIN, COL_M, ROW_M = 170, 300, 66, 28, 34
EDGE_MAX_EM, GAP_OP, FONT = 8.0, 40, 1
PRE_EM = {"arrow":0.0,"wave":1.6,"zigzag":1.6,"double":1.6,"hole":1.6}
def dw(s):  # 表示幅 (全角=1, 半角=0.55)
    return sum(1.0 if unicodedata.east_asian_width(c) in "WFA" else 0.55 for c in s or "")
def trunc(s, m):
    if dw(s) <= m: return s
    o, w = "", 0.0
    for c in s:
        cw = 1.0 if unicodedata.east_asian_width(c) in "WFA" else 0.55
        if w + cw > m - 0.6: break
        o += c; w += cw
    return o + "…"
def nsize(label):  # ラベルが収まる楕円の寸法
    total = dw(label)
    lines = 1 if total <= 12 else 2
    tw, th = total/lines*NODE_FONT, lines*NODE_FONT*LINE_H
    return round(min(NW_MAX, max(NW_MIN, tw/USABLE+24))), round(max(NH_MIN, th/USABLE+18))
def elabel_px(label, glyph):
    return 0.0 if not label else (dw(trunc(label,EDGE_MAX_EM))+PRE_EM.get(glyph,0.0))*EDGE_FONT+10
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
    groups = {}
    for n in kg["nodes"]: groups.setdefault(n.get("community_id","comm_default"),[]).append(n)
    comms = {c["id"]: c for c in kg.get("communities",[])}
    edges = [{"id": e.get("id") or f"r{i+1:03d}", "from": e["from"], "to": e["to"],
              "label": trunc(e.get("label",""), EDGE_MAX_EM),
              "glyph": e["glyph"] if e.get("glyph") in G else "arrow"}
             for i, e in enumerate(kg.get("edges",[]))]
    sizes = {n["id"]: nsize(n["label"]) for n in kg["nodes"]}
    per_row = max(1, math.ceil(math.sqrt(len(groups))))
    cx, cy, rh = OX, OY, 0
    nodes, islands = [], []
    for gi, (cid, mem) in enumerate(groups.items()):
        if gi and gi % per_row == 0: cx, cy, rh = OX, cy+rh+GAP_Y, 0
        cols = max(1, math.ceil(math.sqrt(len(mem)))); rows = math.ceil(len(mem)/cols)
        grid = {(i//cols, i%cols): n for i, n in enumerate(mem)}
        mids = {n["id"] for n in mem}
        colw = [max((sizes[grid[(r,c)]["id"]][0] for r in range(rows) if (r,c) in grid), default=NW_MIN) for c in range(cols)]
        rowh = [max((sizes[grid[(r,c)]["id"]][1] for c in range(cols) if (r,c) in grid), default=NH_MIN) for r in range(rows)]
        pos = {grid[k]["id"]: k for k in grid}
        cgap = [COL_M]*max(1, cols-1); rgap = [ROW_M]*max(1, rows-1)
        for e in edges:                        # スキマはエッジラベルの実幅から決める
            if e["from"] not in mids or e["to"] not in mids: continue
            (r1,c1), (r2,c2) = pos[e["from"]], pos[e["to"]]
            w = elabel_px(e["label"], e["glyph"])
            if r1 == r2 and abs(c1-c2) == 1: i = min(c1,c2); cgap[i] = max(cgap[i], w+COL_M)
            elif c1 == c2 and abs(r1-r2) == 1: i = min(r1,r2); rgap[i] = max(rgap[i], EDGE_FONT*LINE_H+ROW_M)
        colx, x = [], 0.0
        for c in range(cols): colx.append(x); x += colw[c] + (cgap[c] if c < cols-1 else 0)
        rowy, y = [], 0.0
        for r in range(rows): rowy.append(y); y += rowh[r] + (rgap[r] if r < rows-1 else 0)
        w, h = 2*PAD+x, 2*PAD+y; x0, y0 = cx, cy; rh = max(rh, h)
        for (r,c), n in grid.items():
            nw, nh = sizes[n["id"]]
            nodes.append({"id":n["id"],"label":n["label"],
                          "x":round(x0+PAD+colx[c]+(colw[c]-nw)/2),
                          "y":round(y0+PAD+rowy[r]+(rowh[r]-nh)/2),
                          "size":nw,"height":nh,"community_id":cid,"style":{"rough":True}})
        m = comms.get(cid, {})
        islands.append({"community_id":cid,"name":m.get("name",cid),
                        "bbox":[x0,y0,round(x0+w),round(y0+h)],"is_gap":bool(m.get("is_gap",False))})
        cx += w + GAP_X
    return {"detail_level":detail,"nodes":nodes,"edges":edges,"islands":islands,
            "provenance":{"graph_version":kg.get("graph_version","kg"),"generated_for":"foundry_portal"}}
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
        w=n["size"]; h=n.get("height", max(NH_MIN, n["size"]*0.55))
        els.append(base(nid,"ellipse",n["x"],n["y"],w,h,backgroundColor="#fff9db",
                        strokeStyle="dashed" if ing else "solid",
                        opacity=GAP_OP if ing else 100,
                        boundElements=[{"id":tid,"type":"text"}]))
        t=text(tid,n["x"]+8,n["y"]+h/2-10,n["label"],14,"#1e1e1e",GAP_OP if ing else 100,nid)
        t["width"]=w-16; els.append(t)
    ctr={n["id"]:(n["x"]+n["size"]/2,n["y"]+n.get("height",max(NH_MIN,n["size"]*0.55))/2) for n in p["nodes"]}
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
        if lab: els.append(text(eid+"-text",(sx+ex)/2,(sy+ey)/2,lab,EDGE_FONT,col,op,eid))
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
