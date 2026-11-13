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
            Layout.minimumWidth: Kirigami.Units.gridUnit * 22
            Layout.minimumHeight: Kirigami.Units.gridUnit * 4
            font.family: "monospace"
            placeholderText: "komo http://localhost:30000/api/status\nnier http://localhost:30001/api/status"
        }
        QQC2.Label {
            Layout.maximumWidth: Kirigami.Units.gridUnit * 22
            wrapMode: Text.Wrap
            opacity: 0.7
            text: i18n("One server per line: a name, then its status page. An address alone (localhost:30001, https://example.com) also works; /api/status is added. Each server's port is in the Port column of Foundry Server Manager.")
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
