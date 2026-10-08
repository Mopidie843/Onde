// Composants d'interface : toasts, menus contextuels, fenêtres, glisser-déposer.
import { $, esc, icon, isMobile } from "./util.js";

const layer = () => $("#layer");

// ---------------------------------------------------------------- toasts
export function toast(message, { error = false, action = null, timeout = 3200 } = {}) {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = "toast" + (error ? " err" : "");
  el.innerHTML = `<span>${esc(message)}</span>`;
  if (action) {
    const b = document.createElement(action.href ? "a" : "button");
    b.textContent = action.label;
    if (action.href) b.href = action.href;
    b.addEventListener("click", () => { action.onClick?.(); remove(); });
    el.appendChild(b);
  }
  box.appendChild(el);
  while (box.children.length > 3) box.firstChild.remove();
  let t = setTimeout(remove, timeout);
  el.addEventListener("pointerenter", () => clearTimeout(t));
  el.addEventListener("pointerleave", () => { t = setTimeout(remove, 1500); });
  function remove() { el.classList.add("out"); setTimeout(() => el.remove(), 200); }
}
export const toastError = (e) => toast(e?.message || String(e), { error: true, timeout: 5000 });

// ---------------------------------------------------------------- menus
let closeCurrent = null;
export function closeMenu() { if (closeCurrent) closeCurrent(); }

/**
 * items : [{ icon, label, onClick, danger, disabled, on, right, sub: () => items|Promise }, "hr", { heading }]
 */
export function openMenu(items, { x = 0, y = 0, anchor = null, head = "", onClose = null } = {}) {
  closeMenu();
  const sheet = isMobile();
  const scrim = document.createElement("div");
  scrim.className = "scrim" + (sheet ? " dark" : "");
  const menu = document.createElement("div");
  menu.className = "menu" + (sheet ? " sheet" : "");
  menu.setAttribute("role", "menu");
  layer().append(scrim, menu);

  const stack = [];
  const render = (list, withBack = false) => {
    menu.innerHTML = (head && !withBack ? head : "") + (withBack ? `<button class="mi" data-back>${icon("left")}<span>Retour</span></button><hr>` : "");
    list.forEach((it, i) => {
      if (it === "hr") { menu.insertAdjacentHTML("beforeend", "<hr>"); return; }
      if (it.heading) { menu.insertAdjacentHTML("beforeend", `<div class="mi-sub">${esc(it.heading)}</div>`); return; }
      const b = document.createElement("button");
      b.className = "mi" + (it.danger ? " danger" : "") + (it.on ? " on" : "");
      b.setAttribute("role", "menuitem");
      if (it.disabled) b.disabled = true;
      b.innerHTML = `${it.icon ? icon(it.icon) : ""}<span class="ell">${esc(it.label)}</span>${it.sub ? `<span class="mi-r">${icon("right", "sm")}</span>` : it.right ? `<small>${esc(it.right)}</small>` : ""}`;
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (it.sub) {
          stack.push(list);
          menu.innerHTML = `<div class="spinner"></div>`;
          try { render(await it.sub(), true); } catch (e) { close(); toastError(e); }
          return;
        }
        close();
        try { await it.onClick?.(); } catch (e) { toastError(e); }
      });
      menu.appendChild(b);
    });
    const back = menu.querySelector("[data-back]");
    if (back) back.addEventListener("click", (ev) => { ev.stopPropagation(); render(stack.pop(), stack.length > 0); });
    if (!sheet) place();
    const first = menu.querySelector(".mi:not([disabled])");
    first?.focus({ preventScroll: true });
  };

  const place = () => {
    const r = menu.getBoundingClientRect();
    let left = x, top = y;
    if (anchor) {
      const a = anchor.getBoundingClientRect();
      left = a.right - r.width;
      top = a.bottom + 6;
      if (left < 8) left = a.left;
      if (top + r.height > innerHeight - 8) top = a.top - r.height - 6;
    }
    left = Math.max(8, Math.min(left, innerWidth - r.width - 8));
    top = Math.max(8, Math.min(top, innerHeight - r.height - 8));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
  };

  const onKey = (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); close(); }
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      const btns = [...menu.querySelectorAll(".mi:not([disabled])")];
      const i = btns.indexOf(document.activeElement);
      const n = ev.key === "ArrowDown" ? (i + 1) % btns.length : (i - 1 + btns.length) % btns.length;
      btns[n]?.focus();
    }
  };
  function close() {
    if (closeCurrent !== close) return;
    closeCurrent = null;
    scrim.remove(); menu.remove();
    document.removeEventListener("keydown", onKey, true);
    removeEventListener("resize", close);
    onClose?.();
  }
  scrim.addEventListener("pointerdown", (e) => { e.preventDefault(); close(); });
  scrim.addEventListener("contextmenu", (e) => { e.preventDefault(); close(); });
  document.addEventListener("keydown", onKey, true);
  addEventListener("resize", close);
  closeCurrent = close;
  render(items);
  return close;
}

