# River Flows

Live trout / steelhead river-flow dashboard across WA, MT, ID, AZ, and GA.

View live: https://jimbodini19.github.io/river-flows/

## How it refreshes

A GitHub Actions workflow (`.github/workflows/refresh.yml`) runs on a daily cron,
fetches data directly over HTTPS, and commits the rebuilt `index.html`. GitHub Pages
serves the result. No browser or local machine is involved, so the refresh runs even
when nothing is open.

Data sources:
- USGS NWIS Instantaneous Values (IV) for current flow (CFS) and water temp (C)
- USGS NWIS Daily Values (DV) for the 30-day daily-flow sparklines
- BOR Hydromet `yakstats.txt` for the 6 Yakima-basin BOR gauges

`scripts/build.py` scans `index.html` for the gauge list and rewrites only the
per-gauge data values and the page timestamp; all layout, fishability thresholds,
and render logic stay untouched. Values are only overwritten when fresh data is
available, so a source outage leaves the last-known reading in place rather than
blanking a gauge. (BOR occasionally serves an empty file for a few hours; that is
handled the same way.)

Run the rewrite logic offline against the current file:

```
python scripts/build.py selftest
```

Trigger a refresh manually from the Actions tab (Run workflow) or wait for the cron.

## Cowork artifact

The Cowork `river-flows` artifact is separate from this repo. With Actions owning the
data, the Cowork task can be slimmed to: pull this repo's `index.html` and call
`update_artifact`, which removes its Chrome dependency too.
