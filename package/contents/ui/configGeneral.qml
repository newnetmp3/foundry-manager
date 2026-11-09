import QtQuick
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    property alias cfg_statusUrl: statusUrl.text
    property alias cfg_interval: interval.value
    property alias cfg_useSocketFallback: useSocketFallback.checked
    property alias cfg_port: port.value

    Kirigami.FormLayout {
        QQC2.TextField {
            id: statusUrl
            Kirigami.FormData.label: i18n("Status page:")
            placeholderText: "http://localhost:30000/api/status"
            Layout.minimumWidth: Kirigami.Units.gridUnit * 18
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
            text: i18n("Count connections to the port when the status page has no count")
        }
        QQC2.SpinBox {
            id: port
            Kirigami.FormData.label: i18n("Foundry port:")
            from: 1
            to: 65535
            enabled: useSocketFallback.checked
            textFromValue: (v) => String(v)
        }
    }
}
