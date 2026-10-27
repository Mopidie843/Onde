"""Pages locales pour le streamer (jamais vues par les viewers) :
- /control : télécommande à gros boutons (gestes, expressions, effets, pause…) pour un 2e écran, une tablette,
  un modo sur le même réseau ou un Stream Deck (action « Site web » avec « Accès en arrière-plan ») ;
- /sante : santé du live (suivi, webcam, micro, chat Twitch, OBS, sources avatar, pause automatique).
Les actions passent par /api/action?a=<action>&k=<clé> ; la clé protège des sites web malveillants."""
import json
import secrets
import socket

from .hotkeys import HOTKEY_ACTIONS
from .settings import CONFIG_DIR

KEY_FILE = CONFIG_DIR / "control_key.txt"
GROUPS = [("manual:", "Expressions"), ("gesture:", "Gestes"), ("effect:", "Effets"), ("camera:", "Caméra"),
          ("brb:", "Live"), ("mic:", "Live"), ("tracking:", "Live")]
ICONS = {"manual:happy": "😄", "manual:angry": "😠", "manual:sad": "😢", "manual:surprised": "😮",
         "manual:relaxed": "😌", "manual:none": "😐", "gesture:wave": "👋", "gesture:clap": "👏", "gesture:nod": "🙂",
         "gesture:shake": "🙅", "gesture:dance": "💃", "gesture:bow": "🙇", "gesture:think": "🤔",
         "effect:hearts": "💖", "effect:confetti": "🎉", "effect:stars": "⭐", "effect:sakura": "🌸",
         "camera:close": "🔍", "camera:bust": "🧍", "camera:full": "🕺", "brb:toggle": "☕", "mic:toggle": "🎙️",
         "tracking:toggle": "🎯"}


def control_key() -> str:
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if len(key) >= 8:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(9)
    try:
        KEY_FILE.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


