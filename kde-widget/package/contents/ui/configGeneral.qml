import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    property alias cfg_servers: servers.text
    property alias cfg_interval: interval.value
    property alias cfg_useSocketFallback: useSocketFallback.checked

    Kirigami.FormLayout {
        QQC2.TextArea {
            id: servers
            Kirigami.FormData.label: i18n("Servers:")
            Layout.minimumWidth: Kirigami.Units.gridUnit * 34
            Layout.minimumHeight: Kirigami.Units.gridUnit * 4
            font.family: "monospace"
            placeholderText: "komo http://localhost:30000/api/status /home/komovtt/.pm2/logs/komovtt-out.log\nnier http://localhost:30001/api/status /home/komovtt/.pm2/logs/Nier-out.log"
        }
        QQC2.Label {
            Layout.maximumWidth: Kirigami.Units.gridUnit * 34
            wrapMode: Text.Wrap
            opacity: 0.7
            text: i18n("One server per line: a name, its status page, then (optionally) its pm2 log, which is where the names of the people connected come from. An address alone (localhost:30001, https://example.com) also works; /api/status is added. Each server's port is in the Port column of Foundry Server Manager; its pm2 log is ~/.pm2/logs/<pm2 name>-out.log of the user running it.")
        }
        QQC2.SpinBox {
            id: interval
            Kirigami.FormData.label: i18n("Check every (seconds):")
            from: 3
            to: 3600
        }
        QQC2.CheckBox {
            id: useSocketFallback
            Kirigami.FormData.label: i18n("Fallback:")
            text: i18n("Count connections to the port when a status page has no count\n(servers on this computer only)")
        }
    }
}
