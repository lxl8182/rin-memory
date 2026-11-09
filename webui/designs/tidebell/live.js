/* RIN live layer: same-origin API client for the watercolor console. */
(function () {
  "use strict";
  if (window.__RIN_LIVE_LOADED__) return;
  window.__RIN_LIVE_LOADED__ = true;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
  const rid = (prefix) => prefix + "-" + ((globalThis.crypto && crypto.randomUUID)
    ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2));

  const CLEAN_TYPE_LABELS = { fact: "事实", reflection: "反思", growth: "成长", event: "记忆" };
  const CLEAN_PAGE_SIZE = 10;

  const LIVE = {
    connected: false, snapshot: null, engine: null,
    events: [], proposals: [], selectedProposal: null,
    selectedEvent: null, selectedRevision: null,
    cleanupKind: "all", cleanupLimit: CLEAN_PAGE_SIZE,
    modalConfirm: null,
    // A persona edit that lost the revision race is kept here, so "载入最新版本"
    // can rebase the same draft instead of making the user retype it.
    personaDraft: null,
  };
  window.RIN_LIVE = LIVE;
  const SERVICE = Boolean(window.__RIN_SERVICE__);
  let writeIdentity = null;

  const SECTION_LABELS = {
    identity: "身份", tone: "语气", relationship: "相处", habits: "习惯",
    boundaries: "边界", aesthetic: "审美", interests: "兴趣", speech: "语言习惯",
    about_owner: "关于主人", dialogue_examples: "对话示例",
  };
  const SECTION_CLASS = {
    tone: "tone", relationship: "rel", boundaries: "bound", aesthetic: "hobby",
    habits: "hobby", interests: "hobby",
  };

  // A section id is a stable key, never a sentence: whitespace never belongs
  // in one and unsupported punctuation cannot survive a round trip, so the
  // editor derives the id from the human name and caps it below the backend
  // limit (120) while the name is still short enough to read.
  const NEW_SECTION_ID_MAX = 60;
  const SECTION_TEXT_MAX = 4000;
  const SECTION_SCENES = [
    ["all", "全部场景"],
    ["game", "仅游戏会话"],
    ["plugin", "仅插件会话"],
    ["session", "仅指定会话"],
  ];
  // Creation-time templates only: each one fills the same ordinary `add`
  // operation, so a preset goes through the same revision, publish, rollback
  // and freeze path as a hand-typed section. A preset is a convenience, never
  // a whitelist: any other name is equally valid.
  const SECTION_PRESET_UI = [
    { id: "about_owner", label: "关于主人", kind: "profile", evolution: true, frozen: false },
    { id: "dialogue_examples", label: "对话示例", kind: "examples", evolution: false, frozen: true },
    { id: "tone", label: "语气", kind: "tone", evolution: true, frozen: false },
    { id: "relationship", label: "相处", kind: "relationship", evolution: true, frozen: false },
    { id: "habits", label: "习惯", kind: "habits", evolution: true, frozen: false },
    { id: "interests", label: "兴趣", kind: "interests", evolution: true, frozen: false },
    { id: "boundaries", label: "边界", kind: "boundaries", evolution: true, frozen: false },
  ];
  // Engine refusals are stable codes; the editor says what to do about them
  // instead of showing a bare `duplicate_section`.
  const SECTION_REASON_LABELS = {
    duplicate_section: "已经有同名段落，请直接编辑它，不会重复新建",
    invalid_section_id: "段落标识只能用字母、数字、中文、下划线、连字符和点",
    section_id_too_long: "段落标识太长（最多 120 字）",
    invalid_operation: "保存请求不完整，请刷新后重试",
    text_too_long: "段落正文过长（最多 4000 字）",
    section_frozen: "这一段已冻结，自动演化不会改写它",
    section_not_editable: "这一段没有开放给自动演化",
    unknown_section: "找不到这个段落，请刷新后重试",
    no_change: "没有实际改动",
    base_revision_conflict: "版本已更新，草稿已保留",
    not_initialized: "还没有初始人格卡",
    no_initial_card: "还没有可恢复的初始人格卡",
    source_deleted: "段落来源已被删除",
  };
  const FIELD_STYLE = "width:100%;padding:10px 12px;border:1px solid var(--line);"
    + "background:rgba(255,253,248,.9);font:inherit;font-size:14.5px;line-height:1.7;color:var(--ink)";
  const SMALL_FIELD = "width:150px;padding:5px 8px;border:1px solid var(--line);"
    + "background:rgba(255,253,248,.9);font:inherit;font-size:13px;color:var(--ink)";
  const PERM_ROW_STYLE = "display:flex;align-items:center;gap:8px;font-size:13px;"
    + "color:var(--ink-2);margin:0 0 9px";
  const EDITOR_ERROR_STYLE = "border-left-color:rgba(157,79,102,.6);"
    + "background:linear-gradient(120deg,rgba(242,188,198,.2),rgba(242,188,198,.07))";
  const CONSUMER_LABELS = {
    chat: "网页对话", neko_chat: "N.E.K.O 对话", realtime: "实时会话", game: "游戏人格", active: "主动交流",
  };
  const TYPE_CLASS = { "对话": "t-talk", "游戏": "t-game", "更正": "t-fix", "修订": "t-per" };
  const MODE_LABELS = { off: "已关闭", propose: "提议模式", auto: "自动模式" };

  function sectionLabel(id) {
    const key = String(id || "");
    if (SECTION_LABELS[key]) return SECTION_LABELS[key];
    // Real cards imported from the host use host_profile_<中文> / host_override_<中文>.
    const stripped = key.replace(/^host_(profile|override|system_prompt)_/, "");
    return stripped || "段落";
  }

  /* ---------- section identity / permissions (shared by both editors) ---------- */

  // The backend refuses whitespace, slashes, quotes and punctuation in a new
  // section id, and it refuses to silently truncate one: a shortened id would
  // address a *different* section on the next revision. So the editor derives a
  // key that already satisfies the rule, and tells the user what it derived.
  function sectionIdFromName(name) {
    // \p{L}/\p{N} rather than \w: the backend accepts letters in any script, so
    // 「看世界的方式」 must survive instead of being stripped to nothing.
    const slug = String(name || "").replace(/\s+/g, "_")
      .replace(/[^\p{L}\p{N}_.\-]/gu, "")
      .replace(/_+/g, "_").replace(/^[_.\-]+|[_.\-]+$/g, "");
    return slug.slice(0, NEW_SECTION_ID_MAX);
  }

  function sectionIdsOf(card) {
    return new Set(((card && card.sections) || []).map((row) => String(row.section_id || "")));
  }

  // A section only renders for the subject stored in its scope, so a new section
  // copies the subject the card already uses instead of guessing "private" (a
  // card configured with another identity would hide the new section).
  function defaultSubject(card) {
    const counts = {};
    ((card && card.sections) || []).forEach((row) => {
      const subject = String(((row.scope || {}).subject) || "private");
      counts[subject] = (counts[subject] || 0) + 1;
    });
    let best = "private";
    Object.keys(counts).forEach((key) => { if (counts[key] > (counts[best] || 0)) best = key; });
    return best;
  }

  // One sentence for what a section currently permits. Freezing is stated first
  // because it wins over every other permission, and a frozen section never
  // stops the user from editing it by hand.
  function permSummary(section) {
    if (!section) return "";
    const evolution = !!(section.editable_by && section.editable_by.evolution);
    if (section.frozen) {
      return "已冻结 · 自动演化不改动"
        + (evolution ? "（允许演化被冻结覆盖）" : "") + " · 你仍可手改";
    }
    return (evolution ? "可演化 · 自动演化会改写" : "固定 · 自动演化不改") + " · 你仍可手改";
  }

  function scopeSummary(section) {
    const scope = (section && section.scope) || {};
    const subject = String(scope.subject || "private");
    const applies = scope.applies_to;
    let where = "全部场景";
    if (applies && typeof applies === "object") {
      if (applies.game_id) where = "仅游戏 " + String(applies.game_id);
      else if (applies.plugin_id) where = "仅插件 " + String(applies.plugin_id);
      else if (applies.session_id) where = "仅会话 " + String(applies.session_id);
    }
    return (subject === "private" ? "本人" : subject) + " · " + where;
  }

  function sceneOfChoice(choice, detail) {
    const value = String(detail || "").trim().slice(0, 64);
    if (choice === "game") return { game_id: value || "sts2" };
    if (choice === "plugin") return { plugin_id: value || "dsh_memory" };
    if (choice === "session") return { session_id: value || "console" };
    return null;
  }

  function sceneChoiceOf(scope) {
    const applies = (scope && scope.applies_to) || null;
    if (!applies || typeof applies !== "object") return ["all", ""];
    if (applies.game_id) return ["game", String(applies.game_id)];
    if (applies.plugin_id) return ["plugin", String(applies.plugin_id)];
    if (applies.session_id) return ["session", String(applies.session_id)];
    return ["all", ""];
  }

  function sectionReasonLabel(reason) {
    const key = String(reason || "");
    return SECTION_REASON_LABELS[key] || key || "操作被拒绝";
  }

  function sceneOptions(selected) {
    return SECTION_SCENES.map(([value, label]) => '<option value="' + value + '"'
      + (value === selected ? " selected" : "") + ">" + esc(label) + "</option>").join("");
  }

  // Permission + reach controls for one section. They are scoped to the block
  // that renders them rather than addressed by section id, so a host-imported id
  // with dots or CJK in it can never confuse a selector.
  const FROZEN_WINS_NOTE = "冻结优先于允许演化：两个都勾选时，自动演化仍然完全不会改写这一段。"
    + "解冻后才按「允许演化」判定；你手改永远不受影响。";

  function permControls(section) {
    const evolution = !!(section && section.editable_by && section.editable_by.evolution);
    const frozen = !!(section && section.frozen);
    const [scene, detail] = sceneChoiceOf((section && section.scope) || {});
    return '<div class="perm">'
      + '<label style="' + PERM_ROW_STYLE + '"><input type="checkbox" data-perm-evolution'
      + (evolution ? " checked" : "") + ">允许演化改写这一段</label>"
      + '<label style="' + PERM_ROW_STYLE + '"><input type="checkbox" data-perm-frozen'
      + (frozen ? " checked" : "") + ">冻结（自动演化完全不改）</label>"
      + '<label style="' + PERM_ROW_STYLE + '">作用范围 <select data-perm-scene style="'
      + SMALL_FIELD + '">' + sceneOptions(scene) + "</select>"
      + '<input data-perm-scene-detail value="' + esc(detail)
      + '" placeholder="可选：游戏/插件/会话标识" style="' + SMALL_FIELD
      + ';width:190px' + (scene === "all" ? ";opacity:.5" : "") + '"></label>'
      + '<p class="dim perm-note" style="font-size:12.5px;margin:2px 0 0;line-height:1.6">'
      + esc(FROZEN_WINS_NOTE) + "</p>"
      + "</div>";
  }

  // Only the evolution half of editable_by is a console choice: the engine enforces
  // `evolution` here and `user` elsewhere, so the flag is carried through untouched
  // from the section being patched (see permPatch) rather than guessed.
  function readPermControls(scopeNode) {
    const root = scopeNode || document;
    const evolution = $("[data-perm-evolution]", root);
    const frozen = $("[data-perm-frozen]", root);
    const scene = $("[data-perm-scene]", root);
    const detail = $("[data-perm-scene-detail]", root);
    return {
      editable_by: { user: true, evolution: !!(evolution && evolution.checked) },
      frozen: !!(frozen && frozen.checked),
      scene: scene ? scene.value : "all",
      sceneDetail: detail ? detail.value : "",
    };
  }

  // A section's scene reach as the engine stores it: a plugin/game/session map,
  // or nothing at all for "every scene". The backend always reads a missing map
  // as "applies everywhere", so the two spellings below are interchangeable.
  function sceneReach(scope) {
    const applies = (scope || {}).applies_to;
    if (!applies || typeof applies !== "object") return null;
    const reach = {};
    ["game_id", "plugin_id", "session_id"].forEach((key) => {
      const value = String(applies[key] || "").trim();
      if (value) reach[key] = value;
    });
    return Object.keys(reach).length ? reach : null;
  }

  // Only what changed travels as an operation; an untouched permission must not
  // rewrite a section that the user never opened.
  function permPatch(section, current) {
    const patch = {};
    const stored = section.editable_by || {};
    const wasEvolution = !!stored.evolution;
    if (wasEvolution !== current.editable_by.evolution) {
      // `user` is the engine's own flag; carry it over instead of reopening a
      // section that was deliberately closed to hand edits.
      patch.editable_by = { user: stored.user !== false, evolution: current.editable_by.evolution };
    }
    if (!!section.frozen !== current.frozen) patch.frozen = current.frozen;
    const wasReach = sceneReach(section.scope);
    const nextReach = sceneOfChoice(current.scene, current.sceneDetail);
    if (JSON.stringify(wasReach) !== JSON.stringify(nextReach)) {
      const scope = Object.assign({}, section.scope || {});
      if (nextReach) scope.applies_to = nextReach;
      else delete scope.applies_to;
      patch.scope = scope;
    }
    return patch;
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
      && a.getDate() === b.getDate();
  }

  function dayLabel(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const now = new Date();
    if (sameDay(d, now)) return "今天";
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (sameDay(d, yesterday)) return "昨天";
    return (d.getMonth() + 1) + "." + String(d.getDate()).padStart(2, "0");
  }

  function relTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return dayLabel(iso) + " " + fmtTime(iso);
  }

  function sceneLabel(scene) {
    const text = String(scene || "");
    if (text.startsWith("game:")) return "游戏会话";
    if (text === "plugin:dsh_memory") return "DSH 对话";
    if (text.startsWith("plugin:")) return "插件会话 · " + text.slice(7);
    if (text === "host:chat") return "桌面端对话";
    return text.replace(/^host:/, "").replace(/^plugin:/, "") || "记忆来源";
  }

  function sourceLabel(keys) {
    const key = (keys || [])[0] || "";
    if (key.includes(":user:")) return "对话";
    if (key.includes(":private:")) return "对话";
    if (key.startsWith("plugin:")) return "插件";
    return "引擎";
  }

  function eventType(event) {
    const scene = String(event.scene || "");
    const kind = String(event.kind || "");
    if (scene.startsWith("game:")) return "游戏";
    if (kind === "persona_revision" || kind === "revision") return "修订";
    if (String(event.content_class || "") === "correction") return "更正";
    return "对话";
  }

  function statusTag(status) {
    const map = {
      proposed: ["待审核", "wait"], accepted: ["已采纳", "ok"],
      rejected: ["已驳回", "warn"], withdrawn: ["已撤回", ""],
      active: ["活跃", "ok"], archived: ["已归档", ""], needs_rebuild: ["需重建", "warn"],
    };
    const row = map[status] || [status || "—", ""];
    return '<span class="st ' + row[1] + '">' + esc(row[0]) + "</span>";
  }

  function toast(message, actionLabel, onAction) {
    const node = $("#toast");
    if (!node) return;
    node.innerHTML = "";
    const span = document.createElement("span");
    span.textContent = message;
    node.appendChild(span);
    if (actionLabel) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "t-act";
      button.textContent = actionLabel;
      button.addEventListener("click", () => {
        node.classList.remove("on");
        if (onAction) onAction();
      });
      node.appendChild(button);
    }
    node.classList.add("on");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.remove("on"), actionLabel ? 5600 : 2800);
  }

  async function api(path, options, retried) {
    const init = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
    if (SERVICE) {
      path = path.replace(/^\/api\//, "/v1/");
      if (writeIdentity) Object.assign(init.headers, {
        "X-Companion-Client": writeIdentity.client_id,
        "X-Companion-Token": writeIdentity.write_token,
      });
    }
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (SERVICE && response.status === 401 && !retried && path !== "/v1/bootstrap") {
      writeIdentity = await api("/v1/bootstrap");
      return api(path, options, true);
    }
    if (!response.ok) {
      const detail = (payload && (payload.error || payload)) || {};
      const error = new Error((detail.code || response.status) + " · " + (detail.message || "请求失败"));
      error.status = response.status;
      // A refusal carries a stable reason instead of prose: the card editor
      // turns `duplicate_section` and friends back into one actionable
      // sentence, so the user never has to read a bare engine code.
      error.code = String(detail.code || response.status);
      error.reason = error.code === "rejected" ? String(detail.message || "") : String(detail.reason || "");
      error.payload = detail;
      throw error;
    }
    return payload;
  }

  function decisionToast(result, success) {
    if (!result) return "未完成";
    if (result.conflict) return "版本冲突，请刷新后重试";
    if (result.status === "rejected") return "未处理：" + (result.reason || "引擎拒绝");
    if (result.duplicate) return "这条记录已经处理过了";
    return success;
  }

  async function write(path, body, success) {
    try {
      const result = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
      if (path.startsWith("/api/persona/")) CHAT.loaded = false;
      await refresh();
      if (success) toast(typeof success === "function" ? success(result) : success);
      return result;
    } catch (error) {
      toast("操作失败：" + error.message);
      return null;
    }
  }

  /* ---------- chrome ---------- */

  function renderChrome() {
    const { meta, overview } = LIVE.snapshot;
    const stats = overview.stats || {};
    const latest = (overview.recent_events || [])[0];
    const navMode = $(".nav-mode");
    if (navMode) navMode.innerHTML = '<i></i>已连接 · 本地引擎';
    const metaNode = $(".mast .meta");
    if (metaNode) {
      metaNode.innerHTML = "第 " + esc(stats.persona_revision || 0) + " 版 · "
        + esc(stats.active_days || 0) + " 天 · <b>" + esc(stats.events || 0) + "</b> 条记忆 · <b>"
        + esc(stats.facts || 0) + "</b> 条事实";
    }
    const footerStatus = $(".foot-right span");
    if (footerStatus) {
      footerStatus.textContent = "最近写入 " + (latest ? relTime(latest.occurred_at) : "—") + " · 本地引擎";
    }
    const topMode = $("#topMode");
    if (topMode) {
      const who = meta.mode === "live" || meta.mode === "service" ? (meta.character || "—") : "演示";
      topMode.textContent = "本地记忆 · " + (meta.read_only ? "只读" : "可写") + " · 角色 " + who;
    }
    const settingsSub = $('[data-page="settings"] .page-sub');
    if (settingsSub) settingsSub.textContent = "本机 · 改动走引擎，不直接写存储";
    document.title = "铃芽 RIN · 记忆管理台";
  }

  /* ---------- overview ---------- */

  function renderOverview() {
    const snapshot = LIVE.snapshot;
    const overview = snapshot.overview || {};
    const stats = overview.stats || {};
    const activity = overview.activity || [];
    const persona = snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const proposals = (snapshot.evolution && snapshot.evolution.proposals) || [];
    const pendingProps = proposals.filter((row) => row.status === "proposed");
    const pendingConsumers = (persona.pending || []).length;
    const todayCount = activity.length ? Number(activity[activity.length - 1].count || 0) : 0;

    const lead = $(".sum-lead .v b");
    if (lead) lead.textContent = todayCount;
    const cells = $(".sum-cells");
    if (cells) {
      cells.innerHTML = [
        ["events", "记忆事件"], ["facts", "沉淀事实"],
        ["reflections", "反思成长"], ["persona_revision", "人格修订"],
      ].map(([key, label]) => '<div><b>' + esc(stats[key] || 0) + "</b><span>" + label + "</span></div>").join("");
    }

    const tide = $("#tide");
    if (tide) {
      const counts = activity.map((row) => Number(row.count || 0));
      const max = Math.max(1, ...counts);
      tide.innerHTML = '<span class="tide-tip" id="tideTip"></span>';
      activity.forEach((row, index) => {
        const bar = document.createElement("button");
        bar.type = "button";
        bar.className = "bar" + (index === activity.length - 1 ? " today" : "");
        bar.style.left = (index * (100 / activity.length)) + "%";
        bar.style.width = (100 / activity.length * 0.68) + "%";
        bar.style.height = Math.max(6, Math.round(Number(row.count || 0) / max * 100)) + "%";
        bar.style.setProperty("--i", index);
        bar.dataset.tip = dayLabel(row.date + "T12:00:00") + " · " + Number(row.count || 0) + " 条";
        bar.setAttribute("aria-label", bar.dataset.tip);
        bar.addEventListener("mouseenter", showTideTip);
        bar.addEventListener("focus", showTideTip);
        bar.addEventListener("mouseleave", hideTideTip);
        bar.addEventListener("blur", hideTideTip);
        tide.appendChild(bar);
      });
    }
    const tideX = $(".tide-x");
    if (tideX) {
      tideX.innerHTML = "<span>21 天前</span><span>今天 · " + todayCount + " 条</span>";
    }

    const attention = $(".attention span");
    if (attention) {
      if (!pendingProps.length && !pendingConsumers) {
        attention.textContent = "暂无待处理事项，记忆在安静地生长。";
      } else {
        attention.innerHTML = "<b>" + pendingProps.length + "</b> 项提案待审核 · <b>"
          + pendingConsumers + "</b> 处发布等待生效";
      }
    }

    renderOverviewPersona(card, persona, pendingProps.length);
    renderRecentEvents(overview.recent_events || []);
    renderPending(pendingProps);
    renderPublishCard(persona);
    renderQuiet(snapshot);
  }

  function showTideTip(event) {
    const tip = $("#tideTip");
    const tide = $("#tide");
    if (!tip || !tide) return;
    const bar = event.currentTarget;
    tip.textContent = bar.dataset.tip || "";
    const barRect = bar.getBoundingClientRect();
    const tideRect = tide.getBoundingClientRect();
    tip.style.left = (barRect.left - tideRect.left + barRect.width / 2) + "px";
    tip.style.top = (barRect.top - tideRect.top) + "px";
    tip.classList.add("on");
  }

  function hideTideTip() {
    const tip = $("#tideTip");
    if (tip) tip.classList.remove("on");
  }

  function renderOverviewPersona(card, persona, pendingCount) {
    const meta = (LIVE.snapshot && LIVE.snapshot.meta) || {};
    const character = meta.mode === "service" ? String(meta.display_name || meta.character || "").trim()
      : meta.mode === "live" ? String(meta.character || "").trim() : "";
    const nm = $(".identity .nm");
    if (nm && character) nm.textContent = character;
    const portrait = $(".identity .portrait img");
    if (portrait && character) portrait.alt = character;
    const mini = $(".topbar .char .mini");
    if (mini && character) mini.alt = character;
    const topChar = $(".topbar .char b");
    if (topChar && character) topChar.textContent = character;
    const sections = card.sections || [];
    const frozen = sections.filter((row) => row.frozen).length;
    const evolvable = sections.filter((row) => !row.frozen && row.editable_by && row.editable_by.evolution).length;
    const sub = $(".identity .sub");
    if (sub) sub.textContent = "当前角色 · 第 " + (card.revision || 0) + " 版 · "
      + (persona.stale ? "待同步到下一轮对话" : "已同步到引擎投影");
    const nums = $(".identity .nums");
    if (nums) {
      nums.innerHTML = "<span>可演化 <b>" + evolvable + "</b></span><span>冻结 <b>" + frozen
        + "</b></span><span class=\"warn\">待处理 <b>" + pendingCount + "</b></span>";
    }
    const secs = $('[data-page="overview"] .secs');
    const wanted = ["tone", "relationship", "boundaries", "aesthetic"];
    let shownSections = wanted.map((id) => sections.find((row) => row.section_id === id)).filter(Boolean);
    if (!shownSections.length) {
      const preferred = ["核心特质", "说话风格", "互动关系", "背景故事"];
      shownSections = preferred
        .map((zh) => sections.find((row) => sectionLabel(row.section_id) === zh))
        .filter(Boolean);
    }
    if (!shownSections.length) {
      shownSections = sections.filter((row) => String(row.text || "").trim()).slice(0, 4);
    }
    if (secs) {
      let rows = shownSections;
      if (!rows.length) {
        // Imported card: prefer the sections that read best as a summary.
        const preferred = ["核心特质", "说话风格", "互动关系", "背景故事"];
        rows = preferred
          .map((zh) => sections.find((row) => sectionLabel(row.section_id) === zh))
          .filter(Boolean);
      }
      if (!rows.length) rows = sections.filter((row) => String(row.text || "").trim()).slice(0, 4);
      secs.innerHTML = rows.map((section) => {
        const cls = SECTION_CLASS[section.section_id] || "";
        const badge = section.frozen
          ? '<span class="badge frz">已冻结 · 自动演化不改动</span>'
          : '<span class="badge">' + (section.editable_by && section.editable_by.evolution ? "可演化" : "固定") + "</span>";
        return '<div class="sec ' + cls + '"><div class="hd"><b>' + esc(sectionLabel(section.section_id))
          + "</b>" + badge + '<span class="right">第 ' + esc(section.revision || 0) + ' 版</span></div><p>'
          + esc(section.text).replace(/\n/g, "<br>") + "</p></div>";
      }).join("");
    }
    const cardHint = $('[data-page="overview"] .col:first-child .card .card-h .hint');
    if (cardHint) cardHint.textContent = "第 " + (card.revision || 0) + " 版 · " + sections.length + " 段";
    const publishHint = $('[data-page="overview"] .col:last-child .card + .card .card-h .hint');
    if (publishHint) publishHint.textContent = "第 " + (card.revision || 0) + " 版";
    const revs = persona.revisions || [];
    const latest = revs[0];
    const revline = $(".revline");
    if (revline) {
      const changed = latest ? (latest.changed_sections || []).map(sectionLabel).join("、") : "";
      revline.innerHTML = '<span class="k">最近修订</span><b>第 ' + esc(latest ? latest.revision : 0)
        + " 版</b><span>" + esc(latest ? relTime(latest.at) : "—") + (changed ? " · " + esc(changed) : "")
        + '</span><a data-nav="persona" href="#persona">全部修订 →</a>';
    }
    const footer = $('[data-page="overview"] .card .card-f .dim');
    if (footer) {
      const shown = new Set(shownSections.map((row) => row.section_id));
      const rest = sections.filter((row) => !shown.has(row.section_id));
      footer.textContent = rest.length ? "其余 " + rest.length + " 段：" + rest.map((row) => sectionLabel(row.section_id)).join(" · ") : "全部段落已在上方展示";
    }
  }

  function renderRecentEvents(events) {
    const list = $("#tlHome");
    const all = $("#tlAllLink");
    const total = Number((LIVE.snapshot.overview.stats || {}).events || events.length);
    if (all) all.textContent = "全部 " + total + " 条";
    const hint = $('[data-page="overview"] .col:first-child .card:nth-child(2) .card-h .hint');
    if (hint) hint.textContent = "最近 " + Math.min(6, events.length) + " 条";
    if (!list) return;
    list.innerHTML = events.slice(0, 6).map((event) => {
      const type = eventType(event);
      return '<article class="tl-row" data-t="' + esc(type) + '" data-live-event="' + esc(event.id) + '">'
        + '<span class="tl-time">' + fmtTime(event.occurred_at) + '</span><span class="tag ' + (TYPE_CLASS[type] || "t-talk") + '">'
        + esc(type) + '</span><div class="tl-body">' + esc(event.text)
        + '<span class="tl-src">' + dayLabel(event.occurred_at) + " · " + esc(sceneLabel(event.scene))
        + ((event.fact_ids || []).length ? " · " + event.fact_ids.length + " 条事实" : "")
        + "</span></div></article>";
    }).join("");
  }

  function proposalTitle(proposal) {
    const operations = proposal.operations || [];
    return Array.from(new Set(operations.map((op) => sectionLabel(op.section_id)))).join("、")
      + " · " + (operations.some((op) => op.op === "replace") ? "修订" : "调整");
  }

  function proposalDiff(proposal) {
    const changes = proposal.changes || (proposal.operations || []).map((op) => ({
      section_id: op.section_id, before_available: false, after: op.op === "delete" ? "" : op.text || "",
    }));
    const text = (change, side) => {
      const value = side === "before" && !change.before_available
        ? "（第 " + proposal.base_revision + " 版原文已不可用）" : change[side] || "（空段落）";
      return (changes.length > 1 ? sectionLabel(change.section_id) + "：\n" : "") + value;
    };
    return { before: changes.map((change) => text(change, "before")).join("\n\n"),
      after: changes.map((change) => text(change, "after")).join("\n\n"),
      section: changes.map((change) => sectionLabel(change.section_id)).join("、"),
      noop: changes.length > 0 && changes.every((change) => change.noop === true) };
  }

  function publicationState(persona) {
    const consumers = persona.consumers || {};
    // `inline` is the service's own conversation: it reads the current
    // published version per turn and has no adoption step to wait on.
    const names = Object.keys(consumers)
      .filter((name) => consumers[name].status !== "not_connected" && consumers[name].status !== "inline");
    const current = (persona.versions && persona.versions.current) || {};
    const adopted = names.filter((name) => current.version && consumers[name].version === current.version && consumers[name].status !== "failed");
    const pending = names.filter((name) => !adopted.includes(name));
    const notices = [];
    if (persona.stale) notices.push(current.version ? "有人格修改尚未发布" : "人格尚未发布");
    if (current.version) pending.forEach((name) => notices.push((CONSUMER_LABELS[name] || name)
      + (consumers[name].status === "failed" ? "采用失败" : "等待采用新版本")));
    return { names, adopted, pending, notice: notices.join("；") };
  }

  function renderPending(proposals) {
    const list = $("#decList");
    const hint = $("#decHint");
    if (!list) return;
    if (!proposals.length) {
      list.innerHTML = '<div class="empty">没有待处理的提案</div>';
      if (hint) hint.textContent = "已全部处理";
    } else {
      list.innerHTML = proposals.slice(0, 2).map((proposal) => {
        const diff = proposalDiff(proposal);
        const same = diff.noop;
        return '<article class="dec" data-live-key="' + esc(proposal.proposal_id) + '">'
          + '<div class="dec-t">' + esc(proposalTitle(proposal)) + "</div>"
          + (same
            ? '<div class="diff"><span class="lab">内容</span><p>' + esc(diff.after) + '</p><span class="lab">提示</span><p class="dim" style="color:var(--ink-3)">提案修改前后完全一致，采纳不会改变文本。</p></div>'
            : '<div class="diff"><span class="lab">修改前</span><p><del>' + esc(diff.before) + "</del></p>"
              + '<span class="lab">改为</span><p><ins>' + esc(diff.after) + "</ins></p></div>")
          + '<div class="dec-src">来源 · ' + esc(proposal.reason || "演化提案") + " · " + relTime(proposal.created_at) + "</div>"
          + '<div class="dec-act">'
          + (same ? "" : '<button type="button" class="btn pri" data-live-prop-accept="' + esc(proposal.proposal_id)
            + '">采纳</button>')
          + '<button type="button" class="btn' + (same ? " pri" : "") + '" data-live-prop-reject="' + esc(proposal.proposal_id)
          + '">' + (same ? "驳回这条空提案" : "驳回") + '</button><span class="ago">'
          + relTime(proposal.created_at) + "</span></div></article>";
      }).join("");
      if (hint) hint.innerHTML = "显示 " + Math.min(2, proposals.length) + " 项 / 共 " + proposals.length + " 项";
    }
    const showAll = $("#showAll");
    if (showAll) showAll.textContent = proposals.length ? "查看全部 " + proposals.length + " 项 →" : "已全部处理";
    const pend = $("#pendNum");
    if (pend) pend.textContent = proposals.length;
    const badge = $("#evoBadge");
    if (badge) badge.textContent = proposals.length || "";
    const attn = $("#attnProp");
    if (attn) attn.textContent = proposals.length;
  }

  function renderPublishCard(persona) {
    const consumers = persona.consumers || {};
    const current = (persona.versions && persona.versions.current) || {};
    const publication = publicationState(persona);
    const names = publication.names;
    const adopted = names.filter((name) => {
      const row = consumers[name] || {};
      return row.version && row.version === current.version && row.status !== "failed";
    }).length;
    const total = Math.max(names.length, 1);
    const big = $('[data-page="overview"] .pub-big b');
    if (big) big.textContent = adopted + "/" + total;
    const bar = $('[data-page="overview"] .pub-bar');
    if (bar) {
      bar.innerHTML = Array.from({ length: total }, (_, index) =>
        '<i class="' + (index < adopted ? "ok" : "wait") + '"></i>').join("");
    }
    const cardFooter = $('[data-page="overview"] .col:last-child .card + .card .card-f');
    if (cardFooter) {
      const history = (persona.versions && persona.versions.history) || [];
      const previous = history[1];
      cardFooter.innerHTML = (previous ? '<a data-nav="publish" href="#publish">回滚点 · 第 ' + esc(previous.revision) + ' 版</a>' : '<span class="dim">暂无回滚点</span>')
        + '<a data-drawer="publish" href="#publish">查看详情 →</a>';
    }
    const wait = $('[data-page="overview"] .pub-wait');
    if (wait) {
      const pending = names.filter((name) => {
        const row = consumers[name] || {};
        return !(row.version && row.version === current.version);
      });
      wait.innerHTML = '<i></i>' + (pending.length
        ? esc(CONSUMER_LABELS[pending[0]] || pending[0]) + " · 等待采用<b>" + relTime(current.published_at) + "</b>"
        : "已接入的使用方均已采用<b>" + relTime(current.published_at) + "</b>");
    }
  }

  function renderQuiet(snapshot) {
    const block = $(".quiet-block");
    if (!block) return;
    const reflections = (snapshot.reflections.reflections || []).slice(0, 1);
    const growth = (snapshot.reflections.growth || []).slice(0, 1);
    const rows = [];
    reflections.forEach((row) => {
      rows.push('<div class="quiet-row"><span class="tag ' + (row.promoted ? "t-talk" : "t-fix") + '">'
        + (row.promoted ? "已晋升" : "待晋升") + "</span><p>" + esc(row.text) + '</p><span class="t">'
        + dayLabel(row.created_at) + "</span></div>");
    });
    growth.forEach((row) => {
      rows.push('<div class="quiet-row"><span class="tag t-game">成长</span><p>' + esc(row.text)
        + '</p><span class="t">' + dayLabel(row.created_at) + "</span></div>");
    });
    const header = block.querySelector(".quiet-h");
    block.innerHTML = (header ? header.outerHTML : "") + (rows.join("") || '<div class="empty">暂无反思</div>');
  }

  /* ---------- timeline ---------- */

  function renderTimeline() {
    const events = (LIVE.snapshot.timeline && LIVE.snapshot.timeline.events) || [];
    LIVE.events = events;
    window.RIN_TOTAL = events.length;
    const title = $("#tlTitle");
    if (title) title.textContent = "时间流 · " + events.length + " 条";
    const sub = $('[data-page="timeline"] .page-sub');
    if (sub) sub.textContent = events.length
      ? "最近写入 " + relTime(events[0].occurred_at) + " · 按时间倒序" : "还没有写入记忆";

    const chips = $('[data-page="timeline"] .toolbar .chips');
    if (chips) {
      const counts = {};
      events.forEach((event) => {
        const type = eventType(event);
        counts[type] = (counts[type] || 0) + 1;
      });
      const order = ["对话", "游戏", "更正", "修订"].filter((type) => counts[type]);
      chips.innerHTML = '<button type="button" class="chip on" data-filter="全部">全部 <span class="n">'
        + events.length + "</span></button>" + order.map((type) =>
          '<button type="button" class="chip" data-filter="' + type + '">' + type
          + ' <span class="n">' + counts[type] + "</span></button>").join("");
    }

    const list = $('[data-page="timeline"] .tl');
    if (list) {
      const groups = new Map();
      events.forEach((event) => {
        const label = dayLabel(event.occurred_at);
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(event);
      });
      const html = [];
      groups.forEach((rows, label) => {
        html.push('<div class="tl-day">' + esc(label) + "<span>" + rows.length + " 条</span></div>");
        rows.forEach((event) => {
          const type = eventType(event);
          html.push('<article class="tl-row filterable" data-t="' + esc(type)
            + '" data-time="' + relTime(event.occurred_at) + '" data-src="' + esc(sceneLabel(event.scene))
            + '" data-id="' + esc(event.id) + '" data-full="' + esc(event.text)
            + '" data-source="' + esc(event.source_key) + '" data-live-event="' + esc(event.id) + '">'
            + '<span class="tl-time">' + fmtTime(event.occurred_at) + '</span><span class="tag '
            + (TYPE_CLASS[type] || "t-talk") + '">' + esc(type) + '</span><div class="tl-body">'
            + esc(event.text) + '<span class="tl-src">' + dayLabel(event.occurred_at) + " · "
            + esc(sceneLabel(event.scene)) + ((event.fact_ids || []).length ? " · " + event.fact_ids.length + " 条事实" : "")
            + ((event.reflection_ids || []).length ? " · " + event.reflection_ids.length + " 条反思" : "")
            + "</span></div></article>");
        });
      });
      list.innerHTML = html.join("")
        + '<div class="empty" id="tlEmpty"' + (html.length ? " hidden" : "") + '>'
        + (html.length ? "没有匹配的记忆" : "引擎还没有记录到记忆 · 新对话开始后会出现在这里")
        + "</div>";
    }
    const pager = $('[data-page="timeline"] .pager');
    if (pager) pager.style.display = "none";
    const sum = $("#tlSum");
    const foot = $("#tlFoot");
    if (sum) sum.textContent = "共 " + events.length + " 条";
    if (foot) foot.textContent = "共 " + events.length + " 条";
    if (events.length) {
      const first = $('[data-page="timeline"] .tl-row');
      if (first) selectLiveEvent(first.dataset.liveEvent);
    }
  }

  function selectLiveEvent(eventId) {
    const events = LIVE.events || [];
    const event = events.find((row) => row.id === eventId);
    if (!event) return;
    LIVE.selectedEvent = event;
    $$('[data-page="timeline"] .tl-row').forEach((row) => {
      row.classList.toggle("selected", row.dataset.liveEvent === eventId);
    });
    const type = eventType(event);
    const tag = $("#dtTag");
    if (tag) { tag.className = "tag " + (TYPE_CLASS[type] || "t-talk"); tag.textContent = type; }
    const full = $("#dtFull"); if (full) full.textContent = event.text;
    const time = $("#dtTime"); if (time) time.textContent = relTime(event.occurred_at);
    const src = $("#dtSrc"); if (src) src.textContent = sceneLabel(event.scene);
    const typeNode = $("#dtType"); if (typeNode) typeNode.textContent = type;
    const id = $("#dtId"); if (id) id.textContent = event.id;
    const state = $("#dtState");
    if (state) {
      const nFact = (event.fact_ids || []).length;
      const nRefl = (event.reflection_ids || []).length;
      if (nFact) {
        state.textContent = "已沉淀为事实" + (nRefl ? " · 已参与 " + nRefl + " 条反思" : "");
      } else if (nRefl) {
        state.textContent = "已参与 " + nRefl + " 条反思";
      } else {
        // 抽取已完成且确实无可记内容（如问句），不是"还在等"
        state.textContent = "已分析 · 无客户记为事实";
      }
    }
    const rel = $("#dtRel");
    if (rel) {
      const facts = (LIVE.snapshot.facts.facts || []).filter((row) => (event.fact_ids || []).includes(row.id));
      const reflections = (LIVE.snapshot.reflections.reflections || []).filter((row) => (event.reflection_ids || []).includes(row.id));
      const rows = facts.map((row) => '<div class="rel-item"><span class="tag t-fix">事实</span>'
        + esc(row.text) + "<b>" + esc(row.id) + "</b></div>")
        .concat(reflections.map((row) => '<div class="rel-item"><span class="tag t-talk">反思</span>'
          + esc(row.text) + "<b>" + esc(row.id) + "</b></div>"));
      rel.innerHTML = rows.length ? rows.join("") : '<div class="rel-item"><span class="tag">无</span>已分析，这条经历没有可沉淀的事实或反思</div>';
    }
  }

  /* ---------- facts ---------- */

  function renderFacts() {
    const facts = (LIVE.snapshot.facts && LIVE.snapshot.facts.facts) || [];
    const title = $('[data-page="facts"] .page-title');
    if (title) title.textContent = "事实 · " + facts.length + " 条";
    const sub = $('[data-page="facts"] .page-sub');
    const absorbed = facts.filter((row) => row.absorbed).length;
    if (sub) sub.textContent = absorbed + " 条已吸收 · " + (facts.length - absorbed) + " 条待吸收 · 事实由引擎抽取";

    const chips = $('[data-page="facts"] .toolbar .chips');
    if (chips) {
      chips.innerHTML = [
        ["全部", facts.length], ["已吸收", absorbed], ["未吸收", facts.length - absorbed],
      ].map(([label, count], index) => '<button type="button" class="chip' + (index === 0 ? " on" : "")
        + '" data-status="' + label + '">' + label + ' <span class="n">' + count + "</span></button>").join("");
    }
    const body = $('[data-page="facts"] tbody');
    if (body) {
      body.innerHTML = facts.map((fact) => {
        const status = fact.absorbed ? "已吸收" : "未吸收";
        return '<tr data-status="' + status + '"><td class="content">' + esc(fact.text)
          + '</td><td><span class="st ' + (fact.absorbed ? "ok" : "wait") + '">' + status
          + '</span></td><td class="dim">' + sourceLabel(fact.source_keys) + " · " + dayLabel(fact.created_at)
          + '</td><td class="num">' + (typeof fact.confidence === "number" ? fact.confidence.toFixed(2) : "未评分")
          + '</td><td class="mono">' + esc(fact.id) + "</td></tr>";
      }).join("");
    }
    const tools = $('[data-page="facts"] .page-tools');
    if (tools) tools.innerHTML = '<a class="btn" href="/api/export" download="rin-memory.json">导出 JSON</a>';
    const footer = $('[data-page="facts"] .card-f');
    if (footer) footer.innerHTML = '<span class="dim">未提供置信度的事实显示「未评分」· 人格演化依据独立经历与有效证据</span><a href="/api/export" download="rin-memory.json">导出 JSON →</a>';
  }

  /* ---------- reflections ---------- */

  function renderReflections() {
    const reflections = (LIVE.snapshot.reflections.reflections || []);
    const growth = (LIVE.snapshot.reflections.growth || []);
    const title = $('[data-page="reflections"] .page-title');
    if (title) title.textContent = "反思 · " + reflections.length + " 条";
    const pending = reflections.filter((row) => !row.promoted).length;
    const sub = $("#reflSub");
    if (sub) sub.textContent = pending + " 条待晋升 · " + growth.length + " 条成长 · 晋升由引擎维护循环处理";
    const hint = $("#reflListHint");
    if (hint) hint.textContent = reflections.length + " 条反思 · " + growth.length + " 条成长";

    const list = $('[data-page="reflections"] .panel .card-b[style*="padding:0"]');
    if (list) {
      const rows = reflections.map((row, index) => {
        const status = row.promoted ? "已晋升" : row.needs_rebuild ? "需重建" : "待晋升";
        const cls = row.promoted ? "st ok" : row.needs_rebuild ? "st warn" : "st wait";
        return '<div class="refl-row' + (index === 0 ? " selected" : "") + '" data-r="live-' + index
          + '" data-live-refl="' + esc(row.id) + '"><h3>' + esc(row.text) + "</h3><p>证据 "
          + esc(row.evidence_count || 0) + " 条 · 强度 " + esc(row.evidence_strength || 0)
          + '</p><div class="meta"><span class="tag t-talk">证据 ×' + esc(row.evidence_count || 0)
          + '</span><span class="' + cls + '">' + status + "</span></div></div>";
      }).concat(growth.map((row, index) =>
        '<div class="refl-row" data-r="live-g' + index + '" data-live-growth="' + esc(row.id)
        + '"><h3>' + esc(row.text) + '</h3><p>成长 · 支持 ' + esc(row.support_count || 0)
        + ' 条来源</p><div class="meta"><span class="tag t-game">成长</span><span class="st ok">'
        + esc({ active: "生效中", archived: "已归档", withdrawn: "已撤回" }[row.status] || row.status || "生效中")
        + "</span></div></div>")).join("");
      list.innerHTML = rows || '<div class="empty">暂无反思</div>';
    }
    if (reflections.length) selectLiveReflection(reflections[0].id);
    else if (growth.length) selectLiveReflection(growth[0].id);
    else selectLiveReflection(null);
  }

  function selectLiveReflection(reflectionId) {
    const reflections = LIVE.snapshot.reflections.reflections || [];
    const growth = LIVE.snapshot.reflections.growth || [];
    const row = reflections.find((item) => item.id === reflectionId);
    const grow = growth.find((item) => item.id === reflectionId);
    $$('[data-page="reflections"] .refl-row').forEach((node) => {
      node.classList.toggle("selected", node.dataset.liveRefl === reflectionId || node.dataset.liveGrowth === reflectionId);
    });
    const hint = $("#reflHint");
    const title = $("#reflTitle");
    const reason = $("#reflReason");
    const ev = $("#reflEv");
    const suggest = $("#reflSuggest");
    const foot = $("#reflFoot");
    const actions = $('[data-page="reflections"] .detail-act');
    if (!row && !grow) {
      if (hint) hint.textContent = "暂无反思";
      if (title) title.textContent = "暂无反思详情";
      if (reason) reason.textContent = "生成反思或成长后，在左侧选择查看。";
      if (ev) ev.innerHTML = '<tr><td colspan="3" class="dim">暂无证据</td></tr>';
      if (suggest) suggest.textContent = "有新的真实经历时，引擎会按维护计划整理。";
      if (foot) foot.textContent = "尚无可查看的条目";
      if (actions) actions.innerHTML = "";
      return;
    }
    if (grow) {
      if (hint) hint.textContent = grow.id + " · 成长";
      if (title) title.textContent = grow.text;
      if (reason) reason.textContent = "这条成长来自 " + (grow.support_count || 0) + " 条独立来源，已进入人格投影。";
      if (ev) ev.innerHTML = (grow.source_refs || []).map((key) =>
        '<tr><td>' + esc(key) + '</td><td class="dim">—</td><td><span class="strength lv3"><i class="on"></i><i class="on"></i><i class="on"></i></span></td></tr>').join("");
      if (suggest) suggest.textContent = "成长条目由引擎维护；如需删除，请到清理页处理。";
      if (foot) foot.textContent = "支持 " + (grow.support_count || 0) + " 条来源";
      if (actions) actions.innerHTML = '<a class="btn ghost" data-nav="cleanup" href="#cleanup">去清理 →</a>';
      return;
    }
    const status = row.promoted ? "已晋升" : row.needs_rebuild ? "需重建" : "待晋升";
    if (hint) hint.textContent = row.id + " · " + status;
    if (title) title.textContent = row.text;
    if (reason) reason.textContent = "来自 " + (row.evidence_count || 0) + " 条来源 · 证据强度 " + (row.evidence_strength || 0);
    if (ev) ev.innerHTML = (row.evidence || []).map((item) => {
      const level = Math.max(1, Math.min(3, Math.round(Number(item.weight || 1) * 2)));
      const bars = [1, 2, 3].map((n) => '<i class="' + (n <= level ? "on" : "") + '"></i>').join("");
      return '<tr><td>' + esc(item.source_key) + '</td><td class="dim">权重 ' + esc(item.weight)
        + '</td><td><span class="strength lv' + level + '">' + bars + "</span></td></tr>";
    }).join("") || '<tr><td colspan="3" class="dim">暂无证据</td></tr>';
    if (suggest) suggest.textContent = row.promoted
      ? "这条反思已经晋升为成长，并参与人格投影。"
      : row.needs_rebuild ? "来源被更正或删除，引擎会在维护循环中重建这条反思。" : "证据达到阈值后，引擎会在维护循环中晋升这条反思。";
    if (foot) foot.textContent = "证据 " + (row.evidence_count || 0) + " 条 · 强度 " + (row.evidence_strength || 0);
    if (actions) actions.innerHTML = '<span class="dim" style="font-size:13px">晋升由引擎维护循环处理</span>'
      + '<a class="btn ghost" data-nav="evolution" href="#evolution">查看演化 →</a>';
  }

  /* ---------- persona ---------- */

  function uniqueVersions(rows) {
    const seen = new Set();
    return rows.filter((row) => {
      const key = String(row.version || "");
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function renderPersona() {
    if (SERVICE && $("#qqPersonaPanel") && qqPersonaRevision === null && !qqPersonaBusy) loadQqPersona();
    const persona = LIVE.snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const sections = card.sections || [];
    const revisions = persona.revisions || [];
    const historyHint = $('[data-page="persona"] .split.persona .panel:nth-child(2) .card-h .hint');
    if (historyHint) historyHint.textContent = revisions.length + " 个版本";
    const frozen = sections.filter((row) => row.frozen).length;
    const evolvable = sections.filter((row) => !row.frozen && row.editable_by && row.editable_by.evolution).length;
    const title = $('[data-page="persona"] .page-title');
    if (title) title.textContent = "人格卡 · 第 " + (card.revision || 0) + " 版";
    const sub = $('[data-page="persona"] .page-sub');
    if (sub) sub.textContent = sections.length + " 段 · " + evolvable + " 可演化 / " + frozen
      + " 冻结 · 冻结优先于允许演化 · 编辑会创建新版本";
    const secHint = $("#secHint");
    if (secHint) secHint.textContent = "第 " + (card.revision || 0)
      + " 版 · 冻结优先于允许演化：冻结段自动演化完全不改，解冻后才看「允许演化」";
    const tools = $('[data-page="persona"] .page-tools');
    if (tools) tools.innerHTML = '<button type="button" class="btn" data-live-refresh>刷新数据</button>'
      + '<button type="button" class="btn" data-live-new-section>新增段落</button>'
      + '<button type="button" class="btn pri" data-live-edit-persona>编辑人格卡</button>';

    const grid = $('[data-page="persona"] .secs.wide');
    if (grid) {
      grid.innerHTML = sections.map((section) => {
        const cls = SECTION_CLASS[section.section_id] || "";
        const evolution = !!(section.editable_by && section.editable_by.evolution);
        const badge = section.frozen
          ? '<span class="badge frz">已冻结 · 自动演化不改动</span>'
          : '<span class="badge">' + (evolution ? "可演化" : "固定") + "</span>";
        return '<div class="sec ' + cls + '"><div class="hd"><b>' + esc(sectionLabel(section.section_id))
          + "</b>" + badge + '<span class="right">第 ' + esc(section.revision || 0) + ' 版</span></div><p>'
          + esc(section.text).replace(/\n/g, "<br>") + '</p><div class="sec-actions">'
          + '<button type="button" data-live-edit="' + esc(section.section_id) + '">编辑</button>'
          + '<button type="button" data-live-freeze="' + esc(section.section_id) + '" data-frozen="' + (section.frozen ? "1" : "0") + '">'
          + (section.frozen ? "解冻" : "冻结") + "</button>"
          + '<span class="dim" style="font-size:12px;margin-left:auto;white-space:nowrap" title="'
          + esc(permSummary(section) + " · " + scopeSummary(section)) + '">'
          + esc(scopeSummary(section)) + "</span></div></div>";
      }).join("");
    }
    const list = $('[data-page="persona"] .split.persona .panel:nth-child(2) .card-b');
    if (list) {
      const rows = revisions.map((revision, index) => {
        const changed = (revision.changed_sections || []).map(sectionLabel).join("、") || "—";
        const published = persona.versions && persona.versions.current;
        const state = published && revision.revision === published.revision ? "已发布"
          : revision.revision === card.revision ? "待发布" : revision.origin === "imported" ? "初始版本" : "已归档";
        return '<div class="ver-row' + (index === 0 ? " selected" : "") + '" data-v="live-' + revision.revision
          + '" data-live-revision="' + revision.revision + '"><span class="v">第 ' + revision.revision
          + ' 版</span><span class="c">' + esc(changed) + " · " + esc(revision.origin || "user_edit")
          + '</span><span class="s">' + state + "</span></div>";
      }).join("");
      list.innerHTML = rows + '<div class="diff-panel" id="liveDiffPanel"></div>';
    }
    const history = uniqueVersions((persona.versions && persona.versions.history) || []);
    const footer = $('[data-page="persona"] .split.persona .panel:nth-child(2) .card-f');
    if (footer) {
      const previous = history[1];
      footer.innerHTML = '<a data-nav="publish" href="#publish">发布与生效 →</a>'
        + (previous ? '<a data-live-rollback="' + esc(previous.version) + '">回滚到上一版</a>' : '<span class="dim">暂无回滚点</span>');
    }
    if (revisions.length) showLiveRevision(revisions[0].revision);
  }

  let qqPersonaRevision = null;
  let qqPersonaBusy = false;

  async function loadQqPersona() {
    await editQqPersona(false);
  }

  async function editQqPersona(save) {
    if (qqPersonaBusy || (save && qqPersonaRevision === null)) return;
    qqPersonaBusy = true;
    const panel = $("#qqPersonaPanel");
    const field = $("#qqPersonaText");
    const status = $("#qqPersonaStatus");
    panel.hidden = false;
    field.disabled = $("#qqPersonaSave").disabled = $("#qqPersonaReload").disabled = true;
    status.textContent = save ? "保存中" : "读取中";
    try {
      const value = await api("/v1/qq/persona", save ? {
        method: "POST", body: JSON.stringify({ text: field.value, base_revision: qqPersonaRevision }),
      } : undefined);
      qqPersonaRevision = value.revision;
      field.value = value.text;
      status.textContent = value.text ? "第 " + value.revision + " 版" : "使用 QQ 预设";
      if (save) toast("QQ 私聊人设已保存");
    } catch (error) {
      status.textContent = error.status === 409
        ? "已有新版本。草稿已保留，可先复制草稿再重新读取。"
        : (save ? "保存失败：" : "读取失败：") + error.message;
    } finally {
      qqPersonaBusy = false;
      field.disabled = $("#qqPersonaSave").disabled = qqPersonaRevision === null;
      $("#qqPersonaReload").disabled = false;
    }
  }

  function showLiveRevision(revisionNumber) {
    const persona = LIVE.snapshot.persona || {};
    const revisions = persona.revisions || [];
    const revision = revisions.find((row) => row.revision === Number(revisionNumber));
    const panel = $("#liveDiffPanel");
    if (!revision || !panel) return;
    $$('[data-page="persona"] .ver-row').forEach((node) => {
      node.classList.toggle("selected", Number(node.dataset.liveRevision) === Number(revisionNumber));
    });
    const ops = revision.operations || [];
    const changed = (revision.changed_sections || []).map(sectionLabel).join("、") || "—";
    panel.innerHTML = '<div class="h"><b>第 ' + revision.revision + " 版</b><span>"
      + esc(revision.origin || "user_edit") + " · " + relTime(revision.at) + "</span></div>"
      + '<div class="row"><span class="lab">段落</span><p>' + esc(changed) + "</p></div>"
      + '<div class="row"><span class="lab">说明</span><p>' + esc(revision.reason || "—") + "</p></div>"
      + ops.slice(0, 3).map((op) => '<div class="row"><span class="lab">' + esc(op.op === "replace" ? "改为" : op.op)
        + "</span><p>" + esc(op.text || "—") + "</p></div>").join("");
  }

  /* ---------- publish ---------- */

  function renderPublish() {
    const persona = LIVE.snapshot.persona || {};
    const raw = persona.versions && persona.versions.current;
    const current = raw && typeof raw === "object" ? raw : null;
    const currentVersion = current ? String(current.version || "") : "";
    const tokenHint = $('[data-page="publish"] .panel .card-h .hint');
    if (tokenHint) tokenHint.textContent = "版本令牌 " + (currentVersion || "尚未发布");
    const history = uniqueVersions((persona.versions && persona.versions.history) || []);
    const consumers = persona.consumers || {};
    const names = publicationState(persona).names;
    const adopted = currentVersion ? names.filter((name) => {
      const row = consumers[name] || {};
      return row.version && row.version === currentVersion && row.status !== "failed";
    }).length : 0;
    const total = Math.max(names.length, 1);
    const title = $('[data-page="publish"] .page-title');
    if (title) title.textContent = current ? "发布与生效 · 第 " + current.revision + " 版" : "发布与生效";
    const sub = $('[data-page="publish"] .page-sub');
    if (sub) sub.textContent = current
      ? adopted + "/" + total + " 已生效 · 发布于 " + relTime(current.published_at)
      : "尚未发布";
    const tools = $('[data-page="publish"] .page-tools');
    if (tools) tools.innerHTML = '<button type="button" class="btn" data-live-publish="next_turn">发布到下一轮</button>';
    const historyHint = $("#pubHistoryHint");
    if (historyHint) historyHint.textContent = history.length > 1 ? (history.length - 1) + " 个可用回滚点" : "暂无回滚点";
    const actionHint = $("#pubActionHint");
    if (actionHint) actionHint.textContent = current ? "第 " + current.revision + " 版" : "尚未发布";

    const card = $('[data-page="publish"] .panel .card-b');
    if (card) {
      const rows = Object.keys(consumers).map((name) => {
        const row = consumers[name] || {};
        const disconnected = row.status === "not_connected";
        const inline = row.status === "inline";
        const done = currentVersion && row.version === currentVersion && row.status !== "failed";
        const saved = !currentVersion && row.status === "saved" && row.version;
        const state = disconnected ? "未接入" : inline && !done ? "本机对话" : done ? "已采用" : saved ? "已保存" : row.version ? "待生效" : "待同步";
        const detail = disconnected ? "接入后报告版本采用状态" : inline && !done ? "每轮读取当前发布版本，无需采用"
          : done ? "版本 " + esc(String(row.version).slice(0, 8))
            : saved
              ? "等待第一次发布"
              : row.version ? "版本 " + esc(String(row.version).slice(0, 8)) : "尚未采用任何版本";
        return '<div class="consumer' + (disconnected || (inline && !done) ? " offline" : done || saved ? "" : " wait") + '"><div class="n"><i></i>'
          + esc(CONSUMER_LABELS[name] || name) + '</div><div class="st">' + state + '</div><div class="t">'
          + detail + (row.reason ? " · " + esc(row.reason) : "") + "</div></div>";
      }).join("");
      card.innerHTML = '<div class="pub-top"><div class="pub-big"><b>' + adopted + "/" + total
        + '</b><span>' + (currentVersion ? "使用方已采用当前版本" : "尚未发布任何版本") + '</span></div><div class="pub-bar" style="width:300px;margin-top:0">'
        + Array.from({ length: total }, (_, index) => '<i class="' + (index < adopted ? "ok" : "wait") + '"></i>').join("")
        + '</div></div><div class="consumers">' + rows + "</div>";
    }
    const table = $('[data-page="publish"] .tbl tbody');
    if (table) {
      table.innerHTML = history.map((row) => '<tr><td class="mono">第 ' + esc(row.revision)
        + '</td><td class="mono" title="' + esc(row.version) + '">' + esc(String(row.version || "").slice(0, 8))
        + '</td><td class="dim">' + esc(relTime(row.published_at))
        + '</td><td>' + (currentVersion && row.version === currentVersion ? '<span class="st ok">生效中</span>'
          : row.version === (history[1] && history[1].version) ? "回滚点" : "已归档") + "</td></tr>").join("");
    }
    const actionPanel = $('[data-page="publish"] .action-panel .card-b');
    if (actionPanel) {
      const previous = history[1];
      actionPanel.innerHTML = (previous ? '<button type="button" class="btn pri" style="width:100%" data-live-rollback="'
        + esc(previous.version) + '">回滚到上一版</button>' : "")
        + '<button type="button" class="btn" style="width:100%" data-live-publish="realtime">发布到实时会话</button>'
        + '<button type="button" class="btn" style="width:100%" data-live-publish="game">发布到游戏人格</button>'
        + '<div class="token-row"><span>版本令牌</span><b title="'
        + esc(current ? current.version : "") + '">' + esc(current ? String(current.version).slice(0, 8) : "—") + "</b></div>"
        + '<div class="token-row"><span>版本修订</span><b>r' + esc(current ? current.revision : "—") + "</b></div>"
        + '<div class="token-row"><span>发布于</span><b>' + esc(current ? relTime(current.published_at) : "—") + "</b></div>"
        + '<p class="note">回滚会重新下发上一版，并保留切换后新增的对话、更正与删除记录。</p>';
    }
    const footer = $('[data-page="publish"] .panel .card-f');
    if (footer) footer.innerHTML = '<span class="dim">只统计已接入的使用方；未接入项不会产生发布提醒</span><a data-live-refresh>刷新状态</a>';
  }

  /* ---------- evolution ---------- */

  function renderEvolution() {
    const evolution = LIVE.snapshot.evolution || {};
    const proposals = evolution.proposals || [];
    LIVE.proposals = proposals;
    const pending = proposals.filter((row) => row.status === "proposed");
    const title = $("#evoTitle");
    if (title) title.textContent = "演化 · " + proposals.length + " 项";
    const sub = $("#evoSub");
    if (sub) sub.textContent = (MODE_LABELS[evolution.mode] || evolution.mode) + " · " + pending.length + " 项待审核";
    const hint = $("#propListHint");
    if (hint) hint.textContent = proposals.length + " 项 · " + pending.length + " 项待审核";
    const chips = $("#modeChips");
    if (chips) {
      chips.innerHTML = ["off", "propose", "auto"].map((mode) =>
        '<button type="button" class="chip' + (evolution.mode === mode ? " on" : "")
        + '" data-live-mode="' + mode + '">' + ({ off: "关闭", propose: "提议", auto: "自动" }[mode]) + "</button>").join("");
    }
    const list = $('[data-page="evolution"] .panel .card-b[style*="padding:0"]');
    if (list) {
      list.innerHTML = proposals.map((proposal, index) => {
        const diff = proposalDiff(proposal);
        return '<div class="prop-row' + (index === 0 ? " selected" : "") + '" data-prop="live-' + index
          + '" data-live-proposal="' + esc(proposal.proposal_id) + '"><h3>' + esc(proposalTitle(proposal))
          + '</h3><div class="meta"><span>基础 r' + esc(proposal.base_revision || 0) + "</span><span>"
          + esc(diff.section) + '</span><span class="st ' + (proposal.status === "proposed" ? "wait" : proposal.status === "accepted" ? "ok" : "warn")
          + '">' + ({ proposed: "待审核", accepted: "已采纳", rejected: "已驳回", withdrawn: "已撤回" }[proposal.status] || proposal.status)
          + "</span></div></div>";
      }).join("") || '<div class="empty">暂无提案</div>';
    }
    const note = $("#evoNote");
    if (note) note.textContent = evolution.mode === "auto" ? "自动模式只处理可演化段，冻结段不受影响" : "自动模式需要人工开启";
    if (proposals.length) selectLiveProposal(proposals[0].proposal_id);
    else selectLiveProposal(null);
  }

  function selectLiveProposal(proposalId) {
    const proposal = (LIVE.proposals || []).find((row) => row.proposal_id === proposalId);
    LIVE.selectedProposal = proposal || null;
    $$('[data-page="evolution"] .prop-row').forEach((node) => {
      node.classList.toggle("selected", node.dataset.liveProposal === proposalId);
    });
    if (!proposal) {
      const empty = { "#propHint": "暂无提案", "#propTitle": "暂无提案详情",
        "#propBefore": "—", "#propAfter": "—", "#propEv": "暂无证据", "#propImpact": "暂无修改" };
      Object.entries(empty).forEach(([selector, text]) => {
        const node = $(selector);
        if (node) node.textContent = text;
      });
      const noop = $("#propNoop");
      if (noop) noop.hidden = true;
      const actions = $('[data-page="evolution"] .detail-act');
      if (actions) actions.innerHTML = "";
      return;
    }
    const diff = proposalDiff(proposal);
    const hint = $("#propHint");
    if (hint) hint.textContent = proposal.proposal_id + " · " + ({ proposed: "待审核", accepted: "已采纳", rejected: "已驳回", withdrawn: "已撤回" }[proposal.status] || proposal.status);
    const title = $("#propTitle");
    if (title) title.textContent = proposalTitle(proposal);
    const before = $("#propBefore");
    if (before) before.innerHTML = "<del>" + esc(diff.before) + "</del>";
    const after = $("#propAfter");
    if (after) after.innerHTML = "<ins>" + esc(diff.after) + "</ins>";
    const ev = $("#propEv");
    if (ev) {
      const evidence = proposal.evidence || {};
      const facts = (LIVE.snapshot.facts.facts || []).filter((row) => (evidence.fact_ids || []).includes(row.id));
      const reflections = (LIVE.snapshot.reflections.reflections || []).filter((row) => (evidence.reflection_ids || []).includes(row.id));
      ev.innerHTML = facts.map((row) => '<div class="ev"><p>' + esc(row.text) + '</p><div class="m"><span>事实 · '
        + esc(row.id) + "</span><span>强</span></div></div>")
        .concat(reflections.map((row) => '<div class="ev"><p>' + esc(row.text) + '</p><div class="m"><span>反思 · '
          + esc(row.id) + "</span><span>中</span></div></div>")).join("")
        || '<div class="ev"><p>这条提案没有附证据。</p></div>';
    }
    const noopNote = $("#propNoop");
    if (noopNote) noopNote.hidden = proposal.status !== "proposed" || !diff.noop;
    const impact = $("#propImpact");
    if (impact) impact.innerHTML = '<span class="tag t-per">' + esc(diff.section) + "</span><span class=\"tag t-talk\">基于第 "
      + esc(proposal.base_revision || 0) + " 版</span>"
      + (proposal.accepted_revision ? '<span class="tag t-talk">已写入第 ' + esc(proposal.accepted_revision) + " 版</span>" : "");
    const actions = $('[data-page="evolution"] .detail-act');
    if (actions) {
      const open = proposal.status === "proposed";
      const noop = diff.noop;
      actions.innerHTML = (noop ? "" : '<button type="button" class="btn pri" data-live-prop-accept="'
        + esc(proposal.proposal_id) + '"' + (open ? "" : " disabled") + ">采纳</button>")
        + '<button type="button" class="btn" data-live-prop-reject="' + esc(proposal.proposal_id) + '"'
        + (open ? "" : " disabled") + ">驳回</button>"
        + '<button type="button" class="btn ghost" data-live-prop-withdraw="' + esc(proposal.proposal_id) + '"'
        + (open ? "" : " disabled") + ">撤回提案</button>";
    }
  }

  /* ---------- cleanup ---------- */

  let cleanupTimer = null;
  let cleanupPolling = false;
  let cleanupPollFailed = false;
  let cleanupRevision = 0;
  const watchedDeletions = new Set();

  function deletionStatus(row) {
    if (row.status === "completed") return "清理完成";
    if (row.status === "failed") return "清理失败";
    if (row.content_cleaned) return "等待接入端确认";
    return row.stopped_using ? "正在清理本地副本" : "正在登记删除";
  }

  function updateDeletion(receipt) {
    cleanupRevision++;
    const cleanup = LIVE.snapshot.cleanup;
    const target = (receipt.targets || [])[0] || {};
    const row = Object.assign({}, receipt, {
      target_type: target.type || "来源",
      target_id: target.id || (receipt.source_keys || [])[0] || (receipt.episode_ids || [])[0],
      completed_at: receipt.updated_at,
      step_errors: Object.fromEntries(Object.entries(receipt.steps || {})
        .filter(([, step]) => step.detail?.error).map(([name, step]) => [name, step.detail.error])),
    });
    cleanup.deletions = [row, ...(cleanup.deletions || []).filter((item) => item.deletion_id !== row.deletion_id)];
    if (row.stopped_using && target.id) {
      cleanup.deletable = (cleanup.deletable || []).filter((item) => item.id !== target.id || item.type !== target.type);
    }
    if (row.status === "pending") watchedDeletions.add(row.deletion_id);
    renderCleanup();
  }

  function watchCleanup() {
    if (cleanupTimer !== null || cleanupPolling || document.hidden) return;
    const rows = LIVE.snapshot?.cleanup?.deletions || [];
    if (!rows.some((row) => row.status === "pending")
      && !(location.hash === "#cleanup" && rows.some((row) => row.status === "failed"))) return;
    cleanupTimer = setTimeout(pollCleanup, rows.some((row) => row.status === "pending") ? 1500 : 5000);
  }

  async function pollCleanup() {
    cleanupTimer = null;
    if (!LIVE.connected || document.hidden) return;
    cleanupPolling = true;
    const revision = cleanupRevision;
    try {
      const result = await api("/api/cleanup?receipts_only=1");
      if (revision !== cleanupRevision) return;
      const cleanup = LIVE.snapshot.cleanup;
      const changed = cleanupPollFailed || JSON.stringify(cleanup.deletions) !== JSON.stringify(result.deletions);
      cleanupPollFailed = false;
      cleanup.deletions = result.deletions;
      const finished = result.deletions.filter((row) => row.status !== "pending" && watchedDeletions.delete(row.deletion_id));
      if (changed) renderCleanup();
      if (finished.length === 1) toast("删除已生效 · " + deletionStatus(finished[0]));
      if (finished.length > 1) toast(finished.length + " 项删除回执已更新");
      if (finished.length && !watchedDeletions.size) {
        const latest = await api("/api/cleanup");
        if (revision === cleanupRevision) {
          LIVE.snapshot.cleanup = latest;
          renderCleanup();
        }
      }
    } catch (_) {
      cleanupPollFailed = true;
      const hint = $("#cleanHint");
      if (hint) hint.textContent = "暂时无法获取清理进度，稍后重试";
    } finally {
      cleanupPolling = false;
      watchCleanup();
    }
  }

  async function submitDeletion(body) {
    toast("正在提交删除…");
    try {
      const result = await api("/api/cleanup/delete", {
        method: "POST", body: JSON.stringify({ ...body, ...(SERVICE ? { defer_cleanup: true } : {}) }),
      });
      updateDeletion(result);
      if (result.stopped_using && body.source_key && LIVE.snapshot.timeline?.events) {
        const timeline = LIVE.snapshot.timeline;
        timeline.events = timeline.events.filter((event) => event.source_key !== body.source_key);
        renderTimeline();
      }
      toast("删除已生效 · " + deletionStatus(result));
      return result;
    } catch (error) {
      toast("删除请求失败：" + error.message);
      return null;
    }
  }

  function renderCleanup() {
    const cleanup = LIVE.snapshot.cleanup || {};
    const deletable = cleanup.deletable || [];
    const deletions = cleanup.deletions || [];
    const corrections = cleanup.corrections || [];
    const pendingDeletions = deletions.filter((row) => row.status !== "completed");
    const title = $('[data-page="cleanup"] .page-title');
    if (title) title.textContent = "清理 · " + deletable.length + " 项可清理"
      + (pendingDeletions.length ? " · " + pendingDeletions.length + " 项处理中" : "");
    const sub = $('[data-page="cleanup"] .page-sub');
    if (sub) sub.textContent = "删除与更正立即停止使用相关内容，清理进度在此查看";
    const tools = $('[data-page="cleanup"] .page-tools');
    if (tools) tools.innerHTML = '<button type="button" class="btn" data-live-tombstones>查看墓碑</button>';
    const cleanHint = $("#cleanHint");
    if (cleanHint) {
      const failed = pendingDeletions.filter((row) => row.status === "failed").length;
      const label = deletable.length + " 项可清理 · " + (pendingDeletions.length - failed) + " 项处理中"
        + (failed ? " · " + failed + " 项失败" : "");
      if (cleanHint.textContent !== label) cleanHint.textContent = label;
    }

    // type filter + page slice keep the queue readable as the root grows
    const kinds = Array.from(new Set(deletable.map((item) => item.type)));
    LIVE.cleanupKind = kinds.includes(LIVE.cleanupKind) ? LIVE.cleanupKind : "all";
    const visible = LIVE.cleanupKind === "all"
      ? deletable : deletable.filter((item) => item.type === LIVE.cleanupKind);
    const shown = visible.slice(0, LIVE.cleanupLimit);
    const left = $('[data-page="cleanup"] .panel .card-b');
    if (left) {
      const pendingRows = shown.map((item) =>
        '<div class="clean-row"><span class="k">' + esc(CLEAN_TYPE_LABELS[item.type] || item.type)
        + '</span><div class="t">'
        + esc(item.text || item.id) + '<div class="impact">' + esc(item.id) + '</div></div>'
        + '<span class="r">可删除</span><span class="r"><button type="button" class="link danger" data-live-delete="'
        + esc(item.id) + '" data-type="' + esc(item.type) + '">删除</button></span></div>').join("");
      const runningRows = pendingDeletions.map((row) => {
        const problems = Object.entries(row.plugin_receipts || {}).filter(([, receipt]) => receipt.status === "failed")
          .map(([plugin, receipt]) => plugin + " · " + ((receipt.failed || []).join("、") || "副本清理失败"));
        problems.push(...Object.values(row.step_errors || {}));
        const detail = [row.stopped_using ? "已停止召回" : "", ...problems].filter(Boolean).join(" · ");
        const retry = SERVICE && (row.status === "failed" || !row.content_cleaned)
          ? '<button class="link" data-live-cleanup-retry="' + esc(row.deletion_id) + '">重试清理</button>' : "";
        return '<div class="clean-row"><span class="k">删除</span><div class="t">' + esc(row.target_type)
          + " · " + esc(row.target_id) + '<div class="impact">' + esc(detail) + '</div></div>'
          + '<span class="r">' + esc(relTime(row.created_at)) + '</span><span class="r">' + esc(deletionStatus(row))
          + retry + "</span></div>";
      }).join("");
      const chips = kinds.length > 1
        ? '<div class="chips clean-chips">' + [["all", "全部 " + deletable.length]]
          .concat(kinds.map((kind) => [kind, (CLEAN_TYPE_LABELS[kind] || kind) + " "
            + deletable.filter((item) => item.type === kind).length]))
          .map(([key, label]) => '<button type="button" class="chip'
            + (LIVE.cleanupKind === key ? " on" : "") + '" data-clean-kind="' + esc(key) + '">'
            + esc(label) + "</button>").join("") + "</div>"
        : "";
      const more = visible.length > shown.length
        ? '<div class="clean-more"><button type="button" class="btn" data-clean-more>再看 '
          + Math.min(LIVE.cleanupLimit, visible.length - shown.length) + ' 条 · 还有 '
          + (visible.length - shown.length) + ' 条</button></div>'
        : "";
      left.innerHTML = chips + '<div class="clean-group"><h3>可清理 <span class="cnt">'
        + (deletable.length + pendingDeletions.length)
        + '</span></h3>' + (pendingRows + runningRows || '<div class="empty">没有待处理的清理项</div>')
        + "</div>" + more;
    }
    const right = $('[data-page="cleanup"] .panel:nth-child(2) .card-b');
    if (right) {
      const doneDeletions = deletions.filter((row) => row.status === "completed").map((row) =>
        '<div class="clean-row dim" style="grid-template-columns:50px minmax(0,1fr) 86px"><span class="k">删除</span>'
        + '<div class="t">' + esc(row.target_type) + " · " + esc(row.target_id) + '</div><span class="r">'
        + esc(relTime(row.completed_at || row.created_at)) + "</span></div>").join("");
      const doneCorrections = corrections.map((row) =>
        '<div class="clean-row dim" style="grid-template-columns:50px minmax(0,1fr) 86px"><span class="k">更正</span>'
        + '<div class="t">' + esc(row.old_text) + " → " + esc(row.new_text) + '</div><span class="r">'
        + esc(row.store_status === "retracted" ? "已撤回" : relTime(row.created_at))
        + (SERVICE && row.store_status === "active" ? '<button class="link" data-live-correction-retract="' + esc(row.id) + '">撤回更正</button>' : '') + "</span></div>").join("");
      right.innerHTML = '<div class="clean-group" style="padding-top:14px">'
        + (doneDeletions + doneCorrections || '<div class="empty">暂无已完成记录</div>') + "</div>";
    }
    const footer = $('[data-page="cleanup"] .panel .card-f');
    if (footer) footer.innerHTML = '<span class="dim">删除屏障会阻止同一来源被重新写入</span><a data-nav="settings" href="#settings">清理设置 →</a>';
    watchCleanup();
  }

  /* ---------- settings ---------- */

  function renderSettings() {
    const engine = LIVE.engine || {};
    const evolution = LIVE.snapshot.evolution || {};
    const sub = $('[data-page="settings"] .page-sub');
    if (sub) sub.textContent = "本机 · 改动走引擎，不直接写存储";
    const tools = $('[data-page="settings"] .page-tools');
    if (tools) tools.innerHTML = (SERVICE ? '<button type="button" class="btn" data-live-maintain>运行一次维护</button>' : '')
      + '<button type="button" class="btn" data-live-export-settings>导出引擎信息</button>';
    const panel = $('[data-page="settings"] .panel');
    if (panel) {
      const groups = panel.querySelectorAll('.group');
      if (groups[0]) {
        const modeOff = evolution.mode === "off";
        groups[0].innerHTML = '<h3>演化</h3>'
          + '<div class="switch"><div><div class="t">后台自动演化</div><div class="d">'
          + (modeOff ? "关闭时不会评估新提案" : "按冷却时间后台评估提案") + '</div></div>'
          + '<button type="button" class="sw' + (evolution.enabled ? " on" : "") + '" role="switch" aria-checked="'
          + (evolution.enabled ? "true" : "false") + '" data-live-evolution="' + (evolution.enabled ? "0" : "1") + '"></button></div>'
          + '<div class="switch"><div><div class="t">演化模式</div><div class="d">关闭 / 提议 / 自动</div></div>'
          + '<div class="chips" style="margin-left:auto">' + ["off", "propose", "auto"].map((mode) =>
            '<button type="button" class="chip' + (evolution.mode === mode ? " on" : "") + '" data-live-mode="' + mode + '">'
            + ({ off: "关闭", propose: "提议", auto: "自动" }[mode]) + "</button>").join("") + "</div></div>"
          + (groups[0].querySelector(".group-note") ? "" : '<p class="group-note dim" style="font-size:12.5px;color:var(--ink-3);margin:10px 20px 4px">关闭时不会生成新提案；提议只生成待审核项，自动会直接采用可演化段。</p>');
      }
      const memGroup = $("#memBehaviorGroup");
      if (memGroup) memGroup.remove();
    }
    const info = $("#sysInfo");
    if (info) {
      const rows = [
        ["数据根", engine.root || "—", true],
        ["角色", engine.character || "—", false],
        ["人格修订", "第 " + (engine.card_revision || 0) + " 版", false],
        ["发布版本", engine.published_version ? String(engine.published_version).slice(0, 8) : "—", true],
        ["演化模式", engine.evolution_mode || "off", false],
        ["写入", engine.read_only ? "只读" : "允许", false],
      ];
      if (SERVICE) {
        const chat = (engine.capabilities || {}).chat || {};
        rows.push(["对话", chat.status === "ready" ? "已就绪" : "待配置模型或发布人格", false]);
        (engine.config_missing || []).forEach((field) => rows.push(["配置待填写", field, true]));
      }
      info.innerHTML = rows.map(([key, value, mono]) =>
        '<div class="info-row"><span class="k">' + key + '</span><span class="v' + (mono ? " mono" : "") + '">'
        + esc(value) + "</span></div>").join("");
    }
    const sysHint = $("#sysHint");
    if (sysHint) sysHint.textContent = engine.read_only ? "只读" : "可写";
    const about = $('[data-page="settings"] aside.panel:not(#modelCfgPanel) .card-b .reason');
    if (about) about.textContent = "铃芽 RIN · 可编辑人设、持续养成、长期记忆的人格陪伴系统。";
    const aboutAct = $('[data-page="settings"] aside.panel:not(#modelCfgPanel) .detail-act');
    if (aboutAct) aboutAct.remove();
    const aboutFooter = $('[data-page="settings"] aside.panel:not(#modelCfgPanel) .card-f');
    if (aboutFooter) aboutFooter.innerHTML = '<span class="dim">改动走引擎，不直接写存储</span>';
    renderModelConfig();
  }

  /* ---------- model configuration ---------- */

  const MODEL_CFG = { data: null, loaded: false, busy: false, clearKey: false };

  function modelConfigApply(data) {
    MODEL_CFG.data = data;
    MODEL_CFG.loaded = true;
    if (!data || !data.available) {
      const hint = $("#modelCfgHint");
      if (hint) hint.textContent = (data && data.reason) || "不可用";
      const form = $("#modelCfgForm");
      if (form) form.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
      return;
    }
    const set = (sel, value) => { const node = $(sel); if (node) node.value = value == null ? "" : value; };
    set("#cfgBaseUrl", data.base_url || "");
    set("#cfgModelConv", (data.models || {}).conversation || "");
    set("#cfgModelSummary", (data.models || {}).summary || "");
    const effort = $("#cfgEffort");
    if (effort && !effort.options.length) {
      effort.innerHTML = (data.efforts || []).map((item) =>
        '<option value="' + esc(item.value) + '">' + esc(item.label) + "</option>").join("");
    }
    set("#cfgEffort", data.reasoning_effort || "");
    const key = $("#cfgApiKey");
    if (key) key.value = "";
    MODEL_CFG.clearKey = false;

    const keyState = $("#cfgKeyState");
    const info = data.api_key || {};
    if (keyState) {
      if (MODEL_CFG.clearKey) keyState.textContent = "保存后将清除已保存的 Key";
      else if (info.stored) keyState.textContent = "已保存（由管理台录入）";
      else if (info.source === "environment") keyState.textContent = "来自环境变量 " + (info.env_name || "");
      else keyState.textContent = "尚未保存";
    }
    const clear = $("#cfgKeyClear");
    if (clear) clear.hidden = !info.stored;

    const hint = $("#modelCfgHint");
    if (hint) {
      hint.textContent = data.demo ? "离线演示"
        : (data.configured ? (info.present ? "已配置" : "缺少 Key") : "未配置");
    }
    const file = $("#cfgFile");
    if (file) file.textContent = data.demo ? "" : (data.file || "");
  }

  async function loadModelConfig(force) {
    if (MODEL_CFG.loaded && !force) return;
    try {
      modelConfigApply(await api("/api/model/config"));
    } catch (error) {
      const hint = $("#modelCfgHint");
      if (hint) hint.textContent = "读取失败：" + error.message;
    }
  }

  function renderModelConfig() {
    if (!LIVE.connected) return;
    if (!MODEL_CFG.loaded) loadModelConfig(true);
  }

  function modelConfigResult(message, kind) {
    const node = $("#cfgResult");
    if (!node) return;
    node.textContent = message;
    node.className = "result" + (kind ? " " + kind : "");
  }

  async function saveModelConfig() {
    if (MODEL_CFG.busy) return;
    const body = {
      base_url: ($("#cfgBaseUrl") || {}).value || "",
      models: {
        conversation: (($("#cfgModelConv") || {}).value || "").trim(),
        summary: (($("#cfgModelSummary") || {}).value || "").trim(),
      },
      reasoning_effort: ($("#cfgEffort") || {}).value || "",
    };
    const keyInput = $("#cfgApiKey");
    if (keyInput && keyInput.value) body.api_key = keyInput.value;
    if (MODEL_CFG.clearKey) body.clear_api_key = true;

    const button = $("#cfgSave");
    MODEL_CFG.busy = true;
    if (button) { button.disabled = true; button.textContent = "保存中…"; }
    modelConfigResult("");
    try {
      const data = await api("/api/model/config", { method: "POST", body: JSON.stringify(body) });
      modelConfigApply(data);
      modelConfigResult("已保存并应用", "ok");
      toast("模型配置已生效");
      if (CHAT) CHAT.loaded = false;
      await refresh();
    } catch (error) {
      // A rejected value keeps the form as typed so it can be corrected.
      modelConfigResult("保存失败：" + error.message, "bad");
    } finally {
      MODEL_CFG.busy = false;
      if (button) { button.disabled = false; button.textContent = "保存并应用"; }
    }
  }

  const MODEL_FAIL_HINTS = {
    auth: "服务商拒绝了这个 Key",
    config: "模型名或接口地址不对",
    timeout: "连接超时",
    rate_limit: "服务商限流，稍后重试",
    transport: "连不上接口地址",
    format: "返回内容无法解析",
  };

  async function testModelConfig() {
    if (MODEL_CFG.busy) return;
    const button = $("#cfgTest");
    MODEL_CFG.busy = true;
    if (button) { button.disabled = true; button.textContent = "测试中…"; }
    modelConfigResult("正在连接模型…");
    try {
      const result = await api("/api/model/test", { method: "POST", body: "{}" });
      if (result.ok) {
        modelConfigResult("连接成功 · " + (result.reply || "OK"), "ok");
      } else {
        const hint = MODEL_FAIL_HINTS[result.kind] || "调用失败";
        modelConfigResult(hint + "（" + result.kind + "）", "bad");
      }
    } catch (error) {
      modelConfigResult("测试失败：" + error.message, "bad");
    } finally {
      MODEL_CFG.busy = false;
      if (button) { button.disabled = false; button.textContent = "测试连接"; }
    }
  }

  function bindModelConfig() {
    const save = $("#cfgSave");
    if (save) save.addEventListener("click", saveModelConfig);
    const test = $("#cfgTest");
    if (test) test.addEventListener("click", testModelConfig);
    const clear = $("#cfgKeyClear");
    if (clear) clear.addEventListener("click", () => {
      MODEL_CFG.clearKey = true;
      const key = $("#cfgApiKey");
      if (key) key.value = "";
      modelConfigApply(MODEL_CFG.data);
      modelConfigResult("保存后才会清除", "");
    });
    const form = $("#modelCfgForm");
    if (form) form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveModelConfig();
    });
  }

  /* ---------- conversation ---------- */

  const CHAT = { messages: [], status: null, turns: [], sending: false, loaded: false };

  function chatTime(iso) {
    const text = String(iso || "");
    if (!text) return "";
    return " · " + relTime(text);
  }

  function chatBubble(row) {
    const who = row.role === "assistant" ? "铃" : "你";
    const node = document.createElement("div");
    node.className = "chat-row " + (row.role === "assistant" ? "ai" : "user");
    const badge = document.createElement("span");
    badge.className = "chat-who";
    badge.textContent = who;
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = row.text;
    if (row.meta) {
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = row.meta;
      bubble.appendChild(meta);
    }
    const message = document.createElement("div");
    message.className = "chat-message";
    message.appendChild(bubble);
    if (row.id) {
      const actions = document.createElement("div");
      actions.className = "chat-message-actions";
      const addAction = (label, action, requiresModel) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.dataset.chatMutate = "1";
        if (requiresModel) button.dataset.requiresModel = "1";
        button.addEventListener("click", action);
        actions.appendChild(button);
      };
      addAction("编辑", () => editChatMessage(row));
      if (row.role === "assistant") addAction("重新生成", () => regenerateChat(row), true);
      message.appendChild(actions);
    }
    if (row.role === "assistant" && !row.pending) {
      const thoughts = document.createElement("details");
      thoughts.className = "chat-thoughts";
      const summary = document.createElement("summary");
      summary.textContent = "查看思考内容";
      const content = document.createElement("pre");
      content.textContent = row.reasoning || "这条消息没有保存模型返回的思考内容。";
      thoughts.append(summary, content);
      message.appendChild(thoughts);
    }
    node.appendChild(badge);
    node.appendChild(message);
    return node;
  }

  function chatLogNode() {
    const log = $("#chatLog");
    return log;
  }

  function chatPinned() {
    const log = chatLogNode();
    if (!log) return true;
    return log.scrollHeight - log.scrollTop - log.clientHeight < 90;
  }

  function scrollChat(force) {
    const log = chatLogNode();
    if (log && (force || chatPinned())) log.scrollTop = log.scrollHeight;
  }

  function chatAppend(row) {
    const log = chatLogNode();
    if (!log) return;
    const empty = $("#chatEmpty");
    if (empty) empty.remove();
    const stick = chatPinned();
    const node = chatBubble(row);
    node.dataset.chatPending = row.pending ? "1" : "0";
    log.appendChild(node);
    if (stick) scrollChat(true);
    return node;
  }

  function renderChatMessages() {
    const log = chatLogNode();
    if (!log) return;
    log.innerHTML = "";
    if (!CHAT.messages.length) {
      const empty = document.createElement("div");
      empty.className = "chat-empty";
      empty.id = "chatEmpty";
      empty.innerHTML = "<b>开始一段对话</b>说点什么，这一轮会用已发布的人格回答，<br>并把双方原文写进记忆。";
      log.appendChild(empty);
      return;
    }
    CHAT.messages.forEach((row) => log.appendChild(chatBubble(row)));
    scrollChat(true);
  }

  function renderChatStatus() {
    const state = CHAT.status;
    const stateNode = $("#chatState");
    const stateText = $("#chatStateText");
    const input = $("#chatInput");
    const send = $("#chatSend");
    const hint = $("#chatHint");
    const capHint = $("#chatCapHint");
    const ready = Boolean(state && state.ready);
    if (stateNode) stateNode.classList.toggle("warn", !ready);
    if (stateText) {
      if (!state) stateText.textContent = "对话服务不可用";
      else if (ready) stateText.textContent = CHAT.sending ? "正在生成这一轮…" : "就绪 · 消息会写入记忆";
      else stateText.textContent = state.reason || "对话不可用";
    }
    if (input) input.disabled = !ready || CHAT.sending;
    if (send) send.disabled = !ready || CHAT.sending;
    const writable = Boolean(state && state.writable !== false);
    $$("[data-chat-mutate]").forEach((button) => {
      button.disabled = CHAT.sending || !writable || (button.dataset.requiresModel === "1" && !ready);
    });
    const clear = $("#chatClear");
    if (clear) clear.disabled = CHAT.sending || !writable || !CHAT.messages.length;
    const reload = $("#chatReload");
    if (reload) reload.disabled = CHAT.sending;
    if (hint) hint.textContent = CHAT.messages.length ? CHAT.messages.length + " 条消息" : "尚未开始";
    if (capHint) capHint.textContent = ready ? "就绪" : "不可用";

    const who = $("#chatWho");
    if (who) who.textContent = (state && (state.display_name || state.character)) || "—";
    const persona = $("#chatPersona");
    if (persona) persona.textContent = (state && state.persona_version) || "尚未发布";
    const revision = $("#chatRevision");
    if (revision) revision.textContent = state && state.card_revision ? "第 " + state.card_revision + " 版" : "—";
    const model = $("#chatModel");
    if (model) {
      const info = (state && state.model) || {};
      model.textContent = info.demo ? "离线演示模型" : (info.configured ? (info.name || info.tier || "conversation") : "未配置");
    }
    const storage = $("#chatStorage");
    if (storage) {
      const caps = (state && state.capabilities) || {};
      const item = caps.storage || {};
      storage.textContent = item.status === "ready" ? "可写" : (item.reason || item.status || "不可用");
    }
    renderChatTurns();
  }

  function renderChatTurns() {
    const body = $("#chatTurns");
    const hint = $("#chatTurnHint");
    if (!body) return;
    if (!CHAT.turns.length) {
      body.innerHTML = '<p class="dim" style="margin:0;font-size:13.5px">还没有提交记录。</p>';
      if (hint) hint.textContent = "—";
      return;
    }
    if (hint) hint.textContent = CHAT.turns.length + " 轮";
    body.innerHTML = CHAT.turns.slice(0, 12).map((turn) => {
      const status = String(turn.status || "");
      const label = status === "committed" ? (turn.delivered ? "已显示 · 已提交" : "已提交")
        : status === "deleted" ? "已失效"
        : status === "failed" ? "失败"
        : status === "pending" ? "处理中"
        : status === "interrupted" ? "中断" : status;
      const cls = status === "committed" ? "ok" : (status === "deleted" ? "warn" : "wait");
      return '<div class="chat-turn"><span>' + esc(relTime(turn.occurred_at)) + '</span>'
        + '<span class="st ' + cls + '">' + esc(label) + "</span></div>";
    }).join("");
  }

  async function loadChat(force) {
    if (CHAT.loaded && !force) return;
    try {
      const history = await api("/api/chat/history?limit=200");
      CHAT.messages = (history.messages || []).map((row) => ({
        id: row.id, role: row.role === "assistant" ? "assistant" : "user", text: row.text, reasoning: row.reasoning || "",
      }));
      CHAT.turns = history.turns || [];
      CHAT.status = history.status || null;
      CHAT.loaded = true;
      renderChatMessages();
      renderChatStatus();
    } catch (error) {
      CHAT.status = { ready: false, reason: "读取对话失败：" + error.message };
      renderChatStatus();
    }
  }

  function renderChat() {
    if (!LIVE.connected) return;
    if (!CHAT.loaded) {
      loadChat(true);
      return;
    }
    renderChatStatus();
  }

  const pendingKey = "rin-chat-pending:" + location.origin;
  let pendingRequest = null;
  try { pendingRequest = JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch (_) { /* invalid local draft */ }

  async function changeChat(action, payload) {
    if (CHAT.sending) return;
    CHAT.sending = true;
    renderChatStatus();
    let pending;
    if (action === "regenerate") pending = chatAppend({ role: "assistant", text: "正在重新想…", pending: true });
    try {
      const result = await api("/api/chat/" + action, { method: "POST", body: JSON.stringify(payload || {}) });
      if (action === "clear") {
        pendingRequest = null;
        sessionStorage.removeItem(pendingKey);
      }
      if (action === "regenerate" && SERVICE && result.status === "committed") {
        await api("/v1/receipts", { method: "POST", body: JSON.stringify({
          request_id: result.request_id, receipt_id: "display:" + result.request_id,
          kind: "delivered", session_id: "console", scene: "", delivered_chars: Array.from(result.reply || "").length,
        }) });
      }
      await loadChat(true);
      toast(action === "clear" ? "对话已清空" : action === "edit" ? "消息已更新" : "已重新生成");
    } catch (error) {
      toast("操作未完成：" + error.message);
      await loadChat(true);
    } finally {
      if (pending) pending.remove();
      CHAT.sending = false;
      renderChatStatus();
    }
  }

  function editChatMessage(row) {
    openLiveModal("编辑历史消息",
      '<p class="dim">更新聊天文字，之后的对话会使用编辑后的内容。</p>'
      + '<textarea class="chat-edit-input" id="chatEditText" maxlength="8000" aria-label="编辑消息">' + esc(row.text) + '</textarea>',
      "保存修改", async () => {
        const text = ($("#chatEditText").value || "").trim();
        if (!text) { toast("消息不能为空"); return; }
        await changeChat("edit", { message_id: row.id, text });
      });
    const input = $("#chatEditText");
    if (input) input.focus();
  }

  function regenerateChat(row) {
    const run = () => changeChat("regenerate", { message_id: row.id, request_id: rid("regenerate") });
    const index = CHAT.messages.indexOf(row);
    if (index < CHAT.messages.length - 1) {
      openLiveModal("从这里重新生成", "<p>这条回复及其后的聊天会被新回复替换，继续吗？</p>", "重新生成", run);
    } else run();
  }

  async function sendChat() {
    if (CHAT.sending) return;
    const input = $("#chatInput");
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    if (!pendingRequest || pendingRequest.text !== text) pendingRequest = { text, request_id: rid("chat") };
    sessionStorage.setItem(pendingKey, JSON.stringify(pendingRequest));
    CHAT.sending = true;
    input.value = "";
    input.style.height = "";
    chatAppend({ role: "user", text });
    const pending = chatAppend({ role: "assistant", text: "正在想…", pending: true });
    renderChatStatus();
    try {
      const result = await api("/api/chat/send", { method: "POST", body: JSON.stringify(pendingRequest) });
      if (result.status !== "committed") throw new Error(result.message || "轮次仍在处理中，请重试同步");
      const reply = String(result.reply || "");
      if (pending) pending.remove();
      chatAppend({ role: "assistant", text: reply, meta: result.replayed ? "已回放同一轮提交" : "" });
      if (SERVICE) await api("/v1/receipts", { method: "POST", body: JSON.stringify({
        request_id: result.request_id, receipt_id: "display:" + result.request_id,
        kind: "delivered", session_id: "console", scene: "", delivered_chars: Array.from(reply).length,
      }) });
      pendingRequest = null;
      sessionStorage.removeItem(pendingKey);
      await loadChat(true);
      await refresh();
    } catch (error) {
      if (pending) pending.remove();
      chatAppend({ role: "assistant", text: "同步未完成：" + error.message, meta: "重试会沿用同一请求" });
      toast("同步未完成，可重试同一轮");
      input.value = text;
    } finally {
      CHAT.sending = false;
      renderChatStatus();
      input.focus();
    }
  }

  function bindChat() {
    if (pendingRequest && $("#chatInput")) $("#chatInput").value = pendingRequest.text;
    const form = $("#chatForm");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        sendChat();
      });
    }
    const input = $("#chatInput");
    if (input) {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          sendChat();
        }
      });
      input.addEventListener("input", () => {
        input.style.height = "auto";
        input.style.height = Math.min(180, input.scrollHeight) + "px";
      });
    }
    const reload = $("#chatReload");
    if (reload) reload.addEventListener("click", () => { loadChat(true); toast("已刷新对话"); });
    const clear = $("#chatClear");
    if (clear) clear.addEventListener("click", () => {
      openLiveModal("清空对话", "<p>清空当前网页的聊天记录，已记住的事实和人格保留。</p>", "清空对话", () => changeChat("clear"));
    });
  }

  /* ---------- render all ---------- */

  function renderAll() {
    renderChrome();
    renderOverview();
    renderChat();
    renderTimeline();
    renderFacts();
    renderReflections();
    renderPersona();
    renderPublish();
    renderEvolution();
    renderCleanup();
    renderSettings();
  }

  async function refresh() {
    try {
      let initialization = null;
      if (SERVICE) {
        writeIdentity = await api("/v1/bootstrap");
        initialization = writeIdentity.initialization;
      }
      const [snapshot, engine] = await Promise.all([
        api("/api/snapshot"), api("/api/engine/status"),
      ]);
      LIVE.snapshot = snapshot;
      LIVE.engine = engine;
      LIVE.connected = true;
      document.body.classList.add("rin-live");
      renderAll();
      if (SERVICE) {
        $$('a[href="/api/export"]').forEach((link) => link.href = "/v1/export");
        renderInitialization(initialization);
      }
      return true;
    } catch (error) {
      LIVE.connected = false;
      document.body.classList.remove("rin-live");
      const navMode = $(".nav-mode");
      if (navMode) navMode.innerHTML = SERVICE ? "<i></i>服务已断开" : "<i></i>离线预览";
      const footerStatus = $(".foot-right span");
      if (footerStatus) footerStatus.textContent = "离线预览 · 启动本地服务后自动连接";
      if (SERVICE) renderInitialization(null, error.message);
      return false;
    }
  }

  function renderInitialization(state, failure) {
    let panel = $("#rinSetup");
    if (state && state.published && !failure) {
      if (panel) panel.remove();
      return;
    }
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "rinSetup";
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-labelledby", "rinSetupTitle");
      panel.style.cssText = "position:fixed;inset:0;z-index:10000;display:grid;place-items:center;"
        + "background:rgba(243,239,229,.96);padding:32px";
      document.body.appendChild(panel);
    }
    const initialized = state && state.initialized;
    panel.innerHTML = '<form id="rinSetupForm" class="panel" style="width:min(580px,90vw);padding:32px">'
      + '<div class="page-en">铃芽 RIN</div><h2 id="rinSetupTitle">'
      + (failure ? "记忆服务已断开" : initialized ? "发布初始人格" : "创建你的新伙伴") + '</h2>'
      + (failure ? '<p role="alert">' + esc(failure) + '</p><button type="button" class="btn pri" id="rinReconnect">重新连接</button>'
        : '<p>' + (initialized ? "人格已保存。发布后，后续对话将使用这个版本。" : "为新的记忆空间填写名字与初始人格。") + '</p>'
        + '<p class="dim">角色标识 · ' + esc(state.character_id) + '</p>'
        + '<label for="rinDisplayName">显示名</label><input id="rinDisplayName" required maxlength="80" value="'
        + esc(state.display_name) + '" style="display:block;width:100%;margin:8px 0 20px;padding:10px">'
        + (initialized ? ''
          : '<label for="rinInitialPersona">初始人格</label><textarea id="rinInitialPersona" required maxlength="4000" rows="7"'
          + ' style="display:block;width:100%;margin:8px 0 20px;padding:10px;font:inherit" placeholder="她是谁、如何说话、与你如何相处……"></textarea>')
        + '<p id="rinSetupError" role="alert"></p><button class="btn pri" id="rinSetupSubmit" type="submit">'
        + (initialized ? "发布并进入管理台" : "保存初始人格") + '</button>')
      + '</form>';
    const reconnect = $("#rinReconnect");
    if (reconnect) reconnect.addEventListener("click", refresh);
    $("#rinSetupForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#rinSetupSubmit");
      if (!button || button.disabled) return;
      button.disabled = true;
      button.textContent = "正在保存…";
      try {
        if (initialized) {
          // The name is the first line of the identity section, so renaming is
          // one card revision like any other edit; publish binds to the
          // revision this form now has.
          let revision = state.revision;
          const name = ($("#rinDisplayName").value || "").trim();
          if (name && name !== state.display_name) {
            const card = await api("/v1/persona");
            const identity = ((card && card.sections) || []).find((row) => row.section_id === "identity");
            if (identity) {
              const receipt = await api("/v1/persona/revise", {
                method: "POST",
                body: JSON.stringify({
                  base_revision: Number(card.revision) || 0,
                  operations: [{ op: "replace", section_id: "identity",
                                 text: withNameLine(identity.text, name) }],
                  reason: "console_rename",
                }),
              });
              revision = Number(receipt.revision) || revision;
            }
          }
          await api("/v1/persona/publish", { method: "POST", body: JSON.stringify({ revision, target: "next_turn" }) });
        } else {
          await api("/v1/characters", { method: "POST", body: JSON.stringify({
            character_id: state.character_id, display_name: $("#rinDisplayName").value,
            text: $("#rinInitialPersona").value,
          }) });
        }
        CHAT.loaded = false;
        await refresh();
      } catch (error) {
        $("#rinSetupError").textContent = error.message;
        button.disabled = false;
        button.textContent = "重试保存";
      }
    });
  }

  /* ---------- modal / drawer ---------- */

  function openLiveDrawer(title, sub, html) {
    const drawer = $("#drawer");
    if (!drawer) return;
    $("#drawerTitle").textContent = title;
    $("#drawerSub").textContent = sub;
    $("#drawerBody").innerHTML = html;
    drawer.classList.add("on");
    $("#drawerMask").classList.add("on");
    drawer.setAttribute("aria-hidden", "false");
    const x = $(".drawer-x", drawer);
    if (x) x.focus();
  }

  function openLiveModal(title, body, confirmLabel, onConfirm) {
    const modal = $("#modal");
    if (!modal) return;
    LIVE.modalConfirm = onConfirm || null;
    $("#modalBody").innerHTML = "<h3>" + esc(title) + "</h3>" + body
      + '<div class="act"><button type="button" class="btn" data-live-modal-cancel>取消</button>'
      + '<button type="button" class="btn pri" data-live-modal-confirm>' + esc(confirmLabel) + "</button></div>";
    modal.classList.add("on");
    const x = $("#modalX");
    if (x) x.focus();
  }

  function closeLiveModal() {
    const modal = $("#modal");
    if (modal) modal.classList.remove("on");
    LIVE.modalConfirm = null;
  }

  /* ---------- live actions ---------- */

  function openPendingDrawer() {
    const proposals = (LIVE.proposals || []).filter((row) => row.status === "proposed");
    if (!proposals.length) {
      openLiveDrawer("全部待处理", "没有待处理的提案", '<div class="empty">记忆在安静地生长</div>');
      return;
    }
    openLiveDrawer("全部待处理", proposals.length + " 项提案待审核", proposals.map((proposal) => {
      const diff = proposalDiff(proposal);
      return '<article class="dec" data-live-key="' + esc(proposal.proposal_id) + '"><div class="dec-t">'
        + esc(proposalTitle(proposal)) + '</div><div class="diff"><span class="lab">修改前</span><p><del>'
        + esc(diff.before) + '</del></p><span class="lab">改为</span><p><ins>' + esc(diff.after)
        + '</ins></p></div><div class="dec-src">来源 · ' + esc(proposal.reason || "演化提案") + " · "
        + relTime(proposal.created_at) + '</div><div class="dec-act"><button type="button" class="btn pri" data-live-prop-accept="'
        + esc(proposal.proposal_id) + '">采纳</button><button type="button" class="btn" data-live-prop-reject="'
        + esc(proposal.proposal_id) + '">驳回</button></div></article>';
    }).join(""));
  }

  function openPublishDrawer() {
    const persona = LIVE.snapshot.persona || {};
    const current = (persona.versions && persona.versions.current) || {};
    const consumers = persona.consumers || {};
    openLiveDrawer("发布详情", "第 " + (current.revision || 0) + " 版 · " + (current.version || "—"),
      '<div class="consumers" style="grid-template-columns:1fr 1fr;margin-top:0">'
      + Object.keys(consumers).map((name) => {
        const row = consumers[name] || {};
        const disconnected = row.status === "not_connected";
        return '<div class="consumer' + (disconnected ? " offline" : row.version === current.version ? "" : " wait") + '"><div class="n"><i></i>'
          + esc(CONSUMER_LABELS[name] || name) + '</div><div class="st">'
          + (disconnected ? "未接入" : row.version === current.version ? "已采用" : "待同步") + '</div><div class="t">'
          + (disconnected ? "接入后报告版本采用状态" : row.version ? esc(String(row.version).slice(0, 8)) : "尚未采用") + "</div></div>";
      }).join("") + "</div>"
      + '<div style="margin-top:18px"><div class="token-row"><span>版本令牌</span><b title="'
      + esc(current.version || "") + '">' + esc(current.version ? String(current.version).slice(0, 8) : "—")
      + '</b></div><div class="token-row"><span>发布于</span><b>' + esc(current.published_at ? relTime(current.published_at) : "—")
      + '</b></div></div><p class="note">只统计已接入的使用方；回滚保留切换后新增的对话、更正与删除记录。</p>'
      + '<div class="detail-act"><button type="button" class="btn" data-live-refresh>刷新状态</button>'
      + '<button type="button" class="btn pri" data-live-publish="next_turn">发布到下一轮</button></div>');
  }

  // The name lives on the identity section's first line, written as
  // `名字: <name>`. These helpers read and rewrite that line so the editor can
  // offer a plain name box while the stored text keeps one obvious shape.
  function nameLineOf(text) {
    const line = String(text || "").split("\n").map((row) => row.trim()).find((row) => row.length);
    if (!line) return "";
    const match = line.match(/^(名字|姓名)[:：]\s*(.*)$/);
    return match ? match[2].trim() : "";
  }

  function withNameLine(text, name) {
    const lines = String(text || "").split("\n");
    const first = (lines.find((row) => row.trim()) || "").trim();
    const rest = /^(名字|姓名)[:：]/.test(first) ? lines.slice(lines.indexOf(lines.find((row) => row.trim())) + 1) : lines;
    return ["名字: " + name].concat(rest).join("\n").replace(/\n{3,}/g, "\n\n");
  }

  // One editable block: prose, then the permissions that decide who may rewrite
  // it and where it reaches. Every section is shown — including ones closed to
  // evolution — because "open this one to evolution" is itself a card edit.
  function sectionEditorBlock(section, isIdentity) {
    // A custom section's label *is* its id; repeating it would read as a stutter.
    const label = sectionLabel(section.section_id);
    return '<div class="detail-sec" data-sec-block="' + esc(section.section_id) + '"><h4>'
      + esc(label)
      + '<span class="dim" style="font-weight:400;font-size:12.5px;margin-left:8px">'
      + (label === section.section_id ? "" : esc(section.section_id) + " · ")
      + "第 " + esc(section.revision || 0) + " 版 · "
      + esc(permSummary(section)) + "</span></h4>"
      + '<textarea data-live-edit-text="' + esc(section.section_id) + '" maxlength="' + SECTION_TEXT_MAX
      + '" rows="' + (isIdentity ? "5" : "4") + '" style="' + FIELD_STYLE + ';resize:vertical">'
      + esc(section.text) + "</textarea>"
      + permControls(section)
      + '<p class="dim" style="font-size:12px;margin:4px 0 0">' + esc(scopeSummary(section)) + "</p>"
      + "</div>";
  }

  function openPersonaEditor() {
    const persona = LIVE.snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const sections = card.sections || [];
    const name = nameLineOf((sections.find((row) => row.section_id === "identity") || {}).text);
    const ids = sectionIdsOf(card);
    const presets = SECTION_PRESET_UI.filter((preset) => !ids.has(preset.id));
    openLiveDrawer("编辑人格卡", "基于第 " + (card.revision || 0) + " 版 · 保存会创建新版本",
      '<div class="detail-sec" style="margin-top:0"><h4>名字</h4>'
      + '<input id="liveNameField" maxlength="80" value="' + esc(name) + '" style="' + FIELD_STYLE + '">'
      + '<p class="dim" style="font-size:12.5px;margin:6px 0 0">保存在身份段第一行「名字: …」，下面正文无需重复写名字。</p></div>'
      + sections.map((section) => sectionEditorBlock(section, section.section_id === "identity")).join("")
      + '<details id="newSectionPanel" style="margin:18px 0 0;border-top:1px solid var(--line-2);padding-top:16px">'
      + "<summary>新增段落</summary>"
      + '<p class="dim" style="font-size:12.5px;margin:10px 0;line-height:1.6">段落是自由语义单位：预设只填好默认权限，任何名字都能新建。无法开放的字符会被去掉并写进段落标识。</p>'
      + '<label style="display:block;font-size:13px;color:var(--ink-3);margin:0 0 5px">预设</label>'
      + '<select id="newSectionPreset" style="' + FIELD_STYLE + '"><option value="">自定义段落…</option>'
      + presets.map((preset) => '<option value="' + esc(preset.id) + '">' + esc(preset.label) + "</option>").join("")
      + "</select>"
      + '<label style="display:block;font-size:13px;color:var(--ink-3);margin:12px 0 5px">段落名</label>'
      + '<input id="newSectionName" maxlength="' + NEW_SECTION_ID_MAX + '" placeholder="例如：看世界的方式" style="'
      + FIELD_STYLE + '">'
      + '<p id="newSectionIdHint" class="dim" style="font-size:12.5px;margin:6px 0 0;line-height:1.6"></p>'
      + '<label style="display:block;font-size:13px;color:var(--ink-3);margin:12px 0 5px">正文</label>'
      + '<textarea id="newSectionText" rows="3" maxlength="' + SECTION_TEXT_MAX + '" style="' + FIELD_STYLE
      + ';resize:vertical" placeholder="这一段说什么；留空则不新增"></textarea>'
      + '<p id="newSectionError" role="alert" class="note" style="display:none;font-size:12.5px;line-height:1.6"></p>'
      + '<div data-new-block>'
      + permControls({ editable_by: { user: true, evolution: true }, frozen: false, scope: {} })
      + "</div>"
      + "</details>"
      + '<p id="liveEditorError" role="alert" class="note" style="display:none"></p>'
      + '<div class="detail-act"><button type="button" class="btn" data-live-editor-cancel>取消</button>'
      + '<button type="button" class="btn pri" data-live-editor-save>保存为新版本</button></div>');
    $("#drawer").dataset.baseRevision = String(card.revision);
    $("#drawer").dataset.nameLine = name;
    bindSectionEditor(sections);
    restorePersonaDraft();
  }

  function showEditorError(message) {
    const node = $("#liveEditorError");
    if (!node) { if (message) toast(message); return; }
    node.textContent = message || "";
    node.style.cssText = message
      ? ("display:block;font-size:12.5px;line-height:1.6;" + EDITOR_ERROR_STYLE) : "display:none";
  }

  function showNewSectionError(message) {
    const node = $("#newSectionError");
    if (!node) { if (message) toast(message); return; }
    node.textContent = message || "";
    node.style.cssText = message
      ? ("display:block;font-size:12.5px;line-height:1.6;" + EDITOR_ERROR_STYLE) : "display:none";
  }

  // Blocks are found by their data attribute value, never by a selector built
  // from a section id: host-imported ids may contain dots or CJK characters.
  function sectionBlock(sectionId) {
    return $$("[data-sec-block]").find((node) => node.dataset.secBlock === sectionId) || null;
  }

  function newSectionBlock() { return $("[data-new-block]"); }

  // The derived id, the preset choice and the permission controls have to agree,
  // so they are wired as one small form instead of three independent listeners.
  function bindSectionEditor(sections) {
    const byId = {};
    // A name collides when its derived id exists *or* when it reads the same as an
    // existing section: someone typing「语气」means the stored `tone` section, and
    // silently creating a second "语气" would be two sections with one meaning.
    const labelToId = {};
    (sections || []).forEach((section) => {
      byId[section.section_id] = section;
      labelToId[sectionLabel(section.section_id)] = section.section_id;
    });
    const preset = $("#newSectionPreset");
    const nameInput = $("#newSectionName");
    const hint = $("#newSectionIdHint");
    const block = newSectionBlock();

    const refreshHint = () => {
      if (!hint) return;
      const name = nameInput ? nameInput.value.trim() : "";
      // A chosen preset owns its documented id; a custom name is turned into one.
      const chosen = preset ? SECTION_PRESET_UI.find((row) => row.id === preset.value) : null;
      const id = chosen ? chosen.id : sectionIdFromName(name);
      hint.style.color = "";
      if (!name) { hint.textContent = "留空则不新增段落。段落名会转成稳定的段落标识。"; return; }
      if (!id) {
        hint.textContent = "这个名字里没有可用字符，请加上文字、字母或数字。";
        hint.style.color = "var(--rose)";
        return;
      }
      const clash = byId[id] ? id : (labelToId[name] || "");
      if (clash) {
        hint.textContent = "「" + sectionLabel(clash) + "」已存在（标识 " + clash
          + "），请直接编辑上面的段落，不会重复新建。";
        hint.style.color = "var(--rose)";
        return;
      }
      hint.textContent = "段落标识 · " + id
        + (!chosen && id !== name ? "（已按规则整理）" : "");
    };

    const syncSceneDetail = () => {
      if (!block) return;
      const scene = $("[data-perm-scene]", block);
      const detail = $("[data-perm-scene-detail]", block);
      if (scene && detail) detail.style.opacity = scene.value === "all" ? ".5" : "1";
    };

    // Picking a preset fills its defaults; going back to 自定义段落 must clear
    // them again, or a section created right after trying a preset silently keeps
    // that preset's permissions instead of the neutral ones.
    let lastPresetLabel = "";
    const setPerms = (evolutionOn, frozenOn) => {
      const evolution = $("[data-perm-evolution]", block);
      const frozen = $("[data-perm-frozen]", block);
      if (evolution) evolution.checked = !!evolutionOn;
      if (frozen) frozen.checked = !!frozenOn;
      const note = $(".perm-note", block);
      if (note) note.style.color = frozenOn ? "var(--rose)" : "";
    };

    const applyPreset = () => {
      if (!preset || !block) return;
      const chosen = SECTION_PRESET_UI.find((row) => row.id === preset.value);
      if (!chosen) {
        setPerms(true, false);
        if (nameInput && nameInput.value === lastPresetLabel) nameInput.value = "";
        lastPresetLabel = "";
        syncSceneDetail();
        refreshHint();
        return;
      }
      if (nameInput) nameInput.value = chosen.label;
      lastPresetLabel = chosen.label;
      setPerms(chosen.evolution, chosen.frozen);
      syncSceneDetail();
      refreshHint();
    };

    if (preset) preset.addEventListener("change", applyPreset);
    if (nameInput) nameInput.addEventListener("input", refreshHint);
    // A frozen section that is still open to evolution is exactly the case the
    // precedence note exists for: colour the note where the choice is made.
    $$("[data-sec-block]").concat(block ? [block] : []).forEach((node) => {
      const frozenBox = $("[data-perm-frozen]", node);
      const note = $(".perm-note", node);
      if (!frozenBox || !note) return;
      const paint = () => { note.style.color = frozenBox.checked ? "var(--rose)" : ""; };
      frozenBox.addEventListener("change", paint);
      paint();
    });
    if (block) {
      const scene = $("[data-perm-scene]", block);
      if (scene) scene.addEventListener("change", syncSceneDetail);
    }
    syncSceneDetail();
    refreshHint();
  }

  // A draft that lost the revision race is restored into the reopened editor at
  // whatever the current revision is, so a conflict costs no typing.
  function capturePersonaDraft(baseRevision) {
    const texts = {};
    $$("[data-live-edit-text]").forEach((node) => { texts[node.dataset.liveEditText] = node.value; });
    const perms = {};
    $$("[data-sec-block]").forEach((block) => {
      perms[block.dataset.secBlock] = readPermControls(block);
    });
    const nameField = $("#liveNameField");
    const preset = $("#newSectionPreset");
    const nameInput = $("#newSectionName");
    const newText = $("#newSectionText");
    const block = newSectionBlock();
    return {
      baseRevision: Number(baseRevision),
      name: nameField ? nameField.value : "",
      texts,
      perms,
      newSection: {
        preset: preset ? preset.value : "",
        name: nameInput ? nameInput.value : "",
        text: newText ? newText.value : "",
        perms: block ? readPermControls(block) : null,
      },
    };
  }

  function restorePermControls(block, state) {
    if (!block || !state) return;
    const evolution = $("[data-perm-evolution]", block);
    const frozen = $("[data-perm-frozen]", block);
    const scene = $("[data-perm-scene]", block);
    const detail = $("[data-perm-scene-detail]", block);
    if (evolution) evolution.checked = !!(state.editable_by && state.editable_by.evolution);
    if (frozen) frozen.checked = !!state.frozen;
    if (scene) scene.value = state.scene || "all";
    if (detail) {
      detail.value = state.sceneDetail || "";
      detail.style.opacity = (state.scene || "all") === "all" ? ".5" : "1";
    }
  }

  function applyPersonaDraft(draft) {
    if (!draft) return;
    const nameField = $("#liveNameField");
    if (nameField && draft.name != null) nameField.value = draft.name;
    $$("[data-live-edit-text]").forEach((node) => {
      const value = draft.texts[node.dataset.liveEditText];
      if (typeof value === "string") node.value = value;
    });
    Object.keys(draft.perms || {}).forEach((sid) => {
      restorePermControls(sectionBlock(sid), draft.perms[sid]);
    });
    const fresh = draft.newSection || {};
    const preset = $("#newSectionPreset");
    const nameInput = $("#newSectionName");
    const newText = $("#newSectionText");
    if (preset) preset.value = fresh.preset || "";
    if (nameInput) nameInput.value = fresh.name || "";
    if (newText) newText.value = fresh.text || "";
    restorePermControls(newSectionBlock(), fresh.perms);
    if (fresh.text && fresh.text.trim()) {
      const panel = $("#newSectionPanel");
      if (panel) panel.open = true;
    }
    const hint = $("#newSectionIdHint");
    if (hint && nameInput) nameInput.dispatchEvent(new Event("input"));
  }

  function restorePersonaDraft() {
    const draft = LIVE.personaDraft;
    if (!draft) return;
    LIVE.personaDraft = null;
    applyPersonaDraft(draft);
    toast("草稿已恢复，确认后再次保存");
  }

  function closePersonaEditor() {
    const drawer = $("#drawer");
    if (drawer) drawer.classList.remove("on");
    const mask = $("#drawerMask");
    if (mask) mask.classList.remove("on");
  }

  async function retryPersonaEditor() {
    const draft = LIVE.personaDraft;
    // Reload first: the retry has to carry the revision the card is on now,
    // not the one the failed save was based on.
    await refresh();
    LIVE.personaDraft = draft;
    openPersonaEditor();
  }

  // Build one revision of operations from the form. Everything that did not
  // change stays out, so saving a permission flip never rewrites untouched prose.
  function personaEditorOperations(card) {
    const operations = [];
    const nameField = $("#liveNameField");
    const name = nameField ? nameField.value.trim() : "";
    const identity = (card.sections || []).find((row) => row.section_id === "identity");
    const identityField = $("[data-live-edit-text=\"identity\"]");
    // The name box and the identity body are one stored section: write the
    // name line back into the body the user is editing, then revise once.
    if (identity) {
      const body = identityField ? identityField.value : identity.text;
      // Only the full editor owns the name box. The one-section drawer edits the
      // identity body as-is, so it must not rewrite (or drop) the name line.
      const merged = (nameField && name) ? withNameLine(body, name) : body;
      if (merged !== identity.text) operations.push({ op: "replace", section_id: "identity", text: merged });
    }
    $$("[data-live-edit-text]").forEach((node) => {
      if (node.dataset.liveEditText === "identity") return;
      const section = (card.sections || []).find((row) => row.section_id === node.dataset.liveEditText);
      if (section && node.value !== section.text) {
        operations.push({ op: "replace", section_id: section.section_id, text: node.value });
      }
    });
    $$("[data-sec-block]").forEach((block) => {
      const sid = block.dataset.secBlock;
      const section = (card.sections || []).find((row) => row.section_id === sid);
      if (!section) return;
      const patch = permPatch(section, readPermControls(block));
      if (!Object.keys(patch).length) return;
      const existing = operations.find((op) => op.section_id === sid);
      if (existing) Object.assign(existing, patch);
      else operations.push(Object.assign({ op: "replace", section_id: sid, text: section.text }, patch));
    });
    return operations;
  }

  async function savePersonaEditor() {
    const persona = LIVE.snapshot.persona || {};
    const card = persona.card || { sections: [], revision: 0 };
    const operations = personaEditorOperations(card);
    showEditorError("");
    showNewSectionError("");
    const newText = $("#newSectionText");
    if (newText && newText.value.trim()) {
      const nameInput = $("#newSectionName");
      const name = nameInput ? nameInput.value.trim() : "";
      const presetSelect = $("#newSectionPreset");
      const chosen = (presetSelect && presetSelect.value)
        ? SECTION_PRESET_UI.find((row) => row.id === presetSelect.value) : null;
      // A preset keeps its documented id (`about_owner`), which is the stable key
      // other surfaces address; a custom section derives a key from its own name.
      const id = chosen ? chosen.id : sectionIdFromName(name);
      if (!name) { showNewSectionError("请先填写段落名，再写正文。"); return; }
      if (!id) { showNewSectionError("这个名字里没有可用字符，请加上文字、字母或数字。"); return; }
      // The same two ways of colliding as the live hint: the derived id is taken,
      // or the typed name reads the same as a section that is already on the card.
      const takenById = sectionIdsOf(card).has(id);
      const takenByLabel = (card.sections || []).find((row) => sectionLabel(row.section_id) === name);
      if (takenById || takenByLabel) {
        const clash = takenById ? id : takenByLabel.section_id;
        showNewSectionError("「" + sectionLabel(clash) + "」已存在（标识 " + clash
          + "），请直接编辑上面那一段，不会产生重复段落。");
        return;
      }
      const perms = readPermControls(newSectionBlock());
      const subject = defaultSubject(card);
      const scope = { subject };
      const reach = sceneOfChoice(perms.scene, perms.sceneDetail);
      if (reach) scope.applies_to = reach;
      // A preset only fills defaults; the same operation shape is used for a
      // hand-typed section. frozen is always sent, so an explicit choice in this
      // form wins over the examples default exactly as the backend documents.
      operations.push({
        op: "add", section_id: id, kind: chosen ? chosen.kind : id,
        text: newText.value.trim(), scope,
        editable_by: perms.editable_by, frozen: perms.frozen,
      });
    }
    if (!operations.length) { toast("没有修改"); return; }
    await submitPersonaOperations(operations, Number($("#drawer").dataset.baseRevision));
  }

  async function submitPersonaOperations(operations, baseRevision) {
    const button = $("[data-live-editor-save]");
    if (button && button.disabled) return;
    if (button) { button.disabled = true; button.textContent = "正在保存…"; }
    try {
      const result = await api("/api/persona/revise", {
        method: "POST",
        body: JSON.stringify({
          base_revision: baseRevision,
          operations,
          reason: "webui edit",
          request_id: rid("ui-revise"),
        }),
      });
      CHAT.loaded = false;
      LIVE.personaDraft = null;
      closePersonaEditor();
      await refresh();
      toast(decisionToast(result, "已保存为新版本"));
    } catch (error) {
      if (error.status === 409) {
        // The card moved on under us. Keep every field the user typed and offer
        // a rebase against the current revision instead of dropping the draft.
        LIVE.personaDraft = capturePersonaDraft(baseRevision);
        const current = (error.payload && error.payload.current) || {};
        const mine = "第 " + baseRevision + " 版";
        const theirs = current.revision != null ? "第 " + current.revision + " 版" : "更新版本";
        showEditorError(mine + "上的保存与" + theirs + "冲突，你的草稿已保留。点「载入最新版本」把草稿带到最新版本，再次保存即可。");
        const act = $("#drawerBody .detail-act");
        if (act && !$("[data-live-editor-rebase]")) {
          const rebase = document.createElement("button");
          rebase.type = "button";
          rebase.className = "btn";
          rebase.dataset.liveEditorRebase = "1";
          rebase.textContent = "载入最新版本";
          act.insertBefore(rebase, act.firstChild);
        }
      } else if (error.reason) {
        showEditorError("保存被拒绝：" + sectionReasonLabel(error.reason));
      } else {
        showEditorError("保存失败：" + error.message);
      }
      return null;
    } finally {
      if (button) { button.disabled = false; button.textContent = "保存为新版本"; }
    }
  }

  function openDeleteEvent() {
    const event = LIVE.selectedEvent;
    if (!event) { toast("请先选择一条记忆"); return; }
    openLiveModal("删除这条记忆？",
      "<p>删除后同一来源的事件不会因重放被重新写入；派生的事实与反思会一并失效。</p>"
      + '<p class="meta">记忆编号 · ' + esc(event.id) + "　|　会生成墓碑，仅保留来源指纹</p>",
      "删除", async () => {
        const result = await submitDeletion({
          source_key: event.source_key, reason: "webui delete", request_id: rid("ui-delete"),
        });
        if (result) {
          const drawer = $("#drawer");
          if (drawer) drawer.classList.remove("on");
        }
      });
  }

  function openCorrectionModal() {
    const event = LIVE.selectedEvent;
    if (!event) { toast("请先选择一条记忆"); return; }
    openLiveModal("更正这条记忆",
      "<p>填写更正后的文本。引擎会生成更正请求，并失效旧事实与派生内容。</p>"
      + '<p class="meta">原文 · ' + esc(event.text) + "</p>"
      + '<label style="display:block;margin-top:12px"><span style="font-size:13px;color:var(--ink-3)">更正为</span>'
      + '<textarea id="liveCorrectText" rows="3" style="width:100%;margin-top:6px;padding:10px 12px;border:1px solid var(--line);'
      + 'background:rgba(255,253,248,.9);font:inherit;font-size:14.5px;line-height:1.7;color:var(--ink);resize:vertical">'
      + esc(event.text) + "</textarea></label>",
      "提交更正", async () => {
        const value = ($("#liveCorrectText") || {}).value || "";
        if (!value.trim()) { toast("更正内容不能为空"); return; }
        await write("/api/cleanup/correct", {
          old_text: event.text, new_text: value.trim(), request_id: rid("ui-correct"),
        }, "更正已提交");
      });
  }

  /* ---------- delegation ---------- */

  document.addEventListener("click", async (event) => {
    if (!LIVE.connected) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const accept = target.closest("[data-live-prop-accept]");
    if (accept) {
      event.preventDefault();
      const id = accept.dataset.livePropAccept;
      accept.disabled = true;
      accept.textContent = "处理中…";
      await write("/api/evolution/" + encodeURIComponent(id) + "/accept", { reason: "webui accept" },
        (result) => decisionToast(result, "提案已采纳"));
      return;
    }
    const reject = target.closest("[data-live-prop-reject]");
    if (reject) {
      event.preventDefault();
      const id = reject.dataset.livePropReject;
      reject.disabled = true;
      reject.textContent = "处理中…";
      await write("/api/evolution/" + encodeURIComponent(id) + "/reject", { reason: "webui reject" },
        (result) => decisionToast(result, "提案已驳回"));
      return;
    }
    const withdraw = target.closest("[data-live-prop-withdraw]");
    if (withdraw) {
      event.preventDefault();
      const id = withdraw.dataset.livePropWithdraw;
      withdraw.disabled = true;
      await write("/api/evolution/" + encodeURIComponent(id) + "/withdraw", { reason: "webui withdraw" },
        (result) => decisionToast(result, "提案已撤回"));
      return;
    }
    const mode = target.closest("[data-live-mode]");
    if (mode) {
      event.preventDefault();
      const next = mode.dataset.liveMode;
      await write("/api/evolution/mode", { mode: next }, "演化模式已更新");
      const enabled = !!(LIVE.snapshot && LIVE.snapshot.evolution && LIVE.snapshot.evolution.enabled);
      if (next === "off" && enabled) await write("/api/evolution/settings", { enabled: false }, "演化已关闭");
      if (next !== "off" && !enabled) await write("/api/evolution/settings", { enabled: true }, "演化已开启");
      return;
    }
    const rollback = target.closest("[data-live-rollback]");
    if (rollback) {
      event.preventDefault();
      const version = rollback.dataset.liveRollback;
      openLiveModal("回滚到上一版？",
        "<p>回滚会重新下发该版本，并保留切换后新增的对话、更正与删除记录。</p>"
        + '<p class="meta">版本 · ' + esc(version) + "</p>", "确认回滚", async () => {
          await write("/api/persona/rollback", { version }, (result) => decisionToast(result, "已回滚"));
        });
      return;
    }
    const publish = target.closest("[data-live-publish]");
    if (publish) {
      event.preventDefault();
      const targetName = publish.dataset.livePublish;
      const persona = LIVE.snapshot.persona || {};
      const card = persona.card || {};
      await write("/api/persona/publish", { target: targetName, revision: card.revision || undefined },
        (result) => decisionToast(result, "已发布"));
      return;
    }
    const adopt = target.closest("[data-live-adopt]");
    if (adopt && SERVICE) { event.preventDefault(); toast("发起一次对话后会记录实际采用版本"); return; }
    if (adopt) {
      event.preventDefault();
      await write("/api/persona/adopt", {
        consumer: adopt.dataset.liveAdopt, version: adopt.dataset.version,
        receipt_id: rid("ui-adopt"),
      }, (result) => decisionToast(result, "使用方已采用"));
      return;
    }
    const freeze = target.closest("[data-live-freeze]");
    if (freeze) {
      event.preventDefault();
      await write("/api/persona/freeze", {
        section_id: freeze.dataset.liveFreeze,
        frozen: freeze.dataset.frozen !== "1",
      }, (result) => decisionToast(result, "冻结状态已更新"));
      return;
    }
    const editAll = target.closest("[data-live-edit-persona]");
    if (editAll) {
      event.preventDefault();
      openPersonaEditor();
      return;
    }
    const newSection = target.closest("[data-live-new-section]");
    if (newSection) {
      event.preventDefault();
      openPersonaEditor();
      const panel = $("#newSectionPanel");
      if (panel) {
        panel.open = true;
        panel.scrollIntoView({ block: "center" });
      }
      const field = $("#newSectionName");
      if (field) field.focus();
      return;
    }
    const edit = target.closest("[data-live-edit]");
    if (edit) {
      event.preventDefault();
      const sectionId = edit.dataset.liveEdit;
      const persona = LIVE.snapshot.persona || {};
      const card = persona.card || { sections: [], revision: 0 };
      const section = (card.sections || []).find((row) => row.section_id === sectionId);
      if (!section) return;
      // The one-section drawer is the same block as the full editor, so the
      // permissions here behave identically to the ones further up the card.
      openLiveDrawer("编辑段落 · " + sectionLabel(sectionId),
        "基于第 " + (card.revision || 0) + " 版 · " + scopeSummary(section),
        sectionEditorBlock(section, section.section_id === "identity")
        + '<p id="liveEditorError" role="alert" class="note" style="display:none"></p>'
        + '<div class="detail-act"><button type="button" class="btn" data-live-editor-cancel>取消</button>'
        + '<button type="button" class="btn pri" data-live-editor-save>保存为新版本</button></div>');
      $("#drawer").dataset.baseRevision = String(card.revision);
      bindSectionEditor([section]);
      restorePersonaDraft();
      return;
    }
    const rebase = target.closest("[data-live-editor-rebase]");
    if (rebase) {
      event.preventDefault();
      await retryPersonaEditor();
      return;
    }
    const save = target.closest("[data-live-editor-save]");
    if (save) {
      event.preventDefault();
      await savePersonaEditor();
      return;
    }
    const cancel = target.closest("[data-live-editor-cancel]");
    if (cancel) {
      event.preventDefault();
      closePersonaEditor();
      return;
    }
    const cleanKind = target.closest("[data-clean-kind]");
    if (cleanKind) {
      event.preventDefault();
      LIVE.cleanupKind = cleanKind.dataset.cleanKind || "all";
      LIVE.cleanupLimit = CLEAN_PAGE_SIZE;
      renderCleanup();
      return;
    }
    const cleanMore = target.closest("[data-clean-more]");
    if (cleanMore) {
      event.preventDefault();
      LIVE.cleanupLimit += CLEAN_PAGE_SIZE;
      renderCleanup();
      return;
    }
    const del = target.closest("[data-live-delete]");
    const retryCleanup = target.closest("[data-live-cleanup-retry]");
    if (retryCleanup) {
      event.preventDefault();
      retryCleanup.disabled = true;
      toast("正在重试清理…");
      try {
        const result = await api("/v1/cleanup/retry", { method: "POST",
          body: JSON.stringify({ deletion_id: retryCleanup.dataset.liveCleanupRetry }) });
        updateDeletion(result);
        toast(deletionStatus(result));
      } catch (error) {
        toast("重试清理失败：" + error.message);
      } finally {
        retryCleanup.disabled = false;
      }
      return;
    }
    const retractCorrection = target.closest("[data-live-correction-retract]");
    if (retractCorrection) {
      event.preventDefault();
      await write("/v1/cleanup/retract-correction", { correction_id: retractCorrection.dataset.liveCorrectionRetract }, "更正已撤回");
      return;
    }
    if (del) {
      event.preventDefault();
      const type = del.dataset.type;
      const id = del.dataset.liveDelete;
      openLiveModal("删除这条记忆？",
        "<p>删除会生成墓碑，并使派生的事实与反思失效。</p>"
        + '<p class="meta">' + esc(type) + " · " + esc(id) + "</p>", "删除", async () => {
          await submitDeletion({
            type, id, reason: "webui delete", request_id: rid("ui-delete"),
          });
        });
      return;
    }
    const tombstones = target.closest("[data-live-tombstones]");
    if (tombstones) {
      event.preventDefault();
      const rows = (LIVE.snapshot.cleanup.tombstones || []);
      openLiveDrawer("墓碑", rows.length + " 条来源指纹",
        rows.length ? rows.map((row) => '<div class="rel-item"><span class="tag">' + esc(row.target_type)
          + "</span>" + esc(row.target_id) + "<b>" + esc(row.at ? relTime(row.at) : "—") + "</b></div>").join("")
          : '<div class="empty">还没有墓碑</div>');
      return;
    }
    const evolutionSwitch = target.closest("[data-live-evolution]");
    if (evolutionSwitch) {
      event.preventDefault();
      const next = evolutionSwitch.dataset.liveEvolution === "1";
      await write("/api/evolution/settings", { enabled: next }, "演化设置已更新");
      // Keep the switch and the mode chips consistent: turning evolution off
      // also parks the mode at "off", turning it on restores "propose".
      const mode = LIVE.snapshot && LIVE.snapshot.evolution ? LIVE.snapshot.evolution.mode : "off";
      if (!next && mode !== "off") await write("/api/evolution/mode", { mode: "off" }, "演化已关闭");
      if (next && mode === "off") await write("/api/evolution/mode", { mode: "propose" }, "演化已切到提议模式");
      return;
    }
    const exportSettings = target.closest("[data-live-export-settings]");
    if (target.closest("[data-live-maintain]")) {
      event.preventDefault();
      await write("/v1/maintenance", {}, "维护已完成");
      return;
    }
    if (exportSettings) {
      event.preventDefault();
      const payload = { engine: LIVE.engine, meta: LIVE.snapshot.meta, generated_at: new Date().toISOString() };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const anchor = document.createElement("a");
      anchor.href = URL.createObjectURL(blob);
      anchor.download = "rin-engine.json";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(anchor.href);
      toast("引擎信息已导出");
      return;
    }
    if (SERVICE && target.closest('a[href="/v1/export"]')) {
      event.preventDefault();
      const exported = await api("/v1/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = "rin-memory.json"; link.click(); URL.revokeObjectURL(url);
      return;
    }
    if (target.closest("[data-live-refresh]")) {
      event.preventDefault();
      await refresh();
      toast("已刷新");
      return;
    }
    if (target.closest("[data-live-modal-cancel]")) {
      event.preventDefault();
      closeLiveModal();
      return;
    }
    if (target.closest("[data-live-modal-confirm]")) {
      event.preventDefault();
      const action = LIVE.modalConfirm;
      closeLiveModal();
      if (action) await action();
      return;
    }
  });

  /* selection: run after the demo handlers, then override the detail */
  document.addEventListener("click", (event) => {
    if (!LIVE.connected) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const eventRow = target.closest('[data-page="timeline"] .tl-row');
    if (eventRow && eventRow.dataset.liveEvent) {
      setTimeout(() => selectLiveEvent(eventRow.dataset.liveEvent), 0);
      return;
    }
    const reflRow = target.closest('.refl-row');
    if (reflRow && (reflRow.dataset.liveRefl || reflRow.dataset.liveGrowth)) {
      setTimeout(() => selectLiveReflection(reflRow.dataset.liveRefl || reflRow.dataset.liveGrowth), 0);
      return;
    }
    const propRow = target.closest('.prop-row');
    if (propRow && propRow.dataset.liveProposal) {
      setTimeout(() => selectLiveProposal(propRow.dataset.liveProposal), 0);
      return;
    }
    const verRow = target.closest('.ver-row');
    if (verRow && verRow.dataset.liveRevision) {
      setTimeout(() => showLiveRevision(verRow.dataset.liveRevision), 0);
    }
  });

  /* capture-phase overrides for demo-only controls */
  document.addEventListener("click", (event) => {
    if (!LIVE.connected) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest("#dtDelete")) {
      event.preventDefault(); event.stopPropagation(); openDeleteEvent(); return;
    }
    if (target.closest("#dtCorrect")) {
      event.preventDefault(); event.stopPropagation(); openCorrectionModal(); return;
    }
    if (target.closest("[data-edit-persona]")) {
      event.preventDefault(); event.stopPropagation(); openPersonaEditor(); return;
    }
    if (target.closest("#showAll")) {
      event.preventDefault(); event.stopPropagation(); openPendingDrawer(); return;
    }
    if (target.closest("[data-drawer]")) {
      event.preventDefault(); event.stopPropagation(); openPublishDrawer();
    }
  }, true);

  bindChat();
  bindModelConfig();
  document.addEventListener("visibilitychange", watchCleanup);
  window.addEventListener("hashchange", watchCleanup);
  if (SERVICE) {
    $("#qqPersonaForm")?.addEventListener("submit", (event) => {
      event.preventDefault();
      editQqPersona(true);
    });
    $("#qqPersonaReload")?.addEventListener("click", loadQqPersona);
  }
  refresh();
  if (SERVICE) setInterval(() => { if (!LIVE.connected && !document.hidden) refresh(); }, 10000);
})();
