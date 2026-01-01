import json
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from datetime import datetime, timezone
import os

PORT = 8765
DISH_TARGET = "192.168.100.1:9200"

# ✅ KeepAlive every 2 minutes
AUTO_KEEPALIVE_SEC = 120  # 2 min

# ✅ Reset twice per day (UTC)
RESET_TIMES = ["00:00", "12:00"]  # 2x/day

# ✅ Obstruction fraction “tietokartta” every 5 minutes
OBSTRUCTION_5MIN_SEC = 300
OBSTRUCTION_5MIN_PATH = "obstruction_5min.json"

_stop_bg = threading.Event()

_context = None
_context_lock = threading.Lock()


def _pb_to_dict(obj):
    """protobuf -> dict if possible, else repr() fallback"""
    try:
        from google.protobuf.json_format import MessageToDict  # type: ignore
        if hasattr(obj, "DESCRIPTOR"):
            return MessageToDict(obj, preserving_proto_field_name=True)
    except Exception:
        pass
    if isinstance(obj, dict):
        return obj
    return {"_repr": repr(obj)}


def _get_context(starlink_grpc):
    """Try to create & reuse ChannelContext(target=...)."""
    global _context
    with _context_lock:
        if _context is not None:
            return _context

        ctx_cls = getattr(starlink_grpc, "ChannelContext", None)
        if ctx_cls is None:
            return None

        try:
            _context = ctx_cls(target=DISH_TARGET)
            return _context
        except TypeError:
            return None


def check_alignment(status_raw: dict) -> dict:
    def g(d, *keys):
        cur = d
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        return cur

    reasons = []
    verdict = "UNKNOWN"

    dish_state = (
        status_raw.get("dish_state")
        or status_raw.get("state")
        or g(status_raw, "status", "dish_state")
    )
    dish_state_str = str(dish_state).upper() if dish_state is not None else ""

    ok_states = {"ONLINE", "CONNECTED"}
    bad_states = {
        "STOWED", "STOWING", "UNSTOWING",
        "BOOTING", "STARTING", "CALIBRATING",
        "SEARCHING", "NO_SATS", "IDLE"
    }

    if dish_state_str in ok_states:
        verdict = "ALIGNED"
    elif dish_state_str in bad_states or dish_state_str:
        verdict = "NOT_OK"
        reasons.append(f"state={dish_state_str}")
    else:
        verdict = "UNKNOWN"
        reasons.append("state=unknown")

    boresight = (
        status_raw.get("boresight_error_deg")
        or g(status_raw, "alignment_stats", "boresight_error_deg")
        or g(status_raw, "pointing_stats", "boresight_error_deg")
        or g(status_raw, "pointing", "boresight_error_deg")
    )
    boresight_val = None
    if boresight is not None:
        try:
            boresight_val = float(boresight)
            if boresight_val > 2.5:
                verdict = "NOT_OK"
                reasons.append(f"boresight>2.5deg ({boresight_val:.2f}°)")
        except Exception:
            pass

    currently_obstructed = (
        status_raw.get("currently_obstructed")
        or g(status_raw, "obstruction_stats", "currently_obstructed")
    )
    if currently_obstructed is True:
        verdict = "NOT_OK"
        reasons.append("currently_obstructed=true")

    obstruction_fraction = (
        status_raw.get("obstruction_fraction")
        or g(status_raw, "obstruction_stats", "fraction_obstructed")
        or g(status_raw, "obstruction_stats", "obstruction_fraction")
    )
    obstruction_val = None
    if obstruction_fraction is not None:
        try:
            obstruction_val = float(obstruction_fraction)
            if obstruction_val > 0.10:
                verdict = "NOT_OK"
                reasons.append(f"obstruction=HIGH ({obstruction_val:.3f})")
            elif obstruction_val > 0.02:
                verdict = "NOT_OK"
                reasons.append(f"obstruction=MED ({obstruction_val:.3f})")
        except Exception:
            pass

    aligned = (verdict == "ALIGNED")
    explain = "OK" if aligned else ("; ".join(reasons) if reasons else "Not okay")

    return {
        "aligned": aligned,
        "verdict": "ALIGNED" if aligned else ("NOT_OK" if verdict != "UNKNOWN" else "UNKNOWN"),
        "explain": explain,
        "dish_state": dish_state,
        "boresight_error_deg": boresight_val,
        "obstruction_fraction": obstruction_val,
        "currently_obstructed": currently_obstructed,
        "reasons": reasons,
    }


