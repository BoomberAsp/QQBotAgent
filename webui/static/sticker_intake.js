(() => {
  const byId = (id) => document.getElementById(id);
  const base = "/api/resources/sticker-intake";
  const headers = {"Content-Type": "application/json", "X-Resource-Admin": "1"};
  const selection = new Map();

  function element(tag, text, parent) {
    const item = document.createElement(tag);
    if (text) item.textContent = text;
    if (parent) parent.append(item);
    return item;
  }

  function button(parent, text, action) {
    const item = element("button", text, parent);
    item.className = "btn";
    item.type = "button";
    item.onclick = action;
  }

  function field(parent, label, value, maxLength) {
    const box = element("label", label + " ", parent);
    const input = element("input", "", box);
    input.value = value || "";
    input.maxLength = maxLength;
    return input;
  }

  async function api(path, method = "GET", payload) {
    const response = await fetch(base + path, {
      method, headers, body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "操作未完成");
    return data;
  }

  async function loadCandidates() {
    const container = byId("intake-candidates");
    container.replaceChildren();
    selection.clear();
    try {
      const {candidates, uses} = await api("/candidates");
      if (!candidates.length) element("p", "暂无候选。", container);
      for (const candidate of candidates) {
        const card = element("div", "", container);
        card.className = "card";
        const label = element("label", "", card);
        const picked = element("input", "", label);
        picked.type = "checkbox";
        const scope = candidate.scope ? "本群 · " + candidate.scope : "公共图库";
        element("span", candidate.persona + " · " + scope, label);
        const img = element("img", "", card);
        img.src = base + "/candidates/" + encodeURIComponent(candidate.id);
        img.alt = candidate.description || "候选表情包";
        img.loading = "lazy";
        img.style.cssText = "display:block;max-width:180px;max-height:140px;margin:6px 0";
        if (candidate.description) element("p", candidate.description, card);
        if (candidate.repeats) element("p", "群内重复使用 " + candidate.repeats + " 次", card);
        const tags = field(card, "用途", candidate.uses.map((use) => uses[use] || use).join(","), 120);
        const author = candidate.origin === "web" ? field(card, "作者", candidate.author, 120) : null;
        const license = candidate.origin === "web" ? field(card, "复用许可", candidate.license, 160) : null;
        if (candidate.source_url?.startsWith("https://")) {
          const link = element("a", "打开来源页", card);
          link.href = candidate.source_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        }
        picked.onchange = () => {
          if (picked.checked) selection.set(candidate.id, {tags, author, license});
          else selection.delete(candidate.id);
        };
      }
    } catch (error) {
      element("p", error.message, container);
    }
  }

  async function review(action) {
    const status = byId("intake-status");
    try {
      if (!selection.size || selection.size > 30) throw new Error("请选择 1 到 30 个候选");
      const confirmed = byId("intake-confirmed").checked;
      if (action === "approve" && !confirmed) throw new Error("请勾选内容与复用许可确认");
      const items = Array.from(selection, ([id, fields]) => ({
        id, uses: fields.tags.value,
        ...(fields.author ? {author: fields.author.value, license: fields.license.value} : {}),
      }));
      const results = await api("/review", "POST", {action, confirmed, items});
      const completed = results.filter((row) => row.status === (action === "approve" ? "published" : "rejected")).length;
      const missing = results.filter((row) => row.status === "missing_rights").length;
      status.textContent = "已处理 " + completed + " 项。" + (missing ? " " + missing + " 项缺少来源、作者或复用许可。" : "");
      byId("intake-confirmed").checked = false;
      await loadCandidates();
    } catch (error) {
      status.textContent = error.message;
    }
  }

  async function updateSource(path, method, payload) {
    const status = byId("intake-source-status");
    try {
      const result = await api(path, method, payload);
      status.textContent = result.message || "订阅已更新";
      await Promise.all([loadSources(), loadCandidates()]);
    } catch (error) {
      status.textContent = error.message;
    }
  }

  async function loadSources() {
    const container = byId("intake-sources");
    container.replaceChildren();
    try {
      const records = await api("/sources");
      if (!records.length) element("p", "暂无互联网订阅。", container);
      for (const record of records) {
        const entry = element("div", "", container);
        entry.className = "card";
        element("p", record.persona + " · " + (record.enabled ? "已启用" : "已暂停") + " · " + record.url, entry);
        if (record.outcome) element("p", record.outcome, entry);
        const path = "/sources/" + encodeURIComponent(record.id);
        if (record.enabled) button(entry, "立即同步", () => updateSource(path + "/sync", "POST"));
        button(entry, record.enabled ? "暂停" : "启用", () =>
          updateSource("/sources", "POST", {...record, enabled: !record.enabled}));
        button(entry, "移除订阅", () => updateSource(path, "DELETE"));
      }
    } catch (error) {
      element("p", error.message, container);
    }
  }

  byId("intake-refresh").onclick = loadCandidates;
  byId("intake-approve").onclick = () => review("approve");
  byId("intake-reject").onclick = () => review("reject");
  byId("intake-source-form").onsubmit = (event) => {
    event.preventDefault();
    updateSource("/sources", "POST", Object.fromEntries(new FormData(event.currentTarget)));
  };
  loadCandidates();
  loadSources();
})();
