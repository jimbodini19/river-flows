#!/usr/bin/env python3
"""
Rebuild index.html in place with fresh gauge, reservoir, and Toccoa data.

Data sources (fetched directly over HTTPS, no browser needed):
  - USGS NWIS Instantaneous Values (IV)  -> current CFS + water temp C
  - USGS NWIS Daily Values (DV)          -> 30-day daily-flow sparklines
  - BOR Hydromet yakstats.txt            -> current CFS for the 6 Yakima-basin BOR gauges
  - BOR Hydromet daily.pl (per station)  -> Cle Elum / Rimrock outflow (QD) + storage (AF)
  - TVA RestApi (Blue Ridge / BRDG1)     -> Toccoa generation schedule + tailwater discharge

Design notes:
  - index.html is the single source of truth for the gauge list, the RESERVOIRS list,
    and the TOCCOA_* block. We only rewrite per-item DATA VALUES (cfs/tempC/spark30,
    reservoir outflow/storage/series, Toccoa schedule/discharge) plus the page timestamp.
    Everything else (FISHABILITY/FLOAT maps, render logic, filters) is preserved verbatim.
  - We only OVERWRITE a value when we have fresh data for it. If a fetch fails or a source
    returns nothing, the prior value is kept (no regression to null / no-data).
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    PT = None

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "..", "index.html")

USGS_IV = "https://waterservices.usgs.gov/nwis/iv/"
USGS_DV = "https://waterservices.usgs.gov/nwis/dv/"
BOR_URL = "https://www.usbr.gov/pn/hydromet/yakima/yakstats.txt"
BOR_DAILY = "https://www.usbr.gov/pn-bin/daily.pl"
TVA_GEN = "https://www.tva.com/RestApi/generation-releases/BRDG1"
TVA_OBS = "https://www.tva.com/RestApi/observed-data/BRDG1"

UA = {"User-Agent": "river-flows-refresh/1.0 (+https://jimbodini19.github.io/river-flows/)"}
TVA_HEADERS = dict(UA)
TVA_HEADERS["X-Requested-With"] = "XMLHttpRequest"
TVA_HEADERS["Accept"] = "application/json, text/plain, */*"


def http_get(url, retries=3, timeout=45, headers=None):
    last = None
    hdrs = headers or UA
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            sys.stderr.write(f"fetch attempt {attempt+1} failed for {url[:80]}...: {e}\n")
    sys.stderr.write(f"GIVING UP on {url[:80]}...: {last}\n")
    return None


# ---------- parse the gauge list out of index.html ----------

def parse_gauges(html):
    """Return list of dicts: {id, source, borLabel}. Order preserved."""
    gauges = []
    for m in re.finditer(r"\{\s*id:\s*'([^']+)',\s*source:\s*'(usgs|bor)'", html):
        gid, source = m.group(1), m.group(2)
        bl = None
        if source == "bor":
            lm = re.search(
                r"id:\s*'" + re.escape(gid) + r"'.*?borLabel:\s*'((?:[^'\\]|\\.)*)'",
                html,
            )
            if lm:
                bl = lm.group(1).replace("\\'", "'")
        gauges.append({"id": gid, "source": source, "borLabel": bl})
    return gauges


# ---------- USGS ----------

def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def fetch_usgs_current(site_ids):
    """siteCode -> {cfs: float|None, temp: float|None}. Last value per parameter."""
    out = {}
    for grp in chunk(site_ids, 8):
        url = (USGS_IV + "?sites=" + ",".join(grp)
               + "&period=PT2H&parameterCd=00060,00010&format=json")
        body = http_get(url)
        if not body:
            continue
        try:
            j = json.loads(body)
        except Exception as e:
            sys.stderr.write(f"USGS IV parse error: {e}\n")
            continue
        for ts in j.get("value", {}).get("timeSeries", []):
            site = ts["sourceInfo"]["siteCode"][0]["value"]
            pc = ts["variable"]["variableCode"][0]["value"]
            vals = ts["values"][0]["value"] if ts.get("values") else []
            if not vals:
                continue
            try:
                last = float(vals[-1]["value"])
            except Exception:
                continue
            rec = out.setdefault(site, {})
            if pc == "00060":
                rec["cfs"] = last
            elif pc == "00010":
                rec["temp"] = last
    return out


def fetch_usgs_sparklines(site_ids):
    """siteCode -> [int,...] daily mean flow, last ~30 days."""
    today = datetime.now(PT) if PT else datetime.utcnow()
    start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    url = (USGS_DV + "?sites=" + ",".join(site_ids)
           + f"&startDT={start}&endDT={end}&parameterCd=00060&statCd=00003&format=json")
    body = http_get(url)
    out = {}
    if not body:
        return out
    try:
        j = json.loads(body)
    except Exception as e:
        sys.stderr.write(f"USGS DV parse error: {e}\n")
        return out
    for ts in j.get("value", {}).get("timeSeries", []):
        site = ts["sourceInfo"]["siteCode"][0]["value"]
        vals = ts["values"][0]["value"] if ts.get("values") else []
        arr = []
        for v in vals:
            try:
                arr.append(int(round(float(v["value"]))))
            except Exception:
                pass
        if len(arr) >= 3:
            out[site] = arr
    return out


# ---------- BOR gauges (yakstats.txt) ----------

def fetch_bor(bor_gauges):
    """borLabel -> cfs from yakstats.txt RIVER FLOWS section. {} if unavailable."""
    body = http_get(BOR_URL)
    out = {}
    if not body or len(body.strip()) == 0:
        sys.stderr.write("BOR yakstats.txt empty or unavailable; keeping last-known.\n")
        return out
    lines = body.splitlines()
    section = lines
    for i, ln in enumerate(lines):
        if "RIVER FLOWS" in ln.upper():
            section = lines[i + 1:]
            break
    for g in bor_gauges:
        label = (g.get("borLabel") or "").strip()
        if not label:
            continue
        key = re.sub(r"[^a-z0-9]", "", label.lower())
        for ln in section:
            norm = re.sub(r"[^a-z0-9]", "", ln.lower())
            if key and key[:8] in norm:
                nums = re.findall(r"-?\d+(?:\.\d+)?", ln)
                if nums:
                    try:
                        val = float(nums[-1])
                        if 0 <= val <= 100000:
                            out[label] = val
                            break
                    except Exception:
                        pass
    return out


# ---------- BOR reservoirs (daily.pl per station: QD outflow + AF storage) ----------

def fetch_reservoir(station):
    """
    station = 'CLE' | 'RIM'. Returns dict with outflow, outflow30, storageAF,
    storagePrevAF, updated (YYYY-MM-DD of last QD reading), or None on failure.
    daily.pl returns plain CSV: header 'DateTime,<sta>_qd,<sta>_af' then dated rows.
    Latest day is often blank until finalized, so blank values are dropped.
    """
    today = datetime.now(PT) if PT else datetime.utcnow()
    start = today - timedelta(days=33)
    url = (BOR_DAILY + f"?station={station}&format=csv"
           + f"&year={start.year}&month={start.month}&day={start.day}"
           + f"&year={today.year}&month={today.month}&day={today.day}"
           + "&pcode=QD&pcode=AF")
    body = http_get(url)
    if not body:
        return None
    toks = [t for t in re.split(r"\s+", body) if "," in t]
    header = next((t for t in toks if t.startswith("DateTime")), None)
    if not header:
        sys.stderr.write(f"reservoir {station}: no header in daily.pl response\n")
        return None
    cols = header.split(",")
    sl = station.lower()
    qd_i = cols.index(sl + "_qd") if (sl + "_qd") in cols else -1
    af_i = cols.index(sl + "_af") if (sl + "_af") in cols else -1
    if qd_i < 0:
        sys.stderr.write(f"reservoir {station}: no QD column\n")
        return None
    qd, af, last_qd_date = [], [], None
    for t in toks:
        if not re.match(r"\d{4}-\d{2}-\d{2},", t):
            continue
        f = t.split(",")
        if qd_i < len(f) and f[qd_i].strip():
            try:
                qd.append(int(round(float(f[qd_i]))))
                last_qd_date = f[0]
            except Exception:
                pass
        if 0 <= af_i < len(f) and f[af_i].strip():
            try:
                af.append(int(round(float(f[af_i]))))
            except Exception:
                pass
    if not qd:
        sys.stderr.write(f"reservoir {station}: no QD values\n")
        return None
    res = {"outflow": qd[-1], "outflow30": qd[-30:], "updated": last_qd_date}
    if len(af) >= 1:
        res["storageAF"] = af[-1]
    if len(af) >= 2:
        res["storagePrevAF"] = af[-2]
    return res


def fetch_reservoirs(html):
    """Find reservoir ids in the RESERVOIRS array and fetch each. id -> data dict."""
    ids = re.findall(r"\{\s*id:\s*'(CLE|RIM)'", html)
    out = {}
    for sid in ids:
        d = fetch_reservoir(sid)
        if d:
            out[sid] = d
    return out


# ---------- TVA Toccoa / Blue Ridge ----------

def fetch_toccoa():
    """Returns {updated, discharge, gen:[{day,time,gen}]} or None on failure."""
    gen_body = http_get(TVA_GEN, headers=TVA_HEADERS)
    obs_body = http_get(TVA_OBS, headers=TVA_HEADERS)
    if not gen_body or not obs_body:
        return None
    try:
        gen = json.loads(gen_body)
        obs = json.loads(obs_body)
    except Exception as e:
        sys.stderr.write(f"TVA parse error: {e}\n")
        return None
    if not isinstance(gen, list) or not isinstance(obs, list) or not gen or not obs:
        return None
    gens = []
    for g in gen:
        day = (g.get("Day") or "").strip()
        tm = re.sub(r"\s*EDT\s*$", "", (g.get("Time") or "")).strip()
        try:
            gv = int(float(g.get("Generators", "0")))
        except Exception:
            gv = 0
        if day and tm:
            gens.append({"day": day, "time": tm, "gen": gv})
    if not gens:
        return None
    last = obs[-1]
    disch_raw = str(last.get("AverageHourlyDischarge", "")).replace(",", "").strip()
    try:
        disch = int(round(float(disch_raw)))
    except Exception:
        return None
    day = (last.get("Day") or "").strip()
    tm = (last.get("Time") or "").strip()
    try:
        mm, dd, _yy = day.split("/")
        upd = f"{int(mm)}/{int(dd)} {tm}"
    except Exception:
        upd = (day + " " + tm).strip()
    return {"updated": upd, "discharge": disch, "gen": gens}


# ---------- rewrite ----------

def fmt_num(v):
    if v is None:
        return None
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return ("%.2f" % v).rstrip("0").rstrip(".")


def build_html(html, gauges, usgs_cur, usgs_spark, bor):
    lines = html.split("\n")
    by_id = {g["id"]: g for g in gauges}

    def rewrite_line(line):
        m = re.search(r"id:\s*'([^']+)'", line)
        if not m:
            return line
        gid = m.group(1)
        g = by_id.get(gid)
        if not g:
            return line
        if g["source"] == "usgs":
            cur = usgs_cur.get(gid, {})
            if "cfs" in cur:
                line = re.sub(r"cfs:\s*(?:null|-?\d+(?:\.\d+)?)",
                              "cfs: " + fmt_num(cur["cfs"]), line, count=1)
            if "temp" in cur:
                line = re.sub(r"tempC:\s*(?:null|-?\d+(?:\.\d+)?)",
                              "tempC: " + fmt_num(cur["temp"]), line, count=1)
            arr = usgs_spark.get(gid)
            if arr:
                line = re.sub(r"spark30:\s*\[[^\]]*\]",
                              "spark30: [" + ",".join(str(x) for x in arr) + "]",
                              line, count=1)
        else:  # bor
            label = g.get("borLabel")
            if label and label in bor:
                line = re.sub(r"cfs:\s*(?:null|-?\d+(?:\.\d+)?)",
                              "cfs: " + fmt_num(bor[label]), line, count=1)
        return line

    lines = [rewrite_line(ln) for ln in lines]
    html = "\n".join(lines)
    return html


def rewrite_reservoirs(html, res):
    """res: {id: {outflow, outflow30, storageAF, storagePrevAF, updated}}."""
    if not res:
        return html
    dates = [v["updated"] for v in res.values() if v.get("updated")]
    if dates:
        newdate = max(dates)
        html = re.sub(r"(const RES_UPDATED = ')[^']*(';)",
                      lambda m: m.group(1) + newdate + m.group(2), html, count=1)
    for sid, v in res.items():
        m = re.search(r"\{\s*id:\s*'" + re.escape(sid) + r"'.*?outflow30:\s*\[[^\]]*\]\s*\}",
                      html, re.S)
        if not m:
            continue
        block = m.group(0)
        nb = block
        if "outflow" in v:
            nb = re.sub(r"outflow:\s*-?\d+", "outflow: %d" % v["outflow"], nb, count=1)
        if "storageAF" in v:
            nb = re.sub(r"storageAF:\s*-?\d+", "storageAF: %d" % v["storageAF"], nb, count=1)
        if "storagePrevAF" in v:
            nb = re.sub(r"storagePrevAF:\s*-?\d+",
                        "storagePrevAF: %d" % v["storagePrevAF"], nb, count=1)
        if v.get("outflow30"):
            nb = re.sub(r"outflow30:\s*\[[^\]]*\]",
                        "outflow30: [" + ",".join(str(x) for x in v["outflow30"]) + "]",
                        nb, count=1)
        html = html.replace(block, nb, 1)
    return html


def rewrite_toccoa(html, t):
    if not t:
        return html
    html = re.sub(r"(const TOCCOA_UPDATED = ')[^']*(';)",
                  lambda m: m.group(1) + t["updated"] + m.group(2), html, count=1)
    html = re.sub(r"(const TOCCOA_DISCHARGE = )\d+",
                  lambda m: m.group(1) + str(t["discharge"]), html, count=1)
    rows = ",\n".join(
        "  { day: '%s', time: '%s', gen: %d }" % (g["day"], g["time"].replace("'", ""), g["gen"])
        for g in t["gen"])
    new_block = "const TOCCOA_GEN = [\n" + rows + ",\n];"
    html = re.sub(r"const TOCCOA_GEN = \[.*?\];", lambda m: new_block, html, count=1, flags=re.S)
    return html


def stamp_time(html):
    now = datetime.now(PT) if PT else datetime.utcnow()
    stamp = now.strftime("%B %-d, %Y at %-I:%M %p PT")
    return re.sub(r'(<div class="meta" id="meta">Updated: )[^<]*(</div>)',
                  lambda mm: mm.group(1) + stamp + mm.group(2), html, count=1)


def main():
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    gauges = parse_gauges(html)
    usgs_ids = [g["id"] for g in gauges if g["source"] == "usgs"]
    bor_gauges = [g for g in gauges if g["source"] == "bor"]
    sys.stderr.write(f"Parsed {len(gauges)} gauges ({len(usgs_ids)} USGS, {len(bor_gauges)} BOR)\n")

    usgs_cur = fetch_usgs_current(usgs_ids)
    usgs_spark = fetch_usgs_sparklines(usgs_ids)
    bor = fetch_bor(bor_gauges)
    reservoirs = fetch_reservoirs(html)
    toccoa = fetch_toccoa()
    sys.stderr.write(
        f"Fetched: {len(usgs_cur)} USGS current, {len(usgs_spark)} sparklines, "
        f"{len(bor)} BOR gauges, {len(reservoirs)} reservoirs, "
        f"toccoa={'yes' if toccoa else 'no'}\n")

    if not any([usgs_cur, usgs_spark, bor, reservoirs, toccoa]):
        sys.stderr.write("No data fetched from any source; leaving index.html unchanged.\n")
        return 0

    new_html = build_html(html, gauges, usgs_cur, usgs_spark, bor)
    new_html = rewrite_reservoirs(new_html, reservoirs)
    new_html = rewrite_toccoa(new_html, toccoa)
    new_html = stamp_time(new_html)
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(new_html)
    sys.stderr.write("index.html updated.\n")
    return 0


def selftest():
    """Offline test of the rewrite logic (no network)."""
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    gauges = parse_gauges(html)
    assert len(gauges) >= 30, gauges
    usgs_ids = [g["id"] for g in gauges if g["source"] == "usgs"]
    gid = usgs_ids[0]
    cur = {gid: {"cfs": 1234.0, "temp": 11.1}}
    spark = {gid: [1, 2, 3, 4, 5]}
    out = build_html(html, gauges, cur, spark, {})
    assert "cfs: 1234" in out
    assert "tempC: 11.1" in out
    assert "spark30: [1,2,3,4,5]" in out
    other = usgs_ids[1]
    assert re.search(r"id: '" + other + r"'.*cfs: \d", out)

    # reservoir rewrite
    res = {"CLE": {"outflow": 9999, "outflow30": [7, 8, 9], "storageAF": 111111,
                   "storagePrevAF": 222222, "updated": "2026-12-31"}}
    out2 = rewrite_reservoirs(out, res)
    assert "const RES_UPDATED = '2026-12-31';" in out2, "RES_UPDATED not rewritten"
    cle = re.search(r"\{\s*id:\s*'CLE'.*?outflow30:\s*\[[^\]]*\]\s*\}", out2, re.S).group(0)
    assert "outflow: 9999" in cle and "storageAF: 111111" in cle and \
           "storagePrevAF: 222222" in cle and "outflow30: [7,8,9]" in cle, cle
    # RIM left untouched (we only passed CLE)
    rim = re.search(r"\{\s*id:\s*'RIM'.*?outflow30:\s*\[[^\]]*\]\s*\}", out2, re.S).group(0)
    assert "outflow: 9999" not in rim

    # toccoa rewrite
    tt = {"updated": "12/31 9 PM EDT", "discharge": 4242,
          "gen": [{"day": "12/31/2026", "time": "1 AM - Noon", "gen": 0},
                  {"day": "12/31/2026", "time": "Noon - 7 PM", "gen": 1}]}
    out3 = rewrite_toccoa(out2, tt)
    assert "const TOCCOA_UPDATED = '12/31 9 PM EDT';" in out3
    assert "const TOCCOA_DISCHARGE = 4242;" in out3
    assert "{ day: '12/31/2026', time: 'Noon - 7 PM', gen: 1 }" in out3
    print("selftest OK:", len(gauges), "gauges; reservoir + toccoa rewrite verified")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        sys.exit(main())
