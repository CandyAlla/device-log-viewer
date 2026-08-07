# Device Log Viewer

一个运行在本机浏览器中的 Android / iOS 日志查看与装包工具。

它把 `adb logcat`、iOS 控制台、App 筛选、埋点筛选、安装包安装和日志下载集中在一个页面中。工具本身不依赖 Unity 或某个具体项目；项目名称、Android 包名、iOS Bundle ID、埋点格式和默认端口都由 Profile JSON 配置。

## 主要功能

- 实时查看 Android、iOS 模拟器和 iPhone/iPad 真机日志。
- 在网页中以 H.264 低延迟串流查看 Android 和 iPhone/iPad 真机画面，支持 30/60 FPS 档位。
- 一键截取手机 PNG，并把 Android 或 iOS 真机画面录制为 MP4。
- Android 可只看指定 App；App 被杀后仍保持 Logcat 连接，重新启动 App 时自动跟踪新 PID。
- 自动读取设备上已安装的第三方 App，也支持手动输入包名或 Bundle ID。
- 按关键词及 `V / D / I / W / E / F` 日志级别筛选。
- 按 Profile 中的标记筛选埋点，并把支持的埋点格式显示成事件卡片。
- 暂停页面显示、自动滚动、清空页面或清空 Android Logcat 缓冲区。
- 将当前筛选结果下载为 UTF-8 编码的 TXT 文件。
- 拖入本地 APK，安装到指定 Android 设备。
- 通过 FIR 分发链接或直接 APK 地址下载并安装 Android App。
- 通过 iOS 分发链接下载并安装 IPA 到指定 iPhone/iPad。
- 页面内可直接打开工具所在文件夹，不依赖日志服务运行状态。

页面最多保留最近 20,000 行。服务默认只监听 `127.0.0.1`，不会把设备日志暴露给局域网。

## 运行环境

| 功能 | 依赖 |
| --- | --- |
| 基础页面与服务 | macOS、Python 3.9+；无需第三方 Python 包 |
| Android 日志与装包 | Android Platform Tools（`adb`） |
| Android 流畅画面与录屏 | `scrcpy`；网页实时播放还需要 `ffmpeg` |
| iOS 日志与装包 | 完整版 Xcode（`xcrun simctl`、`devicectl`） |
| iPhone/iPad 真机画面与录屏 | USB 数据线、Xcode、QuickTime Player、`ffmpeg`；工具会按系统能力使用 AVFoundation，或 QuickTime + ScreenCaptureKit |
| 远程链接安装 | 无需登录即可访问的公网 HTTP(S) 分发地址 |

Unity 项目不是运行依赖。只有随附的 Profile 生成器会只读扫描 Unity `ProjectSettings` 和源码中的埋点标记。

## 快速开始

### 1. 下载工具

克隆仓库：

```bash
git clone <repository-url>
cd DeviceLogViewer
```

也可以从 GitHub 下载 ZIP。若可执行权限丢失，运行：

```bash
chmod +x start.command
chmod +x OpenDeviceLogViewerFolder.app/Contents/MacOS/OpenDeviceLogViewerFolder
```

### 2. 准备设备

Android：

1. 在设备上开启开发者选项和 USB 调试。
2. 连接设备并在授权弹窗中点击“允许”。
3. 可通过 `adb devices` 确认设备状态为 `device`。

流畅设备画面还需要安装 `scrcpy` 和 `ffmpeg`。使用 Homebrew：

```bash
brew install scrcpy ffmpeg
```

iOS：

1. 安装并至少启动过一次 Xcode。
2. 真机需要解锁、信任当前 Mac，并开启开发者模式。
3. 先在 Xcode 的 **Devices and Simulators** 中确认设备可见。
4. 查看真机画面时必须使用 USB 数据线；仅通过 Wi-Fi 配对无法提供真机画面输入。
5. 首次使用时，按系统提示允许 Terminal 控制 QuickTime Player，并允许 **Device Log Viewer Capture** 录制屏幕。
6. 如果系统使用 AVFoundation 采集并请求相机权限，请允许启动工具的 Terminal 使用相机。

### 3. 启动

双击 `start.command`，或在终端运行：

```bash
./start.command
```

默认使用中性配置 [`profiles/default.json`](profiles/default.json)。页面打开后，选择日志来源、设备和 App，再点击“开始”。

使用指定项目的 Profile：

```bash
./start.command profiles/pawdoku.json
```

也可以直接启动 Python 服务：

```bash
python3 server.py --profile profiles/pawdoku.json
```

