# MJPEG 低延迟测试版

本轮基于 `fix/configurable-tcp-nodelay`，保留原来的可配置 TCP_NODELAY。
FFmpeg 开启 TCP_NODELAY 有效，并不等于已经证明 One-KVM 卡顿也只有这一原因。
当前没有 NanoPi M1 实机验证结果。

## 改了什么

- 原来：生产任务预先封装一帧，向容量 1 的通道执行 `send().await`。
  HTTP 接收受阻时，可能同时保留通道中的旧帧和等待发送的另一帧。
- 现在：HTTP body 被继续读取时才获取最新帧，不设置额外的生产任务和帧队列。
  已交给 Hyper/内核 TCP 缓冲区的数据不能撤回，这不是消除全部网络缓冲。
- 使用 `(sequence, capture_ts)` 区分帧，避免驱动序号复用/重置时漏发新内容。
- 响应断开立即释放客户端注册，不让生产任务继续存活到超时。
- 对新 HTTP/HTTPS 连接显式设置 TCP_NODELAY 的 true/false，debug 日志读取实际值。
- 保留原有重复帧抑制和 JPEG 有效性规则，增加累计诊断计数。
- 增加 `X-Accel-Buffering: no`。仅对支持该响应头的反向代理有效。

## 构建

Actions 的 **MJPEG armhf test package** 会随此分支 push 自动执行，也支持手动运行。
前端使用仓库现有的 Node 24 + `npm install && npm run build`，不依赖 pnpm。
构建产物包含 ARMv7 程序、armhf DEB、提交号及 SHA256SUMS。

本地沿用已经成功的交叉编译环境；前端可执行：

```bash
cd web
npm install
npm run build
cd ..
bash build/build-images.sh armv7
bash build/package-deb.sh armhf
```

需要 Node 24、Rust/cross、Docker 和网络访问。已有本地修改时先检查 `git status`；
不要用 reset 覆盖你为构建做的本地调整。

## 部署确认与诊断

安装新 DEB 后务必重启服务。包版本仍然是 0.2.8，因此不能只凭版本号判断。

```bash
sudo dpkg -i one-kvm_0.2.8_armhf.deb
sudo systemctl restart one-kvm
sudo journalctl -u one-kvm -n 100 --no-pager
```

新程序启动时必须出现 `MJPEG delivery: latest-frame-v1`，并显示
`Web TCP_NODELAY: true`。这能确认服务启动了新实现。
已登录网页的开发者工具 → Network → `/api/stream/mjpeg`，响应头也应有：
`X-One-KVM-Mjpeg: latest-frame-v1`。

若仍卡顿，可停止服务后临时前台运行（沿用 `/etc/one-kvm` 配置与账号）：

```bash
sudo systemctl stop one-kvm
sudo env RUST_LOG='info,one_kvm=debug' /usr/bin/one-kvm --tcp-nodelay true
```

保持网页显示 MJPEG，并让被控端画面持续变化约 15 秒。
记录 `Accepted web TCP connection` 和 `MJPEG delivery (body polling, not browser display)` 日志。
测试后 Ctrl+C，然后 `sudo systemctl start one-kvm`。

在已登录的同一网页 Console 中运行下面代码，取得间隔 10 秒的两份状态：

```javascript
const before = await fetch('/api/stream/status').then(r => r.json());
await new Promise(resolve => setTimeout(resolve, 10000));
const after = await fetch('/api/stream/status').then(r => r.json());
console.log(JSON.stringify({before, after}, null, 2));
```

`mjpeg` 字段里的计数是进程启动以来的累计值；比较前后差值：

| 数据 | 含义 |
|---|---|
| received_frames | 进入 MJPEG handler 的采集帧数 |
| published_frames | 更新为可供消费者读取的帧数，可能包含无客户端时的缓存更新 |
| duplicate_frames | 完全相同的帧被去重；静止桌面数值高属于正常行为 |
| invalid_jpeg_frames | 原始采集 JPEG 未通过既有校验的帧数 |
| http_body_frames | 所有 HTTP MJPEG 客户端交给 Hyper 的帧数，不代表浏览器已显示 |
| current_frame_bytes / current_frame_age_ms | 最新发布帧的大小、帧龄；静止画面去重会增大帧龄 |

debug 日志 `max_frame_age_ms` 是取出帧时的最大帧龄，
`max_body_poll_gap_ms` 是 yield 后至 Hyper 再次读取 body 的最大间隔，
后者同时受网络背压和运行时调度影响，不能直接等同网络耗时。
两者按约 5 秒窗口统计，`body_fps` 也不是浏览器显示 FPS。

- received 高、duplicate 高：先用持续运动的画面确认，而非凭桌面静止时的低输出 FPS 判断。
- received 高、invalid 高：检查 One-KVM 原始 V4L2 payload，而非仅检查 FFmpeg 保存后的图片。
- published 高、body_fps 低且 poll gap 大：继续查 HTTP/TCP 背压或运行时调度。
- body_fps 高而画面仍慢：检查代理缓冲、浏览器接收/解码与已排入 TCP 的数据。

## 无硬件测试

```bash
sudo apt-get install libclang-dev
cargo test --locked --manifest-path test/mjpeg-http/Cargo.toml
```

测试直接编译生产代码 `src/video/{format,frame}.rs`、`src/stream/mjpeg.rs`
与 `src/web/mjpeg.rs`，仅替代与传输无关的事件统计类型并禁用原始像素 JPEG 编码器。
覆盖慢客户端取最新帧、重复序号、断线释放、离线唤醒、multipart 格式及真实 TCP HTTP 传输。
这些测试不验证 MS2130/ARMv7 实机、浏览器 JPEG 解码或 HTTPS 握手。
