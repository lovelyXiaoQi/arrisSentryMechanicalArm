# 哨戒动力臂 (Sentry Mechanical Arm)

基于我的世界网易ModAPI接口，将 **EP 军工** 的枪械与 **机械动力** 的应力网络结合成一座自动索敌炮塔。

灵感来源Java版 [机械动力：哨戒动力臂](https://github.com/Aupoex/Create-SentryMechanicalArm) 

> 作者: 棱花 Arris - lovely_小柒丫
> 版本: 0.0.1
>
> **本项目同时是 arrisCreate 扩展 API 的官方示例**
> 服务端入口 [ModServerSystem.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Server/ModServerSystem.py) 展示了 `Api.ExtensionApi` 的完整用法：
> ready 事件订阅 / `registerBlock` / `@registerComponent` / `arris.Component` / `arris.Field` /
> `hasCapability` + `registerArmPoint`（双端）/ 稳定符号直取 / 降级处理。
> 配套文档见主 mod 仓库的 `docs/EXTENSION-API.md`。

---

## 项目定位

本仓库是一个附属包 (AddOn Component)，**不独立运行**：

- **前置主包**: `arrisCreateScripts`（机械动力 - 提供应力网络 / ECS 世界 / 动力臂交互点框架）
- **前置模组**: `EpJxkScript`（EP 军工 - 提供枪械定义、射击 API、配件系统）
- **引擎最低版本**: ModAPI 3.8

玩家在世界中放置一块 `create:sentry_mechanical_arm` 方块，哨戒臂会：

1. 从机械动力网络消耗 **3 应力 / 小齿轮** 运转
2. 扫描 `scanRange` 半径内的敌对生物
3. 通过 EP 军工的 `Shoot()` API 发射其装备的枪械子弹

---

## 核心特性

| 能力 | 说明 |
| --- | --- |
| 自动索敌 | 默认按实体 type_family 扫描最近敌对生物；登记板自定义模式支持精确条目、`*` 通配、`!` 取反（游戏内 `?` 帮助页有教程） |
| 主人保护 | 放置者自动登记为主人（`ownerId`/`ownerName`），索敌永远绕过主人与创造模式玩家，优先级高于自定义规则 |
| 枪械装备 | 手持 EP 枪械对准方块按 `K` 键或 HUD 按钮即可装备；空手操作取回；保留枪械 `extraId` / 配件 / `userData`（弹药等级、皮肤）状态；bind 变体枪（so14 / holger26 / m4a1_ziptie）可正常识别 |
| 弹药系统 | 弹匣 (`currentMagazine`) + 备用库存 (`ammoReserve`)；支持普通动力臂自动补弹 / 回收 |
| 子弹等级 | 兼容 EP+ 子弹等级体系：接受 `EP_BULLET_SEQUENCE` 内任意等级弹，弹匣逐发记录等级、高级弹优先打出，伤害乘等级倍率（`BULLET_DATA['danger']`）；取出 / 掉落按实际等级返还不降级 |
| 枪械属性 | 完整还原 EP 枪械行为：伤害、射速、栓动/点射/自动、暴击、霰弹、音效、配件加成 |
| 应力联动 | `RPM = 0`、过载、红石锁定时立即停火 |
| IK 瞄准 | 客户端按帧率 (60 Hz+) 插值偏航/俯仰角，服务端 lerp 同步；RPM 越高转速越快 |
| 顶/底放置 | 点击方块下表面 → ceiling 倒挂模式；其他面 → floor 正置模式 |
| 破坏归还 | 方块被破坏时掉落装备的枪械和所有剩余弹药 |

---

## 目录结构

```
arrisSentryMechanicalArm/
├── SentryMechanicalArmBp/              # 行为包 (Behavior Pack)
│   ├── manifest.json
│   ├── blocks/
│   │   └── sentry_mechanical_arm.json  # 方块定义（AABB / 材质 / 状态 / 红石）
│   ├── entities/
│   │   └── sentry_mechanical_arm.json  # 客户端渲染载体实体
│   └── sentryMechanicalArmScripts/
│       ├── modMain.py                  # Mod 入口 (EasyMod 注册)
│       ├── QuModLibs/                  # 趣帆 QuMod 框架
│       └── Content/
│           ├── Shared/                 # 双端共享（纯逻辑，依赖注入）
│           │   ├── SentryArmRegistration.py # SentryArmComponent 定义 + ECS 注册
│           │   └── SentryArmEpCompat.py     # EP+ 数据兼容层(子弹等级/bind 枪/fireSpeed)
│           ├── Server/                 # 服务端逻辑
│           │   ├── ModServerSystem.py       # ECS 组件 / 交互点 / 放置规则注册
│           │   ├── SentryArmTargeting.py    # 状态机 + 扫描 + 射击
│           │   ├── SentryArmInteraction.py  # 装备/取出枪械 RPC
│           │   ├── SentryArmRuntimePoint.py # 动力臂补弹接口 (insert/extract)
│           │   └── SentryArmPlacement.py    # 顶/底面放置规则
│           └── Client/                 # 客户端逻辑
│               ├── ModClientSystem.py       # 客户端实体 / IK / Molang 同步
│               ├── SentryArmInteraction.py  # 准星检测 / 自定义按键
│               └── SentryArmHudProxy.py     # HUD 按钮面板代理
└── SentryMechanicalArmRp/              # 资源包 (Resource Pack)
    ├── manifest.json
    ├── blocks.json / entity/           # 方块与实体渲染配置
    ├── models/                         # 几何模型 (item/block/entity)
    ├── animations/                     # Molang 驱动的瞄准动画
    ├── render_controllers/
    ├── textures/ui/                    # 按钮贴图
    ├── ui/                             # HUD 修改 + 装备按钮面板
    └── texts/zh_CN.lang                # 中文本地化
```

---

## 运行状态机

服务端每 tick 在 [SentryArmTargeting.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Server/SentryArmTargeting.py) 中驱动：

```
         ┌──────────────────────────────────────────────────┐
         ▼                                                  │
   ┌─────────┐     发现目标     ┌─────────┐   瞄准对齐    ┌────┴────┐
   │  IDLE   ├────────────────▶│ AIMING  ├──────────────▶│  LOCKED │
   └─────────┘                 └─────────┘               └────┬────┘
        ▲   ┌─────────┐                                        │ 射击
        │   │SCANNING │◀──────────────── 冷却结束              ▼
        │   └────┬────┘                    ┌────────────┐  ┌──────────┐
        │        └────────────── 无目标 ◀──┤ COOLDOWN   │◀─┤ 扣减弹药  │
        │                                  └────────────┘  └──────────┘
        │   ┌──────────────┐   弹药补满
        └───┤ WAITING_AMMO │◀──── 弹匣空 + 库存空
            └──────────────┘
```

---

## ECS 组件

哨戒臂向主包 World 动态注册 `SentryArmComponent`：

| 字段 | 类型 | 说明 | 持久化 | 客户端同步 |
| --- | --- | --- | :-: | :-: |
| `state` | int | 状态机枚举 | O | O |
| `hasTarget` | bool | 是否锁定目标 | - | O |
| `targetX/Y/Z` | float | 世界坐标瞄准点 | - | O |
| `scanRange` | int | 扫描半径 (默认 32) | O | O |
| `redstoneLocked` | bool | 红石锁定停火 | O | O |
| `goggles` | bool | 工程师护目镜信息 | O | O |
| `weaponItemName` | str | 装备的枪械 ID | O | O |
| `weaponCustomTips` | str | 枪械自定义提示 | O | - |
| `weaponExtraId` | str | 枪械配件/皮肤数据 | O | - |
| `weaponUserData` | str | 枪械物品 userData 的 JSON 快照（取出时原样写回） | O | - |
| `magazineSize` | int | 装枪时持久化的弹匣容量（库存容量 = ×5；恒定判定防动力臂吞弹） | O | - |
| `currentMagazine` | int | 弹匣剩余 | O | O |
| `ammoReserve` | int | 备用弹药储量 | O | O |
| `bulletType` | str | 接受的弹药基础物品 ID（gun data.useBullet） | O | O |
| `reserveBulletType` | str | 库存实际存放的子弹物品名（可为高等级变体） | O | O |
| `magazineBulletList` | str | 弹匣逐发等级记录（EP bullet_list 数字串） | O | O |

初始化时通过主包 `Api.ExtensionApi.registerBlock(...)` 一并注入 `SixFacing` / `Network` / `RPM` / `StressConsumer` / `CogwheelType` + 自定义 `SentryArmComponent`。

---

## 与主包的集成点

分两类：**推荐走公共 API** 和**主 mod 内部路径**（后者未来可能 rename，目前路径稳定）。

### 公共 API — `arrisCreateScripts.Api.ExtensionApi`（facade，v3）

| 用法 | 用途 |
| --- | --- |
| `ServerExtensionApiReady` / `ClientExtensionApiReady` 事件 + `ext.isFrozen()` 兜底 | 注册入口（EXTENSION-API.md §1-§3） |
| `ext.registerBlock(blockName, components=[...])` | 方块的 ECS 组件配置 |
| `@ext.registerComponent` + `ext.Component` / `ext.Field` | 定义 `SentryArmComponent` 并挂到主 mod World 注册表 |
| `ext.registerSystem("ClientWorld", priority=2)` + `ext.System` | 客户端渲染系统挂进主包 ECS |
| `ext.hasCapability("arm_points")` + `ext.registerArmPoint(块名, "take_deposit", 交互点)` | 动力臂交互点（**双端各注册一次**：服务端带 RuntimePoint 实例，客户端只传块名 + 模式） |
| `ext.SixFacingComponent` / `ext.NetworkComponent` / `ext.RPMComponent` / `ext.StressConsumerComponent` / `ext.CogwheelTypeComponent` / `ext.CogSize` | 稳定符号直取（§5.1，替代旧版逐个 ImportModule 子模块） |

### 主 mod 内部路径（可用但非公共承诺）

| 集成方式 | 模块 | 用途 |
| --- | --- | --- |
| `PlacementRulesMeta._registry` | `...Server.Placements.Server` | 顶/底面放置规则（主 mod 未来可能提供公开 API） |
| `EventRegistry("BlockRemoveServerEvent")` | `...Server.EventRegistry` | 方块破坏时掉落武器 |
| `RotationRenderSystem._rotationOffset` | `...Client.Systems.RotationRenderSystem` | 齿轮 22.5° 对齐旋转 |
| `rescueMissingVisuals` / `forgetVisualRescue` / `resetVisualRescue` | `...Content.Client.ClientVisualRescue` | 客户端实体自愈补建（与主包大水车/动力臂共用一套节流与放弃记账） |
| `ServerWorld()` / `ClientWorld()` 单例 | `...Content.Server.ServerWorld` / `...Content.Client.ClientWorld` | 按坐标取 ECS 实体读写 `SentryArmComponent` |

### EP 军工

| 集成方式 | 模块 | 用途 |
| --- | --- | --- |
| `GetEplisItemData` | `EpJxkScriptClientSystem` | 读取配件加成后的完整枪械属性（bind 变体枪会 KeyError，由下行兜底） |
| `epApiClient.GetGunData`（经 `Shared/SentryArmEpCompat` bind 合并） | `EpJxkScript.Api.EpApiClient` | bind 变体枪的有效数据还原 / 无配件基础属性兜底 |
| `epBullet.EP_BULLET_SEQUENCE` / `BULLET_DATA` | `EpJxkScript.modCommon.epBullet` | 子弹等级序列与等级数据（伤害倍率；按调用时读取，兼容附属包运行时扩展） |
| `epApiServer.Shoot` | `EpJxkScript.Api.EpApiServer` | 服务端权威发射（伤害已预乘子弹等级倍率） |

---

## 交互方式

### 装备 / 取出枪械

1. 手持一把 EP 枪械准星对准哨戒臂 → HUD 显示 `[K]装备枪械` + 弹量提示
2. 按 `K` 键或点击 HUD 按钮 → 服务端校验 `IsGun` 后装备
3. 空手对准已装枪的哨戒臂 → `[K]取出枪械` → 归还枪 (保留弹匣) + 备用弹药

自定义按键在 `设置 → 按键 → 哨戒动力臂` 分类下可重绑。

### 动力臂补弹

普通机械臂识别哨戒臂上方 1.5 格的交互点：

- **insert**: 接受该枪弹药序列内任意等级子弹（库存同时只存一种等级，取空后可换）；上限 = 装枪时持久化的 `magazineSize × 5`
- **extract**: 只从 `ammoReserve` 按实际存放等级取料，不动已上膛的 `currentMagazine`

### 自定义索敌（登记板）

手持"哨戒动力臂自定义索敌设置"登记板：

- 左键实体 → 登记精确目标（玩家按名字、生物按实体 ID）
- 右键空气 → 打开管理 UI：输入框可手输匹配规则并回车提交
  （`1234*` 匹配 1234 开头、`!1234` 排除 1234；规则作用于实体 ID 与名字——玩家名/命名牌，取值只用
  `GetEngineTypeStr` + `GetName` 两个接口；`?` 按钮打开内置教程）
- 右键哨戒臂 → 应用配置并**立即生效**（当前目标/冷却即刻丢弃、按新配置重新扫描；列表为空则恢复默认敌对索敌）

### 红石信号

接收 consumer 信号时 `redstoneLocked = true`，哨戒臂立即停火并回到 IDLE。

---

## 视觉表现

- **齿轮旋转**: 服务端 `RPM` → 客户端 `actorRenderComp.SetEntityExtraUniforms(2, ...)` → 着色器 uniform
- **瞄准 IK**: 根据 `targetX/Y/Z - armCenter` 计算 `baseAngle` (偏航) 和 `clawAngle` (俯仰)，
  写入 `query.mod.arm_base_angle` 等 Molang 变量驱动动画
- **lerp 模型**: 一阶指数衰减 `angle += delta * (1 - exp(-rate * dt))`，`rate` 随 RPM 线性变化（RPM=256 时最快）
- **枪械绑定**: 通过 `BindItemToMinecraftModel` 把玩家装备的枪挂到 `item_locator` 骨骼上渲染

---

## 开发/调试

项目托管于 MCStudio，直接打开编辑器加载即可。关键调试点：

- 模块级 `_doRegister()` + `regModLoadFinishHandler` 双保险，解决主包加载时序不确定
- 热重载防重复：`World.getComponentClass("SentryArmComponent")` 命中已存在的类则复用
- 枪械缓存热更新：`_gunInfoCache` 以 `weaponItemName` 为 key，运行时换枪自动刷新
- 客户端实体生命周期跟随 ECS 活跃集：新增 → `CreateClientEntityByTypeStr`；移除 → `DestroyClientEntity`
- 客户端实体自愈：首建静默失败（blockPos 未就位 / LoaderSystem 未就绪 / 引擎返回 None）由主包
  `ClientVisualRescue` 节流补建（20 tick 一扫、单次限 2 个、连败 10 次放弃）；维度切换清空全部映射，
  维度回切 / 区块重激活走 `_reactivatedEntityIds` 分支补建并强制重推渲染参数

---

## 提示与限制

方块物品栏提示：

> **温馨提示**: 需要添加 EP 军工 / 机械动力 后才能正常使用；手持 EP 枪械自动索敌附近敌对生物；使用普通动力臂进行装填子弹。

已知依赖：独立加载时 `_getEpApiServer` 和主包 Import 都会返回 `None`，哨戒臂会直接跳过 tick，不会崩溃但也不工作。