// ---------------------------------------------------------------- fenêtres
export function openModal({ title, body = "", foot = "", wide = false, onMount = null, onClose = null, locked = false }) {
  closeMenu();
  const scrim = document.createElement("div");
  scrim.className = "scrim dark";
  const modal = document.createElement("div");
  modal.className = "modal" + (wide ? " wide" : "");
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.innerHTML = `
    <div class="modal-head"><h2>${esc(title)}</h2><button class="icon-btn" data-close aria-label="Fermer">${icon("close")}</button></div>
    <div class="modal-body">${body}</div>
    ${foot ? `<div class="modal-foot">${foot}</div>` : ""}`;
  layer().append(scrim, modal);
  const prevFocus = document.activeElement;
  const onKey = (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); if (!locked) close(); } };
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    scrim.remove(); modal.remove();
    document.removeEventListener("keydown", onKey, true);
    onClose?.();
    prevFocus?.focus?.({ preventScroll: true });
  }
  if (!locked) scrim.addEventListener("click", close);
  modal.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
  document.addEventListener("keydown", onKey, true);
  onMount?.(modal, close);
  const auto = modal.querySelector("[autofocus]") || modal.querySelector("input, textarea, select, button:not([data-close])");
  if (auto && !isMobile()) setTimeout(() => auto.focus(), 30);
  return { el: modal, close };
}

export function confirmDialog({ title, text, ok = "Supprimer", danger = true }) {
  return new Promise((resolve) => {
    let answered = false;
    openModal({
      title,
      body: `<p class="muted" style="margin:0 0 8px">${esc(text)}</p>`,
      foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn ${danger ? "danger" : "primary"}" data-ok>${esc(ok)}</button>`,
      onMount: (m, close) => m.querySelector("[data-ok]").addEventListener("click", () => { answered = true; resolve(true); close(); }),
      onClose: () => { if (!answered) resolve(false); },
    });
  });
}

export function promptDialog({ title, label = "", value = "", ok = "Enregistrer", placeholder = "" }) {
  return new Promise((resolve) => {
    let answered = false;
    openModal({
      title,
      body: `<form class="form" data-f><label>${esc(label)}<input class="field" name="v" value="${esc(value)}" placeholder="${esc(placeholder)}" autofocus maxlength="120"></label></form>`,
      foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn primary" data-ok>${esc(ok)}</button>`,
      onMount: (m, close) => {
        const done = (e) => { e?.preventDefault(); answered = true; resolve(m.querySelector("input").value.trim()); close(); };
        m.querySelector("[data-ok]").addEventListener("click", done);
        m.querySelector("[data-f]").addEventListener("submit", done);
      },
      onClose: () => { if (!answered) resolve(null); },
    });
  });
}

// ---------------------------------------------------------------- glisser-déposer (souris + tactile)
export function makeSortable(container, { item = ".row", handle = ".grip", onEnd }) {
  container.addEventListener("pointerdown", (ev) => {
    const h = ev.target.closest(handle);
    if (!h || !container.contains(h) || ev.button > 0) return;
    const el = h.closest(item);
    if (!el) return;
    ev.preventDefault();
    const items = () => [...container.querySelectorAll(item)];
    const from = items().indexOf(el);
    const scroller = container.closest("#main, #queue-panel, .np-side, .modal-body") || document.scrollingElement;
    el.classList.add("dragging");
    h.setPointerCapture(ev.pointerId);
    let lastY = ev.clientY;
    let raf = 0;

    const autoScroll = () => {
      const r = scroller.getBoundingClientRect();
      const edge = 60;
      let dy = 0;
      if (lastY < r.top + edge) dy = -Math.ceil((r.top + edge - lastY) / 6);
      else if (lastY > r.bottom - edge) dy = Math.ceil((lastY - (r.bottom - edge)) / 6);
      if (dy) { scroller.scrollTop += dy; reorder(); }
      raf = requestAnimationFrame(autoScroll);
    };
    const reorder = () => {
      for (const other of items()) {
        if (other === el) continue;
        const r = other.getBoundingClientRect();
        if (lastY > r.top && lastY < r.bottom) {
          const after = lastY > r.top + r.height / 2;
          const mine = el.getBoundingClientRect();
          if (after && mine.top < r.top) other.after(el);
          else if (!after && mine.top > r.top) other.before(el);
          break;
        }
      }
    };
    const move = (e) => { lastY = e.clientY; reorder(); };
    const up = () => {
      cancelAnimationFrame(raf);
      h.removeEventListener("pointermove", move);
      h.removeEventListener("pointerup", up);
      h.removeEventListener("pointercancel", up);
      el.classList.remove("dragging");
      const to = items().indexOf(el);
      if (to !== from) onEnd(from, to, el);
    };
    h.addEventListener("pointermove", move);
    h.addEventListener("pointerup", up);
    h.addEventListener("pointercancel", up);
    raf = requestAnimationFrame(autoScroll);
  });
}
