# CareEyes Pro 优化与功能调研

调研日期：2026-09-20。基于本工作区 v5.4 的现有实现优化至 v5.5；保留原有未提交的桌宠、成长及统计改动。本文区分已落地的改动与尚未实现的建议，不把候选功能当作现有能力。

## 已落地

| 改动 | 使用入口 / 实现 | 行为边界 |
| --- | --- | --- |
| 临时免打扰 | 休息页、托盘；`WorkClock.snooze()` | 15 / 30 / 60 分钟，手动可恢复；仅屏蔽自动弹窗和预告，护眼、工作倒计时及活动统计继续 |
| 每日用眼 CSV | 统计页“导出 CSV”；标准库 `csv` | 包含当天及前 31 天范围内已有记录；UTF-8 BOM，日期排序；不编造缺失记录 |
| 渐变计时修复 | `SmoothTransition` → Qt `QTimeLine` | 50ms 采样、OutCubic；按时间追赶，不积压过期帧；连切复用最近成功应用的帧 |
| 写入可靠性 | `_write_atomic()` → Qt `QSaveFile` | 检查打开、完整写入、提交；关闭直接覆盖回退；失败保留原文件并展示错误 |

临时免打扰采用现有单调时钟，不增加后台定时器，也不持久化截止时间。锁屏、休眠、空闲期间不会凭空增加用眼时长；免打扰可以在这些状态下到期，自动休息仍等待用户恢复活动。手动休息、设置重置会取消临时免打扰。

## 问题依据

### 渐变为什么会变慢、跳变

旧实现每次 QTimer 回调只递增一帧，把“经过的时间”误等同于“回调次数”。在隔离复现中，将 150ms 的渐变延迟至 250ms 后执行第一次回调，旧实现仍处于运行状态，输出约 `3240.74K / 0.6833`，未到达目标 `2500K / 0.55`。Qt 时间线在同样延迟后到达目标并结束。

快速连续选择预设时，UI 中的目标值又被当作下一段起始值，而屏幕可能还没有走完上一段渐变。新实现保留最近成功应用的帧作为续接起点；失败写入不会被记成成功帧。原有 Gamma 曲线缓存、守护写入和原始曲线恢复机制不变。

### 配置为什么会“看似保存成功”

旧 `_save_settings()` 对序列化、写入、替换的所有异常直接忽略。现在复用项目已经依赖的 Qt `QSaveFile`，拒绝非标准 JSON 数值，检查完整写入与提交结果，失败返回 `False`，设置页显示具体错误。CSV 复用相同写入路径，取消文件选择或写入失败不覆盖已有文件。

### 构建日志为什么会乱码

本机 Windows PowerShell 5.1 按系统代码页读取无 BOM 的脚本，导致 `build.ps1` 中的中文提示和邻接变量插值被误解析。脚本补上 UTF-8 BOM，保留既有构建命令和哈希生成逻辑；不修改机器的全局编码配置。

## 开源方案与组件取舍

- **Stretchly**：参考其定时暂停、自然休息、应用排除和长短休息的交互。项目已经具备空闲/锁屏/休眠感知，因此不重复实现这些功能，本轮补上独立的临时免打扰。
- **Workrave**：参考其按日统计、完成/跳过/延后分类。当前项目已有每日分钟数据，因此先交付可核验的 CSV 导出，而不是凭历史启动次数推导完成率。
- **Qt QTimeLine / QSaveFile**：直接复用现有 PyQt5 组件，不自建动画时间线或另写文件替换协议。相关行为已在本机 Qt 5.15.2 上验证，没有迁移 Qt 6。
- **Python 标准库 csv**：无需引入 pandas、数据库或新打包依赖。保留范围最多 32 天，用现有 JSON 存储足够。

本轮不新增 pip/npm 依赖，不把 Electron/C++ 同类应用整体嵌入现有 PyQt 程序，也不重写已有桌宠和调度结构。

## 后续功能优先级

以下是基于本项目现状的工程建议，均尚未实现。

| 优先级 | 建议 | 价值与验收要求 |
| --- | --- | --- |
| P1 | 显示器兼容性与错误诊断 | 将 `GammaController.errors` 按显示器展示；区分 API 调用失败与实际效果未生效。微软明确说明 Gamma API 在 HDR 下行为未定义，也可能与系统控制冲突，不应单纯提高守护频率 |
| P1 | 完成 / 跳过 / 延后分开统计 | 当前 `break_count` 是休息启动次数，不等于完整休息次数。增加带日期的事件计数，复用遮罩完成状态；不迁移或伪造旧记录为“完成”，CSV 再增加可靠的完成率字段 |
| P2 | 小休息 + 长休息双周期 | 参考 Stretchly / Workrave；支持短暂离屏与较长休息分别配置。必须共用活动时钟并确保同一时刻只有一个遮罩，跳过和完成分别记账 |
| P2 | 工作日与工作时段计划 | 允许按星期和时间段启用提醒，覆盖跨午夜时段、锁屏恢复与系统时间变化；独立于护眼开关，不在非工作时间重置已有统计 |
| P2 | 单显示器配置与硬件亮度调研 | 外接屏与笔记本可能需要不同亮度/色温。先验证设备标识、热插拔和原始曲线恢复，再评估 DDC/CI；不承诺所有显示器或 HDR 都能用同一实现控制 |

建议先补**显示效果可观测性**和**真实休息完成统计**，再扩展提醒种类；暂不引入云同步、账户或自动更新服务，以免扩大数据与打包维护范围。

## 验证范围

- 基线：107 项 `unittest` 通过；优化后：137 项通过，无跳过。
- 命令：`python -m unittest discover -s tests -v`。
- 已执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1`：产物为 `dist/CareEyesPro.exe`，文件版本 `5.5.0`，大小约 36.52 MiB；实际 SHA256 与根目录 `CareEyesPro.exe.sha256` 一致，并检查了冻结包中的入口、runtime、CSV、QtCore 和 Windows 平台插件。
- 新增用例包括：免打扰继续统计、空闲期间到期、提前恢复、菜单状态同步、CSV 排序与保留边界、取消与失败不覆盖、非有限数值拒绝、延迟渐变追赶、连续切换、失败帧和迟到帧。
- 以隔离配置和模拟显示后端检查 860×640、760×560 的休息页，以及统计页布局，不读取或修改用户真实配置。
- 离屏测试不代表真实显示器、HDR 或驱动兼容性验证；这些仍需专门的硬件矩阵测试。

## 一手资料

1. [Stretchly 功能与高级配置](https://hovancik.net/stretchly/about/)；[项目源码](https://github.com/hovancik/stretchly)。
2. [Workrave 按日统计说明](https://workrave.org/docs/windows/statistics/)；[定时器设置](https://workrave.org/docs/settings/timers/)。
3. [Qt QTimeLine](https://doc.qt.io/qt-6/qtimeline.html)：时间线、更新间隔、缓动和停止状态。
4. [Qt QSaveFile](https://doc.qt.io/qt-6/qsavefile.html)：原子提交、错误处理与 direct-write fallback。
5. [Microsoft SetDeviceGammaRamp](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-setdevicegammaramp)：HDR 与系统控制限制；部分硬件单次调用可能耗时约 200ms。
6. [PowerShell 字符编码](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding)：含非 ASCII 字符的 Windows PowerShell 脚本应使用 UTF-8 BOM。
