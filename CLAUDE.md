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

1. **双保险注册** —— `_setupRegistration()` 在模块导入时订阅主包 `ServerExtensionApiReady` 事件，并用 `ext.isFrozen()` 兜底（订阅来得晚、主包已 freeze 时立即注册一次）。主 mod 加载顺序不确定，哪条路径先到哪条生效。**不要删掉 `_registered` 守卫**。
2. **热重载幂等** —— 在 `@World.registerComponent` 注册 `SentryArmComponent` 之前，先用 `World.getComponentClass("SentryArmComponent")` 复用已存在的类。删除这个检查会在重载时崩溃。
3. **组件注入是声明式的** —— 方块组件（`SixFacing`、`Network`、`RPM`、`StressConsumer(3)`、`CogwheelType(SMALL)`、`SentryArmComponent`）通过 `Api.ExtensionApi.registerBlock(...)` 一并挂载。**不要绕过这个 API 去直接改主 mod 内部状态**。
4. **服务端 tick 分发** —— `@Listen("OnScriptTickServer")` 必须写在入口模块；子模块里的 `@Listen` 不会被触发。新增每 tick 子系统时，从这里的 `_onServerTick` 统一调用。

### 哨戒动力臂状态机 —— `Content/Server/SentryArmTargeting.py`

`IDLE → SCANNING → AIMING → LOCKED → (射击) → COOLDOWN → LOCKED|SCANNING`，外加一条 `WAITING_AMMO` 分支用于弹匣和库存都空的情况。不变量：

- 每 tick 的前置检查：`weaponItemName`、`!redstoneLocked`、`RPM != 0`、`!overStressed`。任一失败必须回到 `IDLE` 并清掉 `_trackedTargets` / `_fireCooldowns` / `_gunInfoCache`，**不要让这些缓存泄漏**。
- **服务端和客户端用同一套 lerp 公式**跟踪瞄准角度（`SERVER_LERP_BASE = 1/1024`），只有 `baseDiff < AIM_THRESHOLD_DEG` 且 `headDiff < AIM_THRESHOLD_DEG`（5°）才允许开火。客户端视觉和服务端开火权威必须保持同步 —— **改一侧的 lerp 数学就要镜像改另一侧**。
- 枪械属性走 `EpJxkScriptClientSystem.GetEplisItemData`（包含配件加成）；`EpApiClient.GetGunInfo` 是无配件的兜底。缓存 key 是 `weaponItemName`，运行时换枪会自动失效。
- 弹药是 ECS 持久化状态。弹匣补弹代价是 `reloadEmptyTick × 30` tick 的 `COOLDOWN` + 换弹音效。备用弹药 `ammoReserve` 通过运行时交互点由普通机械臂注入。
- **EP+ 子弹等级**：`bulletType` 只存基础弹名（gun data.useBullet）；库存实际弹种在 `reserveBulletType`（同时只存一种），弹匣逐发等级在 `magazineBulletList`（EP bullet_list 数字串：每位 = 弹药序列下标，降序排列，末尾先打 → 高级弹优先）。补弹统一走 `_refillMagazine`，射击按末位数字还原弹种并把伤害乘 `BULLET_DATA['danger']`。**不要绕过 `_refillMagazine` 直接改 `currentMagazine`**，数字串会和弹匣数失同步。
- **fireSpeed 双语义**：EP 新版 `< 1` 是秒、`>= 1` 是 tick（对齐 `gunFire._startFireInterval`）。任何读 `gunInfo["fireSpeed"]` 的地方必须过 `EpCompat.fireSpeedToTicks`——直接 `int()` 会把 0.074 截成 0 → 射速失控。
- **主人与创造豁免**：放置时 `SentryArmPlacement.onPlace` 暂存放置者（该事件时刻 ECS 实体尚未创建），`_tickSentryArm` 首帧经 `consumePendingOwner` 写入 `ownerId`（运行时 id，仅当前会话）/ `ownerName`（跨会话）。索敌**无条件**绕过主人与创造模式玩家，优先级高于自定义规则（含纯取反"打一切"列表）；`_tickShooting` 射击前经 `_isExemptTarget` 复检，覆盖锁定期间目标切创造的情况。登记板应用/清空配置后必须调 `SentryArmTargeting.resetTargeting`（丢目标/射击冷却/扫描间隔，保留瞄准角度跟踪）——**不要只写 `customTargets` 而不重置状态机**，否则旧目标会被继续锁定。
- **自定义索敌匹配**统一走 `Shared/SentryTargetMatcher`：匹配键只用两个引擎接口——`GetEngineTypeStr`（实体ID）+ `GetName`（玩家名/命名牌名，**所有实体统一取**）。`customTargets` 逗号分隔 token，支持精确条目、`*` 通配、`!` 取反；`minecraft:player@名字` 玩家条目要求 typeStr 必须是玩家（防同名命名牌生物冒充）；纯取反列表 = 排除之外全部命中。登记板手输规则在 board userData 里以 `typeStr == "custom:pattern"` 存储（`TargetMatcher.CUSTOM_PATTERN_TYPE`）。改匹配语义必须同步 UI 帮助蒙层文案（`SentryMechanicalArmRp/ui/sentry_target_manage.json` 的帮助 label）。

