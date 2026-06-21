#!/usr/bin/env python3
"""
Rebuild index.html in place with fresh gauge data.

Data sources (fetched directly over HTTPS, no browser needed):
  - USGS NWIS Instantaneous Values (IV)  -> current CFS + water temp C
  - USGS NWIS Daily Values (DV)          -> 30-day daily-flow sparklines
  - BOR Hydromet yakstats.txt            -> current CFS for the 6 Yakima-basin BOR gauges

Design notes:
  - index.html is the single source of truth for the gauge list. We scan it for
    every `{ id: '...', source: '...', ... }` line and only rewrite the per-gauge
    data values (cfs / tempC / spark30) plus the page timestamp. Everything else
    (comments, FISHABILITY map, render logic, filters) is preserved verbatim.
  - We only OVERWRITE a value when we have fresh data for it. If a fetch fails or a
    source returns nothing, the prior value is kept (no regression to null/"no data").
    This trades a small staleness risk for far higher display reliability.
  - BOR yakstats.txt is sometimes served empty for a few hours; that case is handled
    by the keep-last-known rule above.
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

UA = {"User-Agent": "river-flows-refresh/1.0 (+https://jimbodini19.github.io/river-flows/)"}


def http_get(url, retries=3, timeout=45):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
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


# ---------- BOR ----------

def fetch_bor(bor_gauges):
    """
    borLabel -> cfs. Documented format: a 'RIVER FLOWS' section where each line is
    '<label>  <cfs>.'  We match each gauge's borLabel by case-insensitive partial
    match and take the last number on the line. Returns {} if the file is empty or
    unparseable (callers then keep last-known values).
    """
    body = http_get(BOR_URL)
    out = {}
    if not body or len(body.strip()) == 0:
        sys.stderr.write("BOR yakstats.txt empty or unavailable; keeping last-known.\n")
        return out
    lines = body.splitlines()
    # isolate the RIVER FLOWS section if present, else scan the whole file
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


# ---------- rewrite ----------

def fmt_num(v):
    """Render a fetched number without a trailing .0 for whole numbers."""
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

    now = datetime.now(PT) if PT else datetime.utcnow()
    stamp = now.strftime("%B %-d, %Y at %-I:%M %p PT")
    html = re.sub(r'(<div class="meta" id="meta">Updated: )[^<]*(</div>)',
                  lambda mm: mm.group(1) + stamp + mm.group(2), html, count=1)
    return html


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
    sys.stderr.write(f"Fetched: {len(usgs_cur)} USGS current, "
                     f"{len(usgs_spark)} sparklines, {len(bor)} BOR values\n")

    if not usgs_cur and not usgs_spark and not bor:
        sys.stderr.write("No data fetched from any source; leaving index.html unchanged.\n")
        return 0  # do not bump the timestamp on a total failure

    new_html = build_html(html, gauges, usgs_cur, usgs_spark, bor)
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
    # synthetic data: bump one USGS gauge
    gid = usgs_ids[0]
    cur = {gid: {"cfs": 1234.0, "temp": 11.1}}
    spark = {gid: [1, 2, 3, 4, 5]}
    out = build_html(html, gauges, cur, spark, {})
    assert f"cfs: 1234" in out
    assert "tempC: 11.1" in out
    assert "spark30: [1,2,3,4,5]" in out
    # untouched gauge keeps its data
    other = usgs_ids[1]
    assert re.search(r"id: '" + other + r"'.*cfs: \d", out)
    print("selftest OK:", len(gauges), "gauges;", gid, "rewritten")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        sys.exit(main())
