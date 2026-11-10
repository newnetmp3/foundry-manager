# Foundry Server Manager

A small desktop tool (Qt, fits in with KDE) for running several Foundry VTT
servers on one Linux machine with pm2, behind Caddy.

- **Lists** every Foundry server pm2 runs for the Foundry user: status,
  address, port, people connected, world, version, data folder.
- **Start / Stop / Restart**, **Log** (last 200 lines), **Open** in the browser.
- **Add server…** in one go:
  1. creates its install and data folders,
  2. installs Foundry from the timed **Node.js** download link (or a zip),
  3. writes `options.json` (port, address, behind Caddy with HTTPS),
  4. starts it under pm2 and saves the pm2 list (so it starts after a reboot),
  5. creates the **Cloudflare** DNS record (a CNAME to your existing server's
     address, proxied or not),
  6. adds a **Caddy** site that copies your existing site's block (same
     settings, new address and port). Caddy gets the HTTPS certificate itself.
- **Remove…**: takes a server out of pm2 and removes the Caddy site this tool
  wrote; its folders (worlds, licence) are kept unless you ask.

The licence key is entered in the browser the first time you open the new
server: use your second key there. Each server keeps its own key in its own
data folder.

## Install (Arch)

```bash
sudo pacman -S --needed pyside6 caddy unzip curl
```

Caddy must be running (`sudo systemctl enable --now caddy`).

Get it:

```bash
git clone https://github.com/newnetmp3/foundry-manager.git ~/local-git/foundry-manager
```

Install the `foundry-manager` command and the application-menu entry (no
sudo; it links into `~/.local/bin` and `~/.local/share/applications`):

```bash
~/local-git/foundry-manager/install.sh
```

Then run it from a terminal with `foundry-manager`, or from the application
menu (Games → Foundry Server Manager). `./install.sh --remove` undoes it.

Update later with `git -C ~/local-git/foundry-manager pull`.

## First run: the setup guide

The first time it starts, a step-by-step guide asks what the tool needs, one
question per page, each with an explanation of why:

1. **Which user runs Foundry.** It suggests users it finds already running
   Foundry or pm2, so usually you just confirm.
2. **Your existing server's address** (for example `play.example.com`), read
   from the running server where possible. New addresses point at it, and new
   Caddy sites copy its site block. Leave it empty if you have none yet.
3. **Cloudflare** (optional): your zone and an API token, with a **Test token**
   button. The page explains how to make a token (template **Edit zone DNS**).
   The token is stored in `~/.config/foundry-manager/cloudflare.ini`, readable
   only by you.
4. **Caddy**: what it does, and where your Caddyfile is (usually
   `/etc/caddy/Caddyfile`, found by itself). It checks Caddy is installed.
5. A summary to check before saving.

Nothing on the system changes during setup. Run it again any time with
**Setup guide…** on the toolbar, or change single answers under **Settings…**.

To set the user from the command line instead (saved for next time):

```bash
foundry-manager --user foundry
```

## Adding a server

**Add server…** walks through the same way: what you'll need, name and
address, the DNS record, port and folders, and last the Foundry download (last because the timed link expires after a few minutes).
A summary page shows everything before **Create server** changes anything.

## Caddy

Each new server gets its own file in a folder the Caddyfile
imports. If the Caddyfile imports none, the tool adds one line at the end:
`import /etc/caddy/conf.d/*.caddy` (keeping a backup as
`Caddyfile.foundry-manager.bak`). The new site copies your existing site's
block, so it forwards to the same host and keeps settings such as `encode`.
The tool checks it with `caddy validate` before reloading Caddy.

## If a step fails

The log window stops at the failed step and says why; the steps before it
stay done. Fix the cause (for example, install a missing program), then click
**Retry from “…”** to carry on from that step. Before anything starts, the tool
also checks that curl, unzip and Caddy are installed, and tells you the pacman
command if not.

If the window was closed, select the server and use **Set up web access…**:
it adds just the Caddy site for a server that's already running. It never
replaces a Caddy file it didn't write.

## How it uses sudo

The tool runs as you. Commands for the Foundry user run through
`sudo -u <Foundry user>`; Caddy and systemctl through `sudo`. It asks for
your password once, checks it with `sudo -v`, and keeps it in memory only for
this session; the commands themselves run with `sudo -n`, so the password is
never passed to them.

## Notes

- Cloudflare's SSL/TLS mode for the zone should be **Full** or **Full
  (strict)**, as it already is if your existing server works through it.
- Each server needs its own port (30000, 30001, …) and its own data folder.
- The Foundry VTT Players KDE widget (below) can watch each server: add a copy per server, with its own
  status address.

## Desktop widget

`kde-widget/` has a KDE Plasma 6 widget, **Foundry VTT Players**, that shows
how many people are connected to a server. See
[kde-widget/README.md](kde-widget/README.md).