def do_status() -> dict:
    """Fetch Starlink status and return JSON-friendly response (summary + alignment + raw)."""
    try:
        import starlink_grpc
    except Exception as e:
        return {"ok": False, "error": f"Import starlink_grpc failed: {e}"}

    try:
        ctx = _get_context(starlink_grpc)

        fn = getattr(starlink_grpc, "get_status", None)
        if not callable(fn):
            fn = getattr(starlink_grpc, "dish_get_status", None)
        if not callable(fn):
            return {"ok": False, "error": "No status function found (get_status / dish_get_status)"}

        if ctx is not None:
            try:
                st = fn(ctx)
            except TypeError:
                st = fn()
        else:
            st = fn()

        raw = _pb_to_dict(st)

        def g(d, *keys):
            cur = d
            for k in keys:
                if not isinstance(cur, dict) or k not in cur:
                    return None
                cur = cur[k]
            return cur

        summary = {
            "dish_state": raw.get("dish_state") or raw.get("state") or g(raw, "status", "dish_state"),
            "pop_ping_latency_ms": raw.get("pop_ping_latency_ms") or raw.get("ping_latency_ms"),
            "pop_ping_drop_rate": raw.get("pop_ping_drop_rate"),
            "downlink_throughput_bps": raw.get("downlink_throughput_bps") or raw.get("download_throughput_bps"),
            "uplink_throughput_bps": raw.get("uplink_throughput_bps") or raw.get("upload_throughput_bps"),
            "obstruction_fraction": (
                raw.get("obstruction_fraction")
                or g(raw, "obstruction_stats", "fraction_obstructed")
                or g(raw, "obstruction_stats", "obstruction_fraction")
            ),
            "seconds_since_last_1s_outage": raw.get("seconds_since_last_1s_outage"),
            "seconds_since_last_2s_outage": raw.get("seconds_since_last_2s_outage"),
            "seconds_since_last_5s_outage": raw.get("seconds_since_last_5s_outage"),
        }

        alignment = check_alignment(raw)
        return {"ok": True, "ts": int(time.time()), "summary": summary, "alignment": alignment, "raw": raw}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_keepalive() -> dict:
    """KeepAlive = status poll"""
    r = do_status()
    if r.get("ok"):
        return {"ok": True, "ts": int(time.time())}
    return {"ok": False, "error": r.get("error", "keepalive failed")}


