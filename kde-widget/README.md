# Foundry VTT Players (KDE Plasma 6 widget)

Shows how many people are connected to a Foundry VTT server, on the desktop
or in a panel, next to Foundry's logo (loaded from your own server's
`/icons/vtt-512.png`, the same file browsers get; the theme's games icon
stands in until it loads). Click it for the world, Foundry and system versions; right-click
for **Check now**, **24-hour history** and **Open Foundry**.

It reads Foundry's own status page, `/api/status`, which anyone can read, so
it works however Foundry is run (Node.js or Docker) and whichever system user
owns the Foundry process:

```json
{"active":true,"version":"14.370","world":"the-shattered-lands-act-1-cont","system":"dnd5e","systemVersion":"6.0.6","users":0,"uptime":9526054}
```

`users` is the number of people connected to the running world.

## Install

```bash
kpackagetool6 -t Plasma/Applet -i ~/local-git/foundry-manager/kde-widget/package
```

Then right-click the desktop (or a panel) → **Add Widgets…** → search
**Foundry VTT Players** → drag it out.

Update after a `git pull`:

```bash
kpackagetool6 -t Plasma/Applet -u ~/local-git/foundry-manager/kde-widget/package
```

Then restart Plasma so it loads the new version: `systemctl --user restart plasma-plasmashell`.

## Settings

Right-click the widget → **Configure Foundry VTT Players…**

- **Status page**: `http://localhost:30000/api/status` when Foundry runs on
  this computer on its default port; otherwise your server's address followed
  by `/api/status` (e.g. `https://example.com/api/status`).
- **Check every**: seconds between checks (default 15).
- **Fallback**: if a status page has no count, count connections to Foundry's
  port with `ss` (one address = one person). Behind a reverse proxy that
  counts the proxy, not players, so the status page is the better source.

## What it shows

- the number in green: people connected
- `0`: the server is up with nobody on, or no world is running (see the popup)
- `–` in red: the status page can't be reached

## 24-hour history

Right-click → **24-hour history** (tick it) to show a chart of the last 24
hours in the popup; untick it to hide it. Each bar is a 5-minute slot, showing
the most people connected during it; the newest is on the right, and the chart
scrolls left as time passes. A thin line means the server was up with nobody
on; a gap means the status page didn't answer. Hover a bar for its time and
count; the top right shows the day's peak.

The widget records while it runs (whether or not the chart is shown) and
saves the last 24 hours in its settings, so the history survives a Plasma
restart. Changing the status page starts a new history.