def local_ip() -> str:
    """Adresse du PC sur le réseau local (pour un modo ou une tablette)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.255.255", 1))      # aucun paquet envoyé : sert juste à choisir la bonne carte réseau
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def actions() -> list[dict]:
    out = []
    for aid, label in HOTKEY_ACTIONS:
        group = next((g for p, g in GROUPS if aid.startswith(p)), "Autres")
        short = label.split(":", 1)[-1].replace("(on/off)", "").strip().capitalize()
        out.append({"id": aid, "label": label, "short": short, "group": group, "icon": ICONS.get(aid, "•")})
    return out


_STYLE = """
:root{--bg:#0f1020;--card:#1b1d36;--line:#2c2f55;--text:#f1efff;--muted:#a9a6c9;--accent:#8f7bff;
--ok:#3ccf91;--warn:#f5b94a;--bad:#ff6b7d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 'Segoe UI',system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 18px;background:#14152b;border-bottom:1px solid var(--line);
position:sticky;top:0;z-index:2;flex-wrap:wrap}
header h1{font-size:18px;margin:0;margin-right:auto}header a{color:var(--accent);text-decoration:none}
main{padding:16px 18px 40px;max-width:1100px;margin:0 auto}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:22px 0 10px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:99px;background:var(--card);
border:1px solid var(--line);font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.ok .dot{background:var(--ok)}.warn .dot{background:var(--warn)}.bad .dot{background:var(--bad)}
@media(max-width:600px){main{padding:12px}}
"""

CONTROL_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nova — Télécommande</title><style>%STYLE%
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
button.act{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:14px;padding:16px 8px;
font:600 14px inherit;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:6px;min-height:96px;
transition:transform .08s,background .2s}button.act:active{transform:scale(.96)}
button.act .i{font-size:30px;line-height:1}button.act.done{background:#23624a}button.act.fail{background:#6b2533}
details{margin-top:28px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px}
summary{cursor:pointer;font-weight:600}code{display:block;background:#0b0c18;padding:6px 8px;border-radius:6px;margin:4px 0;
word-break:break-all;font-size:12px;color:#d9d4ff}.lab{color:var(--muted);font-size:13px;margin-top:8px}
</style></head><body><header><h1>🎮 Nova — Télécommande</h1><span id="st" class="pill"><span class="dot"></span>…</span>
<a href="/sante" id="sante">Santé du live →</a></header><main id="m"></main><script>
const KEY=%KEY%, ACTIONS=%ACTIONS%, BASE=location.origin;
const m=document.getElementById('m'); let group='';let grid;
for(const a of ACTIONS){ if(a.group!==group){group=a.group;const h=document.createElement('h2');h.textContent=group;m.append(h);
 grid=document.createElement('div');grid.className='grid';m.append(grid);}
 const b=document.createElement('button');b.className='act';b.title=a.label;
 b.innerHTML='<span class="i"></span><span class="t"></span>';b.querySelector('.i').textContent=a.icon;
 b.querySelector('.t').textContent=a.short;b.onclick=()=>run(a.id,b);grid.append(b);}
async function run(id,b){try{const r=await fetch(BASE+'/api/action?a='+encodeURIComponent(id)+'&k='+KEY);
 const ok=(await r.json()).ok;flash(b,ok?'done':'fail');}catch(e){flash(b,'fail');}}
function flash(b,c){b.classList.add(c);setTimeout(()=>b.classList.remove(c),450);}
const d=document.createElement('details');d.innerHTML='<summary>Stream Deck : adresses à copier</summary>'+
 '<p class="lab">Action « Site web » (Website), cochez « Accès en arrière-plan » (GET request in background), puis collez :</p>';
for(const a of ACTIONS){const p=document.createElement('div');p.className='lab';p.textContent=a.icon+' '+a.label;
 const c=document.createElement('code');c.textContent=BASE+'/api/action?a='+a.id+'&k='+KEY;d.append(p,c);}
m.append(d);
async function tick(){const st=document.getElementById('st');try{const h=await (await fetch(BASE+'/api/health?k='+KEY)).json();
 const t=h.tracking||{};const ok=t.state==='running';st.className='pill '+(ok?(t.face?'ok':'warn'):'bad');
 st.lastChild.textContent=ok?(t.face?' Suivi actif':' Visage perdu'):' Suivi arrêté';}
 catch(e){st.className='pill bad';st.lastChild.textContent=' Nova injoignable';}}
if(location.hostname!=='127.0.0.1'&&location.hostname!=='localhost')document.getElementById('sante').remove();
tick();setInterval(tick,2000);
</script></body></html>"""

