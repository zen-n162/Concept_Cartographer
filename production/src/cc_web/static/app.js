/* Concept Cartographer Web UI のふるまい (設計書 §5.3)。
 *
 * 方針:
 *  - ビルド工程なしの素の JS。フレームワークもモジュールバンドラも使わない。
 *  - **ユーザー・LLM 由来の文字列は必ず textContent** で入れる (XSS 対策)。
 *    innerHTML を使うのはサーバが生成した地図 SVG だけ。
 *  - 詳細度の切替は SVG の取り直しのみ。生成 (LLM) は一切走らない。
 */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var LEVELS = ["overview", "standard", "detailed"];
  var LEVEL_LABEL = { overview: "Overview", standard: "Standard", detailed: "Detailed" };
  // cc_orchestrator.pipeline.STAGES と同じ並び (進捗チェックリスト用)
  var STAGES = [
    ["routing", "経路判定"], ["ingest", "資料収集"], ["extract", "概念抽出"],
    ["relate", "関係の検証"], ["detail", "詳細度の計算"], ["gaps", "ギャップ検出"],
    ["render", "描画"], ["verify", "独立検証"], ["export", "出力"]
  ];
  var GLYPH_INFO = {
    arrow: { label: "因果", cls: "arrow" },
    wave: { label: "相関", cls: "wave" },
    double: { label: "補強", cls: "double" },
    zigzag: { label: "矛盾", cls: "zigzag" },
    tension: { label: "対立候補", cls: "tension" },
    hole: { label: "ギャップ", cls: "hole" }
  };
  var GAP_TYPE_LABEL = {
    data: "データ不足", extraction: "抽出漏れ", true: "真の空白", unknown: "未分類"
  };
  var POLL_MS = 1500;

  var state = {
    me: null,
    templates: [],
    session: null,
    view: null,
    detail: null,
    summary: null,
    job: null,
    timer: null,
    tab: "map",
    verdicts: {},
    satisfaction: 0,
    settings: { level: "standard", causalVerify: true, localOnly: false, collapsed: false }
  };

  // -------------------------------------------------------------- 小道具
  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function icon(name, cls) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "ic " + (cls || ""));
    var use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  async function api(path, options) {
    var res = await fetch(path, options);
    var text = await res.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) {
      var msg = (data && data.error && data.error.message) || res.status + " " + res.statusText;
      throw new Error(msg);
    }
    return data;
  }
  function postJSON(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  var toastTimer = null;
  function toast(message) {
    var node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, 3200);
  }

  function loadSettings() {
    try {
      var raw = localStorage.getItem("cc.settings");
      if (raw) Object.assign(state.settings, JSON.parse(raw));
    } catch (e) { /* localStorage 不可でも既定値で動く */ }
    if (LEVELS.indexOf(state.settings.level) < 0) state.settings.level = "standard";
  }
  function saveSettings() {
    try { localStorage.setItem("cc.settings", JSON.stringify(state.settings)); }
    catch (e) { /* 保存できなくても致命ではない */ }
  }

  function fmtSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
    if (bytes >= 1024) return (bytes / 1024).toFixed(0) + " KB";
    return bytes + " B";
  }
  function extInfo(ext) {
    if (ext === "pdf") return { icon: "file-pdf", cls: "ext-pdf", label: "PDF" };
    if (ext === "docx" || ext === "doc") return { icon: "file-docx", cls: "ext-docx", label: "Word" };
    if (ext === "xlsx" || ext === "xls") return { icon: "file-xls", cls: "ext-xlsx", label: "Excel" };
    return { icon: "file-text", cls: "ext-other", label: ext.toUpperCase() || "TEXT" };
  }

  // ============================================================ ヘッダー
  function renderAccount(me) {
    state.me = me;
    $("#acct-name").textContent = me.name;
    $("#acct-initials").textContent = me.initials;
    var domain = me.upn && me.upn.indexOf("@") >= 0 ? "@" + me.upn.split("@")[1] : "";
    $("#acct-domain").textContent = domain;
    $("#acct-signed").hidden = !me.signed_in;
  }

  function updateSeg() {
    var buttons = document.querySelectorAll(".seg-btn");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("is-on", buttons[i].dataset.level === state.settings.level);
    }
    var counts = $("#lv-counts");
    var levels = state.view && state.view.levels;
    if (!levels) { counts.textContent = ""; return; }
    counts.textContent = LEVELS.map(function (lv) {
      return LEVEL_LABEL[lv] + " " + ((levels[lv] || {}).nodes || 0);
    }).join(" · ");
  }

  async function setLevel(level) {
    if (LEVELS.indexOf(level) < 0) return;
    state.settings.level = level;
    saveSettings();
    updateSeg();
    if (!state.session) {
      toast("既定の詳細度を " + LEVEL_LABEL[level] + " にしました (次回の生成に使用)");
      return;
    }
    var t0 = performance.now();
    try {
      await loadMap(level);
    } catch (err) {
      toast("地図の取得に失敗しました: " + err.message);
      return;
    }
    var ms = Math.round(performance.now() - t0);
    toast(LEVEL_LABEL[level] + " に切替 (LLM 呼び出しゼロ・" + ms + "ms)");
    postJSON("/api/sessions/" + encodeURIComponent(state.session) + "/evaluation",
      { operation: "level_switch", to: level }).catch(function () { });
  }

  // ============================================================ サイドバー
  function renderHistory(items) {
    var box = $("#history-list");
    clear(box);
    var shown = items.slice(0, state.historyExpanded ? 50 : 4);
    if (!shown.length) {
      box.appendChild(el("p", "side-empty", "まだ履歴はありません"));
      return;
    }
    shown.forEach(function (item) {
      var btn = el("button", "side-link", item.message || "(無題)");
      btn.type = "button";
      btn.title = item.message || "";
      if (item.session && item.session === state.session) btn.classList.add("is-on");
      btn.addEventListener("click", function () {
        if (!item.session) {
          toast("この依頼は地図なしの応答でした (履歴のみ)");
          return;
        }
        openSession(item.session, item.message);
      });
      box.appendChild(btn);
    });
    $("#btn-history-more").textContent =
      state.historyExpanded ? "最近の 4 件だけ表示" : "すべて表示";
  }

  function renderFiles(files) {
    var box = $("#file-list");
    clear(box);
    if (!files.length) {
      box.appendChild(el("p", "side-empty", "inbox/ は空です"));
      return;
    }
    files.slice(0, state.filesExpanded ? 50 : 3).forEach(function (f) {
      var info = extInfo(f.ext);
      var row = el("div", "file-row");
      row.appendChild(icon(info.icon, "ic-15 " + info.cls));
      var meta = el("div", "min0");
      var name = el("p", "file-name", f.name);
      name.title = f.name;
      meta.appendChild(name);
      meta.appendChild(el("p", "file-meta", info.label + " · " + fmtSize(f.size)));
      row.appendChild(meta);
      box.appendChild(row);
    });
  }

  async function refreshHistory() {
    try {
      var data = await api("/api/history");
      renderHistory(data.items || []);
    } catch (e) { /* 履歴が無い初回は空のまま */ }
  }
  async function refreshFiles() {
    try {
      var data = await api("/api/files");
      renderFiles(data.files || []);
    } catch (e) { /* inbox 未作成なら空 */ }
  }

  // ============================================================ ホーム
  function renderTemplates(list) {
    var grid = $("#tpl-grid");
    clear(grid);
    list.forEach(function (tpl) {
      var card = el("button", "tpl-card");
      card.type = "button";
      var box = el("div", "tpl-icon");
      box.style.background = tpl.bg;
      box.style.color = tpl.fg;
      box.appendChild(icon(tpl.icon, "ic-16"));
      card.appendChild(box);
      card.appendChild(el("p", "tpl-title", tpl.title));
      card.appendChild(el("p", "tpl-desc", tpl.description));
      card.addEventListener("click", function () {
        var input = $("#composer-input");
        input.value = tpl.message;
        input.focus();
        autoGrow(input);
      });
      grid.appendChild(card);
    });
  }

  function showHome() {
    state.session = null;
    state.view = null;
    state.summary = null;
    $("#home").hidden = false;
    $("#thread").hidden = true;
    clear($("#thread"));
    updateSeg();
    refreshHistory();
  }

  function showThread() {
    $("#home").hidden = true;
    $("#thread").hidden = false;
  }

  // ============================================================ 送信 / 進捗
  function autoGrow(input) {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  }

  async function send() {
    var input = $("#composer-input");
    var message = input.value.trim();
    if (!message) return;
    input.value = "";
    autoGrow(input);
    showThread();
    clear($("#thread"));
    state.session = null;
    state.view = null;

    var bubble = el("div", "bubble-user", message);
    $("#thread").appendChild(bubble);

    var card = el("div", "card");
    card.id = "progress-card";
    $("#thread").appendChild(card);
    renderProgress(card, { status: "queued", stages_done: [], stage: null });
    scrollDown();

    try {
      var res = await postJSON("/api/jobs", {
        message: message,
        level: state.settings.level,
        local_only: state.settings.localOnly,
        causal_verify: state.settings.causalVerify
      });
      pollJob(res.job_id, message);
    } catch (err) {
      renderError(card, err.message, message);
    }
  }

  function renderProgress(card, job) {
    clear(card);
    card.appendChild(el("p", "card-title", "概念地図を作成しています"));
    var doneSet = {};
    (job.stages_done || []).forEach(function (k) { doneSet[k] = true; });
    var current = job.stage ? job.stage.key : null;
    STAGES.forEach(function (pair) {
      var key = pair[0], label = pair[1];
      var row = el("div", "stage-row");
      if (key === current && job.status === "running") {
        row.classList.add("now");
        row.appendChild(icon("loader-2", "ic-14 spin"));
      } else if (doneSet[key]) {
        row.classList.add("done");
        row.appendChild(icon("circle-check", "ic-14"));
      } else {
        row.appendChild(icon("circle", "ic-14"));
      }
      row.appendChild(el("span", null, label));
      card.appendChild(row);
    });
  }

  function renderError(card, message, retryMessage) {
    clear(card);
    card.className = "card error";
    card.appendChild(el("p", "card-title", "生成に失敗しました"));
    card.appendChild(el("p", null, String(message).slice(0, 300)));
    if (retryMessage) {
      var btn = el("button", "btn-sm", " 再試行");
      btn.type = "button";
      btn.style.marginTop = "10px";
      btn.insertBefore(icon("refresh", "ic-13"), btn.firstChild);
      btn.addEventListener("click", function () {
        $("#composer-input").value = retryMessage;
        send();
      });
      card.appendChild(btn);
    }
  }

  function pollJob(jobId, message) {
    if (state.timer) clearInterval(state.timer);
    var card = $("#progress-card");
    var tick = async function () {
      var job;
      try {
        job = await api("/api/jobs/" + encodeURIComponent(jobId));
      } catch (err) {
        clearInterval(state.timer);
        renderError(card, err.message, message);
        return;
      }
      state.job = job;
      if (job.status === "queued" || job.status === "running") {
        renderProgress(card, job);
        return;
      }
      clearInterval(state.timer);
      state.timer = null;
      if (job.status === "error") {
        renderError(card, job.error || "不明なエラー", message);
        refreshHistory();
        return;
      }
      await onJobDone(job, card);
      refreshHistory();
    };
    state.timer = setInterval(tick, POLL_MS);
    tick();
  }

  async function onJobDone(job, card) {
    var summary = job.summary || {};
    state.summary = summary;
    if (summary.answer) {              // basic / vector 経路 (地図なし)
      card.remove();
      $("#thread").appendChild(el("div", "bubble-ai", summary.answer));
      scrollDown();
      return;
    }
    if (summary.status === "no_documents") {
      renderError(card, summary.hint || "対象期間の資料が見つかりませんでした", null);
      return;
    }
    card.remove();
    await openSession(summary.session, null, summary);
  }

  function scrollDown() {
    var content = $("#content");
    content.scrollTop = content.scrollHeight;
  }

  // ============================================================ セッション
  async function openSession(session, title, summary) {
    showThread();
    if (title) {
      clear($("#thread"));
      $("#thread").appendChild(el("div", "bubble-user", title));
    }
    state.session = session;
    state.summary = summary || null;
    state.verdicts = {};
    state.satisfaction = 0;
    state.tab = "map";

    var card = el("div", "card");
    card.id = "result-card";
    $("#thread").appendChild(card);
    card.appendChild(el("p", "card-title", "読み込み中…"));

    try {
      state.detail = await api("/api/sessions/" + encodeURIComponent(session));
      var level = state.settings.level;
      if (LEVELS.indexOf(level) < 0) level = state.detail.default_level;
      await loadMap(level, card);
    } catch (err) {
      renderError(card, err.message, null);
      return;
    }
    refreshHistory();
  }

  async function loadMap(level, card) {
    var base = "/api/sessions/" + encodeURIComponent(state.session);
    var results = await Promise.all([
      fetch(base + "/svg?level=" + level).then(function (r) {
        if (!r.ok) throw new Error("SVG の取得に失敗しました (" + r.status + ")");
        return r.text();
      }),
      api(base + "/view?level=" + level)
    ]);
    state.view = results[1];
    state.svg = results[0];
    renderResult(card || $("#result-card"));
    updateSeg();
  }

  function renderResult(card) {
    if (!card) return;
    card.className = "card";
    clear(card);
    var view = state.view;
    var summary = state.summary || {};

    // --- サマリ行 ---
    var islands = {};
    view.nodes.forEach(function (n) { islands[n.community_id] = true; });
    var row = el("div", "summary-row");
    row.appendChild(stat("概念", view.nodes.length));
    row.appendChild(stat("関係", view.edges.length));
    row.appendChild(stat("島", Object.keys(islands).length));
    var verification = summary.verification || {};
    if (verification.verdict) {
      var pass = verification.verdict === "PASS";
      var badge = el("span", "badge " + (pass ? "pass" : "fail"),
        pass ? "検証 PASS" : "検証 FAIL");
      badge.title = verification.summary || "";
      row.appendChild(badge);
    }
    card.appendChild(row);

    // --- 関係検証チップ (裁定 7 の結果) ---
    // 生成直後は summary.relation_policy を、履歴から開いた時は KPI の
    // causal 集計 (plan から再計算されたもの) を使う。
    var policy = summary.relation_policy;
    var causalKpi = (state.detail && state.detail.kpi && state.detail.kpi.causal) || null;
    if (policy || causalKpi) {
      var chips = el("div", "chip-line");
      chips.appendChild(el("span", "chip-sm green", "因果を維持 "
        + (policy ? (policy.causal_kept || 0) : (causalKpi.kept_as_causal || 0))));
      chips.appendChild(el("span", "chip-sm", "相関へ降格 "
        + (policy ? (policy.causal_demoted || 0) : (causalKpi.demoted_to_correlation || 0))));
      if (policy) {
        chips.appendChild(el("span", "chip-sm grey",
          "矛盾を非断定化 " + (policy.contradiction_demoted || 0)));
      }
      card.appendChild(chips);
    }

    // --- タブ ---
    var gaps = view.gaps || [];
    var tabs = el("div", "tabs");
    [["map", "地図"], ["gaps", "ギャップ (" + gaps.length + ")"], ["eval", "評価"]]
      .forEach(function (pair) {
        var btn = el("button", "tab" + (state.tab === pair[0] ? " is-on" : ""), pair[1]);
        btn.type = "button";
        btn.addEventListener("click", function () {
          state.tab = pair[0];
          renderResult(card);
        });
        tabs.appendChild(btn);
      });
    card.appendChild(tabs);

    var body = el("div");
    card.appendChild(body);
    if (state.tab === "map") renderMapTab(body);
    else if (state.tab === "gaps") renderGapsTab(body);
    else renderEvalTab(body);
  }

  function stat(label, value) {
    var span = el("span", "stat");
    span.appendChild(el("b", null, value));
    span.appendChild(document.createTextNode(" " + label));
    return span;
  }

  function renderMapTab(body) {
    var bar = el("div", "map-toolbar");
    bar.appendChild(el("span", "chip-sm", LEVEL_LABEL[state.view.level]));
    var hint = el("span", "gap-src", "ノード/関係をクリックすると詳細を表示します");
    bar.appendChild(hint);
    var links = el("div", "grow");
    links.style.display = "flex";
    links.style.gap = "8px";
    var base = "/api/sessions/" + encodeURIComponent(state.session);
    var svgLink = el("a", "linkbtn", " SVG");
    svgLink.href = base + "/svg?level=" + state.view.level;
    svgLink.download = state.session + "_" + state.view.level + ".svg";
    svgLink.insertBefore(icon("download", "ic-12"), svgLink.firstChild);
    links.appendChild(svgLink);
    var sceneLink = el("a", "linkbtn", " .excalidraw");
    sceneLink.href = base + "/excalidraw";
    sceneLink.insertBefore(icon("download", "ic-12"), sceneLink.firstChild);
    links.appendChild(sceneLink);
    bar.appendChild(links);
    body.appendChild(bar);

    var wrap = el("div", "map-wrap");
    // サーバが生成した SVG のみ innerHTML で展開する (ユーザー入力は入らない)
    wrap.innerHTML = state.svg;
    wrap.addEventListener("click", onMapClick);
    body.appendChild(wrap);
  }

  // ---------------------------------------------------------- 地図クリック
  function onMapClick(event) {
    var target = event.target;
    var node = target.closest ? target.closest(".cc-node") : null;
    if (node) {
      if (node.getAttribute("data-kind") === "aggregate") {
        expandAggregate(node.getAttribute("data-aggregate-id")
          || node.getAttribute("data-node-id"));
      } else {
        showNodeInfo(node.getAttribute("data-node-id"));
      }
      return;
    }
    var edge = target.closest ? target.closest(".cc-edge") : null;
    if (edge) {
      showEdgePopover(edge.getAttribute("data-edge-id"), event.clientX, event.clientY);
      return;
    }
    hidePopover();
  }

  function showNodeInfo(nodeId) {
    var node = (state.view.nodes || []).find(function (n) { return n.id === nodeId; });
    if (!node) return;
    var lines = [];
    if (node.importance) {
      lines.push("重要度 " + node.importance.total
        + " (媒介 " + node.importance.betweenness
        + " / 頻度 " + node.importance.frequency
        + " / 新規性 " + node.importance.novelty + ")");
    }
    if (node.visible_at) {
      lines.push("表示レベル: " + LEVELS.filter(function (lv) {
        return node.visible_at[lv];
      }).map(function (lv) { return LEVEL_LABEL[lv]; }).join(" / "));
    }
    toast(node.label + (lines.length ? " — " + lines.join(" / ") : ""));
  }

  async function expandAggregate(aggregateId) {
    if (!aggregateId) return;
    var data;
    try {
      data = await postJSON("/api/sessions/" + encodeURIComponent(state.session)
        + "/expand/" + encodeURIComponent(aggregateId), {});
    } catch (err) {
      toast("展開できませんでした: " + err.message);
      return;
    }
    var body = el("div");
    body.appendChild(el("p", null,
      (data.aggregate && data.aggregate.summary_label) || aggregateId));
    var list = el("div", "member-list");
    (data.members || []).forEach(function (m) {
      list.appendChild(el("span", "chip-sm", m.label));
    });
    body.appendChild(list);
    body.appendChild(el("p", "gap-src",
      "これらは下位レベルで畳まれた概念です。Detailed で個別に表示されます。"));
    var btn = el("button", "btn-primary", "Detailed で開く");
    btn.type = "button";
    btn.style.marginTop = "12px";
    btn.addEventListener("click", function () {
      closeModal();
      setLevel("detailed");
    });
    body.appendChild(btn);
    openModal("集約ノードの展開", body);
    postJSON("/api/sessions/" + encodeURIComponent(state.session) + "/evaluation",
      { operation: "expand_aggregate", aggregate_id: aggregateId }).catch(function () { });
  }

  function showEdgePopover(edgeId, x, y) {
    var edge = (state.view.edges || []).find(function (e) { return e.id === edgeId; });
    if (!edge) return;
    var pop = $("#popover");
    clear(pop);

    var head = el("div", "pop-head");
    var info = GLYPH_INFO[edge.glyph] || { label: edge.glyph, cls: "tension" };
    head.appendChild(el("span", "glyph " + info.cls, info.label));
    head.appendChild(el("span", null, edge.label || ""));
    var close = el("button", "icon-btn");
    close.type = "button";
    close.appendChild(icon("x", "ic-14"));
    close.addEventListener("click", hidePopover);
    head.appendChild(close);
    pop.appendChild(head);

    var spans = edge.evidence_span || [];
    spans.slice(0, 2).forEach(function (span) {
      var quote = el("div", "quote", "「" + (span.surface || "") + "」");
      quote.appendChild(el("div", "quote-src", "出典: " + (span.document_id || "不明")));
      pop.appendChild(quote);
    });
    if (!spans.length) {
      pop.appendChild(el("p", "gap-src", "根拠スパンがありません (evidence 表示率の対象外)"));
    }
    if (edge.causal_check && edge.causal_check.reason) {
      pop.appendChild(el("p", "gap-src", "判定: " + edge.causal_check.reason));
    }
    if (edge.member_edge_ids && edge.member_edge_ids.length > 1) {
      pop.appendChild(el("p", "gap-src",
        "この線は " + edge.member_edge_ids.length + " 本の関係を束ねています"));
    }

    var actions = el("div", "gap-actions");
    actions.style.marginTop = "10px";
    [["correct", "正しい"], ["incorrect", "誤り"], ["undecidable", "判断不能"]]
      .forEach(function (pair) {
        var btn = el("button", "btn-sm" + (state.verdicts[edgeId] === pair[0] ? " is-on" : ""),
          pair[1]);
        btn.type = "button";
        btn.addEventListener("click", async function () {
          try {
            await postJSON("/api/sessions/" + encodeURIComponent(state.session)
              + "/evaluation", { edge_id: edgeId, verdict: pair[0] });
            state.verdicts[edgeId] = pair[0];
            showEdgePopover(edgeId, x, y);
            toast("関係の評価を記録しました");
          } catch (err) {
            toast("記録できませんでした: " + err.message);
          }
        });
        actions.appendChild(btn);
      });
    pop.appendChild(actions);

    pop.hidden = false;
    var rect = pop.getBoundingClientRect();
    var left = Math.min(x + 12, window.innerWidth - rect.width - 12);
    var top = Math.min(y + 12, window.innerHeight - rect.height - 12);
    pop.style.left = Math.max(12, left) + "px";
    pop.style.top = Math.max(12, top) + "px";

    postJSON("/api/sessions/" + encodeURIComponent(state.session) + "/evaluation",
      { operation: "view_evidence", edge_id: edgeId }).catch(function () { });
  }

  function hidePopover() { $("#popover").hidden = true; }

  // ---------------------------------------------------------- ギャップ
  function renderGapsTab(body) {
    var gaps = state.view.gaps || [];
    var usefulness = (state.detail && state.detail.gaps_usefulness) || {};
    var head = el("div", "chip-line");
    var rate = usefulness.usefulness_rate;
    head.appendChild(el("span", "chip-sm",
      "有用率 " + (rate === null || rate === undefined ? "—" : Math.round(rate * 100) + "%")));
    head.appendChild(el("span", "chip-sm grey",
      "確定 " + (usefulness.decided || 0) + " / 候補 " + gaps.length));
    body.appendChild(head);

    if (!gaps.length) {
      body.appendChild(el("p", "gap-src", "ギャップ候補はありません"));
      return;
    }
    gaps.forEach(function (gap) { body.appendChild(gapRow(gap)); });
  }

  function gapRow(gap) {
    var row = el("div", "gap-row");
    var status = gap.status || "candidate";
    var iconName = status === "confirmed" ? "circle-check"
      : status === "dismissed" ? "circle-x" : "circle";
    row.appendChild(icon(iconName, "ic-16 gap-ico " + status));

    var main = el("div", "gap-main");
    main.appendChild(el("p", "gap-reason", gap.reason || gap.gap_id));
    var meta = el("div", "gap-meta");
    var bar = el("div", "bar");
    var fill = el("span");
    fill.style.width = Math.round((gap.confidence || 0) * 100) + "%";
    bar.appendChild(fill);
    bar.title = "信頼度 " + gap.confidence;
    meta.appendChild(bar);
    meta.appendChild(el("span", "chip-sm grey",
      GAP_TYPE_LABEL[gap.presumed_type] || gap.presumed_type));
    (gap.evidence_links || []).slice(0, 2).forEach(function (link) {
      var doc = (link.span && link.span.document_id) || link.node_id || "";
      if (doc) meta.appendChild(el("span", "gap-src", "出典: " + doc));
    });
    if (gap.confirmed_by) {
      meta.appendChild(el("span", "gap-src",
        (status === "confirmed" ? "有用" : "却下") + " · " + gap.confirmed_by));
    }
    main.appendChild(meta);
    row.appendChild(main);

    var actions = el("div", "gap-actions");
    [["confirm", "有用"], ["dismiss", "却下"]].forEach(function (pair) {
      var btn = el("button", "btn-sm", pair[1]);
      btn.type = "button";
      btn.disabled = status !== "candidate";
      if ((status === "confirmed" && pair[0] === "confirm")
        || (status === "dismissed" && pair[0] === "dismiss")) {
        btn.classList.add("is-on");
      }
      btn.addEventListener("click", function () { decideGap(gap.gap_id, pair[0]); });
      actions.appendChild(btn);
    });
    row.appendChild(actions);
    return row;
  }

  async function decideGap(gapId, decision) {
    try {
      var res = await postJSON("/api/sessions/" + encodeURIComponent(state.session)
        + "/gaps/" + encodeURIComponent(gapId), { decision: decision });
      // 手元の view を更新して再描画 (plan 再取得は不要)
      (state.view.gaps || []).forEach(function (g, i) {
        if (g.gap_id === gapId) state.view.gaps[i] = res.gap;
      });
      if (state.detail) state.detail.gaps_usefulness = res.usefulness;
      renderResult($("#result-card"));
      toast(decision === "confirm" ? "ギャップを「有用」で確定しました"
        : "ギャップを「却下」で確定しました");
    } catch (err) {
      toast(err.message);
    }
  }

  // ---------------------------------------------------------- 評価タブ
  function renderEvalTab(body) {
    var line = el("div", "eval-line");
    line.appendChild(el("span", null, "この地図の満足度"));
    var stars = el("div", "stars");
    for (var i = 1; i <= 5; i++) {
      (function (score) {
        var btn = el("button", "star-btn" + (state.satisfaction >= score ? " is-on" : ""));
        btn.type = "button";
        btn.title = score + " / 5";
        btn.appendChild(icon(state.satisfaction >= score ? "star-filled" : "star", "ic-18"));
        btn.addEventListener("click", async function () {
          try {
            await postJSON("/api/sessions/" + encodeURIComponent(state.session)
              + "/evaluation", { satisfaction: score });
            state.satisfaction = score;
            renderResult($("#result-card"));
            toast("満足度 " + score + " を記録しました");
          } catch (err) { toast(err.message); }
        });
        stars.appendChild(btn);
      })(i);
    }
    line.appendChild(stars);
    body.appendChild(line);

    var kpi = (state.detail && state.detail.kpi) || {};
    var evidence = kpi.evidence_display || {};
    var chips = el("div", "chip-line");
    chips.appendChild(el("span", "chip-sm",
      "関係評価 " + Object.keys(state.verdicts).length + " 件"));
    chips.appendChild(el("span", "chip-sm green", "evidence 表示率 "
      + (evidence.rate === null || evidence.rate === undefined
        ? "—" : Math.round(evidence.rate * 100) + "%")));
    var causal = kpi.causal || {};
    chips.appendChild(el("span", "chip-sm grey",
      "因果候補 " + (causal.causal_candidates || 0)
      + " / 維持 " + (causal.kept_as_causal || 0)));
    body.appendChild(chips);
    body.appendChild(el("p", "gap-src",
      "関係の評価は地図上の線をクリックして送れます。評価は logs/evaluation.jsonl に"
      + "ラベルと ID だけが記録されます (本文は記録しません)。"));
  }

  // ============================================================ モーダル
  function openModal(title, bodyNode) {
    $("#modal-title").textContent = title;
    var body = $("#modal-body");
    clear(body);
    body.appendChild(bodyNode);
    $("#overlay").hidden = false;
  }
  function closeModal() { $("#overlay").hidden = true; }

  function openSettings() {
    var box = el("div");
    var f1 = el("div", "field");
    f1.appendChild(el("label", null, "既定の詳細度"));
    var select = document.createElement("select");
    LEVELS.forEach(function (lv) {
      var opt = document.createElement("option");
      opt.value = lv;
      opt.textContent = LEVEL_LABEL[lv];
      if (state.settings.level === lv) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () { setLevel(select.value); });
    f1.appendChild(select);
    box.appendChild(f1);

    box.appendChild(checkboxField("因果の独立検証を行う (別モデルで判定)",
      "causalVerify"));
    box.appendChild(checkboxField("Work IQ を使わない (ローカル資料のみ)",
      "localOnly"));
    box.appendChild(el("p", "gap-src",
      "設定はこのブラウザに保存され、次のジョブ送信時に反映されます。"));
    openModal("設定", box);
  }

  function checkboxField(label, key) {
    var field = el("div", "field");
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!state.settings[key];
    input.id = "set-" + key;
    input.addEventListener("change", function () {
      state.settings[key] = input.checked;
      saveSettings();
    });
    var text = el("label", null, label);
    text.htmlFor = input.id;
    field.appendChild(input);
    field.appendChild(text);
    return field;
  }

  function openHelp() {
    var box = el("div");
    var list = document.createElement("ul");
    [
      "テンプレート: ホームの 4 枚をクリックすると依頼文が入ります。そのまま送信できます。",
      "詳細度: ヘッダーの Overview / Standard / Detailed。切替は再生成なし (LLM 呼び出しゼロ) で、"
      + "同じ地図の粒度だけが変わります。",
      "集約ノード (破線の楕円) をクリックすると、畳まれている概念の一覧が出ます。",
      "関係の線をクリックすると根拠の引用が出ます。正しい / 誤り / 判断不能で評価できます。",
      "ギャップタブでは候補を [有用] [却下] で確定します。確定は取り消せません (監査のため)。",
      "資料は inbox/ に置くかサイドバーからアップロードしてください (pdf / docx / txt / md)。"
    ].forEach(function (text) { list.appendChild(el("li", null, text)); });
    box.appendChild(list);
    openModal("使い方", box);
  }

  function openFeedback() {
    var box = el("div");
    box.appendChild(el("p", null,
      "R1 パイロット中です。評価は★と関係評価から送ってください。"));
    box.appendChild(el("p", "gap-src",
      "満足度・関係の正誤・ギャップの有用性が R2 のゲート判定に使われます。"));
    openModal("フィードバック", box);
  }

  function openAbout() {
    var box = el("div");
    box.appendChild(el("p", null,
      "生成された概念地図は資料からの自動抽出です。因果の矢印は語彙証拠と"
      + "独立検証の 3 点セットを通過したものだけに限っていますが、誤りは残ります。"));
    box.appendChild(el("p", "gap-src",
      "関係の線をクリックすると根拠となった原文の引用が確認できます。"));
    openModal("この回答について", box);
  }

  // ============================================================ 初期化
  function wire() {
    $("#btn-send").addEventListener("click", send);
    var input = $("#composer-input");
    input.addEventListener("input", function () { autoGrow(input); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        send();
      }
    });

    document.querySelectorAll(".seg-btn").forEach(function (btn) {
      btn.addEventListener("click", function () { setLevel(btn.dataset.level); });
    });

    $("#btn-new-chat").addEventListener("click", showHome);
    $("#btn-history-more").addEventListener("click", function () {
      state.historyExpanded = !state.historyExpanded;
      refreshHistory();
    });
    $("#btn-help").addEventListener("click", openHelp);
    $("#btn-feedback").addEventListener("click", openFeedback);
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-about").addEventListener("click", openAbout);
    $("#btn-tpl-more").addEventListener("click", function () {
      toast("R1 のテンプレートはこの 4 件です");
    });
    $("#modal-close").addEventListener("click", closeModal);
    $("#overlay").addEventListener("click", function (event) {
      if (event.target === $("#overlay")) closeModal();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { closeModal(); hidePopover(); hideModeMenu(); }
    });

    // ファイルアップロード
    var fileInput = $("#file-input");
    $("#btn-upload").addEventListener("click", function () { fileInput.click(); });
    $("#btn-upload-side").addEventListener("click", function () {
      state.filesExpanded = !state.filesExpanded;
      refreshFiles();
    });
    fileInput.addEventListener("change", async function () {
      if (!fileInput.files.length) return;
      var form = new FormData();
      for (var i = 0; i < fileInput.files.length; i++) {
        form.append("files", fileInput.files[i]);
      }
      try {
        var res = await api("/api/files", { method: "POST", body: form });
        toast((res.saved || []).length + " 件をアップロードしました");
        refreshFiles();
      } catch (err) {
        toast("アップロードに失敗しました: " + err.message);
      }
      fileInput.value = "";
    });

    // Work IQ トグル (local_only の反転)
    var workiq = $("#btn-workiq");
    var syncWorkiq = function () {
      workiq.classList.toggle("is-on", !state.settings.localOnly);
      workiq.title = state.settings.localOnly
        ? "Work IQ を使わない (ローカル資料のみ)"
        : "Work IQ (OneDrive/SharePoint) から資料を収集";
    };
    workiq.addEventListener("click", function () {
      state.settings.localOnly = !state.settings.localOnly;
      saveSettings();
      syncWorkiq();
      toast(state.settings.localOnly ? "ローカル資料のみを使います"
        : "Work IQ からも資料を収集します");
    });
    syncWorkiq();

    // モードのドロップダウン
    $("#btn-mode").addEventListener("click", function (event) {
      var menu = $("#mode-menu");
      if (!menu.hidden) { hideModeMenu(); return; }
      var rect = event.currentTarget.getBoundingClientRect();
      menu.hidden = false;
      menu.style.left = rect.left + "px";
      menu.style.top = (rect.bottom + 8) + "px";
    });
    document.addEventListener("click", function (event) {
      if (!$("#mode-menu").hidden && !event.target.closest("#mode-menu")
        && !event.target.closest("#btn-mode")) hideModeMenu();
      if (!$("#popover").hidden && !event.target.closest("#popover")
        && !event.target.closest(".map-wrap")) hidePopover();
    });

    // サイドバーの折りたたみ
    $("#btn-collapse").addEventListener("click", function () { setCollapsed(true); });
    $("#btn-reopen").addEventListener("click", function () { setCollapsed(false); });
  }

  function hideModeMenu() { $("#mode-menu").hidden = true; }

  function setCollapsed(collapsed) {
    state.settings.collapsed = collapsed;
    saveSettings();
    document.body.classList.toggle("sidebar-hidden", collapsed);
    $("#btn-reopen").hidden = !collapsed;
  }

  async function init() {
    loadSettings();
    setCollapsed(!!state.settings.collapsed);
    updateSeg();
    wire();
    try {
      renderAccount(await api("/api/me"));
    } catch (e) {
      renderAccount({ name: "ローカル ユーザー", upn: "", initials: "ロユ", signed_in: false });
    }
    try {
      var tpl = await api("/api/templates");
      state.templates = tpl.templates || [];
      renderTemplates(state.templates);
    } catch (e) { /* テンプレなしでも入力はできる */ }
    refreshFiles();
    refreshHistory();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
