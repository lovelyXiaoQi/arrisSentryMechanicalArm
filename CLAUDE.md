# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目背景

这是一个基于网易 **MCStudio / ModAPI 3.8** 的**我的世界基岩版 AddOn 扩展**，依附于 `arrisCreate` 机械动力主 mod。项目只新增一个方块 —— `create:sentry_mechanical_arm`（哨戒动力臂），一座自动索敌哨戒动力臂，从 `EpJxkScript`（EP 军工）读取枪械数据并通过其 API 开火。

**本项目不独立运行**。运行时通过 `serverApi.ImportModule` / `clientApi.ImportModule` 访问：

- `arrisCreateScripts` —— 机械动力主 mod（ECS World、Apps、应力网络、运行时交互点注册表）
- `EpJxkScript` —— EP 军工（枪械定义、Shoot API、配件数据）

任一依赖缺失时 `_doRegister()` 返回 False、tick 路径全部 no-op；mod 不会崩溃但哨戒动力臂也不工作。**这是预期行为，不要加"依赖缺失时崩溃"之类的防御检查**。

本项目同时是 **`arrisCreate.Api.ExtensionApi` 的官方示例消费者**（见 README 顶部与 [ModServerSystem.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Server/ModServerSystem.py)）。修改公共 API 使用方式时需保持和主 mod `docs/EXTENSION-API.md` 对齐。

## 构建 / 运行 / 测试

- **没有构建步骤，也没有测试套件**。脚本是 Python 2 风格源码，由引擎解释加载。开发流程是"编辑 → MCStudio 热重载"。
- 开发世界配置在 [.mcdev.json](.mcdev.json)，`auto_hot_reload_mods: true` 已开启，保存即热更，无需重开世界。
- 包标识和版本号在两个 `manifest.json`（BP 和 RP）里，升级时**一起改**。
- **没有配置 linter / formatter**。代码使用 Python 2 兼容语法，类型注解写成 `# type: (...) -> ...` 形式的注释 —— 保持此风格，不要加 PEP 604 Union、f-string 或 `from __future__ import`，除非该文件已经用了。

## 架构

### 双包结构

- `SentryMechanicalArmBp/` —— 行为包。`blocks/`、`entities/`、`sentryMechanicalArmScripts/`（Python mod）。
- `SentryMechanicalArmRp/` —— 资源包。模型、动画、渲染控制器、HUD `ui/` 修改、`texts/zh_CN.lang`。

Python mod 由 [modMain.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/modMain.py) 使用 `QuModLibs/` 下的 **QuMod `EasyMod`** 框架注册。**把 `QuModLibs/` 视为 vendored 第三方，不要修改**。

### 服务端入口 —— `Content/Server/ModServerSystem.py`

下面几条模式在后续修改中必须保留：

1. **双保险注册** —— `_doRegister()` 在模块导入时调用一次，又通过 `@regModLoadFinishHandler` / `arris.onServerConfigFrozen` 再调一次。主 mod 加载顺序不确定，哪次在主 mod 之后执行哪次生效。**不要删掉 `_registered` 守卫**。
2. **热重载幂等** —— 在 `@World.registerComponent` 注册 `SentryArmComponent` 之前，先用 `World.getComponentClass("SentryArmComponent")` 复用已存在的类。删除这个检查会在重载时崩溃。
3. **组件注入是声明式的** —— 方块组件（`SixFacing`、`Network`、`RPM`、`StressConsumer(3)`、`CogwheelType(SMALL)`、`SentryArmComponent`）通过 `Api.ExtensionApi.registerBlock(...)` 一并挂载。**不要绕过这个 API 去直接改主 mod 内部状态**。
4. **服务端 tick 分发** —— `@Listen("OnScriptTickServer")` 必须写在入口模块；子模块里的 `@Listen` 不会被触发。新增每 tick 子系统时，从这里的 `_onServerTick` 统一调用。

### 哨戒动力臂状态机 —— `Content/Server/SentryArmTargeting.py`

`IDLE → SCANNING → AIMING → LOCKED → (射击) → COOLDOWN → LOCKED|SCANNING`，外加一条 `WAITING_AMMO` 分支用于弹匣和库存都空的情况。不变量：

- 每 tick 的前置检查：`weaponItemName`、`!redstoneLocked`、`RPM != 0`、`!overStressed`。任一失败必须回到 `IDLE` 并清掉 `_trackedTargets` / `_fireCooldowns` / `_gunInfoCache`，**不要让这些缓存泄漏**。
- **服务端和客户端用同一套 lerp 公式**跟踪瞄准角度（`SERVER_LERP_BASE = 1/1024`），只有 `baseDiff < AIM_THRESHOLD_DEG` 且 `headDiff < AIM_THRESHOLD_DEG`（5°）才允许开火。客户端视觉和服务端开火权威必须保持同步 —— **改一侧的 lerp 数学就要镜像改另一侧**。
- 枪械属性走 `EpJxkScriptClientSystem.GetEplisItemData`（包含配件加成）；`EpApiClient.GetGunInfo` 是无配件的兜底。缓存 key 是 `weaponItemName`，运行时换枪会自动失效。
- 弹药是 ECS 持久化状态。弹匣补弹代价是 `reloadEmptyTick × 30` tick 的 `COOLDOWN` + 换弹音效。备用弹药 `ammoReserve` 通过运行时交互点由普通机械臂注入。