浏览器默认打开 <http://127.0.0.1:8765>。终端中按 `Ctrl+C` 停止服务。

重复启动相同版本和 Profile 时，会直接打开现有页面；切换 Profile 或工具版本时，会自动重启同目录中的旧服务。

## 页面使用方法

### 查看日志

1. 选择 `Android`、`iOS 模拟器` 或 `iPhone / iPad`。
2. 选择目标设备。
3. 如果只想查看某个 App，保持“仅指定 App”勾选并选择 App。
4. 点击“开始”。

Android 指定 App 模式不会因为 App 进程被杀而断开 Logcat。页面会等待 App 再次启动，并自动连接新的 PID。

iOS 模拟器指定 App 模式和 iPhone/iPad 真机模式，需要通过 Xcode 命令行工具启动或重启 App 才能连接其控制台。

### 筛选与下载

- 在搜索框中输入关键词、Tag 或进程名。
- 勾选或取消日志级别。
- Profile 启用埋点功能时，可勾选“只看埋点”。
- “暂停显示”只停止页面刷新，后台仍继续接收日志。
- “下载 TXT”保存当前筛选结果，不会改写原始日志文本。

### 查看、截图和录制 Android 手机画面

1. 选择 Android 和目标设备。
2. 点击工具栏中的“设备画面”，页面右侧会展开手机画面区域；窄屏浏览器会使用全屏画面面板，点击“关闭”即可返回日志。
3. 选择画质档位，然后点击“开始实时画面”。默认档位上限为 60 FPS、1920 像素和 12 Mbps。
4. 点击“截图 PNG”会同时在页面显示截图并下载原始 PNG。
5. 点击“开始录屏”；完成后点击“停止并下载录屏”，工具会封装并下载 MP4。

实时预览使用 `scrcpy` 在 Android 设备端编码 H.264，由本机 `ffmpeg` 无损转换为浏览器可播放的 fragmented MP4，并通过本地 WebSocket 传输。它不是连续截图，因此延迟、CPU 和 USB 带宽占用明显低于高帧率图片方案。

实时预览和录屏可以同时运行，此时手机可能同时使用两个视频编码会话。低端设备如果出现编码失败、发热或卡顿，请停止录屏，或改用“均衡 / 省流”档位。受 Android 安全策略保护的 `FLAG_SECURE` 页面和 DRM 内容可能显示为黑屏。

### 查看、截图和录制 iPhone/iPad 真机画面

1. 使用 USB 数据线连接并解锁 iPhone/iPad，确认设备已信任这台 Mac。
2. 在日志来源中选择 `iPhone / iPad` 和目标真机，然后点击“设备画面”。
3. 工具会自动选择当前系统可用的采集方式；检测到多个输入时，选择与目标设备对应的名称。
4. 选择画质后点击“开始实时画面”。截图会下载 PNG；停止录屏后会下载 H.264 MP4。

iOS 真机画面优先使用 macOS AVFoundation 输入；在不再向 AVFoundation 暴露 iPhone 画面的系统版本（包括 macOS 26）上，工具会自动打开 QuickTime 原生真机预览，再通过随附的 ScreenCaptureKit 组件捕获窗口。两种方式都会使用 `ffmpeg` 和 VideoToolbox 编码为浏览器可播放的 H.264 fragmented MP4。画面只在本机 `127.0.0.1` 服务中传输，不会上传到网络。

QuickTime 方式下，请保持手机解锁、亮屏并连接 USB，也不要关闭工具自动打开的 QuickTime 预览窗口。首次使用需要在 **系统设置 → 隐私与安全性** 中开启：

- **辅助功能**：允许启动 `start.command` 的 Terminal 控制 QuickTime Player。
- **屏幕与系统音频录制**：允许 **Device Log Viewer Capture** 捕获 QuickTime 预览窗口。

修改权限后请退出并重新运行 `start.command`。

部分 macOS/iOS 版本只允许一个进程占用同一画面输入。如果同时预览和录屏失败，请先停止实时画面再开始录屏；录屏完成后再恢复预览。

### 安装 Android APK

1. 选择 Android 设备。
2. 停止正在进行的日志采集。
3. 将 APK 拖入页面，或粘贴 FIR/直接 APK 链接。
4. 点击安装并确认目标设备。

工具会校验 APK，并通过 `adb install -r` 覆盖安装。远程下载的临时文件会在安装结束后删除。

### 安装 iOS App

1. 选择 `iPhone / iPad` 和目标真机。
2. 停止正在进行的日志采集。
3. 粘贴可公开访问的 iOS 分发链接。
4. 点击“下载并安装”。

