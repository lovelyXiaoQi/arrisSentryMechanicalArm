# 哨戒动力臂 (Sentry Mechanical Arm)

基于我的世界网易ModAPI接口，将 **EP 军工** 的枪械与 **机械动力** 的应力网络结合成一座自动索敌炮塔。

灵感来源Java版 [机械动力：哨戒动力臂](https://github.com/Aupoex/Create-SentryMechanicalArm) 

> 作者: 棱花 Arris - lovely_小柒丫
> 版本: 0.0.1
>
> **本项目同时是 arrisCreate 扩展 API 的官方示例**
> 服务端入口 [ModServerSystem.py](SentryMechanicalArmBp/sentryMechanicalArmScripts/Content/Server/ModServerSystem.py) 展示了 `Api.ExtensionApi` 的完整用法：
> `registerBlock` / `@registerComponent` / `arris.Component` / `arris.Field` / `onServerConfigFrozen` / 降级处理。
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
| 自动索敌 | 基于 EntityType 位掩码 (Monster / Hostile / Undead / Zombie / Skeleton / Arthropod) 扫描最近的敌对生物 |
| 枪械装备 | 手持 EP 枪械对准方块按 `K` 键或 HUD 按钮即可装备；空手操作取回；保留枪械 `extraId` / 配件状态 |
| 弹药系统 | 弹匣 (`currentMagazine`) + 备用库存 (`ammoReserve`)；支持普通动力臂自动补弹 / 回收 |
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
| `currentMagazine` | int | 弹匣剩余 | O | O |
| `ammoReserve` | int | 备用弹药储量 | O | O |
| `bulletType` | str | 接受的弹药物品 ID | O | O |

初始化时通过主包 `Api.ExtensionApi.registerBlock(...)` 一并注入 `SixFacing` / `Network` / `RPM` / `StressConsumer` / `CogwheelType` + 自定义 `SentryArmComponent`。

---

## 与主包的集成点

分两类：**推荐走公共 API** 和**主 mod 内部路径**（后者未来可能 rename，目前路径稳定）。

### 公共 API — `arrisCreateScripts.Api.ExtensionApi`（Phase A/B）

| 用法 | 用途 |
| --- | --- |
| `arris.registerBlock(blockName, components=[...])` | 方块的 ECS 组件配置（取代旧版 `SetCreateBlockInitComponents`） |
| `@arris.registerComponent` + `arris.Component` / `arris.Field` | 定义 `SentryArmComponent` 并挂到主 mod World 注册表 |
| `arris.onServerConfigFrozen(_doRegister)` | 兜底：所有 mod 加载完毕后重试注册 |

### 稳定 Component 路径（EXTENSION-API.md "稳定 Component 清单"）

| 模块 | 用途 |
| --- | --- |
| `...Content.Shared.Components.FacingComponent` | `SixFacingComponent` |
| `...Content.Shared.Components.NetworkComponent` | `NetworkComponent` |
| `...Content.Shared.Components.RPMComponent` | `RPMComponent` |
| `...Content.Shared.Components.StressConsumerComponent` | `StressConsumerComponent(3)` — 3 SU/RPM |
| `...Content.Shared.Components.CogwheelTypeComponent` | `CogwheelTypeComponent(CogSize.SMALL)` |

### 主 mod 内部路径（可用但非公共承诺）

| 集成方式 | 模块 | 用途 |
| --- | --- | --- |
| `Registry.registerBlockType` / `registerRuntimePoint` | `...Server.Helpers.RuntimePointRegistry` | 让普通动力臂识别哨戒臂为 "take_deposit" 交互点 |
| `PlacementRulesMeta._registry` | `...Server.Placements.Server` | 顶/底面放置规则（主 mod 未来可能提供公开 API） |
| `EventRegistry("BlockRemoveServerEvent")` | `...Server.EventRegistry` | 方块破坏时掉落武器 |
| `RotationRenderSystem._rotationOffset` | `...Client.Systems.RotationRenderSystem` | 齿轮 22.5° 对齐旋转 |

### EP 军工

| 集成方式 | 模块 | 用途 |
| --- | --- | --- |
| `GetEplisItemData` | `EpJxkScriptClientSystem` | 读取配件加成后的完整枪械属性 |
| `epApiServer.Shoot` | `EpJxkScript.Api.EpApiServer` | 服务端权威发射 |

---

## 交互方式

### 装备 / 取出枪械

1. 手持一把 EP 枪械准星对准哨戒臂 → HUD 显示 `[K]装备枪械` + 弹量提示
2. 按 `K` 键或点击 HUD 按钮 → 服务端校验 `IsGun` 后装备
3. 空手对准已装枪的哨戒臂 → `[K]取出枪械` → 归还枪 (保留弹匣) + 备用弹药

自定义按键在 `设置 → 按键 → 哨戒动力臂` 分类下可重绑。

### 动力臂补弹

普通机械臂识别哨戒臂上方 1.5 格的交互点：

- **insert**: 只接受 `comp.bulletType` 匹配的弹药物品；上限 = `magazine × 5`
- **extract**: 只从 `ammoReserve` 取料，不动已上膛的 `currentMagazine`

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

---

## 提示与限制

方块物品栏提示：

> **温馨提示**: 需要添加 EP 军工 / 机械动力 后才能正常使用；手持 EP 枪械自动索敌附近敌对生物；使用普通动力臂进行装填子弹。

已知依赖：独立加载时 `_getEpApiServer` 和主包 Import 都会返回 `None`，哨戒臂会直接跳过 tick，不会崩溃但也不工作。
