// Réglages du son de CET appareil : fondu enchaîné et égaliseur (gardés sur l'appareil).
import { $, $$, esc } from "./util.js";
import { toast } from "./ui.js";
import { EQ_BANDS, EQ_PRESETS } from "./player.js";

const isIOS = () => /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const hz = (f) => (f >= 1000 ? `${f / 1000} kHz` : `${f} Hz`);

export function soundSection(player) {
  const eq = player.eq;
  return `<h2 class="set-title">Son sur cet appareil</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>Fondu enchaîné</h3><p>La fin d'un titre se mélange au début du suivant, sans blanc.${isIOS() ? " <span class='dim'>(Sur iPhone, ça peut ne pas marcher écran verrouillé.)</span>" : ""}</p></div>
      <select class="select" id="xf-sel">${[0, 3, 5, 8, 12].map((s) => `<option value="${s}"${s === player.crossfade ? " selected" : ""}>${s ? `${s} secondes` : "Désactivé"}</option>`).join("")}</select>
    </div>
    <div class="settings-card" style="flex-wrap:wrap">
      <div class="sc-text"><h3>Égaliseur</h3><p>Basses, voix, aigus… pour tes écouteurs ou ton enceinte.${isIOS() ? " <span class='dim'>Sur iPhone, l'égaliseur peut couper le son écran verrouillé : laisse-le désactivé si c'est le cas.</span>" : ""}</p></div>
      <label class="switch"><input type="checkbox" id="eq-on"${eq.on ? " checked" : ""}><span class="sw-track"><span class="sw-thumb"></span></span></label>
      <div class="eq-box" id="eq-box"${eq.on ? "" : " hidden"}>
        <select class="select" id="eq-preset">${Object.entries(EQ_PRESETS).map(([k, p]) => `<option value="${k}"${k === eq.preset ? " selected" : ""}>${esc(p.label)}</option>`).join("")}<option value="custom"${eq.preset === "custom" ? " selected" : ""}>Personnalisé</option></select>
        <div class="eq-bands">${EQ_BANDS.map((f, i) => `<label class="eq-band"><output>${eq.g[i] > 0 ? "+" : ""}${eq.g[i]} dB</output>
          <input type="range" min="-12" max="12" step="1" value="${eq.g[i]}" data-band="${i}" aria-label="${hz(f)}"><span>${hz(f)}</span></label>`).join("")}</div>
      </div>
    </div>`;
}

export function bindSound(el, player) {
  $("#xf-sel", el)?.addEventListener("change", (e) => {
    player.setCrossfade(+e.target.value);
    toast(+e.target.value ? `Fondu enchaîné : ${e.target.value} s` : "Fondu enchaîné désactivé");
  });
  const box = $("#eq-box", el);
  const sliders = () => $$("[data-band]", el);
  const show = (g) => sliders().forEach((s, i) => { s.value = g[i]; s.previousElementSibling.textContent = `${g[i] > 0 ? "+" : ""}${g[i]} dB`; });
  $("#eq-on", el)?.addEventListener("change", (e) => {
    player.setEq({ ...player.eq, on: e.target.checked });
    if (box) box.hidden = !e.target.checked;
  });
  $("#eq-preset", el)?.addEventListener("change", (e) => {
    const p = EQ_PRESETS[e.target.value];
    if (!p) return;
    player.setEq({ on: true, preset: e.target.value, g: p.g.slice() });
    show(p.g);
  });
  el.addEventListener("input", (e) => {
    const s = e.target.closest("[data-band]");
    if (!s) return;
    const g = sliders().map((x) => +x.value);
    s.previousElementSibling.textContent = `${+s.value > 0 ? "+" : ""}${s.value} dB`;
    const preset = $("#eq-preset", el);
    if (preset) preset.value = "custom";
    player.setEq({ on: true, preset: "custom", g });
  });
}
