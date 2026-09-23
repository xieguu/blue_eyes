# CareEyes Pro 开发规范

本文档是 CareEyes Pro 的开发规范，适用于本仓库。开发、测试和打包在本地工作区完成；仅在用户明确要求时提交并推送到已确认的 GitHub 远端。

## 1. 项目定位与运行环境

- 平台：仅 Windows 10/11，不跨平台。
- 解释器：Python 3.10+，不使用虚拟环境的特殊布局。
- 依赖：见 `requirements.txt`，只允许新增有明确用途的依赖；禁止引入 `pywin32`（已移除）。
- 入口：`mainpro.py`；运行命令：`python mainpro.py`。

## 2. 代码组织

- 两层结构，保持现有边界：
  - `careeyes_runtime.py`：纯 Windows API 封装与可测试的运行时逻辑（Gamma、单实例、活动检测、计时、桌宠成长及装备槽），不 import PyQt。
  - `mainpro.py`：UI、业务编排、配置读写。业务规则尽量下沉到 runtime，便于单元测试。
- 命名：
  - 模块/类：`PascalCase`；函数/变量：`snake_case`；常量：`UPPER_SNAKE_CASE`。
  - UI 内私有工具函数一律 `_` 前缀。
- 类型：关键函数保留简洁的类型注解或 docstring；不强制完整 typing。
- 注释：注释说明“为什么”，不重复“做什么”；保留代码内既有中文注释风格。

## 3. 编码约定

- Windows API 一律走 `careeyes_runtime.py` 的封装，`mainpro.py` 不直接 `ctypes` 调用（`_is_admin`、系统指标采集等历史例外保持现状，新增逻辑必须封装进 runtime）。
- 外部数据（配置文件、注册表、系统 API）必须先经过 `_bounded_int` / `_bounded_float` / `_parse_position` 等净化函数再使用。
- 异常处理：
  - runtime 层：API 失败抛 `OSError`，由调用方决定降级策略；
  - UI 层：非致命错误静默降级（显示“不可用”），不弹框、不崩进程；
  - 单实例失败、Gamma 写入失败等必须记录到对应 `errors` 字典。
- 配置持久化和 CSV 导出复用 `_write_atomic` / `QSaveFile`，明确关闭直接覆盖回退；检查打开、完整写入和提交结果，失败不得静默报告成功。
- 配置保存失败由 `_settings_error` 记录并呈现在设置页；序列化禁用 NaN / Infinity，失败保留原配置。
- 业务倒计时（QTimer）保持 1s 粒度；动画与护眼守护保留独立频率。桌宠隐藏时停止其动画，主窗口隐藏或最小化时停止页面预览和系统采样；退出时显式停止定时器。空闲检测阈值常量（`IDLE_PAUSE_SECONDS` 等）集中在 `mainpro.py` 顶部。
- 高频纯计算优先使用标准库的有界缓存，缓存值必须不可变；Gamma 缓存仅复用曲线计算结果，不缓存未验证的驱动写入结果，也不跳过原始曲线的记录与恢复。
- 用户操作和渐变使用 `DisplayManager.apply()` 明确写入；800ms 守护使用 `DisplayManager.ensure()` 回读比较，只修复偏离目标的显示器。驱动量化后的 canonical 回读值按设备与目标曲线关联，目标改变或恢复原始曲线时失效；读取失败必须记录错误，不得盲写。
- 色温和亮度滑条复用单次 `QTimer` 合并事件，写入频率最多 25Hz；预设、自动模式、重置、开关和退出必须取消待处理写入。
- 色温渐变使用 Qt `QTimeLine` 与 OutCubic 曲线，保持 50ms 更新间隔；连续切换从最近成功应用的帧开始，停止后不接受迟到帧，不按回调次数累计进度。
- 免打扰截止时间属于 `WorkClock` 的单调时钟状态，只抑制自动提醒和预告，不停止活动统计、不增加定时器、不写入配置。手动休息和重置设置时取消。
- CSV 仅导出保留范围内的每日用眼分钟数，采用标准库 `csv` 和 UTF-8 BOM；不补造缺失日期，不把历史休息启动次数转成完成次数。
- 全屏进程名判断走 `FULLSCREEN_WHITELIST` / `FULLSCREEN_FORCE_DEFER` 两个集合，新增例外直接改集合，不写散落的 `if`。
- 桌宠成长以 `PetProgress.completed_rests` 为唯一事实来源，等级与解锁由模型推导；服装的穿戴权限由模型控制，不在 renderer 内硬编码锁定状态。
- 休息遮罩只在达到截止时间且未取消时标记完成；结算必须校验当前遮罩身份。跳过、重置和退出不得发放成长奖励。