### EP+ 数据兼容层 —— `Content/Shared/SentryArmEpCompat.py`

EP+ 3.5x 新数据形态的唯一适配点。双端共享**纯逻辑**模块：不 import 引擎跳板，`epBullet` 模块 / `GetGunData` 回调由调用方注入（遵循 `SentryArmRegistration` 的依赖注入模式）。

- **子弹等级**：`EP_BULLET_SEQUENCE`（基础弹 → 高→低等级变体列表）/ `BULLET_DATA`（`danger` 伤害倍率、穿甲数据）；弹匣逐发记录的数字串编解码（`parseMagList` / `magListToStr` / `mergeMagList`）。
- **bind 变体枪**（so14 / holger26 / m4a1_ziptie 等）：自身 JSON 无顶层 `type`/`useBullet`，EP 原生 `IsGun`/`GetGunInfo` 判失败、`GetEplisItemData` 直接 KeyError —— 一律走 `isGunWithBind` / `getGunInfoWithBind` / `resolveGunData`（按 EP 合并语义：bind 本体为底、自身字段覆盖）。
- 枪械物品 `userData`（bullet_list / bullet_priority / ep_skin）装备时经 `dumpUserData` 快照进 `weaponUserData` 字段，取出/掉落时原样写回。**不要丢 userData**——会重置玩家枪的皮肤与弹药等级。
- epBullet / EpApiClient 引用**按调用时读取**（不 snapshot 字典内容），枪械附属包运行时扩展序列也能生效。服务端 epBullet 缓存 getter 在 `SentryArmTargeting._getEpBullet`，客户端在 `Client/SentryArmInteraction._getEpBullet`。

### 客户端渲染 —— `SentryArmRenderSystem.py` 的 tick / 渲染帧分离

[SentryArmRenderSystem.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Client/SentryArmRenderSystem.py) 通过 `@ext.registerSystem("ClientWorld")` 挂进主包 ECS，故意把两步拆开：

- `update()`（ECS tick，30 Hz）—— 读 `SentryArmComponent.targetX/Y/Z`，算出目标角度，**只存入 `_renderTargets`，这里不做 lerp**。
- `updateFrame()`（渲染帧率）—— 按 `dt` 驱动一阶指数 lerp，写入 `query.mod.arm_base_angle` / `arm_claw_angle` 等 Molang 变量。

这是瞄准动画能在 30 FPS 以上流畅的原因。**不要把 lerp 搬回 tick 处理函数里**。

倒挂朝向（`SixFacingComponent.facing == 0`）会翻转 Y 轴并给偏航角补 180° —— **动到瞄准数学时必须同时处理正置和倒挂**。

