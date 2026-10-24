/* Theme the existing select elements without changing their value/event API. */
(() => {
  "use strict";
  if (window.MiFitSelectPicker) return;
  document.querySelectorAll("[data-select-picker-transient]").forEach(node => node.remove());
  const enhanced = new WeakSet(), mobile = matchMedia("(max-width:690px)");
  const ROW = 44;
  let select = null, options = [], draft = null, scrollTimer = null;
  const dialog = document.createElement("dialog");
  dialog.id = "select-picker-dialog";
  dialog.dataset.selectPickerTransient = "";
  dialog.setAttribute("aria-labelledby", "select-picker-title");
  dialog.innerHTML = `<div class="select-picker-handle" aria-hidden="true"></div>
    <header class="select-picker-heading"><h2 id="select-picker-title">选择选项</h2><button id="select-picker-close" type="button" aria-label="取消选择">×</button></header>
    <div id="select-picker-list" role="listbox" aria-labelledby="select-picker-title"></div>
    <div id="select-picker-wheel-wrap" hidden><div class="select-picker-selection" aria-hidden="true"></div><div id="select-picker-wheel" role="listbox" tabindex="0" aria-labelledby="select-picker-title"></div><p class="select-picker-hint">上下滑动选择，确认后生效</p></div>
    <div class="select-picker-actions" hidden><button id="select-picker-cancel" type="button">取消</button><button id="select-picker-confirm" type="button">确认</button></div>`;
  document.body.append(dialog);
  const $ = id => document.getElementById(id);
  const wheel = $("select-picker-wheel"), list = $("select-picker-list");
  const disabled = option => option.disabled || option.parentElement?.matches("optgroup:disabled");
  const visibleOptions = target => [...target.options].filter(option => !option.hidden && !option.parentElement?.hidden);
  const wheelMode = () => mobile.matches && options.length > 5;
  function label(target) {
    if (target.getAttribute("aria-label")) return target.getAttribute("aria-label");
    const source = target.labels?.[0];
    if (!source) return "选择选项";
    const copy = source.cloneNode(true);
    copy.querySelectorAll("select").forEach(node => node.remove());
    return copy.textContent.trim() || "选择选项";
  }
  function position() {
    if (!dialog.open || !select) return;
    if (wheelMode()) {
      dialog.style.removeProperty("left"); dialog.style.removeProperty("top");
      dialog.style.removeProperty("width");
      return;
    }
    const rect = select.getBoundingClientRect();
    dialog.style.width = Math.min(Math.max(rect.width, 180), innerWidth - 24) + "px";
    const width = dialog.offsetWidth, height = dialog.offsetHeight;
    const available = window.visualViewport?.height || innerHeight;
    const top = rect.bottom + height + 6 <= available - 12 ? rect.bottom + 6 : rect.top - height - 6;
    dialog.style.left = Math.max(12, Math.min(rect.left, innerWidth - width - 12)) + "px";
    dialog.style.top = Math.max(12, Math.min(top, available - height - 12)) + "px";
  }
  function markWheel() {
    [...wheel.children].forEach((node, index) => node.setAttribute("aria-selected", String(options[index] === draft)));
    const index = options.indexOf(draft);
    if (index >= 0) wheel.setAttribute("aria-activedescendant", "select-picker-option-" + index);
    else wheel.removeAttribute("aria-activedescendant");
    $("select-picker-confirm").disabled = !draft || disabled(draft);
  }
  function chooseDraft(option, scroll = true) {
    if (!option || disabled(option)) return;
    draft = option;
    markWheel();
    if (scroll) wheel.scrollTop = options.indexOf(draft) * ROW;
  }
  function settleWheel() {
    if (!dialog.open || !wheelMode()) return;
    const index = Math.max(0, Math.min(options.length - 1, Math.round(wheel.scrollTop / ROW)));
    const enabled = options.map((option, i) => ({option, i})).filter(item => !disabled(item.option));
    enabled.sort((a, b) => Math.abs(a.i - index) - Math.abs(b.i - index));
    chooseDraft(enabled[0]?.option);
  }
  function render() {
    options = visibleOptions(select);
    if (!options.includes(draft) || disabled(draft)) draft = options.find(option => option.selected && !disabled(option)) || options.find(option => !disabled(option));
    const isWheel = wheelMode();
    dialog.dataset.mode = isWheel ? "wheel" : "list";
    list.hidden = isWheel;
    $("select-picker-wheel-wrap").hidden = !isWheel;
    dialog.querySelector(".select-picker-actions").hidden = !isWheel;
    const container = isWheel ? wheel : list;
    container.replaceChildren();
    for (const [index, option] of options.entries()) {
      const item = document.createElement(isWheel ? "div" : "button");
      item.id = "select-picker-option-" + index;
      item.className = isWheel ? "select-picker-wheel-item" : "select-picker-option";
      item.dataset.index = index;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(option === draft));
      item.setAttribute("aria-disabled", String(!!disabled(option)));
      item.textContent = option.label;
      if (!isWheel) { item.type = "button"; item.disabled = disabled(option); item.tabIndex = option === draft ? 0 : -1; }
      container.append(item);
    }
    if (isWheel) { markWheel(); wheel.scrollTop = Math.max(0, options.indexOf(draft)) * ROW; }
    position();
  }
  function open(target) {
    if (!(target instanceof HTMLSelectElement) || target.disabled || target.multiple || target.size > 1) return;
    if (dialog.open) close();
    select = target;
    draft = target.selectedOptions[0];
    $("select-picker-title").textContent = label(target);
    select.setAttribute("aria-expanded", "true");
    render(); dialog.showModal(); render();
    if (wheelMode()) wheel.focus({preventScroll:true});
    else list.querySelector('[tabindex="0"]')?.focus({preventScroll:true});
    position();
  }
  function close() {
    clearTimeout(scrollTimer);
    const previous = select; select = null;
    if (dialog.open) dialog.close();
    previous?.setAttribute("aria-expanded", "false");
    if (previous?.isConnected && !previous.disabled) previous.focus({preventScroll:true});
  }
  function commit(option) {
    const target = select;
    if (!target || target.disabled || !option || disabled(option) || ![...target.options].includes(option)) { close(); return; }
    const changed = target.selectedOptions[0] !== option;
    target.selectedIndex = [...target.options].indexOf(option);
    close();
    if (changed) {
      target.dispatchEvent(new Event("input", {bubbles:true}));
      target.dispatchEvent(new Event("change", {bubbles:true}));
    }
  }
  list.addEventListener("click", event => {
    const item = event.target.closest("[data-index]");
    if (item) commit(options[Number(item.dataset.index)]);
  });
  function move(event, current, apply) {
    const enabled = options.filter(option => !disabled(option));
    let index = Math.max(0, enabled.indexOf(current));
    if (event.key === "ArrowDown" || event.key === "ArrowRight") index++;
    else if (event.key === "ArrowUp" || event.key === "ArrowLeft") index--;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = enabled.length - 1;
    else return;
    event.preventDefault();
    apply(enabled[Math.max(0, Math.min(enabled.length - 1, index))]);
  }
  list.addEventListener("keydown", event => move(event, options[Number(document.activeElement.dataset.index)], option => {
    const item = list.querySelector(`[data-index="${options.indexOf(option)}"]`);
    if (item) { list.querySelectorAll("button").forEach(node => node.tabIndex = -1); item.tabIndex = 0; item.focus(); }
  }));
  wheel.addEventListener("keydown", event => move(event, draft, chooseDraft));
  wheel.addEventListener("scroll", () => { clearTimeout(scrollTimer); scrollTimer = setTimeout(settleWheel, 120); }, {passive:true});
  wheel.addEventListener("scrollend", settleWheel);
  wheel.addEventListener("click", event => {
    const item = event.target.closest("[data-index]");
    if (item) chooseDraft(options[Number(item.dataset.index)]);
  });
  $("select-picker-confirm").addEventListener("click", () => { settleWheel(); commit(draft); });
  for (const id of ["select-picker-close", "select-picker-cancel"]) $(id).addEventListener("click", close);
  dialog.addEventListener("cancel", event => { event.preventDefault(); event.stopPropagation(); close(); });
  let backdropPress = false;
  function outside(event) {
    const rect = dialog.getBoundingClientRect();
    return event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom);
  }
  dialog.addEventListener("pointerdown", event => { backdropPress = outside(event); });
  dialog.addEventListener("click", event => { if (backdropPress && outside(event)) close(); backdropPress = false; });
  function enhance(root = document) {
    const candidates = root.matches?.("select") ? [root] : [];
    candidates.push(...root.querySelectorAll?.("select") || []);
    for (const target of candidates) {
      if (enhanced.has(target) || target.multiple || target.size > 1) continue;
      enhanced.add(target);
      target.classList.add("mifit-select");
      target.setAttribute("aria-haspopup", "dialog");
      target.setAttribute("aria-controls", dialog.id);
      target.setAttribute("aria-expanded", "false");
      target.addEventListener("pointerdown", event => { if (!target.disabled && event.button === 0) event.preventDefault(); });
      target.addEventListener("click", event => { event.preventDefault(); open(target); });
      target.addEventListener("keydown", event => {
        if ([" ", "Enter", "ArrowDown", "ArrowUp", "F4"].includes(event.key)) {
          event.preventDefault(); event.stopPropagation(); open(target);
        }
      });
      target.addEventListener("change", () => { if (select === target) { draft = target.selectedOptions[0]; render(); } });
    }
  }
  enhance();
  new MutationObserver(records => {
    let changed = false;
    for (const record of records) {
      if (dialog.contains(record.target)) continue;
      for (const node of record.addedNodes) if (node.nodeType === Node.ELEMENT_NODE) enhance(node);
      if (select && (record.target === select || select.contains(record.target))) changed = true;
    }
    if (select && (!select.isConnected || select.disabled)) close();
    else if (select && changed) render();
  }).observe(document.body, {childList:true, subtree:true, attributes:true, characterData:true, attributeFilter:["disabled", "hidden", "label", "selected", "value"]});
  mobile.addEventListener("change", () => { if (dialog.open) render(); });
  window.addEventListener("resize", position);
  window.visualViewport?.addEventListener("resize", position);
  window.MiFitSelectPicker = {enhance, open, close};
})();
