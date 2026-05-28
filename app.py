import os
import json
import time
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, redirect, request, session, render_template_string
from urllib.parse import urlencode

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "whoop-dashboard-secret-change-me")

CLIENT_ID     = os.environ.get("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET", "")
REDIRECT_URI  = os.environ.get("REDIRECT_URI", "http://localhost:5000/callback")
SCOPE         = "read:recovery read:sleep read:workout read:profile read:body_measurement"

AUTH_URL      = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL     = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE      = "https://api.prod.whoop.com/developer/v1"

# In-memory token + data store (persisted to disk)
TOKEN_FILE = "/data/token.json"
DATA_FILE  = "/data/whoop_data.json"


def save_token(token_data):
    os.makedirs("/data", exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f)


def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return None


def save_data(data):
    os.makedirs("/data", exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return None


def refresh_access_token(token_data):
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": token_data["refresh_token"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if resp.status_code == 200:
        new_token = resp.json()
        new_token["expires_at"] = time.time() + new_token.get("expires_in", 3600)
        save_token(new_token)
        return new_token
    return None


def get_valid_token():
    token = load_token()
    if not token:
        return None
    if time.time() >= token.get("expires_at", 0) - 60:
        token = refresh_access_token(token)
    return token


def api_get(path, token):
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    resp = requests.get(f"{API_BASE}{path}", headers=headers)
    if resp.status_code == 200:
        return resp.json()
    return None


def fetch_whoop_data():
    token = get_valid_token()
    if not token:
        return None

    # Fetch latest cycle (recovery + strain)
    cycles = api_get("/cycle?limit=7", token)
    # Fetch latest sleep
    sleeps = api_get("/activity/sleep?limit=1", token)
    # Fetch recent workouts
    workouts = api_get("/activity/workout?limit=5", token)
    # Fetch profile
    profile = api_get("/user/profile/basic", token)

    if not cycles or not cycles.get("records"):
        return None

    latest = cycles["records"][0]
    score = latest.get("score") or {}
    recovery = round(score.get("recovery_score", 0))
    hrv = round(score.get("hrv_rmssd_milli", 0))
    rhr = round(score.get("resting_heart_rate", 0))
    strain = round(score.get("strain", 0), 1) if score.get("strain") else 0
    spo2 = round(score.get("spo2_percentage", 97), 1)
    skin_temp = round(score.get("skin_temp_fahrenheit", 0), 1)

    # 7-day trends
    hrv_trend = []
    rec_trend = []
    day_labels = []
    for c in reversed(cycles["records"][:7]):
        s = c.get("score") or {}
        hrv_trend.append(round(s.get("hrv_rmssd_milli", 0)))
        rec_trend.append(round(s.get("recovery_score", 0)))
        ts = c.get("start")
        if ts:
            day_labels.append(datetime.fromisoformat(ts[:10]).strftime("%a"))
        else:
            day_labels.append("")

    # Sleep
    sleep_h = sleep_mm = sleep_perf = rem_h = rem_m = 0
    deep_h = deep_m = light_h = light_m = latency = inbed_h = inbed_m = resp_rate = 0
    if sleeps and sleeps.get("records"):
        sl = sleeps["records"][0]
        ss = sl.get("score") or {}
        stages = ss.get("stage_summary") or {}
        total_m = round((sl.get("end", 0) and sl.get("start", 0) and
                   (datetime.fromisoformat(sl["end"][:19]) -
                    datetime.fromisoformat(sl["start"][:19])).seconds / 60) or
                   ss.get("total_in_bed_time_milli", 0) / 60000)
        asleep_m = round(ss.get("total_sleep_time_milli", 0) / 60000)
        rem_total = round(stages.get("total_rem_sleep_time_milli", 0) / 60000)
        deep_total = round(stages.get("total_slow_wave_sleep_time_milli", 0) / 60000)
        light_total = round(stages.get("total_light_sleep_time_milli", 0) / 60000)
        lat = round(stages.get("sleep_latency_milli", 0) / 60000)
        inbed = round(ss.get("total_in_bed_time_milli", 0) / 60000)
        resp = round(ss.get("respiratory_rate", 0), 1)
        perf = round(ss.get("sleep_performance_percentage", 0))

        sleep_h, sleep_mm = divmod(asleep_m, 60)
        rem_h, rem_m = divmod(rem_total, 60)
        deep_h, deep_m = divmod(deep_total, 60)
        light_h, light_m = divmod(light_total, 60)
        inbed_h, inbed_m = divmod(inbed, 60)
        latency = lat
        resp_rate = resp
        sleep_perf = perf

    # Workouts
    workout_list = []
    if workouts and workouts.get("records"):
        for w in workouts["records"][:5]:
            ws = w.get("score") or {}
            workout_list.append({
                "name": w.get("sport_name", "Workout"),
                "duration": round((datetime.fromisoformat(w["end"][:19]) -
                                   datetime.fromisoformat(w["start"][:19])).seconds / 60)
                            if w.get("start") and w.get("end") else 0,
                "strain": round(ws.get("strain", 0), 1),
                "calories": round(ws.get("kilojoule", 0) * 0.239),
                "max_hr": round(ws.get("max_heart_rate", 0)),
                "avg_hr": round(ws.get("average_heart_rate", 0)),
            })

    if recovery >= 67:
        rec_color, rec_label = "#00c47e", "Green — Good"
    elif recovery >= 34:
        rec_color, rec_label = "#f59e42", "Yellow — Moderate"
    else:
        rec_color, rec_label = "#e24b4a", "Red — Rest"

    strain_color = "#e24b4a" if strain >= 18 else "#f59e42" if strain >= 14 else "#4ea6ff"

    username = ""
    if profile:
        username = profile.get("first_name", "")

    data = {
        "recovery": recovery, "rec_color": rec_color, "rec_label": rec_label,
        "hrv": hrv, "rhr": rhr, "strain": strain, "strain_color": strain_color,
        "spo2": spo2, "skin_temp": skin_temp,
        "sleep_h": int(sleep_h), "sleep_mm": int(sleep_mm), "sleep_perf": sleep_perf,
        "sleep_bar_pct": min(100, sleep_perf),
        "rem_h": int(rem_h), "rem_m": int(rem_m),
        "deep_h": int(deep_h), "deep_m": int(deep_m),
        "light_h": int(light_h), "light_m": int(light_m),
        "inbed_h": int(inbed_h), "inbed_m": int(inbed_m),
        "latency": int(latency), "resp_rate": resp_rate,
        "hrv_trend": hrv_trend, "rec_trend": rec_trend, "day_labels": day_labels,
        "hrv_avg": round(sum(hrv_trend) / len(hrv_trend)) if hrv_trend else hrv,
        "workouts": workout_list,
        "username": username,
        "last_updated": datetime.now().strftime("%b %d, %Y at %I:%M %p"),
    }
    save_data(data)
    return data


# Background refresh every hour
def background_refresh():
    while True:
        time.sleep(3600)
        try:
            print("⏰ Auto-refreshing WHOOP data...")
            fetch_whoop_data()
            print("✅ Data refreshed")
        except Exception as e:
            print(f"❌ Refresh error: {e}")


threading.Thread(target=background_refresh, daemon=True).start()


@app.route("/")
def index():
    token = get_valid_token()
    if not token:
        return redirect("/login")
    data = load_data()
    if not data:
        data = fetch_whoop_data()
    if not data:
        return "<h2>Loading your WHOOP data... <a href='/refresh'>Refresh</a></h2>"
    return render_template_string(DASHBOARD_HTML, **data)


@app.route("/login")
def login():
    import secrets
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    return redirect(f"{AUTH_URL}?{urlencode(params)}")


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Error: no code returned", 400
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if resp.status_code != 200:
        return f"Token error: {resp.text}", 400
    token = resp.json()
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    save_token(token)
    fetch_whoop_data()
    return redirect("/")


@app.route("/refresh")
def refresh():
    fetch_whoop_data()
    return redirect("/")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>WHOOP Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&display=swap"/>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0e0f11;--surface:#17191d;--card:#1e2026;--border:rgba(255,255,255,.08);
  --text:#f0f0f0;--muted:#888;--hint:#555;
  --green:#00c47e;--blue:#4ea6ff;--purple:#a78bfa;--amber:#f59e42;--red:#e24b4a;
  --radius:12px;--radius-sm:8px;
}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:2rem 1rem}
.page{max-width:900px;margin:0 auto}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:2rem;flex-wrap:wrap;gap:10px}
.logo-row{display:flex;align-items:center;gap:10px}
.logo{width:32px;height:32px;background:var(--green);border-radius:8px;display:flex;align-items:center;justify-content:center}
.logo i{color:#000;font-size:16px}
h1{font-size:20px;font-weight:600}
.sub{font-size:12px;color:var(--muted);margin-top:2px}
.header-right{display:flex;align-items:center;gap:8px}
.badge{font-size:11px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:4px 10px;color:var(--muted)}
.refresh-btn{font-size:11px;background:transparent;border:1px solid var(--border);border-radius:6px;padding:4px 10px;color:var(--muted);cursor:pointer;text-decoration:none;display:flex;align-items:center;gap:4px}
.refresh-btn:hover{background:var(--surface);color:var(--text)}
.section-label{font-size:10px;font-weight:600;letter-spacing:.08em;color:var(--hint);text-transform:uppercase;margin-bottom:10px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.5rem}
@media(max-width:600px){.grid4{grid-template-columns:repeat(2,1fr)}}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:1.5rem}
@media(max-width:600px){.grid3{grid-template-columns:repeat(2,1fr)}}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.5rem}
@media(max-width:600px){.grid2{grid-template-columns:1fr}}
.metric{background:var(--surface);border-radius:var(--radius-sm);padding:14px;border:1px solid var(--border)}
.metric .lbl{font-size:11px;color:var(--muted);margin-bottom:8px;display:flex;align-items:center;gap:4px}
.metric .val{font-size:24px;font-weight:600;line-height:1}
.metric .unit{font-size:11px;color:var(--muted);margin-top:4px}
.metric .bar{height:3px;background:rgba(255,255,255,.08);border-radius:2px;margin-top:12px;overflow:hidden}
.metric .bar-fill{height:100%;border-radius:2px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1.25rem;margin-bottom:1.5rem}
.ring-wrap{position:relative;width:100px;height:100px;flex-shrink:0}
.ring-wrap svg{position:absolute;top:0;left:0}
.ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring-pct{font-size:26px;font-weight:600}
.ring-lbl{font-size:10px;color:var(--muted)}
.recovery-row{display:flex;align-items:center;gap:20px}
.recovery-stats{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:12px}
.rs-label{font-size:11px;color:var(--muted);margin-bottom:3px}
.rs-val{font-size:16px;font-weight:500}
.sleep-bars{display:flex;flex-direction:column;gap:10px;margin-top:4px}
.sleep-row{display:flex;align-items:center;gap:10px}
.sleep-lbl{font-size:11px;color:var(--muted);width:64px;flex-shrink:0}
.sleep-track{flex:1;height:5px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden}
.sleep-fill{height:100%;border-radius:3px}
.sleep-val{font-size:11px;color:var(--muted);width:40px;text-align:right;flex-shrink:0}
.chart-wrap{position:relative;width:100%;height:100px;margin-top:12px}
.activities{display:flex;flex-direction:column;gap:6px;margin-top:4px}
.act-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:0.5px solid var(--border)}
.act-row:last-child{border-bottom:none}
.act-left{display:flex;align-items:center;gap:10px}
.act-icon{width:30px;height:30px;border-radius:var(--radius-sm);background:rgba(255,255,255,.05);display:flex;align-items:center;justify-content:center;font-size:14px;color:var(--muted)}
.act-name{font-size:13px;font-weight:500}
.act-sub{font-size:11px;color:var(--muted)}
.act-strain{font-size:14px;font-weight:600}
.goals-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.add-btn{display:flex;align-items:center;gap:5px;background:transparent;border:1px solid var(--border);border-radius:var(--radius-sm);padding:6px 12px;font-size:12px;color:var(--muted);cursor:pointer;font-family:inherit}
.add-btn:hover{background:var(--surface);color:var(--text)}
.input-row{display:none;gap:8px;margin-bottom:12px}
.input-row.show{display:flex}
.input-row input{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 12px;font-size:13px;color:var(--text);font-family:inherit;outline:none}
.input-row input:focus{border-color:rgba(255,255,255,.2)}
.input-row select{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 10px;font-size:12px;color:var(--muted);cursor:pointer;font-family:inherit;outline:none}
.input-row button{padding:8px 14px;font-size:12px;border-radius:var(--radius-sm);border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer;font-family:inherit;font-weight:500}
.input-row button:hover{background:var(--surface)}
.goals-list{display:flex;flex-direction:column;gap:6px}
.goal-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface);transition:opacity .15s}
.goal-item.done{opacity:.45}
.goal-item.done .goal-text{text-decoration:line-through;color:var(--muted)}
.goal-check{width:20px;height:20px;border-radius:5px;border:1px solid var(--border);display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:background .1s}
.goal-check.checked{background:var(--green);border-color:var(--green)}
.goal-check.checked i{color:#000;font-size:12px}
.goal-text{font-size:13px;flex:1}
.goal-tag{font-size:10px;padding:2px 8px;border-radius:10px;flex-shrink:0;font-weight:500}
.t-fitness{background:rgba(0,196,126,.15);color:#00c47e}
.t-nutrition{background:rgba(245,158,66,.15);color:#f59e42}
.t-wellness{background:rgba(167,139,250,.15);color:#a78bfa}
.t-work{background:rgba(78,166,255,.15);color:#4ea6ff}
.t-other{background:rgba(255,255,255,.07);color:var(--muted)}
.goal-del{cursor:pointer;color:var(--hint);font-size:15px;flex-shrink:0}
.goal-del:hover{color:var(--muted)}
.empty{font-size:13px;color:var(--hint);text-align:center;padding:2rem 0}
.updated{font-size:11px;color:var(--hint);text-align:center;margin-top:2rem;padding-bottom:2rem}
.divider{height:1px;background:var(--border);margin:1.5rem 0}
</style>
</head>
<body>
<div class="page">

<div class="header">
  <div class="logo-row">
    <div class="logo"><i class="ti ti-activity"></i></div>
    <div>
      <h1>{% if username %}{{ username }}'s{% endif %} WHOOP Dashboard</h1>
      <div class="sub">Live from WHOOP API · auto-refreshes every hour</div>
    </div>
  </div>
  <div class="header-right">
    <div class="badge">Updated {{ last_updated }}</div>
    <a href="/refresh" class="refresh-btn"><i class="ti ti-refresh" style="font-size:12px"></i> Refresh</a>
  </div>
</div>

<div class="section-label">Today's overview</div>
<div class="grid4">
  <div class="metric">
    <div class="lbl"><i class="ti ti-heart-rate-monitor" style="font-size:13px"></i>Recovery</div>
    <div class="val" style="color:{{ rec_color }}">{{ recovery }}<span style="font-size:14px;font-weight:400">%</span></div>
    <div class="unit">{{ rec_label }}</div>
    <div class="bar"><div class="bar-fill" style="width:{{ recovery }}%;background:{{ rec_color }}"></div></div>
  </div>
  <div class="metric">
    <div class="lbl"><i class="ti ti-flame" style="font-size:13px"></i>Day Strain</div>
    <div class="val" style="color:{{ strain_color }}">{{ strain }}</div>
    <div class="unit">of 21 max</div>
    <div class="bar"><div class="bar-fill" style="width:{{ (strain/21*100)|round|int }}%;background:{{ strain_color }}"></div></div>
  </div>
  <div class="metric">
    <div class="lbl"><i class="ti ti-moon" style="font-size:13px"></i>Sleep</div>
    <div class="val">{{ sleep_h }}<span style="font-size:14px;font-weight:400">h {{ sleep_mm }}m</span></div>
    <div class="unit">{{ sleep_perf }}% performance</div>
    <div class="bar"><div class="bar-fill" style="width:{{ sleep_bar_pct }}%;background:var(--purple)"></div></div>
  </div>
  <div class="metric">
    <div class="lbl"><i class="ti ti-heartbeat" style="font-size:13px"></i>HRV</div>
    <div class="val">{{ hrv }}<span style="font-size:14px;font-weight:400">ms</span></div>
    <div class="unit">7-day avg {{ hrv_avg }}ms</div>
    <div class="bar"><div class="bar-fill" style="width:{{ [[(hrv/120*100)|round|int, 100]|min, 0]|max }}%;background:var(--amber)"></div></div>
  </div>
</div>

<div class="section-label">More stats</div>
<div class="grid3">
  <div class="metric">
    <div class="lbl"><i class="ti ti-heart" style="font-size:13px"></i>Resting HR</div>
    <div class="val">{{ rhr }}<span style="font-size:14px;font-weight:400"> bpm</span></div>
    <div class="unit">beats per minute</div>
  </div>
  <div class="metric">
    <div class="lbl"><i class="ti ti-lungs" style="font-size:13px"></i>Resp. Rate</div>
    <div class="val">{{ resp_rate }}<span style="font-size:14px;font-weight:400"> rpm</span></div>
    <div class="unit">breaths per min</div>
  </div>
  <div class="metric">
    <div class="lbl"><i class="ti ti-droplet" style="font-size:13px"></i>SpO₂</div>
    <div class="val">{{ spo2 }}<span style="font-size:14px;font-weight:400">%</span></div>
    <div class="unit">blood oxygen</div>
  </div>
</div>

<div class="grid2">
  <div class="card" style="margin-bottom:0">
    <div class="section-label">Recovery detail</div>
    <div class="recovery-row">
      <div class="ring-wrap">
        <svg width="100" height="100" viewBox="0 0 100 100">
          <circle cx="50" cy="50" r="40" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="8"/>
          <circle cx="50" cy="50" r="40" fill="none" stroke="{{ rec_color }}" stroke-width="8"
            stroke-dasharray="251.3" stroke-dashoffset="{{ (251.3*(1-recovery/100))|round(1) }}"
            stroke-linecap="round" transform="rotate(-90 50 50)"/>
        </svg>
        <div class="ring-center">
          <span class="ring-pct" style="color:{{ rec_color }}">{{ recovery }}</span>
          <span class="ring-lbl">score</span>
        </div>
      </div>
      <div class="recovery-stats">
        <div><div class="rs-label">Resting HR</div><div class="rs-val">{{ rhr }} bpm</div></div>
        <div><div class="rs-label">HRV</div><div class="rs-val">{{ hrv }} ms</div></div>
        <div><div class="rs-label">SpO₂</div><div class="rs-val">{{ spo2 }}%</div></div>
        <div><div class="rs-label">Skin Temp</div><div class="rs-val">{{ skin_temp }}°F</div></div>
      </div>
    </div>
  </div>
  <div class="card" style="margin-bottom:0">
    <div class="section-label">Sleep breakdown</div>
    <div class="sleep-bars">
      <div class="sleep-row">
        <span class="sleep-lbl">Total</span>
        <div class="sleep-track"><div class="sleep-fill" style="width:{{ sleep_bar_pct }}%;background:var(--purple)"></div></div>
        <span class="sleep-val">{{ sleep_h }}h{{ sleep_mm }}m</span>
      </div>
      <div class="sleep-row">
        <span class="sleep-lbl">In Bed</span>
        <div class="sleep-track"><div class="sleep-fill" style="width:{{ [((inbed_h*60+inbed_m)/600*100)|round|int, 100]|min }}%;background:#7c3aed"></div></div>
        <span class="sleep-val">{{ inbed_h }}h{{ inbed_m }}m</span>
      </div>
      <div class="sleep-row">
        <span class="sleep-lbl">REM</span>
        <div class="sleep-track"><div class="sleep-fill" style="width:{{ [((rem_h*60+rem_m)/90*100)|round|int, 100]|min }}%;background:#818cf8"></div></div>
        <span class="sleep-val">{{ rem_h }}h{{ rem_m }}m</span>
      </div>
      <div class="sleep-row">
        <span class="sleep-lbl">Deep</span>
        <div class="sleep-track"><div class="sleep-fill" style="width:{{ [((deep_h*60+deep_m)/75*100)|round|int, 100]|min }}%;background:#6366f1"></div></div>
        <span class="sleep-val">{{ deep_h }}h{{ deep_m }}m</span>
      </div>
      <div class="sleep-row">
        <span class="sleep-lbl">Light</span>
        <div class="sleep-track"><div class="sleep-fill" style="width:{{ [((light_h*60+light_m)/280*100)|round|int, 100]|min }}%;background:#c4b5fd"></div></div>
        <span class="sleep-val">{{ light_h }}h{{ light_m }}m</span>
      </div>
      <div class="sleep-row">
        <span class="sleep-lbl">Latency</span>
        <div class="sleep-track"><div class="sleep-fill" style="width:{{ [(latency/30*100)|round|int, 100]|min }}%;background:#ddd6fe"></div></div>
        <span class="sleep-val">{{ latency }}m</span>
      </div>
    </div>
  </div>
</div>

<div style="margin-top:1.5rem"></div>

<div class="card">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
    <div class="section-label" style="margin:0">HRV trend — 7 days</div>
    <span style="font-size:11px;color:var(--hint)">ms</span>
  </div>
  <div class="chart-wrap">
    <canvas id="hrvChart" role="img" aria-label="7-day HRV trend"></canvas>
  </div>
</div>

<div class="card">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
    <div class="section-label" style="margin:0">Recovery trend — 7 days</div>
    <span style="font-size:11px;color:var(--hint)">%</span>
  </div>
  <div class="chart-wrap">
    <canvas id="recChart" role="img" aria-label="7-day recovery trend"></canvas>
  </div>
</div>

{% if workouts %}
<div class="card">
  <div class="section-label">Recent workouts</div>
  <div class="activities">
    {% for w in workouts %}
    <div class="act-row">
      <div class="act-left">
        <div class="act-icon"><i class="ti ti-barbell"></i></div>
        <div>
          <div class="act-name">{{ w.name }}</div>
          <div class="act-sub">{{ w.duration }} min · {{ w.calories }} cal · Max HR {{ w.max_hr }} bpm</div>
        </div>
      </div>
      <span class="act-strain" style="color:{% if w.strain >= 18 %}#e24b4a{% elif w.strain >= 14 %}#f59e42{% else %}#4ea6ff{% endif %}">{{ w.strain }}</span>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<div class="divider"></div>

<div class="goals-header">
  <div class="section-label" style="margin:0">Daily goals & tasks</div>
  <button class="add-btn" onclick="toggleInput()"><i class="ti ti-plus" style="font-size:13px"></i> Add goal</button>
</div>

<div class="input-row" id="inputRow">
  <input type="text" id="goalInput" placeholder="What do you want to accomplish today?" onkeydown="if(event.key==='Enter')addGoal()"/>
  <select id="goalTag">
    <option value="fitness">Fitness</option>
    <option value="nutrition">Nutrition</option>
    <option value="wellness">Wellness</option>
    <option value="work">Work</option>
    <option value="other">Other</option>
  </select>
  <button onclick="addGoal()">Add</button>
</div>

<div class="goals-list" id="goalsList"></div>
<div class="empty" id="emptyMsg">No goals yet — add something to get done today</div>

<div class="updated">Live data from WHOOP API · {{ last_updated }}</div>

</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const HRV_DATA   = {{ hrv_trend|tojson }};
const REC_DATA   = {{ rec_trend|tojson }};
const DAY_LABELS = {{ day_labels|tojson }};

const base = {
  responsive:true, maintainAspectRatio:false,
  plugins:{legend:{display:false}},
  scales:{
    x:{grid:{display:false},ticks:{font:{size:10},color:'#555'}},
    y:{grid:{color:'rgba(255,255,255,.05)'},ticks:{font:{size:10},color:'#555'}}
  }
};

new Chart(document.getElementById('hrvChart'),{
  type:'line',
  data:{labels:DAY_LABELS,datasets:[{
    data:HRV_DATA,borderColor:'#00c47e',backgroundColor:'rgba(0,196,126,.08)',
    borderWidth:2,pointRadius:3,pointBackgroundColor:'#00c47e',fill:true,tension:.4
  }]},
  options:{...base,scales:{...base.scales,y:{...base.scales.y,
    min:Math.max(0,Math.min(...HRV_DATA)-10),max:Math.max(...HRV_DATA)+10}}}
});

new Chart(document.getElementById('recChart'),{
  type:'line',
  data:{labels:DAY_LABELS,datasets:[{
    data:REC_DATA,borderColor:'#4ea6ff',backgroundColor:'rgba(78,166,255,.08)',
    borderWidth:2,pointRadius:3,pointBackgroundColor:'#4ea6ff',fill:true,tension:.4
  }]},
  options:{...base,scales:{...base.scales,y:{...base.scales.y,min:0,max:100}}}
});

const TAG_LABELS = {fitness:'Fitness',nutrition:'Nutrition',wellness:'Wellness',work:'Work',other:'Other'};
let goals = JSON.parse(localStorage.getItem('whoopGoals')||'[]');

function save(){localStorage.setItem('whoopGoals',JSON.stringify(goals));}
function toggleInput(){
  document.getElementById('inputRow').classList.toggle('show');
  if(document.getElementById('inputRow').classList.contains('show'))
    document.getElementById('goalInput').focus();
}
function addGoal(){
  const inp=document.getElementById('goalInput');
  const tag=document.getElementById('goalTag').value;
  const text=inp.value.trim();
  if(!text)return;
  goals.push({id:Date.now(),text,tag,done:false});
  inp.value='';save();render();
  document.getElementById('inputRow').classList.remove('show');
}
function toggleGoal(id){const g=goals.find(x=>x.id===id);if(g){g.done=!g.done;save();render();}}
function deleteGoal(id){goals=goals.filter(x=>x.id!==id);save();render();}
function render(){
  const list=document.getElementById('goalsList');
  const empty=document.getElementById('emptyMsg');
  if(!goals.length){list.innerHTML='';empty.style.display='block';return;}
  empty.style.display='none';
  list.innerHTML=goals.map(g=>`
    <div class="goal-item${g.done?' done':''}">
      <div class="goal-check${g.done?' checked':''}" onclick="toggleGoal(${g.id})" role="checkbox" aria-checked="${g.done}" tabindex="0">
        ${g.done?'<i class="ti ti-check"></i>':''}
      </div>
      <span class="goal-text">${g.text}</span>
      <span class="goal-tag t-${g.tag}">${TAG_LABELS[g.tag]}</span>
      <i class="ti ti-x goal-del" onclick="deleteGoal(${g.id})" aria-label="Remove" tabindex="0"></i>
    </div>`).join('');
}
render();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
