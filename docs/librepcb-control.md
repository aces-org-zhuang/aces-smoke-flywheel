# 域正对照：LibrePCB（为什么它不能当主仓）

## 一句话

它是**真 EDA**（原理图 + PCB + 库编辑器，375,122 行 C++/Slint），
但它**没有前后端分离**——没有 HTTP 服务端，没有独立构建的前端，
`QTcpServer` / `QHttpServer` / `listen(` 在全部 C++ 源码里零命中。
所以它能满足"域正 + 20 万行 + 有成品用例"，却不能满足"前后端分离 + L2 契约层"。

## 实测数据

| 项 | 值 | 取证方式 |
|---|---|---|
| 仓 | https://github.com/LibrePCB/LibrePCB | — |
| 提交 | `be16f52f2ff2bbe303063e68dbccad524bc0da8e`（2026-10-30） | `git log -1` |
| 源码行数 | **375,122**（C++ 328,293 / Slint 34,528 / Python 8,565 / 其他 3,736） | 逐文件计数，排除 .git/3rdparty/external |
| 前端形态 | Slint（122 个 `.slint` 文件），编译进同一进程 | `find -name '*.slint'` |
| 服务端 | **无**（无 QTcpServer / QHttpServer / listen 命中） | `grep -rl` 全仓 |
| 单元测试 | 124 个 `tests/unittests/*.cpp` | `find` |
| CLI 测试 | 42 个 Python 脚本（`tests/cli/*`） | `find` |
| GUI 测试 | 16 个 funq 脚本（`tests/funq/*`） | `find` |
| 构建依赖 | Qt ≥ 6.2 + CMake ≥ 3.22 + MSVC/MinGW；可选 OpenCASCADE | `README.md` |
| 本机可构建 | **否**：无 Qt 工具链，完整编译预计数小时 | 环境盘点 |

## 为什么不做主仓

1. **L2 契约层无处落脚**：契约测试需要 consumer/provider 两个可独立启动的边界。
   LibrePCB 的 UI 与内核在同一进程，没有可被独立验证的接口面，
   `can-i-deploy` 也就没有"部署"可判——飞轮的第 4、5 圈转不起来。
2. **反馈周期不成立**：Qt 全量编译 + funq GUI 测试的单圈时长与"≤15 分钟全绿"的
   P1 验收标准相差一个数量级，飞轮第一圈就会失速。
3. **演示价值错位**：本次要证明的是"证据驱动的判决机制"，不是"EDA 内核正确性"。

## 但它留在报告里的原因

它是**口径锚点**：证明我们没有因为"跑得动"就把领域悄悄换掉。
主仓 InvenTree 的领域定位（PCB 装配链）在报告首页如实标注，
与 LibrePCB 这一列并排展示，读者可以自己判断这次实践覆盖到哪里、没覆盖到哪里。

## 如果将来要把它纳入

唯一可行路径是给它加一层**进程外契约**（例如把 `librepcb-cli` 的命令行接口
当作 provider 边界，用退出码 + 输出文件做 contract），
再用 `tests/cli` 的 42 个脚本做 L1 语料。这属于"给单体补边界"，是另一个立项。
