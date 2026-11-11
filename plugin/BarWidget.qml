import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import qs.Commons as Commons

// omaPOD in the bar: the episode that is playing, with controls in a popup.
//   left click   -> popup
//   middle click -> play / pause
//   right click  -> open omaPOD
//   scroll       -> rewind / fast-forward
// Talks to the app over its control socket (newline-delimited JSON).
Panel {
  id: root
  moduleName: "io.github.arikisonfire.pod"
  ipcTarget: "io.github.arikisonfire.pod"

  // ---- settings
  readonly property bool showTitle: root.setting("showTitle", true) !== false
  readonly property int maxTitleWidth: Math.max(60, Math.min(600, Number(root.setting("maxTitleWidth", 180)) || 180))
  readonly property bool hideWhenStopped: root.setting("hideWhenStopped", false) === true
  readonly property string defaultLaunchCommand: "uwsm-app -- omapod"
  readonly property string launchCommand: String(root.setting("launchCommand", "") || root.defaultLaunchCommand)
  readonly property string installGuide: "https://github.com/arikisonfire/omapod#install"
  // The widget needs the omaPOD app. Only the default command is checked: whoever set a command of their own
  // (a development copy, say) knows where the app is.
  property bool installed: true
  // From the plugin marketplace the widget comes with the whole app: omaPOD sits one folder up and starts from
  // there, with no installer run (bundled).
  property bool bundled: false
  readonly property string widgetDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, ""))
    .replace(/\/+$/, "")
  readonly property string appDir: widgetDir.substring(0, widgetDir.lastIndexOf("/"))
  readonly property string cacheDir: Quickshell.env("XDG_CACHE_HOME") || (Quickshell.env("HOME") + "/.cache")
  readonly property string socketPath: {
    var custom = String(root.setting("socketPath", "") || "")
    if (custom.length > 0) return custom
    return Quickshell.env("XDG_RUNTIME_DIR") + "/omapod/control.sock"
  }

  // omaPOD writes app.json next to its socket while it runs; only then is it worth connecting.
  readonly property string runtimeDir: socketPath.substring(0, socketPath.lastIndexOf("/"))
  readonly property string presencePath: runtimeDir + "/app.json"
  property bool watching: false
  property bool appPresent: false
  property int retryDelay: 1000
  property int retries: 0
  property var sock: null

  // ---- state pushed by omaPOD
  property bool connected: false
  property var st: ({})
  property var episode: null
  property real position: 0
  property real duration: 0
  property real speed: 1
  property double receivedAt: 0
  property real livePosition: 0
  property string chapter: ""
  property var sleep: null
  readonly property bool playing: connected && (st.status === "playing" || st.status === "loading")
  readonly property bool hasEpisode: connected && episode !== null
  readonly property var chapterStarts: st.chapter_starts || []
  readonly property var queue: st.queue || []
  readonly property var speedPresets: st.speed_presets || [1, 1.25, 1.5]
  readonly property color fg: root.bar ? root.bar.foreground : Commons.Color.foreground
  readonly property color dimFg: Qt.rgba(fg.r, fg.g, fg.b, 0.62)
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
  readonly property real progress: duration > 0 ? Math.max(0, Math.min(1, livePosition / duration)) : 0

  visible: !(root.hideWhenStopped && !root.hasEpisode)
  implicitWidth: visible ? barContent.implicitWidth + Style.space(14) : 0
  implicitHeight: root.bar ? root.bar.barSize : Style.bar.sizeHorizontal

  function send(cmd, args) {
    if (!root.sock || !root.connected) return
    var payload = {}
    if (args) for (var key in args) payload[key] = args[key]
    payload.cmd = cmd
    root.sock.write(JSON.stringify(payload) + "\n")
    root.sock.flush()
  }

  // A new Socket for every attempt: Quickshell before 41651d7d (Sep 2026) never retries a Socket
  // whose connection failed once.
  function connectSocket() {
    root.dropSocket()
    root.sock = socketComponent.createObject(root, { path: root.socketPath })
    // Connect only once root.sock points at it, the connection can succeed synchronously.
    if (root.sock) root.sock.connected = true
  }

  function dropSocket() {
    if (root.sock) root.sock.destroy()
    root.sock = null
    root.connected = false
    root.episode = null
    root.st = ({})
  }

  // FileView only notices app.json appearing when its folder exists, and after a reboot omaPOD
  // has not created it yet.
  function watchPresence() {
    root.watching = false
    root.appPresent = false
    if (makeRuntimeDir.running) return // its onExited starts over with the new folder
    makeRuntimeDir.dir = root.runtimeDir
    makeRuntimeDir.running = true
  }

  function checkInstalled() {
    if (root.launchCommand !== root.defaultLaunchCommand) {
      root.installed = true
      return
    }
    // Also while the app runs: a click after it quits needs to know where it came from.
    if (!findApp.running) findApp.running = true
  }

  Component.onCompleted: {
    root.watchPresence()
    root.checkInstalled()
  }
  onSocketPathChanged: {
    root.dropSocket()
    root.retries = 0
    root.watchPresence()
  }
  onLaunchCommandChanged: root.checkInstalled()
  onConnectedChanged: if (root.connected) root.installed = true
  // Looked up again whenever the popup opens: the app may have been installed meanwhile.
  onOpenedChanged: if (root.opened && !root.connected) root.checkInstalled()

  // What starting omaPOD runs. The bundled app keeps its bytecode in the cache: a file written under the plugins
  // folder makes the shell reload every plugin. -P keeps the working directory (the home folder) out of Python's
  // module path, so that a file there such as json.py cannot stand in for a module.
  function launchArgs() {
    if (root.bundled && root.launchCommand === root.defaultLaunchCommand)
      return ["uwsm-app", "--", "env", "PYTHONPYCACHEPREFIX=" + root.cacheDir + "/omapod/pycache",
              "PYTHONPATH=" + root.appDir, "python3", "-P", "-m", "omapod"]
    return ["sh", "-c", root.launchCommand]
  }

  function launch() {
    if (!root.installed) {
      root.open() // says how to install it
      return
    }
    Quickshell.execDetached(root.launchArgs())
  }

  function handleLine(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (msg.type === "state") {
      root.st = msg
      root.episode = msg.episode || null
      root.duration = msg.duration || 0
      root.speed = msg.speed || 1
      root.chapter = msg.chapter || ""
      root.sleep = msg.sleep || null
      root.setPosition(msg.position || 0)
    } else if (msg.type === "position") {
      root.duration = msg.duration || root.duration
      root.speed = msg.speed || root.speed
      if (msg.chapter !== undefined) root.chapter = msg.chapter || ""
      if (msg.sleep !== undefined) root.sleep = msg.sleep || null
      root.setPosition(msg.position || 0)
    } else if (msg.type === "bye") {
      // omaPOD quits: no reconnecting until it writes app.json again.
      root.appPresent = false
    }
  }

  function setPosition(ms) {
    root.position = ms
    root.receivedAt = Date.now()
    root.livePosition = ms
  }

  function clock(ms) {
    var total = Math.max(0, Math.floor(ms / 1000))
    var h = Math.floor(total / 3600)
    var m = Math.floor((total % 3600) / 60)
    var s = total % 60
    var mm = (h > 0 && m < 10) ? "0" + m : String(m)
    return (h > 0 ? h + ":" : "") + mm + ":" + (s < 10 ? "0" + s : String(s))
  }

  function speedLabel(value) {
    var text = Number(value).toFixed(2).replace(/0+$/, "").replace(/\.$/, ".0")
    return text + "×"
  }

  function chapterIndex(ms) {
    var index = -1
    for (var i = 0; i < chapterStarts.length; i++) {
      if (chapterStarts[i] <= ms + 50) index = i
      else break
    }
    return index
  }

  Component {
    id: socketComponent

    Socket {
      id: socket
      parser: SplitParser {
        onRead: function(data) { root.handleLine(data) }
      }
      onConnectionStateChanged: {
        if (root.sock !== socket) return
        root.connected = socket.connected
        if (socket.connected) {
          root.retryDelay = 1000
          root.retries = 0
        } else {
          root.episode = null
          root.st = ({})
        }
      }
    }
  }

  // Exit 10: the app came with the widget (bundled). Exit 0: a launcher, the one install.sh writes to
  // ~/.local/bin or an omapod anywhere on the PATH. The folder goes in as an argument, never into the script.
  Process {
    id: findApp
    command: ["sh", "-c", "[ -f \"$1/omapod/__main__.py\" ] && exit 10; "
      + "command -v omapod >/dev/null 2>&1 || [ -x \"$HOME/.local/bin/omapod\" ]", "sh", root.appDir]
    onExited: function(exitCode) {
      root.bundled = exitCode === 10
      root.installed = root.connected || exitCode === 0 || exitCode === 10
    }
  }

  Process {
    id: makeRuntimeDir
    property string dir: ""
    command: ["mkdir", "-p", dir]
    onExited: {
      if (makeRuntimeDir.dir === root.runtimeDir) root.watching = true
      else root.watchPresence() // the socket path setting arrived meanwhile
    }
  }

  FileView {
    id: presence
    path: root.watching ? root.presencePath : ""
    watchChanges: true
    printErrors: false
    onLoaded: {
      root.retryDelay = 1000
      root.retries = 0
      root.appPresent = true
    }
    onLoadFailed: root.appPresent = false
    onFileChanged: reload()
  }

  // Connect while omaPOD runs. If the socket does not answer (a crashed app leaves app.json behind),
  // back off and give up after a few tries until app.json changes again.
  Timer {
    interval: root.retryDelay
    running: root.appPresent && !root.connected && root.retries < 6
    repeat: true
    triggeredOnStart: true
    onTriggered: {
      root.retries += 1
      root.retryDelay = Math.min(30000, root.retryDelay * 2)
      root.connectSocket()
    }
  }

  // Move the position smoothly between the app's once-a-second pushes.
  Timer {
    interval: 250
    running: root.playing
    repeat: true
    onTriggered: {
      var next = root.position + (Date.now() - root.receivedAt) * root.speed
      root.livePosition = root.duration > 0 ? Math.min(root.duration, next) : next
    }
  }

  // ---- bar content
  Item {
    id: barContent
    anchors.centerIn: parent
    implicitWidth: barRow.implicitWidth
    implicitHeight: parent.height
    width: implicitWidth
    height: parent.height

    Row {
      id: barRow
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(6)

      Text {
        id: glyph
        textFormat: Text.PlainText
        anchors.verticalCenter: parent.verticalCenter
        text: root.playing ? "󰏤" : "󰦔"
        color: root.connected ? root.barForeground : Qt.rgba(root.barForeground.r, root.barForeground.g, root.barForeground.b, 0.55)
        font.family: root.fontFamily
        font.pixelSize: Style.bar.iconFont
      }

      Item {
        id: titleClip
        visible: root.showTitle && root.hasEpisode && !(root.bar && root.bar.vertical)
        width: Math.min(root.maxTitleWidth, titleText.implicitWidth)
        height: titleText.implicitHeight
        clip: true
        anchors.verticalCenter: parent.verticalCenter

        Text {
          id: titleText
          textFormat: Text.PlainText
          text: root.episode ? root.episode.title : ""
          color: root.barForeground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          anchors.verticalCenter: parent.verticalCenter
          readonly property bool needsScroll: implicitWidth > titleClip.width

          NumberAnimation on x {
            running: titleText.needsScroll && barMouse.containsMouse && !root.opened
            loops: Animation.Infinite
            duration: Math.max(6000, titleText.implicitWidth * 25)
            from: 0
            to: -(titleText.implicitWidth - titleClip.width)
            easing.type: Easing.InOutSine
            onRunningChanged: if (!running) titleText.x = 0
          }
        }
      }
    }

    // A hairline of progress under the widget, in the theme's accent.
    Rectangle {
      visible: root.hasEpisode && root.duration > 0
      anchors.left: barRow.left
      anchors.bottom: parent.bottom
      anchors.bottomMargin: Math.max(2, Style.space(3))
      height: 2
      width: barRow.width * root.progress
      color: Commons.Color.accent
    }
  }

  MouseArea {
    id: barMouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton | Qt.RightButton
    onClicked: function(mouse) {
      if (root.bar) root.bar.hideTooltip(root)
      if (mouse.button === Qt.MiddleButton) {
        if (root.connected) root.send("toggle")
        else root.launch()
      } else if (mouse.button === Qt.RightButton) {
        if (root.connected) root.send("show")
        else root.launch()
      } else {
        root.toggle()
      }
    }
    onWheel: function(wheel) {
      if (!root.hasEpisode) return
      if (wheel.angleDelta.y > 0) root.send("forward")
      else if (wheel.angleDelta.y < 0) root.send("rewind")
    }
    onEntered: if (root.bar) root.bar.showTooltip(root, root.hasEpisode
      ? (root.episode.title + "\n" + root.episode.podcast + "  ·  " + root.clock(root.livePosition) + " / " + root.clock(root.duration))
      : (root.connected ? "omaPOD"
        : (root.installed ? "omaPOD is not running. Click to open it."
          : "omaPOD is not installed. Click for the install guide.")))
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }

  // ---- popup
  KeyboardPanel {
    id: panel
    anchorItem: barContent
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    // Playing with four episodes up next needs about 660 px; a short screen scrolls the rest (below).
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(720))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onActivateRequested: root.send("toggle")
      onMoveRequested: function(dx, dy) {
        if (dx < 0) root.send("rewind")
        else if (dx > 0) root.send("forward")
        if (dy !== 0)
          flick.contentY = Math.max(0, Math.min(flick.contentY + dy * Style.space(56),
                                                flick.contentHeight - flick.height))
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }

      // Taller than the screen allows: scroll, like Omarchy's own panels, instead of cutting off the last rows.
      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(12)

          // -- omaPOD is not running, or not even installed
          Column {
            width: parent.width
            spacing: Style.space(10)
            visible: !root.connected

            Text {
              text: root.installed ? "omaPOD is not running" : "omaPOD is not installed"
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: root.installed
                ? "Start the app to see what is playing and control it from here."
                : "This widget shows and controls the omaPOD podcast app. Install the app first: the guide is on GitHub."
              color: root.dimFg
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Button {
              text: root.installed ? "Start omaPOD" : "Open the install guide"
              iconText: "󰏌"
              foreground: root.fg
              bordered: true
              onClicked: {
                if (root.installed) root.launch()
                else Qt.openUrlExternally(root.installGuide)
                root.close()
              }
            }
          }

          // -- running, nothing loaded
          Column {
            width: parent.width
            spacing: Style.space(10)
            visible: root.connected && !root.hasEpisode

            Text {
              text: "Nothing playing"
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: (root.st.queue_count || 0) > 0
                ? (root.st.queue_count + " episodes are waiting in the queue.")
                : "The queue is empty."
              color: root.dimFg
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
            Row {
              spacing: Style.space(8)
              Button {
                text: "Play queue"
                iconText: "󰐊"
                foreground: root.fg
                bordered: true
                visible: (root.st.queue_count || 0) > 0
                onClicked: root.send("play")
              }
              Button {
                text: "Open omaPOD"
                iconText: "󰏌"
                foreground: root.fg
                bordered: true
                onClicked: { root.send("show"); root.close() }
              }
            }
          }

          // -- now playing
          Column {
            width: parent.width
            spacing: Style.space(12)
            visible: root.hasEpisode

            Row {
              width: parent.width
              spacing: Style.space(12)

              Item {
                width: Style.space(76)
                height: Style.space(76)

                Rectangle {
                  anchors.fill: parent
                  color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08)
                }
                Image {
                  anchors.fill: parent
                  anchors.margins: root.playing ? 2 : 0
                  fillMode: Image.PreserveAspectCrop
                  asynchronous: true
                  source: root.episode && root.episode.cover ? root.episode.cover : ""
                }
                // The playing episode is framed like the focused window.
                Rectangle {
                  anchors.fill: parent
                  color: "transparent"
                  border.width: root.playing ? 2 : 0
                  border.color: Commons.Color.accent
                }
              }

              Column {
                width: parent.width - Style.space(88)
                spacing: Style.space(3)
                anchors.verticalCenter: parent.verticalCenter

                Text {
                  width: parent.width
                  text: root.episode ? root.episode.title : ""
                  textFormat: Text.PlainText
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.subtitle
                  font.bold: true
                  wrapMode: Text.WordWrap
                  maximumLineCount: 2
                  elide: Text.ElideRight
                }
                Text {
                  width: parent.width
                  text: root.episode ? root.episode.podcast : ""
                  textFormat: Text.PlainText
                  color: root.dimFg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
                Text {
                  width: parent.width
                  visible: root.chapter !== ""
                  text: (root.chapterIndex(root.livePosition) + 1) + "/" + root.chapterStarts.length + "  " + root.chapter
                  textFormat: Text.PlainText
                  color: Commons.Color.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
              }
            }

            // Chapters as workspaces: one segment per chapter, the current one lit.
            Item {
              id: segments
              width: parent.width
              height: Style.space(14)

              readonly property var starts: {
                var list = root.chapterStarts.length > 0 ? root.chapterStarts.slice() : [0]
                if (list[0] > 0) list.unshift(0)
                return list
              }
              readonly property int current: Math.max(0, root.chapterIndex(root.livePosition))

              Repeater {
                model: segments.starts.length

                Rectangle {
                  required property int index
                  readonly property real startMs: segments.starts[index]
                  readonly property real endMs: index + 1 < segments.starts.length ? segments.starts[index + 1] : root.duration
                  readonly property real gap: segments.starts.length > 1 ? 3 : 0
                  x: root.duration > 0 ? startMs / root.duration * segments.width + (index > 0 ? gap / 2 : 0) : 0
                  width: Math.max(1, root.duration > 0 ? (endMs - startMs) / root.duration * segments.width - gap : segments.width)
                  height: Style.space(4)
                  anchors.verticalCenter: parent.verticalCenter
                  color: (segments.starts.length > 1 && index === segments.current)
                    ? Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.30)
                    : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.16)

                  Rectangle {
                    height: parent.height
                    color: Commons.Color.accent
                    width: {
                      if (root.duration <= 0) return 0
                      var playedX = root.livePosition / root.duration * segments.width
                      return Math.max(0, Math.min(parent.width, playedX - parent.x))
                    }
                  }
                }
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: function(mouse) {
                  if (root.duration > 0) root.send("seek", { position: Math.round(mouse.x / width * root.duration) })
                }
              }
            }

            Item {
              width: parent.width
              height: elapsed.implicitHeight

              Text {
                id: elapsed
                text: root.clock(root.livePosition)
                color: root.dimFg
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
              Text {
                anchors.right: parent.right
                text: root.duration > 0 ? "−" + root.clock(root.duration - root.livePosition) : ""
                color: root.dimFg
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Row {
              anchors.horizontalCenter: parent.horizontalCenter
              spacing: Style.space(6)

              Button {
                iconText: "󰑟"
                tooltipText: "Rewind " + (root.st.rewind_secs || 10) + " s"
                foreground: root.fg
                onClicked: root.send("rewind")
              }
              Button {
                iconText: root.playing ? "󰏤" : "󰐊"
                tooltipText: root.playing ? "Pause" : "Play"
                foreground: root.fg
                bordered: true
                iconSize: Style.font.iconLarge
                horizontalPadding: Style.spacing.panelGap
                onClicked: root.send("toggle")
              }
              Button {
                iconText: "󰈑"
                tooltipText: "Forward " + (root.st.forward_secs || 30) + " s"
                foreground: root.fg
                onClicked: root.send("forward")
              }
              Button {
                iconText: "󰒭"
                tooltipText: "Skip episode"
                foreground: root.fg
                onClicked: root.send("skip")
              }
            }

            PanelSeparator { foreground: root.fg }

            PanelSectionHeader {
              text: "SPEED  " + root.speedLabel(root.speed)
              foreground: root.fg
              fontFamily: root.fontFamily
            }
            Flow {
              width: parent.width
              spacing: Style.spacing.xs

              Repeater {
                model: root.speedPresets

                Button {
                  required property var modelData
                  text: root.speedLabel(modelData)
                  foreground: root.fg
                  bordered: true
                  selected: Math.abs(Number(modelData) - root.speed) < 0.001
                  onClicked: root.send("speed", { value: Number(modelData) })
                }
              }
            }

            PanelSectionHeader {
              text: root.sleep
                ? ("SLEEP TIMER  " + (root.sleep.mode === "time" ? root.clock(root.sleep.remaining) : "END OF EPISODE"))
                : "SLEEP TIMER"
              foreground: root.fg
              fontFamily: root.fontFamily
            }
            Flow {
              width: parent.width
              spacing: Style.spacing.xs

              Repeater {
                model: [15, 30, 45, 60]

                Button {
                  required property var modelData
                  text: modelData + " min"
                  foreground: root.fg
                  bordered: true
                  onClicked: root.send("sleep", { minutes: modelData })
                }
              }
              Button {
                text: "End of episode"
                foreground: root.fg
                bordered: true
                selected: root.sleep !== null && root.sleep.mode === "episodes"
                onClicked: root.send("sleep", { episodes: 1 })
              }
              Button {
                text: "Off"
                iconText: "󰒲"
                foreground: root.fg
                bordered: true
                visible: root.sleep !== null
                onClicked: root.send("sleep_cancel")
              }
            }
          }

          // -- up next
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.connected && root.queue.length > 0

            PanelSeparator { foreground: root.fg }

            PanelSectionHeader {
              text: "UP NEXT"
              foreground: root.fg
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.queue.slice(0, 4)

              Item {
                id: nextRow
                required property var modelData
                width: column.width
                height: Style.space(40)

                Rectangle {
                  anchors.fill: parent
                  color: nextMouse.containsMouse ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08) : "transparent"
                }
                Image {
                  id: nextCover
                  x: Style.space(4)
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(32)
                  height: Style.space(32)
                  fillMode: Image.PreserveAspectCrop
                  asynchronous: true
                  source: nextRow.modelData.cover || ""
                }
                Column {
                  anchors.left: nextCover.right
                  anchors.leftMargin: Style.space(10)
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(6)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(1)

                  Text {
                    width: parent.width
                    text: nextRow.modelData.title
                    textFormat: Text.PlainText
                    color: root.fg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                    elide: Text.ElideRight
                  }
                  Text {
                    width: parent.width
                    text: nextRow.modelData.podcast + (nextRow.modelData.duration > 0 ? "  ·  " + root.clock(nextRow.modelData.duration - (nextRow.modelData.position || 0)) + " left" : "")
                    textFormat: Text.PlainText
                    color: root.dimFg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }
                MouseArea {
                  id: nextMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.send("play_episode", { id: nextRow.modelData.id })
                }
              }
            }
          }

          Row {
            spacing: Style.space(8)
            visible: root.connected

            Button {
              text: "Now playing"
              foreground: root.fg
              bordered: true
              visible: root.hasEpisode
              onClicked: { root.send("now_playing"); root.close() }
            }
            Button {
              text: "Open omaPOD"
              iconText: "󰏌"
              foreground: root.fg
              bordered: true
              onClicked: { root.send("show"); root.close() }
            }
          }
        }
      }
    }
  }
}