## 4. 测试规范

- 框架：`unittest`，不引入 pytest。
- 位置与命名：`tests/test_<模块>.py`，测试函数 `test_<行为>`。
- 覆盖底线（见现有 `tests/`）：
  - Gamma 恢复（`GammaController`）；
  - 单实例互斥与激活消息（`SingleInstance`）；
  - 休息调度 / 全屏顺延（`WorkClock`、调度函数）；
  - 配置净化与启动清理；
  - UI 冒烟（`test_ui_smoke.py`）；
  - Gamma 缓存边界、守护按需修复、量化回读去重与失败重试；
  - 桌宠/预览/系统采样的显示、隐藏、最小化、恢复和退出生命周期。
  - 等级与解锁阈值、组合穿戴和头饰互斥（`test_pet_progress.py`）；
  - 完整休息一次结算、跳过/取消不发奖、旧配置迁移、跨日成长保留与组合服装绘制边界。
  - 免打扰到期、提前恢复、空闲期间到期、统计连续性，以及页面/托盘状态同步。
  - CSV 排序、保留窗口、非法记录过滤、取消/失败不覆盖文件；设置保存错误可见且成功后恢复。
  - Qt 渐变延迟追赶、连续切换、失败帧不成为衔接起点，以及停止后的迟到回调。
- 运行：
  ```powershell
  python -m unittest discover -s tests -v
  ```
- 规则：纯逻辑改动必须同步更新或新增用例；提交前必须本地全绿，不提交“跳过的测试”。

## 5. 构建与发布

- 打包：PyInstaller，入口 `build.ps1`，配置 `CareEyesPro.spec`。
- `build.ps1` 保留 UTF-8 BOM，确保 Windows PowerShell 5.1 正确解析中文字符串及变量插值。
  ```powershell
  pip install pyinstaller
  powershell -ExecutionPolicy Bypass -File .\build.ps1
  ```
- 产物：`dist/CareEyesPro.exe`；同步生成/更新根目录 `CareEyesPro.exe.sha256`。
- 版本：`mainpro.py` 顶部 `APP_VER` 与版本号同步；发布版本命名 `vX.Y`（次要版本=功能，不维护独立 changelog 分支）。

## 6. Git 提交规范

- 提交和推送须由用户明确要求；推送前确认远端、账号、目标分支及远端进度，不强推、不覆盖远端历史。
- 提交信息格式：`<type>: <简述>`，type 限 `feat` / `fix` / `chore` / `docs` / `test` / `refactor`。
- 粒度：一个可独立验证的改动一次提交；UI + runtime 解耦时分别提交。
- 提交前检查：`git status` 确认未把 `__pycache__/`、`build/`、`dist/`、`tmp/` 带入；这些目录均由 `.gitignore` 排除。
- 大文件（exe、图片）不入库；截图仅保留 `docs/images/` 下被 README 引用的部分。

## 7. 目录约定

- `docs/`：开发日志、文档；`docs/images/`：README 引用的图片；`docs/previews/`：预览生成脚本（工具性质，不参与构建）。
- `tmp/`：本地实验区，永不提交。
- `tests/` 只放测试代码，不放 fixture 外的临时产物。

## 8. 文档同步

- 改动影响以下任一时必须同步 `README.md`：
  - 快捷键、配置字段、默认值；
  - 打包方式、环境要求；
  - 新增 UI 截图。
- 每个 `vX.Y` 发布在 `docs/CHANGELOG.md` 追加一节，记录：新增 / 修复 / 破坏性变更 / 已知问题。
