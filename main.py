"""
Drix Scout — pre-meeting research brief generator.

Single-file web app. WorldMonitor-style free feed intelligence
(Google News RSS + institutional press feeds), aimed at a specific prospect.
Items are scored for importance and presented in four expandable tiers.
No paid APIs, no dependencies beyond the Python standard library.

Local:    python main.py            (opens browser at http://localhost:8787)
          python main.py --no-browser
Railway:  binds 0.0.0.0:$PORT automatically. Optionally set DRIX_KEY to
          require ?key=<value> on first visit (stored in a cookie after that).
"""
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Railway (and most PaaS) inject PORT; locally we default to 8787 on loopback.
IS_CLOUD = "PORT" in os.environ
PORT = int(os.environ.get("PORT", "8787"))
HOST = "0.0.0.0" if IS_CLOUD else "127.0.0.1"
KEY = os.environ.get("DRIX_KEY", "")

# AI synthesis layer — active only when BOTH are set. No defaults on purpose.
AI_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MODEL = os.environ.get("OPENROUTER_MODEL_ID", "")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DrixScout/0.4"}


def gnews(query, days):
    q = urllib.parse.quote_plus(f"{query} when:{days}d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# Industry keyword -> (sector queries, regulator queries, institutional RSS)
INDUSTRY_MAP = {
    "bank": {
        "sector": ['("community bank" OR "regional bank" OR "community banking")',
                   '("interest rate" OR "rate decision" OR "monetary policy") Fed',
                   '("commercial real estate") (bank OR loans)'],
        "regulatory": ['(FDIC OR OCC OR "capital requirements" OR "banking regulation")'],
        "rss": [("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml")],
    },
    "financ": {
        "sector": ['("asset management" OR "wealth management" OR "financial services") trends',
                   '("interest rate" OR "monetary policy") Fed'],
        "regulatory": ['(SEC OR FINRA OR CFTC) (enforcement OR rule)'],
        "rss": [("SEC", "https://www.sec.gov/news/pressreleases.rss")],
    },
    "insur": {
        "sector": ['(insurance OR insurer) (rates OR claims OR "combined ratio")',
                   '(insurance) ("catastrophe losses" OR reinsurance)'],
        "regulatory": ['(NAIC OR "insurance regulation" OR "insurance commissioner")'],
        "rss": [],
    },
    "health": {
        "sector": ['(hospital OR "health system" OR healthcare) (margins OR staffing OR consolidation)'],
        "regulatory": ['(CMS OR HHS OR FDA) (rule OR policy OR reimbursement)'],
        "rss": [],
    },
    "tech": {
        "sector": ['("software industry" OR SaaS OR "enterprise software") (spending OR trends)',
                   '(AI OR "artificial intelligence") enterprise adoption'],
        "regulatory": ['(FTC OR "AI regulation" OR "data privacy law")'],
        "rss": [],
    },
    "energy": {
        "sector": ['("oil and gas" OR utility OR "power grid") (prices OR demand)'],
        "regulatory": ['(FERC OR EPA OR "energy regulation")'],
        "rss": [],
    },
    "real estate": {
        "sector": ['("commercial real estate" OR "housing market") (trends OR rates)'],
        "regulatory": ['(HUD OR zoning OR "building codes") policy'],
        "rss": [],
    },
    "construct": {
        "sector": ['(construction industry) (costs OR labor OR materials)'],
        "regulatory": ['(OSHA OR "building codes" OR permits) construction'],
        "rss": [],
    },
    "manufactur": {
        "sector": ['(manufacturing) (tariffs OR "supply chain" OR reshoring)'],
        "regulatory": ['(OSHA OR EPA OR tariffs) manufacturing'],
        "rss": [],
    },
    "logistic": {
        "sector": ['(logistics OR freight OR trucking OR shipping) (rates OR capacity)'],
        "regulatory": ['(FMCSA OR "port fees" OR tariffs) freight'],
        "rss": [],
    },
    "retail": {
        "sector": ['(retail) ("consumer spending" OR "foot traffic" OR ecommerce)'],
        "regulatory": ['(FTC OR "swipe fees" OR "consumer protection") retail'],
        "rss": [],
    },
    "legal": {
        "sector": ['("law firm" OR "legal industry") (rates OR mergers OR AI)'],
        "regulatory": ['("bar association" OR "legal ethics") rules'],
        "rss": [],
    },
}


def industry_profile(industry):
    low = industry.lower()
    for key, prof in INDUSTRY_MAP.items():
        if key in low:
            return prof
    if industry:
        return {
            "sector": [f'"{industry}" (trends OR outlook OR consolidation)'],
            "regulatory": [f'"{industry}" (regulation OR compliance)'],
            "rss": [],
        }
    return {"sector": [], "regulatory": [], "rss": []}


STOPWORDS = {"bank", "trust", "company", "group", "corp", "inc", "the", "and", "of",
             "north", "south", "east", "west", "new", "first", "national", "capital"}

# Lens key -> (label, query terms). A lens is a targeted "show me this kind of
# trouble" feed, scoped by whatever anchors (industry/region) are filled in.
LENSES = {
    "breaches": ("Breaches", '("data breach" OR ransomware OR cyberattack OR "security incident")'),
    "lawsuits": ("Lawsuits & Fines", '(lawsuit OR "class action" OR fine OR penalty OR settlement OR enforcement)'),
    "layoffs": ("Layoffs", '(layoffs OR "job cuts" OR downsizing OR restructuring)'),
    "ma": ("M&A", '(merger OR acquisition OR acquires OR takeover OR buyout)'),
    "funding": ("Funding", '("funding round" OR "raises" OR "series A" OR "series B" OR "new investment")'),
    "leadership": ("Leadership", '("new CEO" OR "new CFO" OR appoints OR "names new" OR promotes)'),
    "expansion": ("Expansion", '(expansion OR "new office" OR "new location" OR opens OR groundbreaking)'),
}


def tokens(name):
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", name)]
    sig = [w for w in words if w not in STOPWORDS]
    # If everything was generic (e.g. "First National Bank"), fall back to full-phrase match
    return sig if sig else [name.lower()]


def build_feeds(p):
    company = p.get("company", "").strip()
    domain = p.get("domain", "").strip()
    person = p.get("person", "").strip()
    industry = p.get("industry", "").strip()
    solution = p.get("solution", "").strip()
    region = p.get("region", "").strip()
    competitors = [c.strip() for c in p.get("competitors", "").split(",") if c.strip()][:5]
    days = max(3, min(int(p.get("days", "30") or 30), 365))

    prof = industry_profile(industry)
    feeds = []  # (layer, feed_name, url, must_contain_tokens)

    if company:
        stem = domain.split(".")[0] if domain else ""
        q = f'"{company}"' + (f' OR "{stem}"' if len(stem) > 2 else "")
        feeds.append(("target", f"Target: {company}", gnews(q, max(days, 30)),
                      tokens(company) + ([stem.lower()] if len(stem) > 2 else [])))
    elif domain:
        stem = domain.split(".")[0]
        feeds.append(("target", f"Target: {domain}", gnews(f'"{stem}"', max(days, 30)),
                      [stem.lower()]))
    if person:
        feeds.append(("person", f"Person: {person}", gnews(f'"{person}"', max(days, 60)),
                      [person.lower().split()[-1]]))
    for i, q in enumerate(prof["sector"]):
        feeds.append(("sector", f"Sector watch {i + 1}", gnews(q, min(days, 14)), []))
    for i, q in enumerate(prof["regulatory"]):
        feeds.append(("regulatory", f"Regulatory watch {i + 1}", gnews(q, min(days, 30)), []))
    for name, url in prof["rss"]:
        feeds.append(("regulatory", name, url, []))
    if region:
        rq = f'({region}) (economy OR business' + (f' OR "{industry}"' if industry else "") + ")"
        feeds.append(("region", f"Region: {region}", gnews(rq, min(days, 14)), []))
        if industry:
            feeds.append(("region", f"{region} + {industry}",
                          gnews(f'({region}) ("{industry}") (merger OR acquisition OR expansion OR earnings)', 30), []))
    for comp in competitors:
        feeds.append(("competitors", f"Competitor: {comp}", gnews(f'"{comp}"', max(days, 30)), tokens(comp)))
    if solution:
        anchor = f'"{industry}"' if industry else (f'"{company}"' if company else "")
        feeds.append(("solution", f"Solution radar: {solution}",
                      gnews(f'"{solution}" {("(" + anchor + ")") if anchor else ""}', 60), []))
        feeds.append(("solution", "Pain signals",
                      gnews(f'"{solution}" (breach OR outage OR failure OR fine OR lawsuit OR shortage)', 30), []))

    # Lenses: user-picked "show me this kind of trouble" feeds. Scoped by
    # industry and/or region when given, global otherwise. Full lookback window.
    lens_anchor = " ".join(filter(None, [f'({industry})' if industry else "",
                                         f'({region})' if region else ""]))
    lens_keys = [k for k in p.get("lenses", "").split(",") if k in LENSES]
    for k in lens_keys:
        label, terms = LENSES[k]
        feeds.append(("lens", f"Lens: {label}", gnews(f"{terms} {lens_anchor}".strip(), days), []))
    custom = p.get("custom_lens", "").strip()
    if custom:
        feeds.append(("lens", f"Lens: {custom}", gnews(f'"{custom}" {lens_anchor}'.strip(), days), []))
    return feeds, days


def fetch(layer, name, url, must, limit=15):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        return {"layer": layer, "feed": name, "error": str(e)[:120], "items": []}
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if must and not any(t in title.lower() for t in must):
            continue
        pub = (it.findtext("pubDate") or "").strip()
        age_days, date = 999, pub[:16]
        try:
            dt = parsedate_to_datetime(pub)
            date = dt.strftime("%b %d")
            age_days = max(0, (datetime.now(timezone.utc) - dt).days)
        except Exception:
            pass
        items.append({"title": title, "source": (it.findtext("source") or "").strip(),
                      "date": date, "age": age_days, "link": (it.findtext("link") or "").strip()})
        if len(items) >= limit:
            break
    return {"layer": layer, "feed": name, "items": items}


# ---------- Importance scoring ----------

LAYER_BASE = {"target": 40, "person": 38, "lens": 34, "competitors": 30, "region": 18,
              "solution": 15, "sector": 12, "regulatory": 12}
LAYER_LABEL = {"target": "Target", "person": "Person", "lens": "Lens",
               "competitors": "Competitor", "region": "Region", "solution": "Solution",
               "sector": "Sector", "regulatory": "Regulatory"}

SIGNAL_RULES = [
    (25, "M&A / consolidation", r"acquir|merger|merged|takeover|buyout|acquisition"),
    (22, "distress", r"layoff|cuts|closes|closing|bankrupt|downsiz|restructur"),
    (20, "risk / legal", r"breach|lawsuit|fine[sd]?\b|penalty|enforcement|investigation|outage|hack"),
    (15, "expansion", r"expand|opens|opening|launch|new (office|location|branch|center|facility)|hiring"),
    (15, "leadership change", r"names |appoint|joins|new (ceo|cfo|cio|president|chief)|elected|promot"),
    (10, "earnings / results", r"earnings|dividend|quarterly|results|revenue"),
]

TIERS = [
    (60, "critical", "Most Important", "Walk-in-knowing-this territory"),
    (45, "high", "Really Important", "Shapes your angle"),
    (30, "medium", "Important", "Context worth skimming"),
    (0, "interesting", "Interesting", "Everything else the sweep caught"),
]


def score_item(item, layer, target_tokens):
    score = LAYER_BASE.get(layer, 10)
    reasons = []
    title = item["title"]
    # A lens is an explicit user request — its hits get full signal weight
    proximate = layer in ("target", "person", "competitors", "lens")
    for boost, label, pat in SIGNAL_RULES:
        if re.search(pat, title, re.I):
            score += boost if proximate else boost // 2
            reasons.append(label)
    if not proximate and target_tokens and any(t in title.lower() for t in target_tokens):
        score += 12
        reasons.append("mentions target")
    if item["age"] <= 2:
        score += 8
    elif item["age"] <= 7:
        score += 5
    elif item["age"] <= 14:
        score += 2
    return score, reasons


def tier_of(score):
    for cutoff, key, label, hint in TIERS:
        if score >= cutoff:
            return key
    return "interesting"


# ---------- AI synthesis (OpenRouter) ----------

AI_PROMPT = """You are a B2B sales-intelligence analyst. Below is a prospect profile and
recent news items gathered from feeds, ranked by importance. Using ONLY facts present in
these items (never invent events), produce strict JSON — no markdown, no commentary —
with exactly these keys:

"read": 2-3 sentence situation read: what is happening around this prospect right now.
"pain_points": array of {"point": short pain statement, "evidence": the headline(s) that prove it}.
"questions": array of 5-7 discovery questions to ask in the meeting, each tied to something in the news.
"drip_campaign": array of 3-5 {"touch": number, "theme": subject-line-style theme, "hook": the specific news fact this touch references}.
"partner_opportunities": array of opportunities for OTHER, NON-COMPETING solution categories
  spotted in these items — e.g. a cyberattack in the prospect's industry/region is an opening
  for backup/recovery partners AND for security partners in that geography. Each:
  {"signal": the news fact, "solution_category": what kind of solution could ride this,
   "geography": region it applies to or "national", "notify": who should hear about it,
   "why": one sentence}.

If a section has nothing grounded in the items, return it as an empty array. JSON only.

PROSPECT PROFILE:
%s

NEWS ITEMS (importance-ranked, most important first):
%s"""


def ai_synthesize(params, tiers):
    """One OpenRouter call over the top-ranked items. Fails soft: returns {'error': ...}."""
    items = []
    for t in tiers:
        if t["key"] == "interesting":
            continue
        for i in t["items"]:
            items.append(f"[{t['label']}] ({i['layer']}) {i['date']}: {i['title']}"
                         + (f" — signals: {', '.join(i['reasons'])}" if i["reasons"] else ""))
    items = items[:40]
    if not items:
        return {"error": "no ranked items to synthesize"}
    profile = json.dumps({k: v for k, v in params.items() if v and k != "days"})
    body = json.dumps({
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": AI_PROMPT % (profile, "\n".join(items))}],
        "temperature": 0.4,
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json",
                 "X-Title": "Drix Scout"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read())
        text = resp["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {"error": "model returned no JSON"}
        return json.loads(m.group(0))
    except Exception as e:
        return {"error": str(e)[:200]}


def run_brief(params):
    feeds, days = build_feeds(params)
    if not feeds:
        return {"error": "Fill in at least one field or pick a lens — any single one works."}
    target_tokens = tokens(params.get("company", "")) if params.get("company", "").strip() else []
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda f: fetch(*f), feeds))
    best, errors = {}, []
    for r in results:
        if r.get("error"):
            errors.append({"feed": r["feed"], "error": r["error"]})
        for it in r["items"]:
            # Same story can arrive via several feeds (e.g. region AND competitor);
            # keep the copy that scores highest so it lands in the right tier.
            key = re.sub(r"\W+", "", it["title"].lower())[:70]
            score, reasons = score_item(it, r["layer"], target_tokens)
            if r["layer"] == "lens":
                reasons = [r["feed"].removeprefix("Lens: ").lower()] + reasons
            if key in best and best[key]["score"] >= score:
                continue
            best[key] = {**it, "layer": LAYER_LABEL[r["layer"]], "feed": r["feed"],
                         "score": score, "tier": tier_of(score), "reasons": reasons}
    ranked = list(best.values())
    ranked.sort(key=lambda x: (-x["score"], x["age"]))
    tiers = [{"key": key, "label": label, "hint": hint,
              "items": [i for i in ranked if i["tier"] == key]}
             for _, key, label, hint in TIERS]
    return {"tiers": tiers, "errors": errors, "days": days,
            "params": {k: v for k, v in params.items() if v}}


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Drix Scout</title>
<style>
:root { --bg:#11141a; --panel:#1a1f29; --line:#2c3442; --text:#dde3ec; --dim:#8b95a7;
        --gold:#c9a961; --blue:#6ca0dd; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:14px/1.5 'Segoe UI', system-ui, sans-serif; }
.wrap { display:grid; grid-template-columns:320px 1fr; min-height:100vh; }
@media (max-width:760px) { .wrap { grid-template-columns:1fr; } }
aside { background:var(--panel); border-right:1px solid var(--line); padding:22px; }
h1 { font-size:17px; margin:0 0 2px; color:var(--gold); letter-spacing:2px; }
.tag { font-size:11px; color:var(--dim); margin-bottom:20px; }
label { display:block; font-size:11px; text-transform:uppercase; letter-spacing:1px;
        color:var(--dim); margin:13px 0 4px; }
input, select { width:100%; background:var(--bg); border:1px solid var(--line);
        color:var(--text); border-radius:6px; padding:8px 10px; font-size:13.5px; }
input:focus { outline:none; border-color:var(--gold); }
button { width:100%; margin-top:20px; background:var(--gold); color:#14100a; border:0;
        border-radius:6px; padding:11px; font-size:14px; font-weight:600; cursor:pointer; }
button:disabled { opacity:.5; cursor:wait; }
#save { background:transparent; border:1px solid var(--line); color:var(--dim);
        margin-top:10px; display:none; }
.lensrow { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
.lens { width:auto; margin:0; background:var(--bg); border:1px solid var(--line);
        color:var(--dim); border-radius:14px; padding:3px 11px; font-size:11.5px;
        font-weight:400; }
.lens.active { color:var(--gold); border-color:var(--gold); }
main { padding:26px 34px; max-width:980px; }
.empty { color:var(--dim); margin-top:80px; text-align:center; }
.brief-head { border-bottom:2px solid var(--gold); padding-bottom:10px; margin-bottom:18px; }
.brief-head h2 { margin:0; font-size:21px; }
.brief-head .sub { color:var(--dim); font-size:12px; }
details.tier { border:1px solid var(--line); border-radius:8px; margin:10px 0;
        background:var(--panel); overflow:hidden; }
details.tier > summary { cursor:pointer; list-style:none; padding:13px 16px;
        display:flex; align-items:baseline; gap:10px; user-select:none; }
details.tier > summary::-webkit-details-marker { display:none; }
details.tier > summary::before { content:'▸'; color:var(--gold); font-size:13px;
        transition:transform .15s; }
details.tier[open] > summary::before { transform:rotate(90deg); }
.tname { font-size:13px; text-transform:uppercase; letter-spacing:1.5px; color:var(--gold);
        font-weight:600; }
.tier-critical .tname { color:#e8c97e; }
.thint { color:var(--dim); font-size:11.5px; }
.count { margin-left:auto; background:var(--bg); border:1px solid var(--line);
        border-radius:12px; padding:1px 10px; font-size:11.5px; color:var(--dim); }
.tier-critical .count { color:var(--gold); border-color:var(--gold); }
.tbody { padding:4px 16px 12px; border-top:1px solid var(--line); }
.item { padding:7px 0; font-size:13.5px; border-bottom:1px solid #1f2530; }
.item:last-child { border-bottom:0; }
.item a { color:var(--text); text-decoration:none; }
.item a:hover { color:var(--blue); }
.item .m { color:var(--dim); font-size:11.5px; }
.chip { display:inline-block; background:var(--bg); border:1px solid var(--line);
        border-radius:10px; padding:0 8px; font-size:10.5px; color:var(--dim);
        margin-left:5px; vertical-align:1px; }
.chip.hot { color:var(--gold); border-color:#5a4a28; }
.err { color:#d98f8f; font-size:12px; margin-top:12px; }
@media print {
  body { background:#fff; color:#111; } aside { display:none; }
  .wrap { display:block; }
  details.tier { background:#fff; border-color:#ccc; }
  .tname, .tier-critical .tname { color:#7a5c1e; }
  .item a { color:#111; } .item { border-color:#eee; }
  .chip { background:#f4efe3; color:#555; border-color:#ddd; }
}
</style></head><body>
<div class="wrap">
<aside>
  <h1>DRIX SCOUT</h1>
  <div class="tag">Pre-meeting intelligence &middot; free feeds only</div>
  <label>Company</label><input id="company" placeholder="North Dallas Bank & Trust">
  <label>Company domain</label><input id="domain" placeholder="ndbt.com">
  <label>Person you're meeting</label><input id="person" placeholder="Jane Smith">
  <label>Industry</label><input id="industry" list="inds" placeholder="banking">
  <datalist id="inds"><option>banking</option><option>financial services</option>
    <option>insurance</option><option>healthcare</option><option>technology</option>
    <option>energy</option><option>real estate</option><option>construction</option>
    <option>manufacturing</option><option>logistics</option><option>retail</option>
    <option>legal</option></datalist>
  <label>Solution you're selling</label><input id="solution" placeholder="email security">
  <label>City / metro / region</label><input id="region" placeholder="Dallas">
  <label>Competitors (comma-separated)</label><input id="competitors" placeholder="Dallas Capital Bank, Veritex">
  <label>Lenses — hunt for specific trouble</label>
  <div class="lensrow">
    <button class="lens" data-k="breaches">Breaches</button>
    <button class="lens" data-k="lawsuits">Lawsuits &amp; Fines</button>
    <button class="lens" data-k="layoffs">Layoffs</button>
    <button class="lens" data-k="ma">M&amp;A</button>
    <button class="lens" data-k="funding">Funding</button>
    <button class="lens" data-k="leadership">Leadership</button>
    <button class="lens" data-k="expansion">Expansion</button>
  </div>
  <label>Custom lens</label><input id="customlens" placeholder="wire fraud, deposit flight…">
  <label>Lookback</label>
  <select id="days"><option value="7">7 days</option><option value="14">14 days</option>
    <option value="30" selected>30 days</option><option value="90">90 days</option></select>
  <button id="go" onclick="run()">Generate Brief</button>
  <button id="save" onclick="window.print()">Print / Save as PDF</button>
</aside>
<main id="out"><div class="empty">Fill in anything — a single field or lens is enough.<br>
Every extra field adds a layer; lenses hunt for specific trouble in that world.</div></main>
</div>
<script>
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

document.querySelectorAll(".lens").forEach(b => b.onclick = () => b.classList.toggle("active"));

async function run() {
  const ids = ["company","domain","person","industry","solution","region","competitors","days"];
  const q = new URLSearchParams();
  ids.forEach(id => { const v = document.getElementById(id).value.trim(); if (v) q.set(id, v); });
  const lenses = [...document.querySelectorAll(".lens.active")].map(b => b.dataset.k);
  if (lenses.length) q.set("lenses", lenses.join(","));
  const custom = document.getElementById("customlens").value.trim();
  if (custom) q.set("custom_lens", custom);
  const go = document.getElementById("go");
  go.disabled = true; go.textContent = "Pulling feeds…";
  document.getElementById("out").innerHTML = '<div class="empty">Sweeping feeds — usually 5–15 seconds…</div>';
  try {
    const r = await fetch("/api/brief?" + q);
    render(await r.json());
  } catch (e) {
    document.getElementById("out").innerHTML = '<div class="err">Request failed: ' + esc(e) + '</div>';
  }
  go.disabled = false; go.textContent = "Generate Brief";
}

function render(d) {
  const out = document.getElementById("out");
  if (d.error) { out.innerHTML = '<div class="err">' + esc(d.error) + '</div>'; return; }
  const p = d.params, today = new Date().toLocaleDateString("en-US", {month:"long", day:"numeric", year:"numeric"});
  let h = '<div class="brief-head"><h2>Pre-Meeting Brief' + (p.company ? ' — ' + esc(p.company) : '') + '</h2>' +
    '<div class="sub">' + [p.person && "Meeting: " + esc(p.person), p.industry && esc(p.industry),
      p.region && esc(p.region), p.solution && "Selling: " + esc(p.solution),
      d.days + "-day window", today].filter(Boolean).join(" · ") + '</div></div>';

  for (const t of d.tiers) {
    const open = (t.key === "critical" || t.key === "high") && t.items.length ? " open" : "";
    h += '<details class="tier tier-' + t.key + '"' + open + '><summary>' +
      '<span class="tname">' + esc(t.label) + '</span>' +
      '<span class="thint">' + esc(t.hint) + '</span>' +
      '<span class="count">' + t.items.length + '</span></summary><div class="tbody">';
    if (!t.items.length) h += '<div class="m" style="color:var(--dim);padding:8px 0">Nothing landed in this tier.</div>';
    h += t.items.slice(0, t.key === "interesting" ? 40 : 25).map(itemHtml).join('');
    h += '</div></details>';
  }
  if (d.errors && d.errors.length)
    h += '<div class="err">' + d.errors.map(e => esc(e.feed) + ": " + esc(e.error)).join('<br>') + '</div>';
  out.innerHTML = h;
  document.getElementById("save").style.display = "block";
}

function itemHtml(i) {
  const chips = '<span class="chip">' + esc(i.layer) + '</span>' +
    (i.reasons || []).map(r => '<span class="chip hot">' + esc(r) + '</span>').join('');
  return '<div class="item"><a href="' + esc(i.link) + '" target="_blank">' + esc(i.title) + '</a>' +
    chips + '<div class="m">' + esc(i.date) + (i.source ? ' · ' + esc(i.source) : '') + '</div></div>';
}

window.addEventListener("beforeprint", () =>
  document.querySelectorAll("details.tier").forEach(el => el.open = true));
</script></body></html>"""

DENIED = """<!DOCTYPE html><html><body style="background:#11141a;color:#dde3ec;
font-family:system-ui;display:grid;place-items:center;min-height:95vh">
<div style="text-align:center"><h2 style="color:#c9a961">DRIX SCOUT</h2>
<p>This instance is key-protected.<br>Open it once as
<code style="color:#c9a961">https://your-app/?key=YOUR_KEY</code> and you're in.</p>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def authorized(self, url):
        """No DRIX_KEY set -> open. Otherwise accept ?key= once, then cookie."""
        if not KEY:
            return True
        qs = urllib.parse.parse_qs(url.query)
        if qs.get("key", [""])[0] == KEY:
            return "set-cookie"
        return f"drixkey={KEY}" in self.headers.get("Cookie", "")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        auth = self.authorized(url)
        if not auth:
            body = DENIED.encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif url.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if auth == "set-cookie":
                self.send_header("Set-Cookie", f"drixkey={KEY}; Path=/; HttpOnly; Max-Age=2592000")
        elif url.path == "/api/brief":
            params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            body = json.dumps(run_brief(params)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            self.send_response(404)
            body = b"not found"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[scout]", fmt % args)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Drix Scout running on {HOST}:{PORT}" + ("" if IS_CLOUD else f"  -> http://localhost:{PORT}  (Ctrl+C to stop)"))
    if not IS_CLOUD and "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    server.serve_forever()