**客户端实体自愈**（对齐主包 `ClientVisualRescue`）：`onEntityAdded` 一次性建实体有三个静默失败出口（`blockPos` 未就位 / `LoaderSystem` 未就绪 / 引擎返回 None），补建统一走主包 `Content.Client.ClientVisualRescue.rescueMissingVisuals`（20 tick 节流 + 单次 2 个限额 + 连败 10 次放弃），`update()` 里另有 `_reactivatedEntityIds` 分支处理维度回切/区块重激活。三条不变量：`_clientEntityIds` 只在创建成功后写入（骗过补建判据会永久漏建）；`onEntityRemoved` 必须调 `forgetVisualRescue`（原位重放的方块实体 id 相同，不清会继承失败计数）；`onDimensionChanged` 必须清空全部映射并调 `resetVisualRescue`（引擎已销毁所有客户端实体）。**不要把补建改回每 tick 裸重试，也不要删这两处清理**。

### 运行时交互点 —— `SentryArmRuntimePoint.py`

用 v3 公共接口 `ext.registerArmPoint(SENTRY_ARM_BLOCK, "take_deposit", SentryArmRuntimePoint())` 注册（capability `arm_points`，**双端各注册一次**：服务端在 `ModServerSystem._doRegister`，客户端在 `ModClientSystem._doRegisterClient` 只传方块名 + 模式。缺客户端那份，联机远端玩家手持动力臂点不中哨戒臂；缺服务端那份，能打点但搬不动）。让**普通机械臂**能把物品往返哨戒臂。交互点位置为 `blockPos + (0.5, 1.5, 0.5)`，朝向 UP。规则：

- `insert` 接受该枪弹药序列（`EP_BULLET_SEQUENCE[useBullet]`）内**任意等级**子弹；库存同时只存一种等级（`reserveBulletType`），已有存货只收同名弹；容量 = 装枪时持久化的 `magazineSize × 5`。**容量判定必须恒定**：动力臂 collect 的 simulate 预算与 deposit 的真实入库若读到不同容量，差额会滞留在动力臂爪子里（玩家视角=吞子弹）——不要改回用异步 `_gunInfoCache` 算容量。
- `extract` 只从 `ammoReserve` 取料且按 `reserveBulletType` 实际等级返还（不降级），**绝不动 `currentMagazine`**（已上膛的子弹跟着枪走）。
- **必须与主包 `RuntimePoint` 基类全接口同形**：主包 `MechanicalArmSystem` 会无守卫直调 `extractDistributable()`（哨戒臂被配成输入点时逐 tick）与 `popContainerItem()`（每次 insert 成功后），缺方法 = AttributeError 掀掉整个服务端 ECS tick。**不要删这两个方法**。

### 放置规则 —— `SentryArmPlacement.py`

注入到 `PlacementRulesMeta._registry`。点击方块**下表面**（`face == 0`）设 `create:ceiling=true`（倒挂）；其他面默认正置。aux 数据由 `BlockStateApi.getAuxFromBlockStates` 产出。

### 破坏掉落 —— `SentryArmInteraction._onBlockRemove`

通过 `EventRegistry.registerHandler("BlockRemoveServerEvent")` 订阅。主 mod dispatch 此事件是**在拆除方块实体之前**，此时 `comp.weaponItemName` 仍可读。掉落武器（`extraId` = 弹匣数、`userData['bullet_list']` = 逐发等级、其余 userData 原样写回）+ 按 `reserveBulletType` 实际等级掉落备用弹药这两步必须留在这个 handler 里，**不要挪到更晚的事件**。武器/弹药物品字典统一由 `_buildWeaponItemDict` / `_buildReserveItemDict` 构造（取出与掉落共用）。

### 客户端交互 / HUD

