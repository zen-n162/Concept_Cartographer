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
    ["zone", "文脈ラベル付け"], ["claims", "主張の抽出"],
    ["relate", "関係の検証"], ["validate", "主張の検証"],
    ["rhetoric", "論証と矛盾の検出"],
    ["detail", "詳細度の計算"], ["gaps", "ギャップ検出"],
    ["render", "描画"], ["verify", "独立検証"], ["export", "出力"]
  ];
  // 関係記号の表示名。cc_core.normalize.VALID_GLYPHS と 1:1 で対応させること
  // (欠けると根拠ポップオーバーが glyph の生 ID を出してしまう)。
  var GLYPH_INFO = {
    arrow: { label: "因果", cls: "arrow" },
    wave: { label: "相関", cls: "wave" },
    double: { label: "補強", cls: "double" },
    zigzag: { label: "矛盾", cls: "zigzag" },
    tension: { label: "対立候補", cls: "tension" },
    hole: { label: "ギャップ", cls: "hole" },
    isa: { label: "分類", cls: "isa" },
    partof: { label: "構成", cls: "partof" },
    precedes: { label: "時系列", cls: "precedes" },
    question: { label: "疑問", cls: "question" }
  };
  var GAP_TYPE_LABEL = {
    data: "データ不足", extraction: "抽出漏れ", true: "真の空白", unknown: "未分類"
  };
  // QA 経路の出典チップ (R2b 設計書 §2)。cc_orchestrator.qa の sources[].kind と対応
  var SOURCE_KIND = {
    node: { label: "概念", cls: "" },
    edge: { label: "関係", cls: " green" },
    community: { label: "テーマ", cls: " grey" }
  };
  // ギャップの検出信号の種類 (R2a 設計書 §9)。presumed_type とは別の軸
  var GAP_KIND_LABEL = {
    structural: "構造", discourse: "言説", causal: "因果"
  };
  // 層タグの見出し (R2a 設計書 §1 の 4 層 30 種)。クリック展開で
  // 「UI は 8 記号、内部は 30 種」を見せるために使う
  var LAYER_LABEL = {
    layer_A: "A オントロジー", layer_B: "B 言説・構造",
    layer_C: "C 意味・因果", layer_D: "D 認識論・修辞"
  };
  var VALIDATION_LABEL = {
    validated: "検証済み", uncertain: "要レビュー", rejected: "却下"
  };
  // 編集で選べる関係の種類 (R2a 設計書 §10: 8 種)。
  // hole (ギャップ候補) と tension (非断定の対立候補) は**選択肢に出さない** —
  // どちらも「機械がまだ判断できていない」という内部状態で、人が選ぶものでは
  // ないため (人が対立だと確信したなら zigzag = 矛盾を選ぶのが正しい)。
  var EDIT_GLYPHS = [
    ["arrow", "因果"], ["wave", "相関"], ["double", "補強"], ["zigzag", "矛盾"],
    ["isa", "分類"], ["partof", "構成"], ["precedes", "時系列"], ["question", "疑問"]
  ];
  var EDIT_OP_LABEL = {
    rename_node: "概念の改名", delete_node: "概念の削除", add_node: "概念の追加",
    relabel_edge: "関係のラベル変更", retype_edge: "関係の種類変更",
    reverse_edge: "関係の向き反転", delete_edge: "関係の削除",
    add_edge: "関係の追加", revert: "取り消し"
  };
  var LEARNED_KIND_LABEL = {
    rename: "改名", stoplist: "除外", allow: "因果を許可",
    deny: "因果を否定", reverse: "向きを修正"
  };
  var INFO_TEXT = {
    mode: {
      title: "モードについて",
      body: "個人モード: あなたの OneDrive / SharePoint / ローカル資料だけを対象に、"
        + "自分専用の概念地図を作ります。データは他のユーザーと共有されません。"
        + "チームモード・機構横断モードは今後のリリース (R2 以降) で追加予定です。"
    },
    level: {
      title: "詳細度について",
      body: "概念地図の粒度です。Overview は 10〜20 / Standard は 20〜50 / "
        + "Detailed は 50〜100 要素を目安に、重要度の高い概念から表示します "
        + "(表示枠は概念+集約の合計)。切替は再生成なし・待ち時間ほぼゼロ。"
        + "畳まれた「集約ノード」はクリックで中身を展開できます。"
    }
  };
  var POLL_MS = 1500;

  var state = {
    me: null,
    // サイドバーの状態機械 (collapsed=ユーザー設定 / narrow=狭幅 /
    // drawerOpen=狭幅でドロワーを開いている)。init で設定から作り直す。
    sidebar: { collapsed: false, narrow: false, drawerOpen: false },
    templates: [],
    session: null,
    view: null,
    detail: null,
    layers: null,          // GET .../layers の結果 (R2a 以前は null のまま)
    summary: null,
    job: null,
    timer: null,
    tab: "map",
    verdicts: {},
    satisfaction: 0,
    offline: null,         // GET /api/evaluation/offline の直近の結果 (全セッション横断)
    editMode: false,
    pickFrom: null,        // 「関係を追加」の 1 個目に選んだノード id
    mapZoom: 1.0,           // 地図の拡大率 (Excalidraw ライクなズーム)。セッションを開き直すと 1.0 に戻す
    files: [],             // inbox の一覧 (チップのメタ情報に使う)
    attachments: [],       // このページセッションでアップロードした名前
    knownSessions: {},     // 履歴にある session id (QA の出典を開けるか判定する)
    settings: {
      level: "standard", causalVerify: true, localOnly: false,
      collapsed: false, learned: true, layers: true,
      // テストモード (裁定 X)。**既定 OFF**。入れたときだけ前回の結果を再利用する
      testCache: false
    }
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
      var err = new Error(msg);
      err.status = res.status;   // 503 (canvas 未接続) を呼び出し側で区別するため
      throw err;
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

  /* ポップアップブロック時の案内 (設計書 §3-2)。URL はサーバの設定値
   * (EXCALIDRAW_CANVAS_URL) であってユーザー入力ではないが、この
   * ファイルの方針どおり innerHTML は使わず DOM 組み立てで入れる。 */
  function toastLink(message, linkText, url) {
    var node = $("#toast");
    clear(node);
    node.appendChild(document.createTextNode(message + " "));
    var a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = linkText;
    node.appendChild(a);
    node.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, 8000);
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

  async function refreshAccount() {
    try {
      renderAccount(await api("/api/me"));
    } catch (err) {
      toast("アカウント状態を読み込めませんでした: " + err.message);
    }
  }

  // ------------------------------------------------- アカウントのメニュー
  function hideAccountMenu() { $("#acct-menu").hidden = true; }

  /* 裁定 AG (正直な表示): サインアウトが「この Mac の az CLI 全体」に効くこと
   * — つまり CLI での地図生成にも影響すること — を文言から隠さない。 */
  function buildAccountMenu() {
    var menu = $("#acct-menu");
    clear(menu);
    var signedIn = !!(state.me && state.me.signed_in);

    if (signedIn) {
      var out = el("button", "dd-item dd-stack");
      out.type = "button";
      out.appendChild(el("span", null, "サインアウト"));
      out.appendChild(el("span", "dd-note",
        "この Mac の az CLI 全体からサインアウトします"));
      out.addEventListener("click", function () { hideAccountMenu(); signOut(); });
      menu.appendChild(out);

      var reload = el("button", "dd-item", "状態を再読込");
      reload.type = "button";
      reload.addEventListener("click", function () {
        hideAccountMenu();
        refreshAccount();
      });
      menu.appendChild(reload);
    } else {
      var login = el("button", "dd-item", "Microsoft 365 にサインイン");
      login.type = "button";
      login.addEventListener("click", function () {
        hideAccountMenu();
        startSignIn();
      });
      menu.appendChild(login);
    }
    return menu;
  }

  function showAccountMenu(anchor) {
    var menu = buildAccountMenu();
    var rect = anchor.getBoundingClientRect();
    menu.hidden = false;
    menu.style.top = (rect.bottom + 8) + "px";
    // ヘッダー右端のカードなので、はみ出さないよう右に寄せて置き直す
    var width = menu.getBoundingClientRect().width;
    var left = Math.min(rect.left, window.innerWidth - width - 12);
    menu.style.left = Math.max(12, left) + "px";
  }

  // ------------------------------------------------------------ サインアウト
  async function signOut() {
    try {
      var res = await postJSON("/api/auth/logout");
      renderAccount(res.me);
      if (res.ok) {
        toast("サインアウトしました (az CLI 全体)");
      } else {
        toast("サインアウトできませんでした: " + (res.message || ""));
      }
    } catch (err) {
      toast("サインアウトできませんでした: " + err.message);
    }
  }

  // ------------------------------------------------- サインイン (デバイスコード)
  var authTimer = null;
  var authView = null;   // モーダル内の差し替え先 (閉じても状態は死なない)

  function stopAuthPoll() {
    if (authTimer) { clearInterval(authTimer); authTimer = null; }
  }

  function buildSignInBody() {
    var box = el("div", "auth-box");

    var code = el("div", "auth-code", "········");
    box.appendChild(code);

    var row = el("div", "auth-actions");
    var copy = el("button", "btn-sm", "コードをコピー");
    copy.type = "button";
    copy.disabled = true;
    copy.addEventListener("click", async function () {
      try {
        await navigator.clipboard.writeText(code.textContent);
        toast("コードをコピーしました");
      } catch (e) {
        toast("コピーできませんでした。画面のコードを手で入力してください");
      }
    });
    row.appendChild(copy);

    var open = document.createElement("a");
    open.className = "btn-sm";
    open.target = "_blank";
    open.rel = "noopener";
    open.href = "https://microsoft.com/devicelogin";
    open.textContent = "microsoft.com/devicelogin を開く";
    row.appendChild(open);
    box.appendChild(row);

    var hint = el("p", "auth-hint");
    hint.appendChild(icon("loader-2", "ic-14 spin"));
    hint.appendChild(el("span", null, "ブラウザでコードを入力してください…"));
    box.appendChild(hint);

    box.appendChild(el("p", "gap-src",
      "サインインすると、この Mac の az CLI のセッションとして保存されます "
      + "(Web と CLI で共通)。"));

    var msg = el("p", "auth-msg");
    msg.hidden = true;
    box.appendChild(msg);

    var cancel = el("button", "btn-sm danger", "中止");
    cancel.type = "button";
    cancel.addEventListener("click", async function () {
      stopAuthPoll();
      closeModal();
      try { await postJSON("/api/auth/cancel"); } catch (e) { /* 既に終了 */ }
      toast("サインインを中止しました");
    });
    box.appendChild(cancel);

    authView = { code: code, copy: copy, open: open, hint: hint,
                 msg: msg, cancel: cancel };
    return box;
  }

  function applyAuthStatus(status) {
    if (!authView) return;
    if (status.url) authView.open.href = status.url;
    if (status.code) {
      authView.code.textContent = status.code;
      authView.copy.disabled = false;
    }
    if (status.status === "authenticating") {
      authView.hint.lastChild.textContent = "サインインを確認しています…";
    }
  }

  function showAuthFailure(message) {
    if (!authView) { toast(message); return; }
    authView.hint.hidden = true;
    authView.msg.textContent = message;
    authView.msg.hidden = false;
    authView.copy.disabled = true;
    authView.cancel.textContent = "閉じる";
  }

  /* 2 秒ごとに GET /api/auth/login。モーダルを閉じても回し続ける — 手元に
   * コードを控えて閉じた人の手続きを、こちらの都合で無かったことにしない。 */
  function pollAuth() {
    stopAuthPoll();
    authTimer = setInterval(async function () {
      var status;
      try {
        status = await api("/api/auth/login");
      } catch (err) {
        stopAuthPoll();
        showAuthFailure("状態を確認できませんでした: " + err.message);
        return;
      }
      applyAuthStatus(status);
      if (status.status === "done") {
        stopAuthPoll();
        closeModal();
        authView = null;
        await refreshAccount();
        toast("サインインしました");
      } else if (status.status === "error") {
        stopAuthPoll();
        showAuthFailure(status.message || "サインインできませんでした");
      } else if (status.status === "idle") {
        stopAuthPoll();   // 別の経路で中止された
      }
    }, 2000);
  }

  async function startSignIn() {
    stopAuthPoll();
    authView = null;
    var status;
    try {
      status = await postJSON("/api/auth/login");
    } catch (err) {
      // 409 = 既に実行中 (裁定 AH)。今の状態に合流させる
      if (err.status === 409) {
        try { status = await api("/api/auth/login"); }
        catch (e) { toast(err.message); return; }
      } else {
        toast("サインインを開始できませんでした: " + err.message);
        return;
      }
    }
    openModal("Microsoft 365 にサインイン", buildSignInBody());
    applyAuthStatus(status);
    if (status.status === "error") {
      showAuthFailure(status.message || "サインインを開始できませんでした");
      return;
    }
    pollAuth();
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
    // 裁定 AO: Standard と Detailed が同数なのは資料側の上限であって不具合では
    // ない、と小さく添える (水増しはしないので、説明のほうを出す)
    var note = state.view.detail_note;
    if (note) {
      var span = document.createElement("span");
      span.className = "lv-note";
      span.textContent = note;
      counts.appendChild(document.createElement("br"));
      counts.appendChild(span);
    }
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
    // 開ける (= 地図がある) セッションを控えておく。QA の出典チップは
    // ここにあるものだけリンクにする — 開けない id をリンクに見せると、
    // 押した先で「生成に失敗しました」が出て事故に見えるため。
    state.knownSessions = {};
    items.forEach(function (item) {
      if (item.session) state.knownSessions[item.session] = true;
    });
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
    state.files = files;
    renderAttachments();
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

  // ------------------------------------------------------- 添付チップ
  // アップロードした資料を入力欄の直上に出す。何を材料に地図を作るのかが
  // 一目で分かり、× でその場から取り消せる (inbox/ から実削除)。
  function renderAttachments() {
    var row = $("#attach-row");
    clear(row);
    var names = state.attachments.filter(function (name) {
      return state.files.some(function (f) { return f.name === name; });
    });
    state.attachments = names;
    row.hidden = !names.length;
    names.forEach(function (name) {
      var meta = state.files.find(function (f) { return f.name === name; }) || {};
      var info = extInfo(meta.ext || "");
      var chip = el("div", "attach-chip");
      chip.appendChild(icon(info.icon, "ic-14 " + info.cls));
      var label = el("span", "attach-name", name);
      label.title = name;
      chip.appendChild(label);
      if (meta.size !== undefined) {
        chip.appendChild(el("span", "attach-size", fmtSize(meta.size)));
      }
      var close = el("button", "icon-btn");
      close.type = "button";
      close.title = name + " を削除";
      close.appendChild(icon("x", "ic-12"));
      close.addEventListener("click", function () { removeAttachment(name); });
      chip.appendChild(close);
      row.appendChild(chip);
    });
  }

  async function removeAttachment(name) {
    try {
      await api("/api/files/" + encodeURIComponent(name), { method: "DELETE" });
    } catch (err) {
      toast("削除できませんでした: " + err.message);
      return;
    }
    state.attachments = state.attachments.filter(function (n) { return n !== name; });
    toast(name + " を削除しました");
    refreshFiles();
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
    state.editMode = false;
    state.pickFrom = null;
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
        causal_verify: state.settings.causalVerify,
        learned: state.settings.learned !== false,
        layers: state.settings.layers !== false,
        test_cache: state.settings.testCache === true
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

  // 地図なしの回答 (basic / vector と R2b の local / global / hybrid)。
  // QA 経路は summary.sources に出典が付く。**根拠を辿れない答えを地の文だけで
  // 出さない**のがここの要点で、チップは概念/関係/テーマの別と出自セッションを
  // 見せ、開けるセッションはクリックでその地図へ戻れる (設計 §2 の表示)。
  function answerBubble(summary) {
    var bubble = el("div", "bubble-ai", summary.answer || "");
    var sources = summary.sources || [];
    var info = summary.qa || null;
    // 再利用の告知は答えの**前**に置く (黙って再利用しない: 裁定 X)
    var reuse = cacheNotice(summary);
    if (reuse) bubble.insertBefore(reuse, bubble.firstChild);
    if (!sources.length && !info) return bubble;

    var foot = el("div", "qa-foot");
    if (sources.length) {
      foot.appendChild(el("p", "qa-foot-label", "出典 " + sources.length + " 件"));
      var line = el("div", "chip-line");
      sources.slice(0, 12).forEach(function (src) {
        var kind = SOURCE_KIND[src.kind] || { label: "出典", cls: "" };
        var where = [];
        if (src.session) where.push(src.session);
        if (src.document_id) where.push(src.document_id);
        var text = kind.label + ": " + (src.label || "");
        var openable = src.session && state.knownSessions[src.session];
        var chip = el(openable ? "button" : "span", "chip-sm" + kind.cls, text);
        chip.title = where.length ? where.join(" / ") : "出自の記録なし";
        if (openable) {
          chip.type = "button";
          chip.classList.add("chip-link");
          chip.addEventListener("click", function () {
            openSession(src.session, null, null);
          });
        }
        line.appendChild(chip);
      });
      foot.appendChild(line);
      if (sources.length > 12) {
        foot.appendChild(el("p", "qa-foot-note",
          "… 他 " + (sources.length - 12) + " 件"));
      }
    } else if (info) {
      foot.appendChild(el("p", "qa-foot-label", "出典: なし"));
    }

    if (info) {
      var bits = ["LLM " + (info.llm_calls || 0) + " call"];
      if (info.cache_hits) bits.push("要約キャッシュ命中 " + info.cache_hits);
      if (info.sessions && info.sessions.length) {
        bits.push("セッション " + info.sessions.length);
      }
      if (info.communities && info.communities.length) {
        bits.push("テーマ " + info.communities.length);
      }
      if (info.truncated) bits.push("近傍は上限で打ち切り");
      if (info.budget_exceeded) bits.push("呼び出し上限に到達");
      if (info.insufficient) bits.push("材料不足");
      if (info.offline) bits.push("オフライン");
      foot.appendChild(el("p", "qa-foot-note", bits.join(" / ")));
    }
    bubble.appendChild(foot);
    return bubble;
  }

  async function onJobDone(job, card) {
    var summary = job.summary || {};
    state.summary = summary;
    if (summary.answer) {              // basic / vector / QA 経路 (地図なし)
      card.remove();
      $("#thread").appendChild(answerBubble(summary));
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
    state.layers = null;
    state.verdicts = {};
    state.satisfaction = 0;
    state.tab = "map";
    state.editMode = false;
    state.pickFrom = null;
    state.mapZoom = 1.0;

    var card = el("div", "card");
    card.id = "result-card";
    $("#thread").appendChild(card);
    card.appendChild(el("p", "card-title", "読み込み中…"));

    try {
      state.detail = await api("/api/sessions/" + encodeURIComponent(session));
      state.layers = await loadLayers(session);
      var level = state.settings.level;
      if (LEVELS.indexOf(level) < 0) level = state.detail.default_level;
      await loadMap(level, card);
    } catch (err) {
      renderError(card, err.message, null);
      return;
    }
    refreshHistory();
  }

  // 多層分析のサイドカー (R2a 設計書 §10)。R2a 以前の地図は 404 が正常なので、
  // **失敗を握りつぶして null にする** — 地図そのものは層が無くても開ける。
  // null と「主張 0 件」は区別する (null なら機械タグの節を出さない)。
  async function loadLayers(session) {
    try {
      return await api("/api/sessions/" + encodeURIComponent(session) + "/layers");
    } catch (err) {
      return null;
    }
  }

  // nanopub_id -> 主張。エッジの claim_refs から本文を引くための索引
  function claimIndex() {
    var index = {};
    ((state.layers && state.layers.claims) || []).forEach(function (c) {
      if (c.nanopub_id) index[c.nanopub_id] = c;
    });
    return index;
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

    // --- 再利用の告知 (裁定 X)。**黙って再利用しない** ---
    var reuse = cacheNotice(summary);
    if (reuse) card.appendChild(reuse);

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

    // --- Work IQ タイムアウトのフォールバック警告 (黙ってローカル限定にしない) ---
    var ing = summary.ingest || {};
    if (ing.note) {
      var iw = el("div", "chip-line");
      iw.appendChild(el("span", "chip-sm warn", ing.note));
      card.appendChild(iw);
    }

    // --- 描画フォールバック警告 (ライブキャンバスへ描けなかった回) ---
    if (summary.render_fallback && summary.render_note) {
      var rw = el("div", "chip-line");
      rw.appendChild(el("span", "chip-sm warn", summary.render_note));
      card.appendChild(rw);
    }

    // --- ラベルの重なり警告 (レイアウト重なり設計書 裁定 AC) ---
    // 逃げ場が無くて重ねたときは黙らない。CLI 側 (_summary_body) と同じ情報。
    var ov = summary.overlaps;
    if (ov && ov.clean === false) {
      var un = ov.unresolved_labels || [];
      var msg = "ラベルの重なりを完全には解消できませんでした";
      if (un.length) {
        msg += " — 逃げ場なし " + un.length + " 件 ("
          + un.slice(0, 5).map(function (u) { return u.edge; }).join(", ") + ")";
      }
      var warn = el("div", "chip-line");
      warn.appendChild(el("span", "chip-sm warn", "⚠️ " + msg));
      card.appendChild(warn);
    }

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

    // --- 多層分析チップ (R2a 設計書 §10) ---
    // 数は layers サイドカー (state.layers.stats) を一次情報にする。生成直後の
    // summary にも同じ数があるが、履歴から開いた時は summary が無いため。
    var lstats = (state.layers && state.layers.stats) || null;
    if (lstats && lstats.claims) {
      var rchips = el("div", "chip-line");
      rchips.appendChild(el("span", "chip-sm", "主張 " + (lstats.claims || 0)
        + " 件 (検証済 " + (lstats.validated || 0) + ")"));
      if (lstats.rejected) {
        rchips.appendChild(el("span", "chip-sm grey", "却下 " + lstats.rejected));
      }
      rchips.appendChild(el("span", "chip-sm" + (lstats.refutes ? "" : " grey"),
        "矛盾 " + (lstats.refutes || 0) + " 件"));
      card.appendChild(rchips);
    }

    // --- 学習チップ (§8.2)。何を機械が自動適用したかを必ず見せる ---
    var learned = summary.learned;
    if (learned && learned.enabled && learnedCount(learned)) {
      var lchips = el("div", "chip-line");
      var lbtn = el("button", "chip-sm learn", " " + learnedSummaryText(learned));
      lbtn.type = "button";
      lbtn.title = "適用した内訳を表示";
      lbtn.insertBefore(icon("school", "ic-12"), lbtn.firstChild);
      lbtn.addEventListener("click", function () { openLearnedDetails(learned); });
      lchips.appendChild(lbtn);
      card.appendChild(lchips);
    }

    // --- 使用量チップ (裁定 Z)。測れなかった実行には出さない ---
    var tchip = tokenChip(summary.tokens);
    if (tchip) card.appendChild(tchip);

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

  /* テストモードで前回の結果を再利用したときの告知 (裁定 X)。
   * 「黙って再利用しない」が原則なので、結果より**前**に、目立つ形で出す。
   * これが出ないまま古い地図が返ると、直したはずの挙動が直っていないように
   * 見えて、キャッシュの存在ごと信用を失う。 */
  function cacheNotice(summary) {
    var cache = (summary || {}).cache;
    if (!cache || !cache.hit) return null;
    var box = el("div", "chip-line");
    box.appendChild(el("span", "chip-sm", cache.note
      || ("♻ 前回の結果を再利用 (テストモード / " + (cache.age_min || 0) + " 分前)")));
    // 判定はサーバ (pipeline._replay_render) が state に畳んである。ここで
    // 生フィールドから読み直すと、同じ summary に CLI と違う文言が出る
    var render = cache.render || {};
    if (render.state === "redrawn") {
      box.appendChild(el("span", "chip-sm grey",
        "canvas へ再描画 " + (render.elements || 0) + " 要素"));
    } else if (render.state === "reused_files") {
      box.appendChild(el("span", "chip-sm grey", "出力ファイルは前回のまま"));
    } else if (render.state === "failed") {
      box.appendChild(el("span", "chip-sm", "⚠ 再描画できませんでした"));
    }
    return box;
  }

  /* トークン使用量のチップ (裁定 Z)。**測れなかった実行には何も出さない** —
   * 0 と表示すると「使っていない」と読めるが、実際は「測っていない」なので。 */
  function tokenChip(tokens) {
    if (!tokens || typeof tokens.calls !== "number") return null;
    var line = el("div", "chip-line");
    if (!tokens.calls) {
      line.appendChild(el("span", "chip-sm green", "LLM 呼び出しなし (0 call)"));
      return line;
    }
    if (tokens.unknown && tokens.unknown >= tokens.calls) {
      line.appendChild(el("span", "chip-sm grey",
        "トークン: 不明 (LLM " + tokens.calls + " call)"));
      return line;
    }
    line.appendChild(el("span", "chip-sm", "入力 "
      + (tokens.input || 0).toLocaleString() + " token"));
    line.appendChild(el("span", "chip-sm", "出力 "
      + (tokens.output || 0).toLocaleString() + " token"));
    line.appendChild(el("span", "chip-sm grey", "LLM " + tokens.calls + " call"));
    return line;
  }

  function learnedCount(report) {
    return (report.renames || 0) + (report.stoplisted || 0)
      + (report.causal_allow || 0) + (report.causal_deny || 0);
  }

  function learnedSummaryText(report) {
    var parts = [];
    if (report.renames) parts.push("改名 " + report.renames);
    if (report.stoplisted) parts.push("除外 " + report.stoplisted);
    var causal = (report.causal_allow || 0) + (report.causal_deny || 0);
    if (causal) parts.push("因果上書き " + causal);
    if (report.reversed) parts.push("向き修正 " + report.reversed);
    return "学習を適用: " + parts.join("・");
  }

  function openLearnedDetails(report) {
    var box = el("div");
    box.appendChild(el("p", null,
      "過去にあなたが直した内容を、今回の生成へ自動で適用した一覧です。"));
    (report.details || []).forEach(function (d) {
      var row = el("div", "learn-row");
      row.appendChild(el("span", "chip-sm grey learn-kind",
        LEARNED_KIND_LABEL[d.kind] || d.kind));
      var text = d.kind === "rename" ? "「" + d.from + "」→「" + d.to + "」"
        : d.kind === "stoplist" ? "「" + d.label + "」を除外"
          : "「" + d.from + "」→「" + d.to + "」";
      row.appendChild(el("span", null, text));
      box.appendChild(row);
    });
    if (!(report.details || []).length) {
      box.appendChild(el("p", "gap-src", "適用された項目はありません"));
    }
    box.appendChild(el("p", "gap-src",
      "「学習」はモデルの再学習ではありません。用語辞書・除外リスト・因果の"
      + "上書きを決定的に当てているだけで、設定の「過去の修正から学習を適用」"
      + "を切れば止まります。"));
    openModal("学習の適用内訳", box);
  }

  // ---------------------------------------------------------- 編集モード
  function clearPick() {
    state.pickFrom = null;
    var picked = document.querySelectorAll(".cc-node.is-pick");
    for (var i = 0; i < picked.length; i++) picked[i].classList.remove("is-pick");
  }

  function setEditMode(on) {
    state.editMode = !!on;
    clearPick();
    hidePopover();
    renderResult($("#result-card"));
    toast(state.editMode
      ? "編集モード: ノード/関係をクリックして修正します"
      : "編集モードを終了しました");
  }

  function learnedDeltaText(delta) {
    if (!delta || !delta.changed) return "";
    var names = {
      lexicon: "用語辞書", lexicon_auto: "自動改名", stoplist: "除外候補",
      stoplist_auto: "自動除外", causal_overrides: "因果上書き", few_shot: "事例"
    };
    var keys = Object.keys(delta.changed);
    if (!keys.length) return "";
    return "学習 " + keys.map(function (k) {
      var v = delta.changed[k];
      return (names[k] || k) + (v > 0 ? "+" + v : String(v));
    }).join("・");
  }

  /* 編集を 1 件送る。成功したら地図を取り直して即時反映する
   * (詳細度切替と同じ経路なので体感は同じ速さ)。*/
  async function postEdit(op, okMessage) {
    if (!state.session) return null;
    var level = (state.view && state.view.level) || state.settings.level;
    var url = "/api/sessions/" + encodeURIComponent(state.session)
      + "/edits?level=" + encodeURIComponent(level);
    var res;
    try {
      res = await postJSON(url, op);
    } catch (err) {
      toast("編集できませんでした: " + err.message);
      return null;
    }
    hidePopover();
    closeModal();
    clearPick();
    try {
      state.detail = await api("/api/sessions/" + encodeURIComponent(state.session));
    } catch (e) { /* KPI が取れなくても地図は出す */ }
    try {
      await loadMap(level);
    } catch (err) {
      toast("地図の再取得に失敗しました: " + err.message);
    }
    var extra = learnedDeltaText(res.learned_delta);
    toast((okMessage || "編集を反映しました") + (extra ? " / " + extra : ""));
    (res.warnings || []).forEach(function (w) { console.warn("edit warning: " + w); });
    return res;
  }

  function editToolbar(bar) {
    var toggle = el("button", "btn-sm" + (state.editMode ? " is-on" : ""), " 編集");
    toggle.type = "button";
    toggle.insertBefore(icon("edit", "ic-12"), toggle.firstChild);
    toggle.addEventListener("click", function () { setEditMode(!state.editMode); });
    bar.appendChild(toggle);

    var history = el("button", "btn-sm", " 編集履歴");
    history.type = "button";
    history.insertBefore(icon("history", "ic-12"), history.firstChild);
    history.addEventListener("click", openEditHistory);
    bar.appendChild(history);

    if (!state.editMode) return;
    var addNode = el("button", "btn-sm", " 概念を追加");
    addNode.type = "button";
    addNode.insertBefore(icon("plus", "ic-12"), addNode.firstChild);
    addNode.addEventListener("click", openAddNodeDialog);
    bar.appendChild(addNode);

    var addEdge = el("button", "btn-sm" + (state.pickFrom ? " is-on" : ""), " 関係を追加");
    addEdge.type = "button";
    addEdge.insertBefore(icon("link", "ic-12"), addEdge.firstChild);
    addEdge.addEventListener("click", function () {
      if (state.pickFrom) { clearPick(); renderResult($("#result-card")); return; }
      state.pickFrom = "await";      // 次のノードクリックを始点にする
      renderResult($("#result-card"));
      toast("始点にする概念をクリックしてください");
    });
    bar.appendChild(addEdge);
  }

  function renderMapTab(body) {
    var bar = el("div", "map-toolbar");
    bar.appendChild(el("span", "chip-sm", LEVEL_LABEL[state.view.level]));
    if (state.view.editable !== false) editToolbar(bar);
    var hint = el("span", "gap-src", state.editMode
      ? (state.pickFrom === "await" ? "始点の概念をクリック"
        : state.pickFrom ? "終点の概念をクリック (もう一度ボタンで取消)"
          : "ノード/関係をクリックして修正します")
      : "ノード/関係をクリックすると詳細を表示します");
    if (state.editMode) hint.className = "edit-hint";
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
    links.appendChild(excalidrawOpenButton());
    bar.appendChild(links);
    body.appendChild(bar);

    var outer = el("div", "map-outer");
    var wrap = el("div", "map-wrap" + (state.editMode ? " is-editing" : ""));
    // サーバが生成した SVG のみ innerHTML で展開する (ユーザー入力は入らない)
    wrap.innerHTML = state.svg;
    wrap.addEventListener("click", onMapClick);
    outer.appendChild(wrap);
    outer.appendChild(buildMapZoomBar(wrap));
    body.appendChild(outer);
    if (state.pickFrom && state.pickFrom !== "await") markPick(wrap, state.pickFrom);
  }

  // ---------------------------------------------------------- 地図ズーム/パン
  // Excalidraw ライクな操作: ⌘/Ctrl+ホイールでカーソル位置を中心にズーム、
  // 背景ドラッグでパン、左下のズームバーで +/-/100%/全体表示。
  // CSS transform ではなく SVG 自体の width/height 属性を書き換える方式
  // (viewBox は不変) — ベクタなので拡大しても鮮明で、スクロール範囲も自動で
  // 合い、既存のクリック/scrollIntoView/ポップオーバーの座標計算がそのまま動く。
  function clampZoom(z) { return Math.max(0.1, Math.min(5.0, z)); }

  // click イベントは pointerup の後に発火するため、パン確定後の 1 回だけ
  // 抑止するフラグ。wrap は renderResult のたびに作り直すが、onMapClick は
  // 使い回しの単一関数なのでモジュール変数で受け渡す。
  var mapPanSuppressClick = false;

  function buildMapZoomBar(wrap) {
    var mapSvg = wrap.querySelector("svg");
    var naturalW = mapSvg ? parseFloat(mapSvg.getAttribute("width")) || 0 : 0;
    var naturalH = mapSvg ? parseFloat(mapSvg.getAttribute("height")) || 0 : 0;

    function applyZoom(zoom) {
      state.mapZoom = clampZoom(zoom);
      if (mapSvg && naturalW && naturalH) {
        mapSvg.setAttribute("width", naturalW * state.mapZoom);
        mapSvg.setAttribute("height", naturalH * state.mapZoom);
      }
      label.textContent = Math.round(state.mapZoom * 100) + "%";
    }

    var bar = el("div", "map-zoombar");
    var btnOut = el("button", "zoom-btn", "−");
    btnOut.type = "button";
    btnOut.title = "縮小";
    btnOut.addEventListener("click", function () { applyZoom(state.mapZoom * 0.8); });
    var label = el("button", "zoom-label", Math.round(state.mapZoom * 100) + "%");
    label.type = "button";
    label.title = "クリックで 100% に戻します (⌘/Ctrl+ホイール、トラックパッドのピンチでも拡大縮小できます)";
    label.addEventListener("click", function () { applyZoom(1.0); });
    var btnIn = el("button", "zoom-btn", "+");
    btnIn.type = "button";
    btnIn.title = "拡大";
    btnIn.addEventListener("click", function () { applyZoom(state.mapZoom * 1.25); });
    var btnFit = el("button", "zoom-btn zoom-fit", "全体表示");
    btnFit.type = "button";
    btnFit.title = "図全体が枠に収まる大きさにします";
    btnFit.addEventListener("click", function () {
      if (!naturalW || !naturalH) return;
      var pad = mapWrapPadding(wrap);
      var availW = wrap.clientWidth - pad.left - pad.right;
      // 高さは今の clientHeight ではなく CSS の max-height (66vh) を基準に
      // する — 縮小中は枠が内容に合わせて縮んでいるので、そのまま使うと
      // 「全体表示」を押しても小さいままになる
      var availH = Math.max(wrap.clientHeight,
        Math.round(window.innerHeight * 0.66)) - pad.top - pad.bottom;
      var fit = Math.min(availW / naturalW, availH / naturalH, 1.0);
      applyZoom(fit > 0 ? fit : 1.0);
    });
    bar.appendChild(btnOut);
    bar.appendChild(label);
    bar.appendChild(btnIn);
    bar.appendChild(btnFit);

    // 初回適用 (レベル切替や編集後の再描画でも state.mapZoom を維持)
    applyZoom(state.mapZoom);

    // カーソル (ピンチ中心) の直下の点が動かないようにズームする共通処理
    function zoomAt(clientX, clientY, newZoom) {
      if (!mapSvg || !naturalW || !naturalH) return;
      var rect = wrap.getBoundingClientRect();
      var pad = mapWrapPadding(wrap);
      var offsetX = (typeof clientX === "number" ? clientX : rect.left + rect.width / 2)
        - rect.left;
      var offsetY = (typeof clientY === "number" ? clientY : rect.top + rect.height / 2)
        - rect.top;
      var oldZoom = state.mapZoom;
      var contentX = (wrap.scrollLeft + offsetX - pad.left) / oldZoom;
      var contentY = (wrap.scrollTop + offsetY - pad.top) / oldZoom;
      applyZoom(newZoom);
      wrap.scrollLeft = contentX * state.mapZoom + pad.left - offsetX;
      wrap.scrollTop = contentY * state.mapZoom + pad.top - offsetY;
    }

    // --- ⌘/Ctrl+ホイールでカーソル位置を中心にズーム。素のホイールは
    //     preventDefault しないのでネイティブのスクロールに任せる。
    //     Chrome/Edge/Firefox はトラックパッドのピンチも ctrlKey 付きの
    //     wheel イベントとして届くので、この 1 本で両方に効く ---
    wrap.addEventListener("wheel", function (e) {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      // マウスホイールは 1 ノッチ deltaY≈120 で exp 式だと一撃で 1/3 に
      // なってしまう。1 イベントの変化量を ±1.5 倍にクランプする
      // (トラックパッドのピンチは小刻みな delta で届くので影響しない)
      var factor = Math.exp(-e.deltaY * 0.01);
      factor = Math.max(1 / 1.5, Math.min(1.5, factor));
      zoomAt(e.clientX, e.clientY, state.mapZoom * factor);
    }, { passive: false });

    // --- Safari のトラックパッドピンチは wheel ではなく独自の gesture
    //     イベント (gesturestart/change/end) で届く【ピンチが効かない実測の
    //     原因】。e.scale = ピンチ開始時からの倍率なので開始時の zoom に掛ける ---
    var pinchBase = null;
    wrap.addEventListener("gesturestart", function (e) {
      e.preventDefault();
      pinchBase = state.mapZoom;
    });
    wrap.addEventListener("gesturechange", function (e) {
      e.preventDefault();
      if (pinchBase === null) return;
      zoomAt(e.clientX, e.clientY, pinchBase * (e.scale || 1));
    });
    wrap.addEventListener("gestureend", function (e) {
      e.preventDefault();
      pinchBase = null;
    });

    // --- 背景ドラッグでパン。ノード/エッジ上から始めたドラッグはクリック
    //     操作を優先してパンしない ---
    var pan = null;
    wrap.addEventListener("pointerdown", function (e) {
      if (e.button !== undefined && e.button !== 0) return;
      var onElement = e.target.closest
        && (e.target.closest(".cc-node") || e.target.closest(".cc-edge"));
      if (onElement) return;
      pan = {
        x: e.clientX, y: e.clientY, pointerId: e.pointerId,
        scrollLeft: wrap.scrollLeft, scrollTop: wrap.scrollTop, moved: false
      };
    });
    wrap.addEventListener("pointermove", function (e) {
      if (!pan || pan.pointerId !== e.pointerId) return;
      var dx = e.clientX - pan.x;
      var dy = e.clientY - pan.y;
      if (!pan.moved) {
        if (Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
        pan.moved = true;
        try { wrap.setPointerCapture(e.pointerId); } catch (err) { /* 無視 */ }
        wrap.classList.add("is-panning");
      }
      wrap.scrollLeft = pan.scrollLeft - dx;
      wrap.scrollTop = pan.scrollTop - dy;
    });
    function endPan(e) {
      if (!pan || pan.pointerId !== e.pointerId) return;
      if (pan.moved) {
        mapPanSuppressClick = true;
        wrap.classList.remove("is-panning");
        try { wrap.releasePointerCapture(pan.pointerId); } catch (err) { /* 無視 */ }
      }
      pan = null;
    }
    wrap.addEventListener("pointerup", endPan);
    wrap.addEventListener("pointercancel", endPan);

    return bar;
  }

  // .map-wrap の padding (CSS で 6px 固定だが計算値から拾って安全に保つ)
  function mapWrapPadding(wrap) {
    var cs = getComputedStyle(wrap);
    return {
      left: parseFloat(cs.paddingLeft) || 0, top: parseFloat(cs.paddingTop) || 0,
      right: parseFloat(cs.paddingRight) || 0, bottom: parseFloat(cs.paddingBottom) || 0
    };
  }

  var EXCALIDRAW_BTN_LABEL = " Excalidraw で開く";

  /* 地図ツールバーの「Excalidraw で開く」(設計書 §3)。今見ている詳細度を
   * ローカル canvas へ描画してから新しいタブで開く。canvas は 1 面しか
   * ないため置き換わることを title で明記する。 */
  function excalidrawOpenButton() {
    var btn = el("button", "linkbtn", EXCALIDRAW_BTN_LABEL);
    btn.type = "button";
    btn.title = "現在のキャンバスを置き換えます";
    btn.insertBefore(icon("edit", "ic-12"), btn.firstChild);
    btn.addEventListener("click", function () { openInExcalidraw(btn); });
    return btn;
  }

  async function openInExcalidraw(btn) {
    var level = state.view.level;
    btn.disabled = true;
    clear(btn);
    btn.appendChild(icon("loader-2", "ic-12 spin"));
    btn.appendChild(document.createTextNode(" 描画中…"));
    try {
      var res = await postJSON("/api/sessions/" + encodeURIComponent(state.session)
        + "/render?level=" + encodeURIComponent(level), {});
      var win = window.open(res.url, "_blank", "noopener");
      if (!win) {
        toastLink("ブラウザがタブを塞ぎました。", "ここをクリック", res.url);
      } else {
        toast(LEVEL_LABEL[level] + " を Excalidraw で開きました (" + res.elements + " 要素)");
      }
    } catch (err) {
      if (err.status === 503) {
        toast("ローカルの Excalidraw に接続できませんでした。"
          + ".excalidraw をダウンロードして開いてください");
      } else {
        toast("描画できませんでした: " + err.message);
      }
    } finally {
      btn.disabled = false;
      clear(btn);
      btn.appendChild(icon("edit", "ic-12"));
      btn.appendChild(document.createTextNode(EXCALIDRAW_BTN_LABEL));
    }
  }

  function markPick(wrap, nodeId) {
    var groups = wrap.querySelectorAll('[data-node-id="' + cssEscape(nodeId) + '"]');
    for (var i = 0; i < groups.length; i++) groups[i].classList.add("is-pick");
  }

  // querySelector に入れる id を安全にする (id は英数字前提だが念のため)
  function cssEscape(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }

  // ---------------------------------------------------------- 地図クリック
  function onMapClick(event) {
    if (mapPanSuppressClick) { mapPanSuppressClick = false; return; }
    var target = event.target;
    var node = target.closest ? target.closest(".cc-node") : null;
    if (node) {
      var nodeId = node.getAttribute("data-node-id");
      var isAggregate = node.getAttribute("data-kind") === "aggregate";
      if (state.editMode && state.pickFrom && !isAggregate) {
        pickForEdge(nodeId);
        return;
      }
      if (isAggregate) {
        expandAggregate(node.getAttribute("data-aggregate-id") || nodeId);
      } else if (state.editMode) {
        showNodePopover(nodeId, event.clientX, event.clientY);
      } else {
        showNodeInfo(nodeId);
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

  /* 「関係を追加」の 2 クリック。1 個目は選択スタイル、2 個目でダイアログ。 */
  function pickForEdge(nodeId) {
    if (state.pickFrom === "await") {
      state.pickFrom = nodeId;
      renderResult($("#result-card"));
      toast("終点にする概念をクリックしてください");
      return;
    }
    if (state.pickFrom === nodeId) {
      toast("同じ概念どうしは繋げません");
      return;
    }
    openAddEdgeDialog(state.pickFrom, nodeId);
  }

  function nodeById(nodeId) {
    return (state.view.nodes || []).find(function (n) { return n.id === nodeId; });
  }

  function originBadge(element) {
    if (!element || !element.origin) return null;
    return el("span", "badge-user",
      element.origin === "user_added" ? "あなたが追加" : "あなたが編集");
  }

  function placePopover(pop, x, y) {
    pop.hidden = false;
    var rect = pop.getBoundingClientRect();
    var left = Math.min(x + 12, window.innerWidth - rect.width - 12);
    var top = Math.min(y + 12, window.innerHeight - rect.height - 12);
    pop.style.left = Math.max(12, left) + "px";
    pop.style.top = Math.max(12, top) + "px";
  }

  /* 編集モードのノードポップオーバー: ラベル編集 / 削除 / ここから関係 */
  function showNodePopover(nodeId, x, y) {
    var node = nodeById(nodeId);
    if (!node) return;
    var pop = $("#popover");
    clear(pop);

    var head = el("div", "pop-head");
    head.appendChild(el("span", "chip-sm", "概念"));
    head.appendChild(el("span", null, node.label));
    var badge = originBadge(node);
    if (badge) head.appendChild(badge);
    var close = el("button", "icon-btn");
    close.type = "button";
    close.appendChild(icon("x", "ic-14"));
    close.addEventListener("click", hidePopover);
    head.appendChild(close);
    pop.appendChild(head);

    var form = el("div", "pop-form");
    form.appendChild(el("p", "pop-label", "ラベル"));
    var input = document.createElement("input");
    input.type = "text";
    input.value = node.label;
    form.appendChild(input);

    var row = el("div", "pop-row");
    var save = el("button", "btn-sm is-on", "保存");
    save.type = "button";
    save.addEventListener("click", function () {
      var label = input.value.trim();
      if (!label || label === node.label) { hidePopover(); return; }
      postEdit({ op: "rename_node", target: nodeId, payload: { label: label } },
        "概念を改名しました");
    });
    row.appendChild(save);

    var link = el("button", "btn-sm", " ここから関係");
    link.type = "button";
    link.insertBefore(icon("link", "ic-12"), link.firstChild);
    link.addEventListener("click", function () {
      hidePopover();
      state.pickFrom = nodeId;
      renderResult($("#result-card"));
      toast("終点にする概念をクリックしてください");
    });
    row.appendChild(link);

    var del = el("button", "btn-sm danger", " 削除");
    del.type = "button";
    del.insertBefore(icon("trash", "ic-12"), del.firstChild);
    del.addEventListener("click", function () {
      if (!window.confirm("「" + node.label + "」と、その関係をすべて削除します。"
        + "\n(編集履歴から取り消せます)")) return;
      postEdit({ op: "delete_node", target: nodeId }, "概念を削除しました");
    });
    row.appendChild(del);
    form.appendChild(row);
    pop.appendChild(form);
    placePopover(pop, x, y);
  }

  /* ノード/関係の編集ダイアログ (ツールバーから) */
  function openAddNodeDialog() {
    var box = el("div");
    box.appendChild(el("p", "pop-label", "概念のラベル"));
    var input = document.createElement("input");
    input.type = "text";
    input.placeholder = "例: 線量評価プロトコル";
    box.appendChild(input);

    box.appendChild(el("p", "pop-label", "所属する島"));
    var select = document.createElement("select");
    (state.view.islands || []).forEach(function (isl) {
      var opt = document.createElement("option");
      opt.value = isl.community_id;
      opt.textContent = isl.name || isl.community_id;
      select.appendChild(opt);
    });
    var newOpt = document.createElement("option");
    newOpt.value = "__new__";
    newOpt.textContent = "＋ 新しい島をつくる";
    select.appendChild(newOpt);
    box.appendChild(select);

    var add = el("button", "btn-primary", "追加する");
    add.type = "button";
    add.style.marginTop = "14px";
    add.addEventListener("click", function () {
      var label = input.value.trim();
      if (!label) { toast("ラベルを入力してください"); return; }
      var payload = select.value === "__new__"
        ? { label: label, new_island: true }
        : { label: label, community_id: select.value };
      postEdit({ op: "add_node", payload: payload }, "概念を追加しました");
    });
    box.appendChild(add);
    box.appendChild(el("p", "gap-src",
      "追加した概念には根拠スパンがありません (手動追加として記録されます)。"));
    openModal("概念を追加", box);
    setTimeout(function () { input.focus(); }, 0);
  }

  function openAddEdgeDialog(fromId, toId) {
    var from = nodeById(fromId), to = nodeById(toId);
    if (!from || !to) { clearPick(); return; }
    var box = el("div");
    box.appendChild(el("p", null, "「" + from.label + "」 → 「" + to.label + "」"));

    box.appendChild(el("p", "pop-label", "関係のラベル"));
    var input = document.createElement("input");
    input.type = "text";
    input.placeholder = "例: 影響する";
    box.appendChild(input);

    box.appendChild(el("p", "pop-label", "関係の種類"));
    var select = document.createElement("select");
    EDIT_GLYPHS.forEach(function (pair) {
      var opt = document.createElement("option");
      opt.value = pair[0];
      opt.textContent = pair[1];
      if (pair[0] === "wave") opt.selected = true;   // 既定は相関 (安全側)
      select.appendChild(opt);
    });
    box.appendChild(select);

    var add = el("button", "btn-primary", "関係を追加");
    add.type = "button";
    add.style.marginTop = "14px";
    add.addEventListener("click", function () {
      postEdit({
        op: "add_edge",
        payload: {
          from: fromId, to: toId,
          label: input.value.trim(), glyph: select.value
        }
      }, "関係を追加しました");
    });
    box.appendChild(add);
    box.appendChild(el("p", "gap-src",
      "手動で追加した関係は根拠なしとして扱われ、因果精度の集計からは"
      + "除外されます (AI の性能を測る指標のため)。"));
    openModal("関係を追加", box);
    setTimeout(function () { input.focus(); }, 0);
  }

  // ---------------------------------------------------------- 編集履歴
  async function openEditHistory() {
    var data;
    try {
      data = await api("/api/sessions/" + encodeURIComponent(state.session) + "/edits");
    } catch (err) {
      toast("編集履歴を取得できませんでした: " + err.message);
      return;
    }
    var box = el("div");
    (data.warnings || []).forEach(function (w) {
      box.appendChild(el("p", "gap-src", "⚠ " + w));
    });
    var edits = data.edits || [];
    if (!edits.length) {
      box.appendChild(el("p", "gap-src", "まだ編集はありません"));
    }
    edits.slice().reverse().forEach(function (edit) {
      box.appendChild(editRow(edit));
    });
    box.appendChild(el("p", "gap-src",
      "取り消しても履歴は消えません (取り消し行を追記する方式です)。"
      + "元の抽出結果は常に保持されています。"));
    openModal("編集履歴", box);
  }

  function editRow(edit) {
    var row = el("div", "edit-row"
      + (edit.reverted ? " is-reverted" : "")
      + (edit.op === "revert" ? " is-revert" : ""));
    var main = el("div", "edit-main");
    main.appendChild(el("p", "edit-op", editDescription(edit)));
    main.appendChild(el("p", "edit-meta",
      edit.edit_id + " · " + (edit.ts || "").replace("T", " ")
      + " · " + (edit.user || "")));
    row.appendChild(main);
    if (edit.op !== "revert" && !edit.reverted) {
      var btn = el("button", "btn-sm", " 取り消す");
      btn.type = "button";
      btn.insertBefore(icon("arrow-back-up", "ic-12"), btn.firstChild);
      btn.addEventListener("click", function () { revertEdit(edit.edit_id); });
      row.appendChild(btn);
    }
    return row;
  }

  function editDescription(edit) {
    var name = EDIT_OP_LABEL[edit.op] || edit.op;
    var payload = edit.payload || {};
    var before = edit.before || {};
    if (edit.op === "rename_node") {
      return name + ": 「" + (before.label || edit.target) + "」→「" + payload.label + "」";
    }
    if (edit.op === "delete_node") {
      return name + ": 「" + ((before.node || {}).label || edit.target) + "」";
    }
    if (edit.op === "add_node") return name + ": 「" + payload.label + "」";
    if (edit.op === "add_edge") {
      return name + ": 「" + (before.from_label || payload.from) + "」→「"
        + (before.to_label || payload.to) + "」";
    }
    if (edit.op === "revert") return name + ": " + edit.target;
    var pair = "「" + (before.from_label || "?") + "」→「" + (before.to_label || "?") + "」";
    if (edit.op === "retype_edge") {
      var label = (EDIT_GLYPHS.find(function (g) { return g[0] === payload.glyph; })
        || [payload.glyph, payload.glyph])[1];
      return name + ": " + pair + " を " + label + " へ";
    }
    if (edit.op === "relabel_edge") return name + ": " + pair + " 「" + payload.label + "」";
    return name + ": " + pair;
  }

  async function revertEdit(editId) {
    var level = (state.view && state.view.level) || state.settings.level;
    try {
      await postJSON("/api/sessions/" + encodeURIComponent(state.session)
        + "/edits/" + encodeURIComponent(editId) + "/revert?level="
        + encodeURIComponent(level), {});
    } catch (err) {
      toast("取り消せませんでした: " + err.message);
      return;
    }
    closeModal();
    try {
      state.detail = await api("/api/sessions/" + encodeURIComponent(state.session));
      await loadMap(level);
    } catch (e) { /* 地図が取れなくても取り消し自体は成立している */ }
    toast(editId + " を取り消しました");
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
    var badge = originBadge(edge);
    if (badge) head.appendChild(badge);
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
      pop.appendChild(el("p", "gap-src", edge.origin === "user_added"
        ? "手動追加 (根拠なし)"
        : "根拠スパンがありません (evidence 表示率の対象外)"));
    }
    if (edge.causal_check && edge.causal_check.reason) {
      pop.appendChild(el("p", "gap-src", "判定: " + edge.causal_check.reason));
    }
    if (edge.member_edge_ids && edge.member_edge_ids.length > 1) {
      pop.appendChild(el("p", "gap-src",
        "この線は " + edge.member_edge_ids.length + " 本の関係を束ねています"));
    }
    layersSection(pop, edge);

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

    if (state.editMode) pop.appendChild(edgeEditSection(edge, x, y));

    placePopover(pop, x, y);

    postJSON("/api/sessions/" + encodeURIComponent(state.session) + "/evaluation",
      { operation: "view_evidence", edge_id: edgeId }).catch(function () { });
  }

  /* クリック展開の「機械が何を見てこの記号にしたか」(R2a 設計書 §10)。
   *
   * これが「UI は 8 種、内部 30 種を失わない」の実装。画面の記号は畳んだ
   * 結果でしかないので、4 層 30 種のタグ・検証スコア・主張の本文をここで
   * 開いて見せる。**ユーザーが編集した関係では機械の投影は走っていない**
   * ので、その旨を明記する (機械の判断だと誤読させない)。 */
  function layersSection(pop, edge) {
    var tags = edge.layer_tags || {};
    var refs = edge.claim_refs || [];
    var validation = edge.validation || null;
    var userEdited = typeof edge.origin === "string" && edge.origin.indexOf("user") === 0;
    var hasTags = ["layer_A", "layer_B", "layer_C", "layer_D"].some(function (k) {
      return (tags[k] || []).length;
    });
    if (!hasTags && !validation && !refs.length && !userEdited) return;

    var box = el("div", "pop-form pop-sep");
    box.appendChild(el("p", "pop-label", "機械タグ (内部 30 種)"));
    if (hasTags) {
      ["layer_A", "layer_B", "layer_C", "layer_D"].forEach(function (key) {
        var values = tags[key] || [];
        if (!values.length) return;
        var line = el("div", "tag-line");
        line.appendChild(el("span", "chip-sm grey", LAYER_LABEL[key] || key));
        values.forEach(function (t) { line.appendChild(el("code", "tag", t)); });
        box.appendChild(line);
      });
    } else {
      box.appendChild(el("p", "gap-src",
        userEdited ? "機械タグはありません (この関係はあなたが指定したものです)"
          : "機械タグはありません (R2a 以前の生成、または多層分析を切った地図)"));
    }

    if (validation && validation.combined !== undefined && validation.combined !== null) {
      var label = VALIDATION_LABEL[validation.status] || validation.status || "—";
      var line2 = el("p", "gap-src",
        "検証: combined " + validation.combined + " (" + label + ")");
      if (validation.scores) {
        line2.title = Object.keys(validation.scores).map(function (k) {
          return k + "=" + validation.scores[k];
        }).join(" / ");
      }
      box.appendChild(line2);
      if (validation.requires_human_review) {
        box.appendChild(el("p", "gap-src", "※ 裏付けが足りません。人の確認が要ります"));
      }
    }

    var index = claimIndex();
    refs.slice(0, 3).forEach(function (ref) {
      var claim = index[ref];
      if (!claim) return;
      var text = (claim.assertion && claim.assertion.claim_text) || "";
      var status = (claim.validation && claim.validation.status) || "";
      var quote = el("div", "quote", "「" + text + "」");
      quote.appendChild(el("div", "quote-src",
        "主張 " + ref + (status ? " · " + (VALIDATION_LABEL[status] || status) : "")));
      box.appendChild(quote);
    });

    if (userEdited) {
      box.appendChild(el("p", "gap-src",
        "表示はあなたの指定です (機械の投影は上書きしていません)"));
    }
    pop.appendChild(box);
  }

  /* 根拠ポップオーバーに足す編集セクション (§8.2)。
   * ラベル編集 / 種類 (8 記号) / 向き反転 / 削除。 */
  function edgeEditSection(edge, x, y) {
    var box = el("div", "pop-form pop-sep");
    box.appendChild(el("p", "pop-label", "関係のラベル"));
    var input = document.createElement("input");
    input.type = "text";
    input.value = edge.label || "";
    box.appendChild(input);

    var row1 = el("div", "pop-row");
    var save = el("button", "btn-sm is-on", "ラベルを保存");
    save.type = "button";
    save.addEventListener("click", function () {
      postEdit({ op: "relabel_edge", target: edge.id,
        payload: { label: input.value.trim() } }, "ラベルを変更しました");
    });
    row1.appendChild(save);
    box.appendChild(row1);

    box.appendChild(el("p", "pop-label", "種類"));
    var row2 = el("div", "pop-row");
    EDIT_GLYPHS.forEach(function (pair) {
      var btn = el("button", "btn-sm" + (edge.glyph === pair[0] ? " is-on" : ""), pair[1]);
      btn.type = "button";
      btn.disabled = edge.glyph === pair[0];
      btn.addEventListener("click", function () {
        postEdit({ op: "retype_edge", target: edge.id, payload: { glyph: pair[0] } },
          pair[1] + " に変更しました");
      });
      row2.appendChild(btn);
    });
    box.appendChild(row2);

    var row3 = el("div", "pop-row");
    var rev = el("button", "btn-sm", " 向きを反転");
    rev.type = "button";
    rev.insertBefore(icon("arrows-exchange", "ic-12"), rev.firstChild);
    rev.addEventListener("click", function () {
      postEdit({ op: "reverse_edge", target: edge.id }, "向きを反転しました");
    });
    row3.appendChild(rev);

    var del = el("button", "btn-sm danger", " 削除");
    del.type = "button";
    del.insertBefore(icon("trash", "ic-12"), del.firstChild);
    del.addEventListener("click", function () {
      postEdit({ op: "delete_edge", target: edge.id }, "関係を削除しました");
    });
    row3.appendChild(del);
    box.appendChild(row3);

    if (edge.glyph !== "arrow") {
      box.appendChild(el("p", "gap-src",
        "因果にすると 3 点セットの検証は通しません (人間の判断が優先されます)。"));
    }
    return box;
  }

  function hidePopover() { $("#popover").hidden = true; }

  // ---------------------------------------------------------- 説明パネル
  function showInfoPopover(key, anchor) {
    var pop = $("#info-pop");
    var text = INFO_TEXT[key];
    if (!text) return;
    if (!pop.hidden && pop.dataset.key === key) { hideInfoPopover(); return; }
    clear(pop);
    pop.dataset.key = key;
    var head = el("div", "pop-head");
    head.appendChild(icon("info-circle", "ic-14"));
    head.appendChild(el("span", null, text.title));
    var close = el("button", "icon-btn");
    close.type = "button";
    close.appendChild(icon("x", "ic-14"));
    close.addEventListener("click", hideInfoPopover);
    head.appendChild(close);
    pop.appendChild(head);
    pop.appendChild(el("p", null, text.body));
    var rect = anchor.getBoundingClientRect();
    pop.hidden = false;
    var width = pop.getBoundingClientRect().width;
    pop.style.left = Math.max(12,
      Math.min(rect.left - width + rect.width, window.innerWidth - width - 12)) + "px";
    pop.style.top = (rect.bottom + 8) + "px";
    anchor.classList.add("is-on");
  }

  function hideInfoPopover() {
    var pop = $("#info-pop");
    pop.hidden = true;
    pop.dataset.key = "";
    var buttons = document.querySelectorAll(".hdr-info.is-on");
    for (var i = 0; i < buttons.length; i++) buttons[i].classList.remove("is-on");
  }

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

    // 「次の一手」レポート (R2c 設計書 §2.1)。資料を横断して調べるので
    // 押した瞬間には終わらない — 進行中はボタン自身に状態を出す
    if (gaps.length) {
      var reportBtn = el("button", "btn-sm", "レポートを作る");
      reportBtn.type = "button";
      reportBtn.style.marginLeft = "auto";
      reportBtn.addEventListener("click", function () { makeGapReport(reportBtn); });
      head.appendChild(reportBtn);
    }
    body.appendChild(head);

    if (!gaps.length) {
      body.appendChild(el("p", "gap-src", "ギャップ候補はありません"));
      return;
    }
    gaps.forEach(function (gap) { body.appendChild(gapRow(gap)); });
  }

  async function makeGapReport(btn) {
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "作成中…";
    try {
      var report = await postJSON("/api/sessions/"
        + encodeURIComponent(state.session) + "/gap-report", {});
      openModal("ギャップレポート", gapReportBody(report));
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* レポート本文。finding (決定的) と suggestion (LLM の提案) を
   * **見た目で区別する** — どちらも同じ書体で並べると、検索して確かめた
   * 事実と生成された文章の区別がつかなくなる。 */
  function gapReportBody(report) {
    var box = el("div", "gap-report");
    var counts = report.counts || {};
    var head = el("div", "chip-line");
    ["structural", "discourse", "causal"].forEach(function (kind) {
      if (counts[kind]) {
        head.appendChild(el("span", "chip-sm kind-" + kind,
          (GAP_KIND_LABEL[kind] || kind) + " " + counts[kind]));
      }
    });
    head.appendChild(el("span", "chip-sm grey",
      "横断 " + (report.sessions_searched || 0) + " セッション"));
    head.appendChild(el("span", "chip-sm grey",
      report.external_used ? "外部照会あり" : "外部照会なし"));
    head.appendChild(el("span", "chip-sm grey",
      (report.kb && report.kb.note) || "kb: 未接続"));
    box.appendChild(head);

    if (!report.suggestions) {
      box.appendChild(el("p", "gap-note",
        "LLM の提案はありません (以下はすべて資料検索でわかった事実です)"));
    }

    (report.items || []).forEach(function (item) {
      var card = el("div", "gr-item");
      var top = el("div", "gr-top");
      top.appendChild(el("span", "chip-sm kind-" + item.gap_type,
        GAP_KIND_LABEL[item.gap_type] || item.gap_type));
      top.appendChild(el("span", "gap-src", item.gap_id));
      card.appendChild(top);
      if (item.reason) card.appendChild(el("p", "gr-reason", item.reason));
      card.appendChild(el("p", "gr-finding", item.finding || ""));
      if (item.suggestion) {
        var sug = el("p", "gr-suggestion");
        sug.appendChild(el("span", "gr-tag", "提案"));
        sug.appendChild(document.createTextNode(" " + item.suggestion));
        card.appendChild(sug);
      }
      (item.sources || []).forEach(function (src) {
        var where = src.name || src.document_id || "(資料不明)";
        card.appendChild(el("p", "gap-src", "出典: " + where
          + (src.label ? " 〈" + src.label + "〉" : "")
          + " · " + (src.session || "")));
      });
      (item.external || []).forEach(function (ext) {
        card.appendChild(el("p", "gap-src",
          "外部: [" + (ext.source || "") + "] " + (ext.title || "")));
      });
      box.appendChild(card);
    });

    // ダウンロードはこのアプリの流儀どおりサーバの GET へ張る
    var dl = el("a", "linkbtn", " JSON をダウンロード");
    dl.href = "/api/sessions/" + encodeURIComponent(state.session) + "/gap-report";
    dl.download = "gap_report_" + state.session + ".json";
    dl.insertBefore(icon("download", "ic-12"), dl.firstChild);
    box.appendChild(dl);
    return box;
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
    // 型バッジ (R2a 設計書 §9)。構造/言説/因果 = 何を見て見つけたか。
    // Toulmin の grounds / warrant を title に入れて、判断材料をその場で読めるようにする
    if (gap.gap_type) {
      var kind = el("span", "chip-sm kind-" + gap.gap_type,
        GAP_KIND_LABEL[gap.gap_type] || gap.gap_type);
      var toulmin = gap.toulmin || {};
      kind.title = [
        gap.detection_signal ? "検出信号: " + gap.detection_signal : "",
        toulmin.grounds ? "根拠: " + toulmin.grounds : "",
        toulmin.warrant ? "判断規則: " + toulmin.warrant : ""
      ].filter(Boolean).join("\n");
      meta.appendChild(kind);
    }
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

    // --- オフライン評価 (R2c 設計書 §1.2)。コーパス横断なので毎回取り直す ---
    var offline = el("div", "off-eval");
    body.appendChild(offline);
    if (state.offline) renderOfflineEval(offline, state.offline);
    else offline.appendChild(el("p", "off-next-label", "オフライン評価を読み込み中…"));
    loadOfflineEval(offline);
  }

  // -------------------------------------- オフライン評価 (R2c 設計書 §1.2)
  //
  // 上の満足度・チップが「この地図」の話なのに対し、ここは**溜まった判定を
  // 正解セットとして扱った累積の KPI** で、全セッション横断。同じタブに
  // 並べるが数え方が違うので、見出しで別物だと分かるようにしてある。
  var OFFLINE_METRICS = [
    ["relation_accuracy", "関係正答率"],
    ["causal_precision", "因果精度"],
    ["gap_usefulness", "ギャップ有用率"],
    ["coverage", "網羅率"]
  ];

  function meterBar(value, cls) {
    var node = el("div", "bar bar-wide");
    var fill = el("span", cls);
    var ratio = typeof value === "number" ? Math.max(0, Math.min(1, value)) : 0;
    fill.style.width = Math.round(ratio * 100) + "%";
    node.appendChild(fill);
    return node;
  }

  function offlineMetricRow(grid, label, metric) {
    var value = metric.value;
    var na = typeof value !== "number";
    grid.appendChild(el("span", "off-name", label));
    grid.appendChild(el("span", "off-value" + (na ? " na" : ""),
      na ? "—" : value.toFixed(2)));
    // 目標のある指標だけ達成/未達で色を変える。網羅率は目標が無いので既定色
    grid.appendChild(meterBar(value, metric.meets_target === true ? "hit"
      : metric.meets_target === false ? "miss" : null));
    grid.appendChild(el("span", "off-goal",
      (typeof metric.target === "number" ? "目標 " + metric.target.toFixed(2) : "目標 —")
      + " ・ n=" + (metric.n || 0)));
  }

  function renderOfflineEval(section, report) {
    clear(section);
    var head = el("div", "off-head");
    head.appendChild(icon("chart-dots-3", "ic-16"));
    head.appendChild(el("span", "off-title", "オフライン評価 (全セッション累積)"));
    head.appendChild(el("span", "chip-sm grey", "LLM 呼び出しゼロ"));
    section.appendChild(head);

    var metrics = report.metrics || {};
    var cov = metrics.coverage || {};
    var counts = metrics.labels || {};
    if (report.empty) {
      // 判定 0 件はエラーではない。集め方を書いて次の一手を示す (受け入れ基準 2)
      section.appendChild(el("p", "off-empty", report.hint));
    } else {
      var grid = el("div", "off-grid");
      OFFLINE_METRICS.forEach(function (pair) {
        offlineMetricRow(grid, pair[1], metrics[pair[0]] || {});
      });
      section.appendChild(grid);
      // 判定はあるのに 1 件も分母に入らない状態を黙って «—» で済ませない。
      // 「集めたのに何も出ない」はバグに見えるので、なぜ 0 なのかを言う
      if (counts.total && !counts.matched) {
        section.appendChild(el("p", "off-empty",
          "判定は " + counts.total + " 件ありますが、いまの知識グラフの関係とは"
          + " 1 件も一致しませんでした ("
          + (counts.missing ? "削除済み・別セッションの関係への判定 "
            + counts.missing + " 件" : "")
          + (counts.user_origin ? " / ユーザーが編集した関係 "
            + counts.user_origin + " 件は分母から除外" : "")
          + ")。下のボタンから、いま地図にある関係を判定していってください。"));
      }
    }

    var prog = el("div", "off-prog");
    [["関係", cov.gold_relations], ["ギャップ", cov.gold_gaps]].forEach(function (pair) {
      var p = pair[1] || {};
      var item = el("div", "off-prog-item");
      item.appendChild(el("span", null, "正解セット " + pair[0]));
      item.appendChild(meterBar(p.value, p.meets_target ? "hit" : null));
      item.appendChild(el("b", null, (p.n || 0) + " / " + (p.target || 0)));
      item.title = p.meets_target ? "目標に到達しています"
        : "目標まであと " + (p.remaining || 0) + " 件";
      prog.appendChild(item);
    });
    section.appendChild(prog);

    var next = report.next_unlabeled;
    var foot = el("div", "off-next");
    if (next) {
      var btn = el("button", "btn-sm", "次の未評価の関係を開く");
      btn.type = "button";
      btn.addEventListener("click", function () { openNextUnlabeled(next); });
      foot.appendChild(btn);
      foot.appendChild(el("span", "off-next-label",
        next.from_label + " → " + next.to_label
        + " ・ 未判定 " + (report.unlabeled || 0) + " 本"));
    } else {
      foot.appendChild(el("span", "off-next-label",
        "未判定の関係はありません"));
    }
    section.appendChild(foot);
    section.appendChild(el("p", "gap-src",
      "logs/evaluation.jsonl と tests/gold/*.jsonl の判定を、いまの知識グラフと"
      + "突き合わせた値です。ユーザーが編集・追加した関係は分母から除きます。"
      + "因果精度の causal_ok は gold ファイルにしかありません。"));
  }

  async function loadOfflineEval(section) {
    try {
      var report = await api("/api/evaluation/offline");
      state.offline = report;
      // タブを切り替えた後に届いた応答は捨てる (section は既に外れている)
      if (section.isConnected) renderOfflineEval(section, report);
    } catch (err) {
      clear(section);
      section.appendChild(el("p", "gap-src",
        "オフライン評価を読み込めませんでした: " + err.message));
    }
  }

  /* 「次の未評価の関係を開く」= 地図タブで該当エッジのポップオーバーを開く。
   * キューは全セッション横断なので、別セッションの関係なら先に開き直す。 */
  async function openNextUnlabeled(next) {
    try {
      if (next.session && next.session !== state.session) {
        if (!state.knownSessions[next.session]) {
          toast("この関係は履歴に無いセッションのものです: " + next.session);
          return;
        }
        await openSession(next.session, null, null);
      }
      state.tab = "map";
      renderResult($("#result-card"));
      var wrap = $(".map-wrap");
      var target = wrap && wrap.querySelector(
        '[data-edge-id="' + cssEscape(next.edge_id) + '"]');
      if (!target) {
        // overview では集約に畳まれて線が出ないことがある。黙って何も起きない
        // より、なぜ開けないかを言う
        toast("この関係は今の詳細度では表示されていません (詳細度を上げてください)");
        return;
      }
      target.scrollIntoView({ block: "center", inline: "center" });
      var rect = target.getBoundingClientRect();
      showEdgePopover(next.edge_id, rect.left + rect.width / 2,
        rect.top + rect.height / 2);
    } catch (err) {
      toast(err.message);
    }
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
    box.appendChild(checkboxField("多層分析を行う (既定 ON)", "layers"));
    box.appendChild(el("p", "gap-src",
      "文を「結果・手法・結論」などの語り口でラベル付けし、主張を取り出して"
      + "別モデルで検証します。矢印 (因果) と ⚡ (矛盾) はこの検証を通った"
      + "ものだけに点きます。切ると R1.5 と同じ、語彙証拠だけの地図になります "
      + "(生成は少し速くなります)。"));
    box.appendChild(checkboxField(
      "テストモード — 同じ依頼は前回の結果を再利用 (既定 OFF)", "testCache"));
    box.appendChild(el("p", "gap-src",
      "同じ文言・同じ設定でもう一度送ると、前回の結果をそのまま返します "
      + "(LLM を 1 回も呼ばないので費用がかかりません)。再利用したときは結果に "
      + "「♻ 前回の結果を再利用」と必ず出ます。試行錯誤で同じ依頼を繰り返すとき"
      + "だけ入れてください — 資料を更新しても新しい地図にならなくなります "
      + "(6 時間で期限切れ)。"));
    box.appendChild(checkboxField("過去の修正から学習を適用 (既定 ON)", "learned"));
    box.appendChild(el("p", "gap-src",
      "「学習」はモデルの再学習ではありません。あなたが直した用語辞書・除外"
      + "リスト・因果の上書きを決定的に当て、抽出プロンプトへ注意書きを添える"
      + "だけです。適用した内容は毎回、結果カードのチップに出ます。"));
    var show = el("button", "btn-sm", "学習している内容を見る");
    show.type = "button";
    show.addEventListener("click", openLearnedStore);
    box.appendChild(show);
    box.appendChild(el("p", "gap-src",
      "設定はこのブラウザに保存され、次のジョブ送信時に反映されます。"));
    openModal("設定", box);
  }

  /* 学習ストアの中身 (GET /api/learned)。「黙って直さない」の担保として、
   * 何が自動適用の対象になっているかをいつでも確認できるようにする。 */
  async function openLearnedStore() {
    var data;
    try {
      data = await api("/api/learned");
    } catch (err) {
      toast("学習内容を取得できませんでした: " + err.message);
      return;
    }
    var box = el("div");
    var s = data.summary || {};
    var chips = el("div", "chip-line");
    chips.appendChild(el("span", "chip-sm", "用語辞書 " + s.lexicon
      + " (自動 " + s.lexicon_auto + ")"));
    chips.appendChild(el("span", "chip-sm", "除外 " + s.stoplist
      + " (自動 " + s.stoplist_auto + ")"));
    chips.appendChild(el("span", "chip-sm grey", "因果上書き " + s.causal_overrides));
    chips.appendChild(el("span", "chip-sm grey", "事例 " + s.few_shot));
    box.appendChild(chips);

    (data.lexicon || []).forEach(function (e) {
      var row = el("div", "learn-row");
      row.appendChild(el("span", "chip-sm" + (e.auto ? "" : " grey") + " learn-kind",
        e.auto ? "自動改名" : "ヒントのみ"));
      row.appendChild(el("span", null, "「" + e.from + "」→「" + e.to + "」 ×" + e.n));
      box.appendChild(row);
    });
    (data.stoplist || []).forEach(function (e) {
      var row = el("div", "learn-row");
      row.appendChild(el("span", "chip-sm" + (e.auto ? "" : " grey") + " learn-kind",
        e.auto ? "自動除外" : "ヒントのみ"));
      row.appendChild(el("span", null, "「" + e.label + "」 ×" + e.n));
      box.appendChild(row);
    });
    (data.causal_overrides || []).forEach(function (o) {
      var row = el("div", "learn-row");
      row.appendChild(el("span", "chip-sm grey learn-kind",
        LEARNED_KIND_LABEL[o.decision] || o.decision));
      row.appendChild(el("span", null,
        "「" + o.from_label + "」→「" + o.to_label + "」"));
      box.appendChild(row);
    });
    (data.warnings || []).forEach(function (w) {
      box.appendChild(el("p", "gap-src", "⚠ " + w));
    });
    if (!s.lexicon && !s.stoplist && !s.causal_overrides) {
      box.appendChild(el("p", "gap-src",
        "まだ学習した内容はありません。地図を編集すると、その差分がここに"
        + "溜まっていきます。"));
    }
    openModal("学習している内容", box);
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
    var intro = el("p");
    var link = document.createElement("a");
    link.href = "/static/overview.html";
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "📖 Concept Cartographer の詳しい説明ページを開く";
    intro.appendChild(link);
    box.appendChild(intro);
    var list = document.createElement("ul");
    [
      "テンプレート: ホームの 4 枚をクリックすると依頼文が入ります。そのまま送信できます。",
      "詳細度: ヘッダーの Overview / Standard / Detailed。切替は再生成なし (LLM 呼び出しゼロ) で、"
      + "同じ地図の粒度だけが変わります。",
      "集約ノード (破線の楕円) をクリックすると、畳まれている概念の一覧が出ます。",
      "関係の線をクリックすると根拠の引用が出ます。正しい / 誤り / 判断不能で評価できます。",
      "ギャップタブでは候補を [有用] [却下] で確定します。確定は取り消せません (監査のため)。",
      "資料は inbox/ に置くかサイドバーからアップロードしてください (pdf / docx / txt / md)。"
      + " アップロードした資料は入力欄の上にチップで出ます (× で削除)。",
      "地図ツールバーの [編集] で編集モードに入ると、概念の改名・削除・追加、関係の"
      + "ラベル/種類/向きの変更・削除・追加ができます。元の抽出結果は書き換えず、"
      + "編集は追記ログとして残るので [編集履歴] からいつでも取り消せます。",
      "編集内容は用語辞書・除外リスト・因果の上書きとして次回以降の生成に反映されます"
      + " (設定の「過去の修正から学習を適用」で ON/OFF)。"
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
      if (event.key === "Escape") {
        closeModal(); hidePopover(); hideModeMenu(); hideInfoPopover();
        hideAccountMenu();
        if (state.pickFrom) { clearPick(); renderResult($("#result-card")); }
        // 狭幅のドロワーは「上に乗っているもの」なので Esc で閉じる
        if (state.sidebar.narrow && state.sidebar.drawerOpen) setCollapsed(true);
      }
    });

    // ヘッダーの ℹ︎ (モード / 詳細度)
    $("#btn-info-mode").addEventListener("click", function (event) {
      event.stopPropagation();
      showInfoPopover("mode", event.currentTarget);
    });
    $("#btn-info-level").addEventListener("click", function (event) {
      event.stopPropagation();
      showInfoPopover("level", event.currentTarget);
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
        (res.saved || []).forEach(function (name) {
          if (state.attachments.indexOf(name) < 0) state.attachments.push(name);
        });
        toast((res.saved || []).length + " 件アップロードしました");
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
    // アカウントのドロップダウン (サインイン/アウト)
    $("#btn-account").addEventListener("click", function (event) {
      event.stopPropagation();
      if (!$("#acct-menu").hidden) { hideAccountMenu(); return; }
      showAccountMenu(event.currentTarget);
    });

    document.addEventListener("click", function (event) {
      if (!$("#acct-menu").hidden && !event.target.closest("#acct-menu")
        && !event.target.closest("#btn-account")) hideAccountMenu();
      if (!$("#mode-menu").hidden && !event.target.closest("#mode-menu")
        && !event.target.closest("#btn-mode")) hideModeMenu();
      if (!$("#popover").hidden && !event.target.closest("#popover")
        && !event.target.closest(".map-wrap")) hidePopover();
      if (!$("#info-pop").hidden && !event.target.closest("#info-pop")
        && !event.target.closest(".hdr-info")) hideInfoPopover();
    });

    // サイドバーの折りたたみ (狭幅ではドロワーの開閉になる)
    $("#btn-collapse").addEventListener("click", function () { setCollapsed(true); });
    $("#btn-reopen").addEventListener("click", function () { setCollapsed(false); });
    $("#drawer-scrim").addEventListener("click", function () { setCollapsed(true); });
  }

  function hideModeMenu() { $("#mode-menu").hidden = true; }

  // ------------------------------------------- サイドバー (狭幅対応の状態機械)
  //
  // 状態は 3 つだけ。**表示副作用を持たない純関数**にしてあるので、DOM 無しで
  // そのまま検証できる (狭幅の回帰はスクリーンショットだけでは気づけない)。
  //   collapsed  : ユーザーが選んだ既定。**localStorage に保存されるのはこれだけ**
  //   narrow     : matchMedia(NARROW_MQ) の判定 (画面が狭いか)
  //   drawerOpen : 狭幅でドロワーを開いているか (保存しない — 一時的な状態)
  var NARROW_MQ = "(max-width:960px)";

  function sidebarView(sb) {
    var hidden = sb.narrow ? !sb.drawerOpen : !!sb.collapsed;
    return { hidden: hidden, reopen: hidden, scrim: !!sb.narrow && !hidden };
  }

  // 「閉じる/開く」操作。狭幅では**ユーザー設定を汚さない** — 画面が狭いから
  // 閉じただけなのに「サイドバーは閉じておく」を既定へ書き込むと、広い画面へ
  // 戻ったときに勝手に閉じたままになる。
  function sidebarToggle(sb, collapsed) {
    if (sb.narrow) {
      return { collapsed: sb.collapsed, narrow: true, drawerOpen: !collapsed };
    }
    return { collapsed: !!collapsed, narrow: false, drawerOpen: false };
  }

  // 幅の変化。狭幅へ入るときは必ず閉じた状態から始め、広幅へ戻るときは
  // 保存済みのユーザー設定 (collapsed) がそのまま効く。
  function sidebarResize(sb, narrow) {
    if (!!narrow === !!sb.narrow) return sb;
    return { collapsed: sb.collapsed, narrow: !!narrow, drawerOpen: false };
  }

  function applySidebar() {
    var view = sidebarView(state.sidebar);
    document.body.classList.toggle("sidebar-hidden", view.hidden);
    $("#btn-reopen").hidden = !view.reopen;
    $("#drawer-scrim").hidden = !view.scrim;
  }

  function setCollapsed(collapsed) {
    state.sidebar = sidebarToggle(state.sidebar, collapsed);
    if (!state.sidebar.narrow) {           // 狭幅の開閉は既定を書き換えない
      state.settings.collapsed = state.sidebar.collapsed;
      saveSettings();
    }
    applySidebar();
  }

  function watchNarrow() {
    var mq = window.matchMedia(NARROW_MQ);
    function onChange(event) {
      state.sidebar = sidebarResize(state.sidebar, event.matches);
      applySidebar();
    }
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);      // 古い Safari
    state.sidebar = sidebarResize(state.sidebar, mq.matches);
  }

  async function init() {
    loadSettings();
    state.sidebar = { collapsed: !!state.settings.collapsed,
                      narrow: false, drawerOpen: false };
    watchNarrow();                 // 起動時点で狭ければドロワーは閉じた状態
    applySidebar();
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

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", init);
  }

  // Node から狭幅の状態機械だけを取り出して検証するための口。ブラウザには
  // module が無いのでこの行は何も起こさない (ビルド工程なしの方針は不変)。
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { sidebarView: sidebarView, sidebarToggle: sidebarToggle,
                       sidebarResize: sidebarResize, NARROW_MQ: NARROW_MQ };
  }
})();
