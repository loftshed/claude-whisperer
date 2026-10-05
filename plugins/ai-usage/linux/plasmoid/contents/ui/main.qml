import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as Plasma5Support

PlasmoidItem {
    id: root

    property var snapshot: null
    property bool refreshing: false
    property string failure: ""
    readonly property var accounts: snapshot ? snapshot.accounts : []
    readonly property bool vertical: Plasmoid.formFactor === PlasmaCore.Types.Vertical
    readonly property string summary: accounts.length ? accounts.map(function(account) {
        return account.short + " " + (account.headline ? account.headline.text : "?");
    }).join(vertical ? "\n" : "  ·  ") : "AI Usage"

    preferredRepresentation: compactRepresentation
    toolTipMainText: "AI Usage"
    toolTipSubText: failure || "Subscription quota remaining · Click for limits and reset times"

    function refresh(force) {
        if (refreshing) return;
        refreshing = true;
        failure = "";
        runner.connectSource('"$HOME/.local/bin/ai-usage" json' + (force ? " --refresh" : ""));
    }

    function resetText(window) {
        if (!window.resetsAt) return "Reset time unavailable";
        return "Resets " + new Date(window.resetsAt).toLocaleString(Qt.locale(), Locale.ShortFormat);
    }

    function quotaColor(window) {
        if (window.exhausted || window.blockedBy) return Kirigami.Theme.negativeTextColor;
        if (window.remainingPct < 20) return Kirigami.Theme.neutralTextColor;
        return Kirigami.Theme.positiveTextColor;
    }

    Plasma5Support.DataSource {
        id: runner
        engine: "executable"
        connectedSources: []
        onNewData: function(sourceName, data) {
            disconnectSource(sourceName);
            root.refreshing = false;
            if (data["exit code"] !== 0) {
                root.failure = "Could not read AI usage. Check that ai-usage is installed.";
                return;
            }
            try {
                const value = JSON.parse(data.stdout);
                if (value.schema !== "ai-usage.snapshot.v1" || !Array.isArray(value.accounts)) {
                    throw new Error("Unsupported quota response");
                }
                root.snapshot = value;
            } catch (error) {
                root.failure = "Could not read quota data: " + error.message;
            }
        }
    }

    Timer {
        interval: 120000
        repeat: true
        running: true
        onTriggered: root.refresh(false)
    }
    Component.onCompleted: refresh(false)

    compactRepresentation: PlasmaComponents.ToolButton {
        text: root.summary
        font.bold: true
        Accessible.name: "AI Usage: " + root.summary
        Layout.minimumWidth: root.vertical ? 0 : implicitWidth
        Layout.preferredWidth: implicitWidth
        onClicked: root.expanded = !root.expanded
    }

    fullRepresentation: ColumnLayout {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 22
        Layout.preferredWidth: Kirigami.Units.gridUnit * 26
        Layout.minimumHeight: Kirigami.Units.gridUnit * 20
        Layout.preferredHeight: Kirigami.Units.gridUnit * 30
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: Kirigami.Units.largeSpacing
            Kirigami.Heading {
                text: "AI Usage"
                level: 2
                Layout.fillWidth: true
            }
            QQC2.BusyIndicator {
                running: root.refreshing
                visible: running
                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                implicitHeight: implicitWidth
            }
            PlasmaComponents.ToolButton {
                icon.name: "view-refresh"
                text: "Refresh"
                enabled: !root.refreshing
                onClicked: root.refresh(true)
            }
        }

        QQC2.ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            QQC2.ScrollBar.horizontal.policy: QQC2.ScrollBar.AlwaysOff

            ColumnLayout {
                width: parent.width
                spacing: Kirigami.Units.largeSpacing

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    Layout.margins: Kirigami.Units.largeSpacing
                    visible: root.failure.length > 0
                    text: root.failure + (root.snapshot ? " Showing the previous reading." : "")
                    color: Kirigami.Theme.negativeTextColor
                    wrapMode: Text.Wrap
                }

                Repeater {
                    model: root.accounts
                    delegate: ColumnLayout {
                        id: accountSection
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.leftMargin: Kirigami.Units.largeSpacing
                        Layout.rightMargin: Kirigami.Units.largeSpacing

                        Kirigami.Heading {
                            Layout.fillWidth: true
                            level: 3
                            text: accountSection.modelData.label
                        }
                        PlasmaComponents.Label {
                            Layout.fillWidth: true
                            visible: accountSection.modelData.ok === false
                            text: accountSection.modelData.error || "Quota unavailable"
                            wrapMode: Text.Wrap
                            color: Kirigami.Theme.neutralTextColor
                        }
                        PlasmaComponents.Label {
                            visible: accountSection.modelData.ok === false && accountSection.modelData.windows.length > 0
                            text: "Showing the last successful reading"
                            color: Kirigami.Theme.neutralTextColor
                        }
                        Repeater {
                            model: accountSection.modelData.windows
                            delegate: ColumnLayout {
                                id: quota
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing
                                RowLayout {
                                    Layout.fillWidth: true
                                    PlasmaComponents.Label {
                                        Layout.fillWidth: true
                                        text: quota.modelData.label
                                        elide: Text.ElideRight
                                    }
                                    PlasmaComponents.Label {
                                        text: Math.floor(quota.modelData.remainingPct) + "% left"
                                        color: root.quotaColor(quota.modelData)
                                        font.bold: true
                                    }
                                }
                                QQC2.ProgressBar {
                                    Layout.fillWidth: true
                                    from: 0
                                    to: 100
                                    value: quota.modelData.remainingPct
                                    Accessible.name: quota.modelData.label + " quota remaining"
                                }
                                PlasmaComponents.Label {
                                    Layout.fillWidth: true
                                    text: root.resetText(quota.modelData)
                                    opacity: 0.7
                                    wrapMode: Text.Wrap
                                }
                                PlasmaComponents.Label {
                                    visible: Boolean(quota.modelData.blockedBy)
                                    text: "Unavailable until the " + quota.modelData.blockedBy + " limit resets"
                                    color: Kirigami.Theme.negativeTextColor
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                }
                            }
                        }
                        PlasmaComponents.Label {
                            visible: Boolean(accountSection.modelData.credits)
                            text: accountSection.modelData.credits ? "Credit balance: " + accountSection.modelData.credits.text : ""
                        }
                        Kirigami.Separator { Layout.fillWidth: true }
                    }
                }

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    Layout.margins: Kirigami.Units.largeSpacing
                    text: root.snapshot ? "Updated " + new Date(root.snapshot.generatedAt).toLocaleTimeString()
                        : "Reading quotas…"
                    opacity: 0.7
                }
            }
        }
    }
}