- [Client/SentryArmInteraction.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Client/SentryArmInteraction.py) 每 tick 调 `PickFacing()` 并用 `EpCompat.isGunWithBind`（`EpApiClient.IsGun` + bind 变体枪兜底）判定显示什么 HUD 文本；弹药装填判定 `_canLoadBullet` 必须与服务端 `RuntimePoint.insert` 门禁一致（序列内任意等级 + 库存同名门禁）。`K` 键（分类"哨戒动力臂"）触发 RPC。
- [SentryArmHudProxy.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Client/SentryArmHudProxy.py) 是绑定到 `hud.hud_screen` 的 `CustomUIScreenProxy` —— 按钮面板通过 `ui/hud_screen.json` modifications 注入。`getHudProxy()` 是单例访问器，**不要自己构造新实例**。
- [Client/SentryTargetManageUi.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Client/SentryTargetManageUi.py) 是登记板管理 UI（`sentry_target_manage.sentry_screen`）。`common.base_screen` 内容路径前缀与主包 `PackageFilterUi` 同源——`$screen_content` 面板的**子控件**直接挂在 `root_screen_panel` 下（路径不含面板自身名）；jsonui 里挪动输入框时要同步 `_EDIT_BOX` 常量。输入框提交走 `BF_EditFinished`（回车/失焦；`SetEditText("")` 清空会再触发一次，空串分支必须保留静默忽略）。帮助提示显隐走 `#help_page_visible` **双属性绑定**（`help_bg` 面板与 `background` 暗色蒙层两处，各挂 `#visible` + `#enabled` 一对；`binding_condition` 必须 `always`——`always_when_visible` 在隐藏后不再评估、就永远显示不出来；json 静态 `visible/enabled` 均 false 兜底首帧）。不要删掉 `#enabled` 那份绑定（隐藏面板内的按钮不应可焦点）或改回 SetVisible 路径调用。帮助层已无 modal，若将来加回 modal 控件必须同样受 `#enabled` 绑定约束。

### 枪械渲染

`BindItemToMinecraftModel(clientEid, itemDict, "item_locator", True, offset, rotation, 0.15)`。注意：**`modelId == 0` 是合法 ID**（和主 mod 行为一致）。存在性判断绝对不能写 `if modelId:`，要写 `modelId is not None and modelId != -1`。

## 本仓库特有的约定

- **常量**：`SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"`、`_MAIN_PACK = "arrisCreateScripts"`、`_EP_PACK = "EpJxkScript"` 在跨模块文件中大量重复。**拼写要完全一致** —— 它们同时作为 dict key 和 `ImportModule` 路径使用。
- **Molang 查询命名空间**：所有自定义 query 都放在 `query.mod.arm_*` 下（在 `ModClientSystem._registerMolangQueries` 里注册）。新增动画驱动字段需要同时在那里注册 query，并在客户端 tick 或 render tick 里写入值。
- **RPC 表面**：`@AllowCall @InjectHttpPlayerId` 装饰的 `equipGunToSentry(playerId, data)` 是客户端唯一可调的服务端入口。**服务端必须再校验一次所有输入**（`_isGun`、方块名），因为客户端不可信。
- **类型注解**：一律用 Python 2 的 `# type:` 注释形式，例如 `# type: (tuple, int) -> dict | None`。不要转成真正的注解语法。
- **稳定性标注**（来自 README）：优先用公共 API `arris.registerBlock` / `arris.registerComponent` / `arris.registerArmPoint`（v3 起交互点注册已收口为公共接口），主包稳定 Component 走 facade 直取（`ext.SixFacingComponent` 等 §5.1 稳定符号）。对 `PlacementRulesMeta._registry`、`_rotationOffset`、`ClientVisualRescue`、`EventRegistry`、`ServerWorld`/`ClientWorld` 单例的使用是内部路径 —— 目前能用但主 mod 将来可能改名；**注册类调用集中写在 `_doRegister` 里**，未来迁移时方便替换。
- **对主包 EXTENSION-API 文档的已知偏差（有意保留，勿"修复"）**：方块 ID 用了 `create:` 前缀（文档 §7 预留给主包）、`SentryArmComponent` 无 mod 前缀、渲染系统 `priority=2`（文档建议 ≥500，这里刻意与主包 `MechanicalArmRenderSystem` 对齐）。三者均已核实与主包当前无冲突；若主包未来占用同名需迁移。空手右键被主包 CIM gatekeeper 吃掉是已知行为——取枪走 `K` 键，若要支持空手右键需补 `ext.registerEmptyHandPassthrough`（§15.9）。
