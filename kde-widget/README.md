# Foundry VTT Players (KDE Plasma 6 widget)

Shows how many people are connected to each of your Foundry VTT servers (by
default **komo** and **nier**), on the desktop or in a panel, next to
Foundry's logo (loaded from your own server's `/icons/vtt-512.png`, the same
file browsers get; the theme's games icon stands in until it loads). Click it
for each server's world, Foundry and system versions; right-click for
**Check now**, **History** and **Open komo** / **Open nier**.

It reads each server's own status page, `/api/status`, which anyone can read,
so it needs no sudo and works whichever system user runs Foundry under pm2
(here, `komovtt`):

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

- **Servers**: one per line, a name and then its status page:

  ```
  komo http://localhost:30000/api/status
  nier http://localhost:30001/api/status
  ```

  Those are the defaults; set the ports to the ones in the **Port** column of
  Foundry Server Manager. An address alone (`localhost:30001`,
  `https://example.com`) also works; `/api/status` is added.
- **Check every**: seconds between checks (default 15).
- **Fallback**: if a status page has no count, count connections to that
  server's port with `ss` (one address = one person). Only for servers on
  this computer.

## What it shows

In the panel, each server's name and count:

- the number in green: people connected
- `0`: the server is up with nobody on, or no world is running (see the popup)
- `–` in red: the status page can't be reached

## History (up to a month)

Right-click → **History** (tick it) to show a chart per server in the popup;
untick it to hide it. It opens on the last 24 hours, newest on the right.

- **Scroll** over a chart to zoom, from 1 hour up to 30 days, around the
  point under the mouse.
- **Drag** (or Shift+scroll) to move back and forth in time.
- **Double-click** (or **Last 24 h**) to go back to the last 24 hours.

Each bar is the most people connected during its time: 5 minutes when zoomed
in, widening to 10 minutes, an hour… up to a day as you zoom out. A thin line
means the server was up with nobody on; a gap means its status page didn't
answer. Hover a bar for its time and count (all charts show the same moment);
otherwise each chart shows its peak over the range shown.

The widget records while it runs (whether or not the charts are shown) and
saves the last 30 days in its settings, so the history survives a Plasma
restart. A server whose name or address changes starts a new history; the
24-hour history of the one-server version is carried over to the server with
the same status page.
