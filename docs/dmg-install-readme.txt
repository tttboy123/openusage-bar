OpenUsage Bar 安装说明 / Installation Guide
============================================

正常安装 / Standard installation
---------------------------------
1. 将 OpenUsage Bar 拖入 Applications。
2. 从访达的“应用程序”打开 OpenUsage Bar。

1. Drag OpenUsage Bar to Applications.
2. Open OpenUsage Bar from Finder > Applications.

如果 macOS 显示“OpenUsage Bar 已损坏”
--------------------------------------
本开源预发布版尚未使用 Apple Developer ID 公证。这个提示通常来自 macOS
下载隔离，并不表示 DMG 校验失败。拖入 Applications 后，打开“终端”，只对
OpenUsage Bar 执行：

xattr -dr com.apple.quarantine "/Applications/OpenUsage Bar.app"

然后再次从“应用程序”打开。该命令只移除 OpenUsage Bar 的下载隔离属性，
不要全局关闭 Gatekeeper。

If macOS says “OpenUsage Bar is damaged”
-----------------------------------------
This open-source pre-release is not notarized with an Apple Developer ID. After
dragging the app to Applications, open Terminal and run this command for
OpenUsage Bar only:

xattr -dr com.apple.quarantine "/Applications/OpenUsage Bar.app"

Open the app again from Applications. This removes only the downloaded app's
quarantine attribute; it does not disable Gatekeeper system-wide.
