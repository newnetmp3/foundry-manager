// Foundry VTT Players: how many people are connected to your Foundry VTT
// servers (several at once, e.g. komo and nier).
//
// Reads each server's own status page (GET /api/status), which any user can
// read, so it works whichever system user runs Foundry:
//   {"active":true,"version":"14.370","world":"…","system":"dnd5e",
//    "systemVersion":"6.0.6","users":2,"uptime":…}
// "users" is the number of people connected to the world right now. If a
// server's page has no count, the widget can count connections to Foundry's
// port with `ss` instead (only for servers on this computer; behind a reverse
// proxy that counts the proxy, not players).
//
// Who is connected comes from each server's pm2 log, where Foundry writes
//   "User authentication successful for user Nier", then
//   "Vended World data to User [ORwITUl8eeqkicFt]" when they join, and
//   "Logged user ORwITUl8eeqkicFt out of active World" when they leave.
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    // ---- servers ------------------------------------------------------------------
    // Settings hold one server per line: "name address [pm2 log]", e.g.
    //   komo http://localhost:30000/api/status /home/komovtt/.pm2/logs/komovtt-out.log
    //   nier https://nier.example.com
    // "/api/status" is added when missing, "http://" when there's no scheme.
    // The log (optional) is where the names of the people connected come from.
    readonly property var servers: parseServers(Plasmoid.configuration.servers)

    function parseServers(text) {
        const out = [];
        const names = {};
        for (const raw of String(text || "").split(/[\n;]/)) {
            const line = raw.trim();
            if (!line || line.startsWith("#")) continue;
            const parts = line.split(/\s+/);
            let log = "";
            if (parts.length > 1 && /^[~\/]/.test(parts[parts.length - 1])) log = parts.pop();
            let url = parts.length > 1 ? parts[parts.length - 1] : parts[0];
            if (!/^https?:\/\//i.test(url)) url = "http://" + url;
            if (!/\/api\/status\/?$/.test(url)) url = url.replace(/\/+$/, "") + "/api/status";
            let name = parts.length > 1 ? parts.slice(0, -1).join(" ") : hostOf(url);
            if (names[name]) name += " " + (++names[name]); else names[name] = 1;
            out.push({ name: name, url: url, key: name + "|" + url, log: /^[\w.\/~+@-]+$/.test(log) ? log : "" });
        }
        return out;
    }
    function hostOf(url) {
        const m = String(url).match(/^https?:\/\/([^/:]+|\[[^\]]+\])(?::(\d+))?/i);
        return m ? m[1] : String(url);
    }
    function siteUrl(url) { return String(url).replace(/\/api\/status\/?$/, "/"); }
    // Foundry's logo, as the server itself serves it to browsers.
    function logoUrlOf(url) { return String(url).replace(/\/api\/status\/?$/, "") + "/icons/vtt-512.png"; }
    // The port to count connections on, for a server on this computer only.
    function localPort(url) {
        const m = String(url).match(/^(https?):\/\/(localhost|127\.[\d.]+|\[::1\])(?::(\d+))?/i);
        if (!m) return 0;
        return m[3] ? Number(m[3]) : 30000;
    }

    // ---- state, one entry per server (same order as `servers`) --------------------
    // {name, key, url, users (-1 unknown), reachable, worldActive, world, version,
    //  system, howCounted ("status page"/"connections"), problem, checkedAt (ms),
    //  hist: {start: first slot (ms), vals: [people per slot, -1 = no answer]},
    //  log, logProblem, sessions: [{n: name, id: Foundry user id, s: joined (ms), e: left (ms, 0 = still on)}]}
    property var srv: []
    property int stateRev: 0            // bumped on every change (re-evaluates bindings)

    readonly property bool anyOnline: { stateRev; return srv.some((s) => s.reachable && s.worldActive); }
    readonly property int totalUsers: {
        stateRev;
        let n = 0;
        for (const s of srv) if (s.reachable && s.worldActive && s.users > 0) n += s.users;
        return n;
    }

    function setup() {
        if (srv.length) flushHistory();
        const saved = loadSaved();
        const list = [];
        for (const s of servers) {
            const h = saved[s.key];
            list.push({
                name: s.name, key: s.key, url: s.url, log: s.log, logProblem: "",
                sessions: h && Array.isArray(h.p) ? h.p.slice() : [],
                users: -1, reachable: false, worldActive: false, world: "", version: "", system: "",
                howCounted: "", problem: "", checkedAt: 0,
                hist: h && Array.isArray(h.v) ? { start: Number(h.s) || 0, vals: h.v.slice() } : { start: 0, vals: [] }
            });
        }
        srv = list;
        stateRev++;
        historyRev++;
        rebuildActions();
        pollAll();
    }
    onServersChanged: setup()
    Component.onCompleted: setup()
    Component.onDestruction: flushHistory()

    // ---- history: a month of 5-minute slots per server ----------------------------
    // The most people connected in each 5-minute slot, for the last 30 days.
    // Slots with no answer from the server are -1 (gaps in the chart). Saved in
    // the widget's settings so it survives a Plasma restart; a server whose name
    // or address changes starts a new history.
    readonly property real slotMs: 5 * 60 * 1000
    readonly property real dayMs: 24 * 60 * 60 * 1000
    readonly property real keepMs: 30 * dayMs
    property int historyRev: 0
    property bool showHistory: Plasmoid.configuration.showHistory

    function loadSaved() {
        let saved = {};
        try { saved = JSON.parse(Plasmoid.configuration.historyData || "{}") || {}; } catch (e) { saved = {}; }
        // Bring over the 24-hour history of the one-server version of this widget.
        if (Plasmoid.configuration.history) {
            try {
                const old = JSON.parse(Plasmoid.configuration.history);
                const s = servers.find((x) => x.url === String(old.url));
                if (s && !saved[s.key] && Array.isArray(old.slots) && old.slots.length) {
                    const start = old.slots[0].t;
                    const vals = [];
                    for (const o of old.slots) {
                        const i = Math.round((o.t - start) / slotMs);
                        while (vals.length < i) vals.push(-1);
                        vals[i] = o.v;
                    }
                    saved[s.key] = { s: start, v: vals };
                }
            } catch (e) {}
            Plasmoid.configuration.history = "";
        }
        return saved;
    }
    function flushHistory() {
        saveTimer.stop();
        const out = {};
        for (const s of srv)
            if (s.hist.vals.length || s.sessions.length) out[s.key] = { s: s.hist.start, v: s.hist.vals, p: s.sessions };
        Plasmoid.configuration.historyData = JSON.stringify(out);
    }
    // Settings are written at most every 30 seconds.
    Timer { id: saveTimer; interval: 30 * 1000; onTriggered: root.flushHistory() }

    function record(st, people) {
        if (people < 0) return;
        const h = st.hist;
        const slot = Math.floor(Date.now() / slotMs) * slotMs;
        if (!h.vals.length) {
            h.start = slot;
            h.vals = [people];
        } else {
            const i = Math.round((slot - h.start) / slotMs);
            if (i < 0) return;                               // the clock went back
            if (i < h.vals.length) {
                if (people <= h.vals[i]) return;
                h.vals[i] = people;
            } else {
                while (h.vals.length < i) h.vals.push(-1);
                h.vals.push(people);
            }
        }
        const oldest = slot - keepMs;
        if (h.start < oldest) {
            const drop = Math.ceil((oldest - h.start) / slotMs);
            h.vals.splice(0, drop);
            h.start += drop * slotMs;
        }
        historyRev++;
        if (!saveTimer.running) saveTimer.start();
    }
    // Most people in [t0, t1): -1 when the server never answered then.
    function maxBetween(h, t0, t1) {
        if (!h.vals.length) return -1;
        const i0 = Math.max(0, Math.ceil((t0 - h.start) / slotMs));
        const i1 = Math.min(h.vals.length, Math.ceil((t1 - h.start) / slotMs));
        let best = -1;
        for (let i = i0; i < i1; i++) if (h.vals[i] > best) best = h.vals[i];
        return best;
    }

    // ---- chart view: zoom with the mouse wheel, drag to move ----------------------
    readonly property real minSpanMs: 60 * 60 * 1000
    property real viewSpan: dayMs
    property real viewEnd: 0            // 0: the right edge follows the clock
    property real hoverT: -1            // time under the mouse (shared by all charts)
    readonly property bool live: viewEnd === 0

    function viewEndNow() { return viewEnd || Date.now(); }
    function setView(end, span) {
        const now = Date.now();
        span = Math.max(minSpanMs, Math.min(keepMs, span));
        end = Math.min(end, now);
        end = Math.max(end, now - keepMs + span);
        viewSpan = span;
        viewEnd = now - end < Math.max(slotMs / 2, span * 0.02) ? 0 : end;
    }
    // Zoom by `steps` wheel notches (up = in), keeping the time at `frac` across the chart still.
    function zoomAt(frac, steps) {
        const end = viewEndNow();
        const anchor = end - viewSpan + frac * viewSpan;
        const span = Math.max(minSpanMs, Math.min(keepMs, viewSpan * Math.pow(1.25, -steps)));
        setView(anchor + (1 - frac) * span, span);
    }
    function resetView() { setView(Date.now(), dayMs); }

    // Bar width: the smallest step that leaves bars at least 3 pixels wide.
    function bucketMs(span, width) {
        const steps = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440];
        const want = span / Math.max(1, width / 3);
        for (const m of steps) if (m * 60000 >= want) return m * 60000;
        return steps[steps.length - 1] * 60000;
    }
    // Steps are aligned to local clock time (and weeks to Mondays).
    function alignLocal(t, step) {
        const off = new Date(t).getTimezoneOffset() * 60000 + (step === 7 * dayMs ? 4 * dayMs : 0);
        return Math.floor((t - off) / step) * step + off;
    }
    function tickMs(span) {
        for (const m of [15, 30, 60, 180, 360, 720, 1440, 2880, 10080])
            if (span / (m * 60000) <= 7) return m * 60000;
        return 10080 * 60000;
    }
    function durationText(ms) {
        const min = Math.round(ms / 60000);
        if (min < 120) return i18np("1 minute", "%1 minutes", min);
        const h = Math.round(ms / 3600000);
        if (h < 48) return i18np("1 hour", "%1 hours", h);
        return i18np("1 day", "%1 days", Math.round(ms / dayMs));
    }
    function rangeText() {
        if (live) return i18n("Last %1", durationText(viewSpan));
        const fmt = viewSpan < 2 * dayMs ? "ddd d MMM hh:mm" : "ddd d MMM";
        return Qt.formatDateTime(new Date(viewEnd - viewSpan), fmt) + " – " + Qt.formatDateTime(new Date(viewEnd), fmt);
    }
    function bucketText(t, bucket) {
        if (bucket >= dayMs) return Qt.formatDateTime(new Date(t), "ddd d MMM");
        if (bucket > slotMs)
            return Qt.formatDateTime(new Date(t), "ddd hh:mm") + "–" + Qt.formatTime(new Date(t + bucket), "hh:mm");
        return Qt.formatDateTime(new Date(t), viewSpan > dayMs ? "ddd hh:mm" : "hh:mm");
    }

    // ---- text ---------------------------------------------------------------------
    function prettyWorld(id) {
        return String(id || "").replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    }
    function countText(s) {
        if (!s || !s.reachable) return "–";
        if (!s.worldActive) return "0";
        return s.users >= 0 ? String(s.users) : "?";
    }
    function countColor(s) {
        return !s || !s.reachable ? Kirigami.Theme.negativeTextColor
            : s.worldActive && s.users > 0 ? Kirigami.Theme.positiveTextColor
            : Kirigami.Theme.textColor;
    }
    function summary(s) {
        if (!s.reachable) return s.problem || i18n("Can't reach the server");
        if (!s.worldActive) return i18n("Server up, no world running");
        const who = s.users < 0 ? i18n("players unknown")
            : i18np("1 person connected", "%1 people connected", s.users);
        const names = onlineNames(s);
        return who + (names.length ? " (" + names.join(", ") + ")" : "") + " · " + prettyWorld(s.world)
            + (s.howCounted === "connections" ? " " + i18n("(counted from connections)") : "");
    }

    Plasmoid.icon: "applications-games"
    Plasmoid.status: totalUsers > 0 ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus

    toolTipMainText: i18n("Foundry VTT")
    toolTipSubText: { stateRev; return srv.map((s) => s.name + ": " + summary(s)).join("\n") || i18n("No servers set"); }

    preferredRepresentation: compactRepresentation

    // ---- polling --------------------------------------------------------------------
    function pollAll() { for (const s of srv) { poll(s); readLog(s); } }
    function poll(st) {
        const xhr = new XMLHttpRequest();
        xhr.timeout = 8000;
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== XMLHttpRequest.DONE) return;
            if (root.srv.indexOf(st) < 0) return;            // servers changed meanwhile
            st.checkedAt = Date.now();
            if (xhr.status !== 200) {
                st.reachable = false;
                st.problem = xhr.status ? i18n("Status page answered %1", xhr.status) : i18n("Can't reach the server");
                root.stateRev++;
                countConnections(st);
                return;
            }
            let d = null;
            try { d = JSON.parse(xhr.responseText); } catch (e) { d = null; }
            if (!d || typeof d !== "object") {
                st.reachable = false;
                st.problem = i18n("The status page isn't Foundry's");
                root.stateRev++;
                return;
            }
            st.reachable = true;
            st.problem = "";
            st.worldActive = !!d.active;
            st.world = d.world || "";
            st.version = d.version || "";
            st.system = d.system || "";
            if (typeof d.users === "number") {
                st.users = d.users;
                st.howCounted = "status page";
                root.record(st, st.worldActive ? d.users : 0);
                root.reconcile(st, st.worldActive ? d.users : 0);
            } else if (!st.worldActive) {
                st.users = 0;
                st.howCounted = "status page";
                root.record(st, 0);
                root.reconcile(st, 0);
            } else {
                st.users = -1;
                countConnections(st);
            }
            root.stateRev++;
        };
        xhr.open("GET", st.url);
        xhr.send();
    }

    // Fallback: established TCP connections to Foundry's port, by remote address
    // (a browser opens several connections; one address = one person).
    function countConnections(st) {
        if (!Plasmoid.configuration.useSocketFallback) return;
        const port = localPort(st.url);
        if (!port) return;
        run("ss -Htn state established '( sport = :" + port + " )'", (data) => {
            if (root.srv.indexOf(st) < 0 || data["exit code"] !== 0) return;
            const peers = new Set();
            for (const line of String(data.stdout || "").split("\n")) {
                const cols = line.trim().split(/\s+/);
                if (cols.length < 4) continue;
                const peer = cols[cols.length - 1];
                peers.add(peer.replace(/:\d+$/, "").replace(/^\[|\]$/g, ""));
            }
            if (st.users < 0 || st.howCounted !== "status page") {
                st.users = peers.size;
                st.howCounted = "connections";
                root.record(st, peers.size);
                root.stateRev++;
            }
        });
    }

    // Run a command; `done` gets its {stdout, stderr, "exit code"}.
    property var execWaiting: ({})      // command → done
    function run(cmd, done) {
        execWaiting[cmd] = done;
        exec.connectSource(cmd);
    }
    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            disconnectSource(source);
            const done = root.execWaiting[source];
            delete root.execWaiting[source];
            if (done) done(data);
        }
    }

    // ---- who is connected, from the pm2 log ---------------------------------------------
    // Only the join/leave lines are read (the last 3000 of them: weeks of play).
    function readLog(st) {
        if (!st.log) return;
        const q = "'" + st.log.replace(/^~\//, "") + "'";
        const path = st.log.startsWith("~/") ? "\"$HOME\"/" + q : q;
        run("if [ -r " + path + " ]; then grep -aE 'User authentication successful for user|Vended World data to User|out of active World' "
            + path + " | tail -n 3000; else echo __UNREADABLE__; fi", (data) => {
            if (root.srv.indexOf(st) < 0) return;
            const out = String(data.stdout || "");
            const problem = out.startsWith("__UNREADABLE__") ? i18n("Can't read %1", st.log) : "";
            if (problem !== st.logProblem) { st.logProblem = problem; root.stateRev++; }
            if (!problem) applyLog(st, out);
        });
    }
    function logTime(line) {
        const m = line.match(/(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)/);
        return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() : 0;
    }
    function applyLog(st, text) {
        const known = {};                       // id → name, from earlier sessions
        for (const p of st.sessions) known[p.id] = p.n;
        const open = {};                        // id → session, while reading
        const found = [];
        let pendingName = "";
        for (const line of text.split("\n")) {
            const t = logTime(line);
            if (!t) continue;
            let m;
            if ((m = line.match(/User authentication successful for user (.+?)\s*(?:"|$)/))) {
                pendingName = m[1].trim();
            } else if ((m = line.match(/Vended World data to User \[(\w+)\]/))) {
                const id = m[1];
                const name = pendingName || known[id] || id;
                known[id] = name;
                pendingName = "";
                if (open[id]) continue;             // a reload: still the same visit
                open[id] = { n: name, id: id, s: t, e: 0 };
                found.push(open[id]);
            } else if ((m = line.match(/Logged user (\w+) out of active World/))) {
                if (open[m[1]]) { open[m[1]].e = t; delete open[m[1]]; }
            }
        }
        // Merge with what we have. A visit we closed ourselves (the status page
        // said fewer people were on) stays closed.
        const byKey = {};
        for (const p of st.sessions) byKey[p.id + "@" + p.s] = p;
        let changed = false;
        for (const f of found) {
            const p = byKey[f.id + "@" + f.s];
            if (!p) { st.sessions.push(f); changed = true; }
            else if (f.e && p.e !== f.e) { p.e = f.e; changed = true; }
            else if (p.n !== f.n && f.n !== f.id) { p.n = f.n; changed = true; }
        }
        const oldest = Date.now() - keepMs;
        const kept = st.sessions.filter((p) => !p.e || p.e > oldest);
        if (kept.length !== st.sessions.length) { st.sessions = kept; changed = true; }
        if (changed) {
            st.sessions.sort((a, b) => a.s - b.s);
            stateRev++;
            historyRev++;
            if (!saveTimer.running) saveTimer.start();
        }
    }
    // The status page has the real count: when fewer people are on than the log
    // says (someone dropped without a leave line, or Foundry restarted), close the
    // longest-running visits. Visits that began in the last minute are left alone,
    // since the log may be newer than the status page.
    function reconcile(st, count) {
        const now = Date.now();
        const open = st.sessions.filter((p) => !p.e);
        let extra = open.length - count;
        if (extra <= 0) return;
        let changed = false;
        for (const p of open) {
            if (extra <= 0) break;
            if (p.s > now - 60000) continue;
            p.e = now;
            extra--;
            changed = true;
        }
        if (changed) {
            historyRev++;
            if (!saveTimer.running) saveTimer.start();
        }
    }
    function onlineNames(s) {
        if (!s || !s.reachable || !s.worldActive) return [];
        return [...new Set(s.sessions.filter((p) => !p.e).map((p) => p.n))];
    }
    // Who was on at some point in [t0, t1).
    function namesBetween(s, t0, t1) {
        const now = Date.now();
        return [...new Set(s.sessions.filter((p) => p.s < t1 && (p.e || now) > t0).map((p) => p.n))];
    }

    Timer {
        interval: Math.max(3, Plasmoid.configuration.interval) * 1000
        running: true
        repeat: true
        onTriggered: root.pollAll()
    }

    // ---- right-click menu -------------------------------------------------------------
    PlasmaCore.Action {
        id: checkNowAction
        text: i18n("Check now")
        icon.name: "view-refresh"
        onTriggered: root.pollAll()
    }
    PlasmaCore.Action {
        id: historyAction
        text: i18n("History")
        icon.name: "office-chart-bar"
        checkable: true
        checked: root.showHistory
        onTriggered: {
            Plasmoid.configuration.showHistory = checked;
            if (checked) root.expanded = true;
        }
    }
    Component {
        id: openActionComponent
        PlasmaCore.Action {
            property string url: ""
            icon.name: "internet-web-browser"
            onTriggered: Qt.openUrlExternally(url)
        }
    }
    property var openActions: []
    function rebuildActions() {
        for (const a of openActions) a.destroy();
        openActions = servers.map((s) => openActionComponent.createObject(root, {
            text: i18n("Open %1", s.name), url: siteUrl(s.url)
        }));
        Plasmoid.contextualActions = [checkNowAction, historyAction].concat(openActions);
    }

    // Foundry's logo from the server; the theme's games icon until it loads (or
    // if it can't). Faded while the server is down or no world is running.
    component Logo: Item {
        property string url: ""
        property bool dim: false
        opacity: dim ? 0.45 : 1
        Image {
            id: logoImage
            anchors.fill: parent
            source: parent.url
            sourceSize.width: 128
            sourceSize.height: 128
            fillMode: Image.PreserveAspectFit
            smooth: true
            mipmap: true
            asynchronous: true
            cache: true
            visible: status === Image.Ready
        }
        Kirigami.Icon {
            anchors.fill: parent
            source: "applications-games"
            visible: logoImage.status !== Image.Ready
        }
    }

    function colorString(c, a) {
        return "rgba(" + Math.round(c.r * 255) + "," + Math.round(c.g * 255) + "," + Math.round(c.b * 255) + "," + a + ")";
    }

    // ---- history chart: every server on one chart ------------------------------------------
    // Side-by-side bars per time step, one colour per server, newest on the right.
    // Each bar is the most people connected in its time (5 minutes when zoomed
    // in, up to a day when zoomed out). Wheel: zoom; drag or Shift+wheel: move;
    // double-click: last 24 h.
    function serverColor(i) {
        const c = [Kirigami.Theme.highlightColor, Kirigami.Theme.neutralTextColor,
                   Kirigami.Theme.positiveTextColor, Qt.rgba(0.61, 0.35, 0.71, 1),
                   Qt.rgba(0.91, 0.26, 0.58, 1), Qt.rgba(0, 0.66, 0.66, 1)];
        return c[i % c.length];
    }
    // Wide enough that each server's bar gets at least 3 pixels.
    function chartBucketMs(width) { return bucketMs(viewSpan, width / Math.max(1, servers.length)); }

    component HistoryChart: ColumnLayout {
        id: chartBox
        spacing: Kirigami.Units.smallSpacing

        // Legend: each server's colour and name, with its peak over the range
        // shown, or its count at the time under the mouse.
        Flow {
            Layout.fillWidth: true
            spacing: Kirigami.Units.largeSpacing
            PlasmaComponents.Label {
                visible: root.hoverT >= 0
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                text: {
                    if (root.hoverT < 0) return "";
                    const bucket = root.chartBucketMs(chart.width);
                    return root.bucketText(root.alignLocal(root.hoverT, bucket), bucket);
                }
            }
            Repeater {
                model: root.servers.length
                delegate: Row {
                    required property int index
                    spacing: Math.round(Kirigami.Units.smallSpacing * 1.5)
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Kirigami.Units.gridUnit * 0.6
                        height: width
                        radius: 2
                        color: root.serverColor(index)
                    }
                    PlasmaComponents.Label {
                        font: Kirigami.Theme.smallFont
                        text: {
                            root.historyRev; root.viewSpan; root.viewEnd; chart.tick; root.stateRev;
                            const s = root.srv[index];
                            if (!s) return "";
                            const bucket = root.chartBucketMs(chart.width);
                            if (root.hoverT >= 0) {
                                const t = root.alignLocal(root.hoverT, bucket);
                                const v = root.maxBetween(s.hist, t, t + bucket);
                                if (v < 0) return i18n("%1: no answer", s.name);
                                const who = root.namesBetween(s, t, t + bucket);
                                return (bucket > root.slotMs ? i18n("%1: up to %2", s.name, v) : i18n("%1: %2", s.name, v))
                                    + (who.length ? " (" + who.join(", ") + ")" : "");
                            }
                            const end = root.viewEndNow(), start = end - root.viewSpan;
                            let best = -1, bestT = 0;
                            for (let i = 0; i < s.hist.vals.length; i++) {
                                const t = s.hist.start + i * root.slotMs;
                                if (t + root.slotMs <= start || t >= end) continue;
                                if (s.hist.vals[i] > best) { best = s.hist.vals[i]; bestT = t; }
                            }
                            return best >= 0
                                ? i18n("%1: peak %2 · %3", s.name, best, Qt.formatDateTime(new Date(bestT), root.viewSpan > root.dayMs ? "ddd d MMM hh:mm" : "hh:mm"))
                                : i18n("%1: no data", s.name);
                        }
                    }
                }
            }
        }

        Canvas {
            id: chart
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit * 7
            property int tick: 0

            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            Connections {
                target: root
                function onHistoryRevChanged() { chart.requestPaint(); }
                function onViewSpanChanged() { chart.requestPaint(); }
                function onViewEndChanged() { chart.requestPaint(); }
                function onHoverTChanged() { chart.requestPaint(); }
                function onSrvChanged() { chart.requestPaint(); }
            }
            // Scroll with the clock while the chart is open.
            Timer {
                interval: 60 * 1000
                repeat: true
                running: chart.visible && root.expanded
                onTriggered: { chart.tick++; chart.requestPaint(); }
            }

            onPaint: {
                const ctx = getContext("2d");
                ctx.reset();
                const list = root.srv;
                const n = list.length;
                if (!n) return;
                const w = width, h = height;
                const top = 12, base = h - 1;
                const end = root.viewEndNow(), span = root.viewSpan, start = end - span;
                const bucket = root.chartBucketMs(w);
                const xOf = (t) => (t - start) / span * w;
                const text = Kirigami.Theme.textColor;

                // [{t, v: [people per server, -1 = no answer]}]
                const buckets = [];
                let max = 1;
                for (let t = root.alignLocal(start, bucket); t < end; t += bucket) {
                    const v = list.map((s) => root.maxBetween(s.hist, t, t + bucket));
                    buckets.push({ t: t, v: v });
                    for (const x of v) if (x > max) max = x;
                }

                // Guide lines: the top value and the middle (whole numbers only).
                ctx.strokeStyle = root.colorString(text, 0.15);
                ctx.lineWidth = 1;
                ctx.fillStyle = root.colorString(text, 0.55);
                ctx.font = "10px sans-serif";
                const levels = max >= 2 ? [max, Math.round(max / 2)] : [max];
                for (const lv of levels) {
                    const y = Math.round(base - (lv / max) * (base - top)) + 0.5;
                    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
                    ctx.fillText(String(lv), 2, y - 2);
                }
                // Time marks (the same as the labels under the chart).
                const step = root.tickMs(span);
                for (let t = root.alignLocal(start, step) + step; t < end; t += step) {
                    const x = Math.round(xOf(t)) + 0.5;
                    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, base); ctx.stroke();
                }
                // Bars: one group per time step, one bar per server in its colour.
                const groupW = bucket / span * w;
                const gap = groupW > 4 * n ? 1 : 0;           // space between groups
                const bw = Math.max(1, (groupW - gap) / n);
                const hoverB = root.hoverT < 0 ? -1 : root.alignLocal(root.hoverT, bucket);
                const colors = list.map((s, i) => root.serverColor(i));
                for (const b of buckets) {
                    const x0 = xOf(b.t);
                    const hovered = b.t === hoverB;
                    for (let i = 0; i < n; i++) {
                        const v = b.v[i];
                        if (v < 0) continue;
                        const x = x0 + i * bw;
                        if (v > 0) {
                            const bh = Math.max(2, (v / max) * (base - top));
                            ctx.fillStyle = root.colorString(colors[i], hovered ? 1 : 0.8);
                            ctx.fillRect(x, base - bh, Math.max(1, bw), bh);
                        } else {
                            // Server up, nobody on: a thin line, so it differs from a gap (no answer).
                            ctx.fillStyle = root.colorString(colors[i], hovered ? 0.7 : 0.4);
                            ctx.fillRect(x, base - 2, Math.max(1, bw), 2);
                        }
                    }
                }
                // Hover line.
                if (root.hoverT >= 0) {
                    ctx.fillStyle = root.colorString(text, 0.4);
                    ctx.fillRect(Math.round(xOf(root.hoverT)), top, 1, base - top);
                }
                // Baseline.
                ctx.fillStyle = root.colorString(text, 0.35);
                ctx.fillRect(0, base, w, 1);
            }

            ChartMouse { anchors.fill: parent }
        }
    }

    // Mouse handling shared by the chart and the time axis.
    component ChartMouse: MouseArea {
        id: ma
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton
        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.CrossCursor
        property real pressX: 0
        property real pressEnd: 0

        function timeAt(x) { return root.viewEndNow() - root.viewSpan + x / width * root.viewSpan; }
        onPositionChanged: (mouse) => {
            if (pressed) root.setView(pressEnd - (mouse.x - pressX) / width * root.viewSpan, root.viewSpan);
            root.hoverT = timeAt(Math.max(0, Math.min(width - 1, mouse.x)));
        }
        onPressed: (mouse) => { pressX = mouse.x; pressEnd = root.viewEndNow(); }
        onExited: root.hoverT = -1
        onDoubleClicked: root.resetView()
        onWheel: (wheel) => {
            const dx = wheel.angleDelta.x, dy = wheel.angleDelta.y;
            if ((wheel.modifiers & Qt.ShiftModifier) || Math.abs(dx) > Math.abs(dy)) {
                // Move: up/left goes back in time.
                const d = dx !== 0 ? dx : dy;
                root.setView(root.viewEndNow() - d / 120 * root.viewSpan * 0.1, root.viewSpan);
            } else {
                root.zoomAt(Math.max(0, Math.min(1, wheel.x / width)), dy / 120);
            }
            root.hoverT = timeAt(Math.max(0, Math.min(width - 1, wheel.x)));
            wheel.accepted = true;
        }
    }

    // ---- the history chart, with the shown range and a time axis ----------------------
    component HistoryCharts: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            Kirigami.Heading {
                level: 4
                text: { axis.tick; return root.rangeText(); }
            }
            Item { Layout.fillWidth: true }
            PlasmaComponents.ToolButton {
                visible: !root.live || Math.abs(root.viewSpan - root.dayMs) > 1000
                icon.name: "zoom-original"
                text: i18n("Last 24 h")
                display: PlasmaComponents.AbstractButton.TextBesideIcon
                onClicked: root.resetView()
            }
        }

        HistoryChart { Layout.fillWidth: true }

        Canvas {
            id: axis
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit
            property int tick: 0
            onWidthChanged: requestPaint()
            Connections {
                target: root
                function onViewSpanChanged() { axis.requestPaint(); }
                function onViewEndChanged() { axis.requestPaint(); }
            }
            Timer {
                interval: 60 * 1000
                repeat: true
                running: axis.visible && root.expanded
                onTriggered: { axis.tick++; axis.requestPaint(); }
            }
            onPaint: {
                const ctx = getContext("2d");
                ctx.reset();
                const w = width, h = height;
                const end = root.viewEndNow(), span = root.viewSpan, start = end - span;
                const step = root.tickMs(span);
                ctx.fillStyle = root.colorString(Kirigami.Theme.textColor, 0.6);
                ctx.font = "10px sans-serif";
                ctx.textBaseline = "middle";
                let lastRight = -1;
                for (let t = root.alignLocal(start, step) + step; t < end; t += step) {
                    const d = new Date(t);
                    const midnight = d.getHours() === 0 && d.getMinutes() === 0;
                    const label = step >= root.dayMs ? Qt.formatDate(d, "d MMM")
                        : midnight ? Qt.formatDate(d, "ddd d") : Qt.formatTime(d, "hh:mm");
                    const tw = ctx.measureText(label).width;
                    const x = Math.max(0, Math.min(w - tw, (t - start) / span * w - tw / 2));
                    if (x <= lastRight + 4) continue;
                    ctx.fillText(label, x, h / 2);
                    lastRight = x + tw;
                }
            }
            ChartMouse { anchors.fill: parent }
        }

        PlasmaComponents.Label {
            Layout.fillWidth: true
            opacity: 0.5
            font: Kirigami.Theme.smallFont
            wrapMode: Text.Wrap
            text: i18n("Scroll to zoom (1 hour to 30 days) · drag or Shift+scroll to move · double-click for the last 24 hours")
        }
    }

    // ---- panel / desktop icon with each server's count ---------------------------------
    compactRepresentation: MouseArea {
        id: compact
        Layout.minimumWidth: row.implicitWidth
        Layout.preferredWidth: row.implicitWidth + Kirigami.Units.smallSpacing * 2
        hoverEnabled: true
        onClicked: root.expanded = !root.expanded

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: Kirigami.Units.smallSpacing
            Logo {
                url: root.servers.length ? root.logoUrlOf(root.servers[0].url) : ""
                dim: !root.anyOnline
                Layout.preferredWidth: Math.min(compact.height, Kirigami.Units.iconSizes.medium)
                Layout.preferredHeight: Layout.preferredWidth
            }
            Repeater {
                model: root.servers.length
                delegate: RowLayout {
                    required property int index
                    spacing: Math.round(Kirigami.Units.smallSpacing / 2)
                    PlasmaComponents.Label {
                        visible: root.servers.length > 1
                        opacity: 0.7
                        font: Kirigami.Theme.smallFont
                        text: { root.stateRev; return root.srv[index] ? root.srv[index].name : ""; }
                    }
                    PlasmaComponents.Label {
                        text: { root.stateRev; return root.countText(root.srv[index]); }
                        font.bold: true
                        font.pixelSize: Math.max(Kirigami.Theme.defaultFont.pixelSize, compact.height * 0.5)
                        color: { root.stateRev; return root.countColor(root.srv[index]); }
                    }
                }
            }
        }
    }

    // ---- popup with details -------------------------------------------------------------
    fullRepresentation: ColumnLayout {
        Layout.preferredWidth: Kirigami.Units.gridUnit * (root.showHistory ? 30 : 20)
        Layout.minimumHeight: Kirigami.Units.gridUnit * 8
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading {
            level: 2
            text: i18n("Foundry VTT")
        }
        PlasmaComponents.Label {
            visible: root.servers.length === 0
            text: i18n("No servers set: right-click → Configure.")
        }

        Repeater {
            model: root.servers.length
            delegate: RowLayout {
                id: serverRow
                required property int index
                readonly property var st: { root.stateRev; return root.srv[index] || null; }
                Layout.fillWidth: true
                spacing: Kirigami.Units.largeSpacing

                Logo {
                    url: serverRow.st ? root.logoUrlOf(serverRow.st.url) : ""
                    dim: !(serverRow.st && serverRow.st.reachable && serverRow.st.worldActive)
                    Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                    Layout.alignment: Qt.AlignTop
                }
                PlasmaComponents.Label {
                    Layout.alignment: Qt.AlignTop
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 1.5
                    horizontalAlignment: Text.AlignHCenter
                    text: root.countText(serverRow.st)
                    font.pixelSize: Kirigami.Units.gridUnit * 2
                    font.bold: true
                    color: root.countColor(serverRow.st)
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    Kirigami.Heading {
                        level: 4
                        text: serverRow.st ? serverRow.st.name : ""
                    }
                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: {
                            const s = serverRow.st;
                            if (!s) return "";
                            if (!s.reachable) return s.checkedAt ? (s.problem || i18n("Can't reach the server")) : i18n("Checking…");
                            if (!s.worldActive) return i18n("No world running");
                            return (s.users === 1 ? i18n("person connected") : i18n("people connected"))
                                + " · " + root.prettyWorld(s.world);
                        }
                    }
                    PlasmaComponents.Label {
                        readonly property var names: root.onlineNames(serverRow.st)
                        visible: !!serverRow.st && (names.length > 0 || serverRow.st.logProblem !== "")
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        color: names.length ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.neutralTextColor
                        text: !serverRow.st ? ""
                            : names.length ? i18n("On: %1", names.join(", "))
                            : serverRow.st.logProblem
                    }
                    PlasmaComponents.Label {
                        visible: !!serverRow.st && serverRow.st.reachable
                        opacity: 0.7
                        font: Kirigami.Theme.smallFont
                        text: serverRow.st ? i18n("Foundry %1 · %2", serverRow.st.version, serverRow.st.system || "–")
                            + (serverRow.st.howCounted === "connections" ? " · " + i18n("counted from connections") : "") : ""
                    }
                }
                PlasmaComponents.ToolButton {
                    Layout.alignment: Qt.AlignTop
                    icon.name: "internet-web-browser"
                    onClicked: Qt.openUrlExternally(root.siteUrl(serverRow.st.url))
                    PlasmaComponents.ToolTip { text: i18n("Open %1", serverRow.st ? serverRow.st.name : "") }
                }
            }
        }

        HistoryCharts {
            visible: root.showHistory && root.servers.length > 0
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.smallSpacing
        }
        Item { Layout.fillHeight: true }
        RowLayout {
            Layout.fillWidth: true
            PlasmaComponents.Label {
                opacity: 0.6
                text: {
                    root.stateRev;
                    const t = Math.max(0, ...root.srv.map((s) => s.checkedAt));
                    return t > 0 ? i18n("Checked %1", Qt.formatTime(new Date(t))) : i18n("Checking…");
                }
            }
            Item { Layout.fillWidth: true }
            PlasmaComponents.Button {
                icon.name: "view-refresh"
                text: i18n("Check now")
                onClicked: root.pollAll()
            }
        }
    }
}