### 客户端的 `OnScriptTickClient` vs `GameRenderTickEvent` 分离

[ModClientSystem.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Client/ModClientSystem.py) 故意把两步拆开：

- `OnScriptTickClient`（30 Hz）—— 读 `SentryArmComponent.targetX/Y/Z`，算出目标角度，**只存入 `_renderTargets`，这里不做 lerp**。
- `GameRenderTickEvent`（帧率）—— 按 `dt` 驱动一阶指数 lerp，写入 `query.mod.arm_base_angle` / `arm_claw_angle` 等 Molang 变量。

这是瞄准动画能在 30 FPS 以上流畅的原因。**不要把 lerp 搬回 tick 处理函数里**。

倒挂朝向（`SixFacingComponent.facing == 0`）会翻转 Y 轴并给偏航角补 180° —— **动到瞄准数学时必须同时处理正置和倒挂**。

### 运行时交互点 —— `SentryArmRuntimePoint.py`

用 `Registry.registerBlockType(..., "take_deposit")` 注册，让**普通机械臂**能把物品往返哨戒臂。交互点位置为 `blockPos + (0.5, 1.5, 0.5)`，朝向 UP。规则：

- `insert` 只接受 `newItemName == comp.bulletType` 的物品；库存容量 = `magazine × 5`。
- `extract` 只从 `ammoReserve` 取料，**绝不动 `currentMagazine`**（已上膛的子弹跟着枪走）。

### 放置规则 —— `SentryArmPlacement.py`

注入到 `PlacementRulesMeta._registry`。点击方块**下表面**（`face == 0`）设 `create:ceiling=true`（倒挂）；其他面默认正置。aux 数据由 `BlockStateApi.getAuxFromBlockStates` 产出。

### 破坏掉落 —— `SentryArmInteraction._onBlockRemove`

通过 `EventRegistry.registerHandler("BlockRemoveServerEvent")` 订阅。主 mod dispatch 此事件是**在拆除方块实体之前**，此时 `comp.weaponItemName` 仍可读。掉落武器（`extraId = current magazine`）+ 掉落备用弹药这两步必须留在这个 handler 里，**不要挪到更晚的事件**。

### 客户端交互 / HUD

- [Client/SentryArmInteraction.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Client/SentryArmInteraction.py) 每 tick 调 `PickFacing()` 并用 `EpApiClient.IsGun` 判定显示什么 HUD 文本。`K` 键（分类"哨戒动力臂"）触发 RPC。
- [SentryArmHudProxy.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Client/SentryArmHudProxy.py) 是绑定到 `hud.hud_screen` 的 `CustomUIScreenProxy` —— 按钮面板通过 `ui/hud_screen.json` modifications 注入。`getHudProxy()` 是单例访问器，**不要自己构造新实例**。

### 枪械渲染

`BindItemToMinecraftModel(clientEid, itemDict, "item_locator", True, offset, rotation, 0.15)`。注意：**`modelId == 0` 是合法 ID**（和主 mod 行为一致）。存在性判断绝对不能写 `if modelId:`，要写 `modelId is not None and modelId != -1`。

## 本仓库特有的约定

- **常量**：`SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"`、`_MAIN_PACK = "arrisCreateScripts"`、`_EP_PACK = "EpJxkScript"` 在跨模块文件中大量重复。**拼写要完全一致** —— 它们同时作为 dict key 和 `ImportModule` 路径使用。
- **Molang 查询命名空间**：所有自定义 query 都放在 `query.mod.arm_*` 下（在 `ModClientSystem._registerMolangQueries` 里注册）。新增动画驱动字段需要同时在那里注册 query，并在客户端 tick 或 render tick 里写入值。
- **RPC 表面**：`@AllowCall @InjectHttpPlayerId` 装饰的 `equipGunToSentry(playerId, data)` 是客户端唯一可调的服务端入口。**服务端必须再校验一次所有输入**（`_isGun`、方块名），因为客户端不可信。
- **类型注解**：一律用 Python 2 的 `# type:` 注释形式，例如 `# type: (tuple, int) -> dict | None`。不要转成真正的注解语法。
- **稳定性标注**（来自 README）：优先用公共 API `arris.registerBlock` / `arris.registerComponent`。对 `PlacementRulesMeta._registry`、`Registry._registry`、`_rotationOffset` 的使用是内部路径 —— 目前能用但主 mod 将来可能改名；**把这些调用集中写在 `_doRegister` 里**，未来迁移时方便替换。
