# Foundry Server Manager

A small desktop tool (Qt, fits in with KDE) for running several Foundry VTT
servers on one Linux machine with pm2 and nginx.

- **Lists** every Foundry server pm2 runs for the Foundry user: status,
  address, port, people connected, world, version, data folder.
- **Start / Stop / Restart**, **Log** (last 200 lines), **Open** in the browser.
- **Add server…** in one go:
  1. creates its install and data folders,
  2. installs Foundry from the timed **Node.js** download link (or a zip),
  3. writes `options.json` (port, address, behind nginx with HTTPS),
  4. starts it under pm2 and saves the pm2 list (so it starts after a reboot),
  5. creates the **Cloudflare** DNS record (a CNAME to your existing server's
     address, proxied or not),
  6. gets a **Let's Encrypt** certificate (through Cloudflare DNS, or nginx),
  7. writes the nginx site next to your existing one, checks it, reloads nginx.
- **Remove…**: takes a server out of pm2 and removes the nginx site this tool
  wrote; its folders (worlds, licence) are kept unless you ask.

The licence key is entered in the browser the first time you open the new
server: use your second key there. Each server keeps its own key in its own
data folder.

## Install (Arch)

```bash
sudo pacman -S --needed pyside6 certbot certbot-nginx certbot-dns-cloudflare unzip curl
```

Get it:

```bash
git clone https://github.com/newnetmp3/foundry-manager.git ~/local-git/foundry-manager
```

Run it:

```bash
python3 ~/local-git/foundry-manager/foundry_manager.py
```

Or add it to the application menu:

```bash
cp ~/local-git/foundry-manager/foundry-manager.desktop ~/.local/share/applications/
```

Update later with `git -C ~/local-git/foundry-manager pull`.

## First run: Settings

- **Foundry runs as user**: the user pm2 runs Foundry as (`komosatuo`).
- **Existing server address**: `komosatuo.skotic.com`; new subdomains point
  at it (CNAME), and the new nginx site goes next to its nginx file.
- **Cloudflare zone**: `skotic.com`.
- **Cloudflare API token**: dash.cloudflare.com → My Profile → API Tokens →
  Create Token → **Edit zone DNS**, for the zone `skotic.com`. Stored in
  `~/.config/foundry-manager/cloudflare.ini`, readable only by you (certbot
  reads the same file).
- **Let's Encrypt email**: for expiry notices (optional).

## How it uses sudo

The tool runs as you. Commands for the Foundry user run through
`sudo -u komosatuo`; nginx, certbot and systemctl through `sudo`. It asks for
your password once, checks it with `sudo -v`, and keeps it in memory only for
this session; the commands themselves run with `sudo -n`, so the password is
never passed to them.

## Notes

- Cloudflare's SSL/TLS mode for the zone should be **Full** or **Full
  (strict)**, as it already is if your existing server works through it.
- Each server needs its own port (30000, 30001, …) and its own data folder.
- The [Foundry VTT Players](https://github.com/newnetmp3/roll20embetterment/tree/main/kde-widget)
  KDE widget can watch each server: add a copy per server, with its own
  status address.