企业签名或 Ad Hoc 签名必须有效；Ad Hoc 包还必须包含目标设备的 UDID。安装过程中请保持设备解锁。

## Profile 配置

Profile 的完整结构由 [`schemas/device-log-viewer-profile.schema.json`](schemas/device-log-viewer-profile.schema.json) 定义。

最小示例：

```json
{
  "$schema": "../schemas/device-log-viewer-profile.schema.json",
  "schemaVersion": 1,
  "id": "sample-game",
  "displayName": "Sample Game Device Logs",
  "defaultPort": 8765,
  "apps": {
    "android": {
      "default": "com.example.game",
      "presets": [
        { "id": "com.example.game", "label": "Sample Game Android" }
      ]
    },
    "ios": {
      "default": "com.example.game",
      "presets": [
        { "id": "com.example.game", "label": "Sample Game iOS" }
      ]
    }
  },
  "analytics": {
    "enabled": true,
    "marker": "[EventLog]:",
    "parser": "gamefoundation-eventlog",
    "platforms": ["Firebase", "Facebook", "Adjust", "AppsFlyer"]
  }
}
```

常用字段：

| 字段 | 作用 |
| --- | --- |
| `id` | Profile 的稳定标识，只能使用小写字母、数字、点、下划线和连字符 |
| `displayName` | 浏览器标题及页面左上角名称 |
| `defaultPort` | 启动器首先尝试的本机端口 |
| `apps.android.default` | 默认 Android 包名，可留空 |
| `apps.ios.default` | 默认 iOS Bundle ID，可留空 |
| `apps.*.presets` | 页面优先展示的 App 列表和名称 |
| `analytics.enabled` | 是否显示“只看埋点”功能 |
| `analytics.marker` | 用于识别埋点日志的字符串，匹配时忽略大小写 |
| `analytics.parser` | `plain` 或 `gamefoundation-eventlog` |
| `analytics.platforms` | GameFoundation 解析器接受的平台前缀 |

仓库内包含：

- [`profiles/default.json`](profiles/default.json)：无项目依赖的基础配置。
- [`profiles/pawdoku.json`](profiles/pawdoku.json)：Pawdoku 示例配置。

验证 Profile：

```bash
python3 server.py --profile profiles/pawdoku.json --print-profile-id
python3 server.py --profile profiles/pawdoku.json --print-port
```

## 为 Unity 项目生成 Profile

生成器只读扫描项目，不会修改 Unity 工程：

```bash
python3 skills/device-log-viewer-profile/scripts/generate_profile.py /absolute/path/to/project --dry-run
python3 skills/device-log-viewer-profile/scripts/generate_profile.py /absolute/path/to/project
```

它会读取 `ProjectSettings/ProjectSettings.asset` 中的：

- `productName`
- Android `applicationIdentifier`
- iPhone `applicationIdentifier`

同时会搜索 `[EventLog]:`。如果发现 GameFoundation 的 Firebase、Facebook、Adjust、AppsFlyer 格式，会自动选择 `gamefoundation-eventlog` 解析器。

如果仓库中包含多个 Unity 工程，需要明确指定：

```bash
python3 skills/device-log-viewer-profile/scripts/generate_profile.py /absolute/path/to/repository \
  --unity-root /absolute/path/to/repository/UnityProject \
  --dry-run
```

默认输出到 `profiles/<profile-id>.json`。已存在的文件不会被覆盖；只有明确确认后才应使用 `--force`。

查看全部参数：

```bash
python3 skills/device-log-viewer-profile/scripts/generate_profile.py --help
```

## 安装 Codex Skill

仓库随附 `$device-log-viewer-profile` Skill。将它链接到个人 Codex Skills 目录：

```bash
mkdir -p "${HOME}/.codex/skills"
ln -s /absolute/path/to/DeviceLogViewer/skills/device-log-viewer-profile \
  "${HOME}/.codex/skills/device-log-viewer-profile"
```

之后可以在 Codex 中输入：

```text
使用 $device-log-viewer-profile 为当前 Unity 项目生成 DeviceLogViewer Profile
```

Skill 会先进行只读预览，核对识别结果后生成配置，并拒绝意外覆盖已有文件。

## 命令行参数

```text
python3 server.py [--profile PROFILE] [--host HOST] [--port PORT]
                  [--adb ADB_PATH] [--no-open]
```

