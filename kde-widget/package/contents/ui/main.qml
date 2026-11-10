// Foundry VTT Players: how many people are connected to a Foundry VTT server.
//
// Reads Foundry's own status page (GET /api/status), which any user can read,
// so it works whichever system user runs Foundry:
//   {"active":true,"version":"14.370","world":"…","system":"dnd5e",
//    "systemVersion":"6.0.6","users":2,"uptime":…}
// "users" is the number of people connected to the world right now. If a
// server's page has no count, the widget can count connections to Foundry's
// port with `ss` instead (that sees a reverse proxy, not players, when one is
// in front of Foundry).
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    // State
    property int users: -1          // -1: unknown
    property bool reachable: false  // the status page answered
    property bool worldActive: false
    property string world: ""
    property string version: ""
    property string system: ""
    property string howCounted: ""  // "status page" or "connections"
    property string problem: ""
    property date checkedAt: new Date(0)

    // ---- 24-hour history ----------------------------------------------------
    // The most people connected in each 5-minute slot, for the last 24 hours:
    // [{t: slot start (ms), v: people}]. Slots with no answer from the server
    // are missing (gaps in the chart). Saved in the widget's settings so it
    // survives a Plasma restart; started afresh when the status page changes.
    readonly property int slotMs: 5 * 60 * 1000
    readonly property int spanMs: 24 * 60 * 60 * 1000
    property var history: []
    property int historyRev: 0          // bumped on every change (redraws the chart)
    property bool showHistory: Plasmoid.configuration.showHistory

    function loadHistory() {
        try {
            const saved = JSON.parse(Plasmoid.configuration.history || "{}");
            history = saved.url === String(statusUrl) && Array.isArray(saved.slots) ? saved.slots : [];
        } catch (e) {
            history = [];
        }
        historyRev++;
    }
    function saveHistory() {
        Plasmoid.configuration.history = JSON.stringify({ url: String(statusUrl), slots: history });
    }
    function record(people) {
        if (people < 0) return;
        const now = Date.now();
        const slot = Math.floor(now / slotMs) * slotMs;
        const h = history.filter((s) => s.t > now - spanMs - slotMs);
        const last = h.length ? h[h.length - 1] : null;
        let changed = h.length !== history.length;
        if (last && last.t === slot) {
            if (people > last.v) { last.v = people; changed = true; }
        } else {
            h.push({ t: slot, v: people });
            changed = true;
        }
        history = h;
        if (changed) { historyRev++; saveHistory(); }
    }
    function peak() {
        let best = null;
        for (const s of history) if (!best || s.v > best.v) best = s;
        return best;
    }

    readonly property string statusUrl: Plasmoid.configuration.statusUrl
    readonly property bool online: reachable && worldActive
    // Foundry's logo, as the server itself serves it to browsers (not copied
    // into this widget): <server>/icons/vtt-512.png.
    readonly property string logoUrl: String(statusUrl).replace(/\/api\/status\/?$/, "") + "/icons/vtt-512.png"

    Plasmoid.icon: "applications-games"
    Plasmoid.status: online && users > 0 ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus

    toolTipMainText: i18n("Foundry VTT")
    toolTipSubText: summary()

    preferredRepresentation: compactRepresentation

    function prettyWorld(id) {
        return String(id || "").replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    }
    function countText() {
        if (!reachable) return "–";
        if (!worldActive) return "0";
        return users >= 0 ? String(users) : "?";
    }
    function summary() {
        if (!reachable) return problem || i18n("Can't reach the server");
        if (!worldActive) return i18n("Server up, no world running");
        const who = users < 0 ? i18n("Players: unknown")
            : i18np("1 person connected", "%1 people connected", users);
        return who + "\n" + prettyWorld(world)
            + (howCounted === "connections" ? "\n" + i18n("(counted from connections)") : "");
    }

    // ---- polling --------------------------------------------------------------
    function poll() {
        const xhr = new XMLHttpRequest();
        xhr.timeout = 8000;
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== XMLHttpRequest.DONE) return;
            root.checkedAt = new Date();
            if (xhr.status !== 200) {
                root.reachable = false;
                root.problem = xhr.status ? i18n("Status page answered %1", xhr.status) : i18n("Can't reach the server");
                countConnections();
                return;
            }
            let d = null;
            try { d = JSON.parse(xhr.responseText); } catch (e) { d = null; }
            if (!d || typeof d !== "object") {
                root.reachable = false;
                root.problem = i18n("The status page isn't Foundry's");
                return;
            }
            root.reachable = true;
            root.problem = "";
            root.worldActive = !!d.active;
            root.world = d.world || "";
            root.version = d.version || "";
            root.system = d.system || "";
            if (typeof d.users === "number") {
                root.users = d.users;
                root.howCounted = "status page";
                root.record(root.worldActive ? d.users : 0);
            } else if (!root.worldActive) {
                root.users = 0;
                root.howCounted = "status page";
                root.record(0);
            } else {
                root.users = -1;
                countConnections();
            }
        };
        xhr.open("GET", statusUrl);
        xhr.send();
    }

    // Fallback: established TCP connections to Foundry's port, by remote
    // address (a browser opens several connections; one address = one person).
    function countConnections() {
        if (!Plasmoid.configuration.useSocketFallback) return;
        const port = Number(Plasmoid.configuration.port) || 30000;
        exec.connectSource("ss -Htn state established '( sport = :" + port + " )'");
    }
    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            disconnectSource(source);
            if (data["exit code"] !== 0) return;
            const peers = new Set();
            for (const line of String(data.stdout || "").split("\n")) {
                const cols = line.trim().split(/\s+/);
                if (cols.length < 4) continue;
                const peer = cols[cols.length - 1];
                peers.add(peer.replace(/:\d+$/, "").replace(/^\[|\]$/g, ""));
            }
            if (root.users < 0 || root.howCounted !== "status page") {
                root.users = peers.size;
                root.howCounted = "connections";
                root.record(peers.size);
            }
        }
    }

    Timer {
        interval: Math.max(3, Plasmoid.configuration.interval) * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.poll()
    }
    onStatusUrlChanged: { loadHistory(); poll(); }
    Component.onCompleted: loadHistory()

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Check now")
            icon.name: "view-refresh"
            onTriggered: root.poll()
        },
        PlasmaCore.Action {
            text: i18n("24-hour history")
            icon.name: "office-chart-bar"
            checkable: true
            checked: root.showHistory
            onTriggered: {
                Plasmoid.configuration.showHistory = checked;
                if (checked) root.expanded = true;
            }
        },
        PlasmaCore.Action {
            text: i18n("Open Foundry")
            icon.name: "internet-web-browser"
            onTriggered: Qt.openUrlExternally(String(root.statusUrl).replace(/\/api\/status\/?$/, "/"))
        }
    ]

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

    // ---- 24-hour history chart ------------------------------------------------------
    // One bar per 5-minute slot, newest on the right; it scrolls left as time
    // passes. Hover a bar for its time and count.
    component HistoryChart: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            Kirigami.Heading {
                level: 4
                text: i18n("Last 24 hours")
            }
            Item { Layout.fillWidth: true }
            PlasmaComponents.Label {
                opacity: 0.7
                text: {
                    root.historyRev;  // re-evaluate on new data
                    const p = root.peak();
                    if (chart.hoverText) return chart.hoverText;
                    return p ? i18n("Peak %1 at %2", p.v, Qt.formatTime(new Date(p.t), "hh:mm")) : i18n("No data yet");
                }
            }
        }

        Canvas {
            id: chart
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit * 6
            property string hoverText: ""
            property real hoverX: -1

            function colorString(c, a) {
                return "rgba(" + Math.round(c.r * 255) + "," + Math.round(c.g * 255) + "," + Math.round(c.b * 255) + "," + a + ")";
            }
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            onHoverXChanged: requestPaint()
            Connections {
                target: root
                function onHistoryRevChanged() { chart.requestPaint(); }
            }
            // Scroll with the clock while the chart is open.
            Timer {
                interval: 60 * 1000
                repeat: true
                running: chart.visible && root.expanded
                onTriggered: chart.requestPaint()
            }

            onPaint: {
                const ctx = getContext("2d");
                ctx.reset();
                const w = width, h = height;
                const top = 12, base = h - 1;
                const now = Date.now(), start = now - root.spanMs;
                const slots = root.history.filter((s) => s.t + root.slotMs > start);
                let max = 1;
                for (const s of slots) max = Math.max(max, s.v);
                const text = Kirigami.Theme.textColor;
                const bar = Kirigami.Theme.highlightColor;

                // Guide lines: the top value and the middle (whole numbers only).
                ctx.strokeStyle = colorString(text, 0.15);
                ctx.lineWidth = 1;
                ctx.fillStyle = colorString(text, 0.55);
                ctx.font = "10px sans-serif";
                const levels = max >= 2 ? [max, Math.round(max / 2)] : [max];
                for (const lv of levels) {
                    const y = Math.round(base - (lv / max) * (base - top)) + 0.5;
                    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
                    ctx.fillText(String(lv), 2, y - 2);
                }
                // Hour marks every 6 hours of clock time.
                const sixH = 6 * 3600 * 1000;
                const tzOff = new Date().getTimezoneOffset() * 60000;
                for (let t = Math.ceil((start - tzOff) / sixH) * sixH + tzOff; t < now; t += sixH) {
                    const x = Math.round((t - start) / root.spanMs * w) + 0.5;
                    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, base); ctx.stroke();
                }
                // Bars.
                const bw = Math.max(1, root.slotMs / root.spanMs * w);
                const hoverT = hoverX < 0 ? -1 : start + hoverX / w * root.spanMs;
                for (const s of slots) {
                    const x = (s.t - start) / root.spanMs * w;
                    const hovered = hoverT >= s.t && hoverT < s.t + root.slotMs;
                    ctx.fillStyle = colorString(bar, hovered ? 1 : 0.75);
                    if (s.v > 0) {
                        const bh = Math.max(2, (s.v / max) * (base - top));
                        ctx.fillRect(x, base - bh, Math.max(1, bw - (bw > 3 ? 1 : 0)), bh);
                    } else {
                        // Server up, nobody on: a thin line, so it differs from a gap (no answer).
                        ctx.fillStyle = colorString(text, 0.3);
                        ctx.fillRect(x, base - 1, bw, 1);
                    }
                }
                // Baseline.
                ctx.fillStyle = colorString(text, 0.35);
                ctx.fillRect(0, base, w, 1);
            }

            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onPositionChanged: (mouse) => {
                    chart.hoverX = mouse.x;
                    const t = Date.now() - root.spanMs + mouse.x / chart.width * root.spanMs;
                    const slot = Math.floor(t / root.slotMs) * root.slotMs;
                    const s = root.history.find((x) => x.t === slot);
                    const when = Qt.formatTime(new Date(slot), "hh:mm");
                    chart.hoverText = s ? i18np("%2: 1 person", "%2: %1 people", s.v, when)
                                        : i18n("%1: no answer", when);
                }
                onExited: { chart.hoverX = -1; chart.hoverText = ""; }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            PlasmaComponents.Label { opacity: 0.6; font: Kirigami.Theme.smallFont; text: i18n("24 h ago") }
            Item { Layout.fillWidth: true }
            PlasmaComponents.Label { opacity: 0.6; font: Kirigami.Theme.smallFont; text: i18n("12 h ago") }
            Item { Layout.fillWidth: true }
            PlasmaComponents.Label { opacity: 0.6; font: Kirigami.Theme.smallFont; text: i18n("now") }
        }
    }

    // ---- panel / desktop icon with the count ------------------------------------
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
                url: root.logoUrl
                dim: !root.online
                Layout.preferredWidth: Math.min(compact.height, Kirigami.Units.iconSizes.medium)
                Layout.preferredHeight: Layout.preferredWidth
            }
            PlasmaComponents.Label {
                text: root.countText()
                font.bold: true
                font.pixelSize: Math.max(Kirigami.Theme.defaultFont.pixelSize, compact.height * 0.5)
                color: !root.reachable ? Kirigami.Theme.negativeTextColor
                    : root.online && root.users > 0 ? Kirigami.Theme.positiveTextColor
                    : Kirigami.Theme.textColor
            }
        }
    }

    // ---- popup with details -----------------------------------------------------
    fullRepresentation: ColumnLayout {
        Layout.preferredWidth: Kirigami.Units.gridUnit * (root.showHistory ? 26 : 16)
        Layout.minimumHeight: Kirigami.Units.gridUnit * 8
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            spacing: Kirigami.Units.smallSpacing
            Logo {
                url: root.logoUrl
                dim: !root.online
                Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                Layout.preferredHeight: Kirigami.Units.iconSizes.medium
            }
            Kirigami.Heading {
                level: 2
                text: i18n("Foundry VTT")
            }
        }
        RowLayout {
            spacing: Kirigami.Units.largeSpacing
            PlasmaComponents.Label {
                text: root.countText()
                font.pixelSize: Kirigami.Units.gridUnit * 2.5
                font.bold: true
                color: !root.reachable ? Kirigami.Theme.negativeTextColor
                    : root.online && root.users > 0 ? Kirigami.Theme.positiveTextColor
                    : Kirigami.Theme.textColor
            }
            PlasmaComponents.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: !root.reachable ? (root.problem || i18n("Can't reach the server"))
                    : !root.worldActive ? i18n("No world running")
                    : root.users === 1 ? i18n("person connected") : i18n("people connected")
            }
        }
        PlasmaComponents.Label {
            visible: root.reachable && root.worldActive
            Layout.fillWidth: true
            elide: Text.ElideRight
            text: i18n("World: %1", root.prettyWorld(root.world))
        }
        PlasmaComponents.Label {
            visible: root.reachable
            opacity: 0.7
            text: i18n("Foundry %1 · %2", root.version, root.system || "–")
        }
        PlasmaComponents.Label {
            visible: root.howCounted === "connections"
            opacity: 0.7
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: i18n("Counted from connections to the port (the status page gave no count).")
        }
        PlasmaComponents.Label {
            opacity: 0.6
            text: root.checkedAt.getTime() > 0 ? i18n("Checked %1", Qt.formatTime(root.checkedAt)) : i18n("Checking…")
        }
        HistoryChart {
            visible: root.showHistory
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.smallSpacing
        }
        Item { Layout.fillHeight: true }
        PlasmaComponents.Button {
            Layout.alignment: Qt.AlignRight
            icon.name: "view-refresh"
            text: i18n("Check now")
            onClicked: root.poll()
        }
    }
}
