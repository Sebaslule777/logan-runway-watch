# Logan Runway Watch

A phone app that shows which runways Boston Logan is using and tells you when
planes will be over the house.

**Open it:** https://sebaslule777.github.io/logan-runway-watch/
On iPhone tap Share, then **Add to Home Screen**. On Android tap the menu, then
**Add to Home screen**.

How it works: a scheduled GitHub job reads Logan's digital ATIS (the tower's
recorded broadcast of the runways in use) every 10 minutes, saves the reading
to `site/status.json` and `site/history.json`, publishes the page, and pushes
an alert to your phones when your runways switch on or off.

## One-time setup

1. **Turn on the website:** Settings, then Pages, then under "Build and
   deployment" set Source to **GitHub Actions**.
2. **Phone alerts:** install the free **ntfy** app on each phone and subscribe
   to a topic name nobody would guess, e.g. `logan-quiet-house-x7k2`. Then in
   this repository go to Settings, then Secrets and variables, then Actions,
   and add a secret named `NTFY_TOPIC` with that name.
3. **Your runways** (optional): on the same page, under the Variables tab, add
   `WATCH`, for example `dep:33L arr:27`. Without it the job watches 33L and 27
   for both landings and takeoffs.

The page has its own runway picker, saved on each phone. Keep it in step with
`WATCH` so the page and the alerts agree.

## Using the page

- **Top banner:** red "Planes over the house" or green "Quiet", and since when.
- **Runways in use:** landing runways look like yellow taxiway signs, departing
  ones like black location signs, and yours are ringed in red. The diagram shows
  every runway, which ones are active, and where the wind is coming from.
- **Quiet hours:** for each hour of the day, how often your runways were in use
  over the last 14 days. Use it to plan sleep and time outside.
- **They're loud now:** tap it whenever planes are loud. After a few taps it
  shows which runway is really yours and offers to watch only that one.
- **Loud stretches:** every stretch your runways were in use over the last week,
  with the overnight part called out. **Copy for a noise complaint** puts the
  list on your clipboard, ready to paste into Massport's complaint form.

## Complaining with evidence

Massport's Noise Abatement Office takes complaints at **(617) 561-3333**
(staffed weekdays during the day, voicemail otherwise) or through the noise
complaint form on massport.com under Community, then Noise Abatement. You
register once, and they need your address. Complaints are logged by address,
so a steady record of specific times counts for more than one angry call.

## Why it isn't just the wind

The FAA tower picks the runway setup, and wind is only one input. Others are
traffic, weather minimums, noise rules and closed runways. Most of the time
Logan's wind is light enough that several setups work, so the tower keeps the
one it has. That is why the same path over your house can last for hours or
days as the wind shifts. The runway rotation program meant to spread the noise
(PRAS) was abandoned in 2012 after it failed to cut noise.

## Good to know

- GitHub's scheduler can run late at busy times, so a reading may be 10 to 20
  minutes old. The page shows how old it is.
- In a public repository, GitHub pauses scheduled jobs after 60 days with no
  activity. The job commits its readings, which should count, but if updates
  stop, open the Actions tab and re-enable the workflow.
- The ATIS feed (`datis.clowd.io`) is a free community service. If it is down,
  the page keeps the last reading and says so.

## Computer version

`logan_runways.py` is the same tool as a command-line program (Python 3.8+, no
installs). Run it with `python3 logan_runways.py --help`.

| Command | What it does |
|---|---|
| `python3 logan_runways.py` | what's in use now |
| `... watch -r 33L -r 27 --ntfy TOPIC` | check every 5 minutes, alert on change |
| `... mark` | record "planes overhead right now" |
| `... report -d 7` | loud stretches for a complaint |
| `... stats` | setups seen and quiet hours |
| `python3 -m unittest` | run the tests |