HEALTH_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nova — Santé du live</title><style>%STYLE%
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;border-left:5px solid var(--muted)}
.card.ok{border-left-color:var(--ok)}.card.warn{border-left-color:var(--warn)}.card.bad{border-left-color:var(--bad)}
.card h3{margin:0 0 6px;font-size:15px;display:flex;justify-content:space-between;gap:8px}
.big{font-size:26px;font-weight:700}.msg{color:var(--muted);font-size:13px;margin-top:4px;word-break:break-word}
.bar{height:8px;background:#0b0c18;border-radius:6px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;
background:var(--ok);width:0;transition:width .2s}
code{background:#0b0c18;padding:3px 6px;border-radius:6px;font-size:12px;color:#d9d4ff;word-break:break-all}
</style></head><body><header><h1>🩺 Nova — Santé du live</h1><span class="pill" id="upd"><span class="dot"></span>…</span>
<a href="/control">Télécommande →</a></header><main><div class="cards" id="c"></div>
<h2>Télécommande ailleurs</h2><div class="msg" id="lan"></div></main><script>
const KEY=%KEY%;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function card(title,cls,big,msg,extra){return `<div class="card ${cls}"><h3><span>${title}</span></h3>`+
 (big!==''?`<div class="big">${big}</div>`:'')+`<div class="msg">${msg}</div>${extra||''}</div>`;}
function stale(p,s){return p&&p.age!==undefined&&p.age>s;}
async function tick(){let h;const u=document.getElementById('upd');
 try{h=await (await fetch('/api/health?k='+KEY)).json();u.className='pill ok';u.lastChild.textContent=' À jour';}
 catch(e){u.className='pill bad';u.lastChild.textContent=' Nova ne répond pas';return;}
 const out=[];const t=h.tracking||{};
 if(t.state==='running'&&!stale(t,4)){const fps=t.fps||0;const cls=!t.face?'warn':fps<15?'warn':'ok';
  out.push(card('🎯 Suivi du visage',cls,Math.round(fps)+' i/s',(t.face?'Visage détecté':'Visage perdu')+
   ` · traitement ${Math.round(t.latency||0)} ms`+(t.light?` · ${esc(t.light)}`:'')));}
 else out.push(card('🎯 Suivi du visage',t.state==='error'?'bad':'warn','',esc(t.message||'Suivi arrêté')));
 out.push(card('📷 Webcam',t.camera?(t.state==='error'?'bad':'ok'):(t.state==='error'?'bad':''),'',
  esc(t.camera?('Caméra '+t.camera):(t.message||'Pas encore ouverte'))));
 const m=h.micro||{};const lvl=Math.round((m.level||0)*100);
 if(m.active&&!stale(m,3))out.push(card('🎙️ Micro',lvl>2?'ok':'warn','',lvl>2?'Son reçu':'Silence (micro coupé ou trop bas ?)',
  `<div class="bar"><i style="width:${lvl}%"></i></div>`));
 else out.push(card('🎙️ Micro',m.error?'bad':'',' ',esc(m.error||'Lip sync au micro désactivé')));
 const tw=h.twitch||{};const twc={connected:'ok',connecting:'warn',error:'bad'}[tw.state]||'';
 out.push(card('💬 Chat Twitch',twc,'',esc(tw.message||'Non connecté')));
 const o=h.obs||{};const oc={ok:'ok',absent:'warn',off:'warn',error:'bad'}[o.state]||'';
 out.push(card('🎬 OBS',oc,o.state==='ok'?esc(o.scene||''):'',esc(o.message||'…')));
 const ov=h.overlay||{};out.push(card('🧍 Avatar dans OBS',ov.obs>0?'ok':'warn',ov.obs??0,
  ov.obs>0?'source(s) navigateur affichent l\\'avatar':'Aucune source OBS n\\'affiche l\\'avatar'));
 const pf=h.perf||{};out.push(card('🚀 Anti-bridage',pf.state==='off'?'warn':pf.state?'ok':'',
  pf.game?esc(pf.game):'',esc(pf.message||'…')+(pf.state&&pf.state!=='off'?` · priorité ${pf.priority==='high'?'haute':'au-dessus de la normale'}`+
  (pf.gpu?` · GPU ${pf.gpu==='high'?'haute':pf.gpu==='above_normal'?'élevée':'normale'}`:''):'')));
 const a=h.autopause||{};out.push(card('☕ Pause automatique',a.state==='paused'?'warn':a.state==='on'?'ok':'','',
  esc(a.state==='off'?'Désactivée':(a.message||'Active'))+(a.away>3&&a.state!=='paused'?` · absent depuis ${Math.round(a.away)} s`:'')));
 document.getElementById('c').innerHTML=out.join('');
 const c=h.control||{};document.getElementById('lan').innerHTML=c.lan?
  `Sur une tablette, un téléphone ou le PC d'un modo <b>du même réseau</b> : <code>${esc(c.lan)}</code>`:
  'Réseau local désactivé : la télécommande ne marche que sur ce PC. Pour un modo ou une tablette, mettez '+
  '<code>"lan_control": true</code> dans la partie <code>"stream"</code> de votre profil, puis redémarrez Nova.';}
tick();setInterval(tick,1000);
</script></body></html>"""


def control_page() -> bytes:
    return (CONTROL_HTML.replace("%STYLE%", _STYLE).replace("%KEY%", json.dumps(control_key()))
            .replace("%ACTIONS%", json.dumps(actions(), ensure_ascii=False))).encode("utf-8")


def health_page() -> bytes:
    return HEALTH_HTML.replace("%STYLE%", _STYLE).replace("%KEY%", json.dumps(control_key())).encode("utf-8")
