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

    readonly property string statusUrl: Plasmoid.configuration.statusUrl
    readonly property bool online: reachable && worldActive

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
    onStatusUrlChanged: poll()

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Check now")
            icon.name: "view-refresh"
            onTriggered: root.poll()
        },
        PlasmaCore.Action {
            text: i18n("Open Foundry")
            icon.name: "internet-web-browser"
            onTriggered: Qt.openUrlExternally(String(root.statusUrl).replace(/\/api\/status\/?$/, "/"))
        }
    ]

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
            Kirigami.Icon {
                source: "applications-games"
                Layout.preferredWidth: Math.min(compact.height, Kirigami.Units.iconSizes.medium)
                Layout.preferredHeight: Layout.preferredWidth
                opacity: root.online ? 1 : 0.45
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
        Layout.preferredWidth: Kirigami.Units.gridUnit * 16
        Layout.minimumHeight: Kirigami.Units.gridUnit * 8
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Heading {
            level: 2
            text: i18n("Foundry VTT")
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
        Item { Layout.fillHeight: true }
        PlasmaComponents.Button {
            Layout.alignment: Qt.AlignRight
            icon.name: "view-refresh"
            text: i18n("Check now")
            onClicked: root.poll()
        }
    }
}