def do_reset_obstruction_map() -> dict:
    """Reset obstruction map (if supported by your starlink_grpc version)."""
    try:
        import starlink_grpc
    except Exception as e:
        return {"ok": False, "error": f"Import starlink_grpc failed: {e}"}

    fn = getattr(starlink_grpc, "reset_obstruction_map", None)
    if not callable(fn):
        return {"ok": False, "error": "reset_obstruction_map not found. Try: pip install --upgrade starlink-grpc-core"}

    try:
        ctx = _get_context(starlink_grpc)
        if ctx is not None:
            try:
                fn(ctx)
            except TypeError:
                fn()
        else:
            fn()
        return {"ok": True, "ts": int(time.time())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def log_obstruction_fraction_5min():
    """Logs obstruction_fraction every 5 minutes into a JSON file (rolling ~7 days)."""
    status = do_status()
    if not status.get("ok"):
        return

    obs = status.get("summary", {}).get("obstruction_fraction")
    if obs is None:
        obs = status.get("alignment", {}).get("obstruction_fraction")
    if obs is None:
        return

    try:
        obs_f = float(obs)
    except Exception:
        return

    now = datetime.now(timezone.utc)
    entry = {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "obstruction_fraction": obs_f,
        "obstruction_percent": round(obs_f * 100, 3),
    }

    data = []
    if os.path.exists(OBSTRUCTION_5MIN_PATH):
        try:
            with open(OBSTRUCTION_5MIN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    data = []
        except Exception:
            data = []

    data.append(entry)

    # keep last ~7 days (7d * 24h * 12 samples/h = 2016)
    MAX_KEEP = 2016
    if len(data) > MAX_KEEP:
        data = data[-MAX_KEEP:]

    with open(OBSTRUCTION_5MIN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _bg_loop():
    """Background scheduler (runs even without UI)."""
    next_ka = time.time() + 2
    next_obs_5min = time.time() + 5
    last_reset_times = set()
    print("[Starlink] 🛰️")
    print(f"  ✅️ {AUTO_KEEPALIVE_SEC} sekunttia")
    print(f"  🕒 kahdesti päivässä.")
    print(f"  🌍 {OBSTRUCTION_5MIN_SEC} sekunttia ")
    while not _stop_bg.is_set():
        now = time.time()
        if now >= next_ka:
            r = do_keepalive()
            print("[Starlink]", "✅️" if r.get("ok") else f"🤔 {r.get('error')}")
            next_ka = now + AUTO_KEEPALIVE_SEC
        now_dt = datetime.now(timezone.utc)
        now_hm = now_dt.strftime("%H:%M")
        if now_hm in RESET_TIMES and now_hm not in last_reset_times:
            r = do_reset_obstruction_map()
            print("[Starlink]", "🕒" if r.get("ok") else f"🤔 {r.get('error')}")
            last_reset_times.add(now_hm)
        if now_hm == "00:01":
            last_reset_times.clear()
        if now >= next_obs_5min:
            log_obstruction_fraction_5min()
            print("[Starlink] 🌍")
            next_obs_5min = now + OBSTRUCTION_5MIN_SEC
        time.sleep(1)

INDEX_HTML = r"""<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Starlink Control</title>
<style>
:root{
  --bg:#000;
  --border:#111;
  --text:#e6e6e6;
  --muted:#777;
  --ok:#00ff88;
  --err:#ff3355;
  --accent:#00b3ff;
  --line1:#00b3ff;
  --line2:#00ff88;
  --line3:#ffcc00;
  --line4:#ff3355;
}
*{box-sizing:border-box}
body{
  margin:0; min-height:100vh;
  background:radial-gradient(circle at top, #020202, #000 70%);
  color:var(--text);
  font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;
  display:flex; align-items:center; justify-content:center;
  padding:18px;
}
.panel{
  width:100%; max-width:1180px;
  background:#000;
  border:1px solid var(--border);
  border-radius:18px;
  padding:22px;
  box-shadow:0 0 40px rgba(0,0,0,.9), inset 0 0 40px rgba(255,255,255,.02);
}
h1{
  margin:0 0 16px;
  font-size:20px; letter-spacing:1px; text-transform:uppercase; color:#fff;
}
.row{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
.grid{ display:grid; grid-template-columns:1fr; gap:14px; }
@media (min-width: 980px){ .grid{ grid-template-columns: 1.05fr 0.95fr; } }
.card{
  background:#000;
  border:1px solid var(--border);
  border-radius:16px;
  padding:14px;
}
.status{
  padding:8px 14px; border-radius:999px;
  font-size:13px; border:1px solid var(--border);
  background:#000;
}
.ok{ color:var(--ok); box-shadow:0 0 12px rgba(0,255,136,.22);}
.err{ color:var(--err); box-shadow:0 0 12px rgba(255,51,85,.22);}
.idle{ color:var(--muted);}
.big{ font-size:14px; letter-spacing:1px; text-transform:uppercase; }
button{
  background:#000; color:var(--text);
  border:1px solid var(--border);
  border-radius:14px; padding:12px 16px;
  cursor:pointer; font-size:14px;
  transition:all .2s ease;
}
button:hover{ border-color:var(--accent); box-shadow:0 0 12px rgba(0,179,255,.22); }
button.danger:hover{ border-color:var(--err); box-shadow:0 0 12px rgba(255,51,85,.22); }
.kv{
  display:grid;
  grid-template-columns: 190px 1fr;
  gap:8px 12px;
  font-size:13px;
}
.k{ color:var(--muted); }
.v{ color:var(--text); word-break:break-word; }

.canvasWrap{
  border:1px solid var(--border);
  border-radius:14px;
  overflow:hidden;
  background:#000;
}
.canvasHead{
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:10px 12px;
  border-bottom:1px solid var(--border);
  color:var(--muted);
  font-size:12px;
}
canvas{
  display:block;
  width:100%;
  height:220px;
  background:#000;
}

pre{
  margin:0;
  padding:12px;
  background:#000;
  border:1px solid var(--border);
  border-radius:14px;
  min-height:200px;
  max-height:340px;
  overflow:auto;
  font-size:12px;
  color:#bfbfbf;
}
small{ color:#555; }
</style>
</head>
<body>
<div class="panel">
  <h1>Starlink Control Panel</h1>

  <div class="row" style="margin-bottom:14px">
    <div id="status" class="status idle">IDLE</div>
    <div id="alignPill" class="status big idle">ALIGN: —</div>
    <div class="status idle">KeepAlive: <b>120s</b></div>
    <div class="status idle">Reset: <b>00:00 / 12:00 UTC</b></div>
    <div class="status idle">Refresh: <b>5s</b></div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <button onclick="keepAliveNow()">KeepAlive now</button>
        <button onclick="resetNow()">Reset obstruction map</button>
        <button class="danger" onclick="stopUI()">Stop UI timers</button>
      </div>

      <div class="kv" style="margin-bottom:14px">
        <div class="k">Dish state</div><div class="v" id="dishState">—</div>
        <div class="k">Boresight error</div><div class="v" id="boresight">—</div>
        <div class="k">Currently obstructed</div><div class="v" id="curObs">—</div>
        <div class="k">Obstruction</div><div class="v" id="obstruct">—</div>
        <div class="k">Align reasons</div><div class="v" id="alignReasons">—</div>
        <div class="k">Updated</div><div class="v" id="updated">—</div>
      </div>

      <div class="canvasWrap" style="margin-bottom:12px">
        <div class="canvasHead"><span>Ping (ms)</span><span id="pingLast">—</span></div>
        <canvas id="chartPing"></canvas>
      </div>

      <div class="canvasWrap" style="margin-bottom:12px">
        <div class="canvasHead"><span>Throughput (Mbps)</span><span id="tpLast">—</span></div>
        <canvas id="chartTP"></canvas>
      </div>

      <div class="canvasWrap" style="margin-bottom:12px">
        <div class="canvasHead"><span>Obstruction (%)</span><span id="obsLast">—</span></div>
        <canvas id="chartObs"></canvas>
      </div>

      <div class="canvasWrap">
        <div class="canvasHead"><span>Boresight error (°)</span><span id="boreLast">—</span></div>
        <canvas id="chartBore"></canvas>
      </div>

      <div style="margin-top:10px">
        <small>Graafit näyttää ~10 min historian (120 pistettä * 5s). Obstruction “tietokartta” tallentuu 5 min välein tiedostoon.</small>
      </div>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between; margin-bottom:10px">
        <div class="status idle">RAW STATUS (JSON)</div>
        <button onclick="toggleRaw()">Toggle</button>
      </div>
      <pre id="rawBox" style="display:none">{}</pre>
      <div style="margin-top:10px">
        <small>RAW on piilossa oletuksena. Graafit käyttää status-summary/arvoja.</small>
      </div>
    </div>
  </div>
</div>

<script>
let resetTimer=null, kaTimer=null, statusTimer=null;
const kaMs=120000, statusMs=5000;

const statusEl=document.getElementById('status');
const alignPill=document.getElementById('alignPill');
const rawBox=document.getElementById('rawBox');
let rawVisible=false;

const elDishState=document.getElementById('dishState');
const elObs=document.getElementById('obstruct');
const elUpd=document.getElementById('updated');
const elBoresight=document.getElementById('boresight');
const elCurObs=document.getElementById('curObs');
const elAlignReasons=document.getElementById('alignReasons');

const pingLast=document.getElementById('pingLast');
const tpLast=document.getElementById('tpLast');
const obsLast=document.getElementById('obsLast');
const boreLast=document.getElementById('boreLast');

const cvsPing=document.getElementById('chartPing');
const cvsTP=document.getElementById('chartTP');
const cvsObs=document.getElementById('chartObs');
const cvsBore=document.getElementById('chartBore');

const CSS = getComputedStyle(document.documentElement);
const C_BORDER = CSS.getPropertyValue('--border').trim();
const C_MUTED = CSS.getPropertyValue('--muted').trim();
const C_L1 = CSS.getPropertyValue('--line1').trim();
const C_L2 = CSS.getPropertyValue('--line2').trim();
const C_L3 = CSS.getPropertyValue('--line3').trim();
const C_L4 = CSS.getPropertyValue('--line4').trim();

function setStatus(txt,cls){
  statusEl.className='status '+cls;
  statusEl.textContent=txt;
}
function setAlign(aligned, explain){
  if(aligned === true){
    alignPill.className='status big ok';
    alignPill.textContent='ALIGN: OK';
  }else if(aligned === false){
    alignPill.className='status big err';
    alignPill.textContent='ALIGN: BAD';
  }else{
    alignPill.className='status big idle';
    alignPill.textContent='ALIGN: —';
  }
  alignPill.title = explain || '';
}

function fmtPct(x){
  if(x==null) return "—";
  const v=Number(x);
  if(Number.isNaN(v)) return String(x);
  return (v*100).toFixed(2)+" %";
}
function fmtDeg(x){
  if(x==null) return "—";
  const v=Number(x);
  if(Number.isNaN(v)) return String(x);
  return v.toFixed(2)+" °";
}
function fmtMbpsFromBps(bps){
  if(bps==null) return null;
  const v=Number(bps);
  if(Number.isNaN(v)) return null;
  return v/1_000_000;
}
function fmtMs(x){
  if(x==null) return "—";
  const v=Number(x);
  if(Number.isNaN(v)) return String(x);
  return v.toFixed(0)+" ms";
}
function fmtMbps(x){
  if(x==null) return "—";
  const v=Number(x);
  if(Number.isNaN(v)) return String(x);
  return v.toFixed(1)+" Mbps";
}

async function post(p){
  const r=await fetch(p,{method:'POST'});
  return r.json();
}
async function getStatus(){
  const r=await fetch('/api/status');
  return r.json();
}

async function keepAliveNow(){
  try{
    const j=await post('/api/keepalive');
    setStatus(j.ok ? 'KEEPALIVE OK' : 'KEEPALIVE ERR', j.ok ? 'ok' : 'err');
  }catch(e){
    setStatus('KEEPALIVE ERR','err');
  }
}
async function resetNow(){
  try{
    const j=await post('/api/reset');
    setStatus(j.ok ? 'RESET OK' : 'RESET ERR', j.ok ? 'ok' : 'err');
  }catch(e){
    setStatus('RESET ERR','err');
  }
}

function toggleRaw(){
  rawVisible=!rawVisible;
  rawBox.style.display = rawVisible ? 'block' : 'none';
}

/*** ------- CHARTS (canvas) ------- ***/
const MAX_POINTS = 120; // ~10 minutes at 5s refresh
const series = {
  ping: [],
  down: [],
  up: [],
  obs: [],
  bore: [],
};

function pushBounded(arr, v){
  arr.push(v);
  if(arr.length > MAX_POINTS) arr.shift();
}

function resizeCanvas(c){
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  const w = Math.max(300, Math.floor(rect.width));
  const h = Math.max(180, Math.floor(rect.height));
  if(c.width !== w*dpr || c.height !== h*dpr){
    c.width = w*dpr; c.height = h*dpr;
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr,0,0,dpr,0,0);
  }
}

function drawChart(c, data, opts){
  resizeCanvas(c);
  const ctx = c.getContext('2d');
  const rect = c.getBoundingClientRect();
  const W = Math.floor(rect.width);
  const H = Math.floor(rect.height);

  ctx.clearRect(0,0,W,H);

  const padL = 42, padR = 10, padT = 10, padB = 22;
  const x0 = padL, y0 = padT, x1 = W - padR, y1 = H - padB;

  ctx.strokeStyle = C_BORDER;
  ctx.lineWidth = 1;
  ctx.strokeRect(x0, y0, x1-x0, y1-y0);

  const clean = data.filter(v => typeof v === 'number' && isFinite(v));
  if(clean.length < 2){
    ctx.fillStyle = C_MUTED;
    ctx.font = '12px system-ui, Segoe UI, Roboto, Arial';
    ctx.fillText('no data', x0+10, y0+18);
    return;
  }

  let min = (opts.min != null) ? opts.min : Math.min(...clean);
  let max = (opts.max != null) ? opts.max : Math.max(...clean);
  if(min === max){ max = min + 1; }

  ctx.strokeStyle = C_BORDER;
  ctx.setLineDash([3,4]);
  for(let i=1;i<=3;i++){
    const y = y0 + (y1-y0)*i/4;
    ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  ctx.fillStyle = C_MUTED;
  ctx.font = '11px system-ui, Segoe UI, Roboto, Arial';
  ctx.fillText(String(max.toFixed(opts.dp ?? 1)), 6, y0+10);
  ctx.fillText(String(min.toFixed(opts.dp ?? 1)), 6, y1);

  const n = data.length;
  const sx = (x1-x0) / (n-1);

  function yFor(v){
    const t = (v - min) / (max - min);
    return y1 - t*(y1-y0);
  }

  ctx.strokeStyle = opts.color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  for(let i=0;i<n;i++){
    const v = data[i];
    const x = x0 + i*sx;
    const y = (typeof v === 'number' && isFinite(v)) ? yFor(v) : null;
    if(y == null){
      ctx.stroke();
      ctx.beginPath();
      continue;
    }
    if(i===0) ctx.moveTo(x,y);
    else ctx.lineTo(x,y);
  }
  ctx.stroke();

  for(let i=n-1;i>=0;i--){
    const v = data[i];
    if(typeof v === 'number' && isFinite(v)){
      const x = x0 + i*sx;
      const y = yFor(v);
      ctx.fillStyle = opts.color;
      ctx.beginPath(); ctx.arc(x,y,3,0,Math.PI*2); ctx.fill();
      break;
    }
  }
}

function overlayLine(c, data, color){
  resizeCanvas(c);
  const ctx = c.getContext('2d');
  const rect = c.getBoundingClientRect();
  const W = Math.floor(rect.width);
  const H = Math.floor(rect.height);

  const padL = 42, padR = 10, padT = 10, padB = 22;
  const x0 = padL, y0 = padT, x1 = W - padR, y1 = H - padB;

  const clean = data.filter(v => typeof v === 'number' && isFinite(v));
  if(clean.length < 2) return;

  let min = 0;
  let max = Math.max(...clean);
  if(max <= 1) max = 1;

  const n = data.length;
  const sx = (x1-x0) / (n-1);
  function yFor(v){
    const t = (v - min) / (max - min);
    return y1 - t*(y1-y0);
  }

  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  for(let i=0;i<n;i++){
    const v = data[i];
    const x = x0 + i*sx;
    const y = (typeof v === 'number' && isFinite(v)) ? yFor(v) : null;
    if(y == null){ ctx.stroke(); ctx.beginPath(); continue; }
    if(i===0) ctx.moveTo(x,y);
    else ctx.lineTo(x,y);
  }
  ctx.stroke();
}

function renderCharts(){
  drawChart(cvsPing, series.ping, {color: C_L1, dp: 0, min: 0});
  drawChart(cvsTP, series.down, {color: C_L2, dp: 1, min: 0});
  overlayLine(cvsTP, series.up, C_L3);
  drawChart(cvsObs, series.obs, {color: C_L4, dp: 2, min: 0, max: 100});
  drawChart(cvsBore, series.bore, {color: C_L1, dp: 2, min: 0});
}

/*** ------- STATUS refresh ------- ***/
async function refreshStatus(){
  try{
    const j = await getStatus();
    if(!j.ok){
      setStatus('STATUS ERR','err');
      setAlign(null);
      return;
    }

    const s = j.summary || {};
    const a = j.alignment || {};

    elDishState.textContent = s.dish_state ?? "—";
    elObs.textContent = fmtPct(s.obstruction_fraction);
    elUpd.textContent = new Date((j.ts||Date.now())*1000).toLocaleString('fi-FI');

    elBoresight.textContent = fmtDeg(a.boresight_error_deg);
    elCurObs.textContent = (a.currently_obstructed === true) ? "true" : (a.currently_obstructed === false ? "false" : "—");
    elAlignReasons.textContent = a.explain ?? ((a.reasons && a.reasons.length) ? a.reasons.join(", ") : "—");
    setAlign(a.aligned, elAlignReasons.textContent);

    const ping = (s.pop_ping_latency_ms != null) ? Number(s.pop_ping_latency_ms) : null;
    const downMbps = (s.downlink_throughput_bps != null) ? fmtMbpsFromBps(s.downlink_throughput_bps) : null;
    const upMbps = (s.uplink_throughput_bps != null) ? fmtMbpsFromBps(s.uplink_throughput_bps) : null;
    const obsPct = (s.obstruction_fraction != null) ? (Number(s.obstruction_fraction)*100) : null;
    const bore = (a.boresight_error_deg != null) ? Number(a.boresight_error_deg) : null;

    pushBounded(series.ping, (isFinite(ping) ? ping : null));
    pushBounded(series.down, (isFinite(downMbps) ? downMbps : null));
    pushBounded(series.up, (isFinite(upMbps) ? upMbps : null));
    pushBounded(series.obs, (isFinite(obsPct) ? obsPct : null));
    pushBounded(series.bore, (isFinite(bore) ? bore : null));

    pingLast.textContent = fmtMs(ping);
    tpLast.textContent = `D ${fmtMbps(downMbps)} / U ${fmtMbps(upMbps)}`;
    obsLast.textContent = (obsPct==null) ? "—" : obsPct.toFixed(2)+" %";
    boreLast.textContent = fmtDeg(bore);

    renderCharts();

    if(rawVisible){
      rawBox.textContent = JSON.stringify(j.raw || {}, null, 2);
    }

    setStatus('RUNNING','ok');
  }catch(e){
    setStatus('STATUS ERR','err');
    setAlign(null);
  }
}

function stopUI(){
  clearInterval(kaTimer); clearInterval(statusTimer);
  setStatus('STOPPED','idle');
  setAlign(null);
}

function startUI(){
  keepAliveNow();
  refreshStatus();
  kaTimer=setInterval(keepAliveNow,kaMs);
  statusTimer=setInterval(refreshStatus,statusMs);
  setStatus('RUNNING','ok');
}

window.addEventListener('resize', () => renderCharts());
setTimeout(startUI,300);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path

        if p in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if p == "/api/health":
            body = json.dumps({"ok": True, "ts": int(time.time())}).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        if p == "/api/status":
            result = do_status()
            body = json.dumps(result).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        p = urlparse(self.path).path

        if p == "/api/reset":
            result = do_reset_obstruction_map()
            body = json.dumps(result).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        if p == "/api/keepalive":
            result = do_keepalive()
            body = json.dumps(result).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        self._send(404, b"Not found", "text/plain; charset=utf-8")


def main():
    print("Starlink Online Helper running.")
    print(f"UI: http://127.0.0.1:{PORT}/")
    print(f"Dish target assumed: {DISH_TARGET}")

    t = threading.Thread(target=_bg_loop, daemon=True)
    t.start()

    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