- `--profile`：Profile JSON 路径。
- `--host`：监听地址，默认 `127.0.0.1`。
- `--port`：覆盖 Profile 中的默认端口。
- `--adb`：明确指定 `adb` 可执行文件路径。
- `--no-open`：启动后不自动打开浏览器。
- `--version`：显示工具版本。
- `--print-profile-id` / `--print-port`：验证配置并输出对应值。

服务运行后：

- `GET /api/config`：返回页面正在使用的公开 Profile 配置。
- `GET /api/status`：返回工具版本、Profile id、依赖检测和采集状态。

## 常见问题

### 页面提示“未找到 adb”

确认 Android Platform Tools 已安装，并执行：

```bash
adb version
adb devices
```

也可以明确指定路径：

```bash
python3 server.py --adb /absolute/path/to/adb
```

### “设备画面”提示缺少 `scrcpy` 或 `ffmpeg`

执行：

```bash
brew install scrcpy ffmpeg
```

安装完成后打开“设备画面”，或点击错误提示中的“重新检测工具”。工具也会自动检查 `/opt/homebrew/bin` 和 `/usr/local/bin`。

### 实时画面连接失败或延迟不断增加

- 确认手机已解锁，并且 `adb devices` 中状态为 `device`。
- 改用“均衡 · 30 FPS”档位。
- 停止其他正在运行的 `scrcpy` 或手机录屏程序。
- 更换数据线或 USB 接口，避免只支持充电的线材。
- 旋转手机后如果画面尺寸没有正确更新，停止并重新开始实时画面。

### iPhone/iPad 日志可用，但“设备画面”提示未发现输入

- 必须使用可传输数据的 USB 线；`devicectl` 通过 Wi-Fi 显示“已连接”并不代表可以读取真机画面。
- 解锁设备，重新确认“信任这台电脑”，并保持设备停留在亮屏状态。
- 如果页面提示 QuickTime 权限不足，打开 **系统设置 → 隐私与安全性 → 辅助功能**，允许启动工具的 Terminal 控制 QuickTime Player。
- 如果页面提示屏幕录制权限不足，打开 **系统设置 → 隐私与安全性 → 屏幕与系统音频录制**，允许 **Device Log Viewer Capture**。
- 如果页面显示 AVFoundation 采集方式，打开 **系统设置 → 隐私与安全性 → 相机**，允许启动工具的 Terminal 访问视频输入。
- QuickTime 采集方式下不要关闭工具自动打开的真机预览窗口；AVFoundation 方式下则关闭 QuickTime、OBS、会议软件等可能占用输入的程序。
- 拔插数据线后重新打开“设备画面”。

### Android 设备显示 `unauthorized`

解锁设备，在 USB 调试授权弹窗中点击“允许”，然后刷新设备列表。必要时重新连接 USB。

### 找不到 iOS 模拟器

先启动 Xcode Simulator，并确保至少有一个模拟器处于 Booted 状态。

### 找不到 iPhone/iPad

确认设备已解锁、信任当前 Mac、开启开发者模式，并先在 Xcode 的 **Devices and Simulators** 中确认可见。

### 安装按钮不可用

安装前必须停止日志采集，并选择有效设备及安装包或分发链接。

### App 被杀后没有日志

Android 指定 App 模式下无需停止采集。保持页面连接，重新启动 App 后工具会自动识别新 PID。

### 端口被占用

`start.command` 会从 Profile 的 `defaultPort` 开始尝试连续 11 个端口。也可以手动指定：

```bash
python3 server.py --profile profiles/default.json --port 8766
```

### “打开所在文件夹”没有反应

先通过 `start.command` 启动一次，让 macOS 注册随附的小助手。浏览器首次调用时可能会询问是否允许打开。

### macOS 阻止打开脚本

可以在 Finder 中右键 `start.command`，选择“打开”；也可以直接在终端执行 `./start.command`。

## 目录结构

```text
DeviceLogViewer/
├── server.py
├── index.html
├── start.command
├── native/
│   ├── DeviceLogViewerCapture.swift
│   ├── DeviceLogViewerCapture-Info.plist
│   └── prepare_quicktime_capture.applescript
├── OpenDeviceLogViewerFolder.app
├── profiles/
│   ├── default.json
│   └── pawdoku.json
├── schemas/
│   └── device-log-viewer-profile.schema.json
└── skills/
    └── device-log-viewer-profile/
        ├── SKILL.md
        ├── agents/openai.yaml
        ├── references/profile-format.md
        └── scripts/generate_profile.py
```

核心服务只使用 Python 标准库，前端为单文件 HTML，不需要 npm、pip 或数据库。
