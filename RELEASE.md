# Core 0.024.35 — Grok 隔离更新

源代码：`xbar-mirror` 提交 `a9345f2`。

镜像：`langrentaole/xbar-core:0.024.35`（amd64、arm64）。本次发布的不可变摘要：`sha256:51e7b672db89ca116aba270bd9c32e568a4c3a0260893b3430db56b7c0351c1c`。

Grok Bot 在页面中隐藏，并在服务端禁用 Bot 页面、REST 和 WebSocket 通道。普通 Grok 对话继续提供服务。

会话及图片、视频、文件按站内用户校验归属；覆盖图库列表、直接资源路径、上传任务和 HTTP/WebSocket 附件引用。跨车队续聊保留文件传输及副本复用。旧资源有归属记录的继续可用；无法证明归属的旧共享图库内容不向用户展示，不自动分配给访问者。

升级会执行新增的 `00126_grok_media_ownership.sql`。更新前备份数据库和环境文件；由于镜像版本号保持不变，必须重新拉取镜像并重建 `core-api`、`core-edge`，不能只重启旧容器。

验证：全量 Go 测试、`go vet`、Grok/会话/Edge 竞态测试通过；已确认的安全复核阻断项均关闭。
