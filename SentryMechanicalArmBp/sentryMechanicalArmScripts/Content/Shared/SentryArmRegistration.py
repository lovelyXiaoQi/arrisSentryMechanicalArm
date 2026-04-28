# -*- coding: utf-8 -*-
"""
SentryArmRegistration - 哨戒动力臂 ECS 注册（双端共享）

服务端、客户端各自的入口模块都必须调用 registerSentryArmEcs() 一次,
原因:
    - arris.World._componentRegistry 是 per-process 的,联机时房主和加入玩家是
      两个不同的 Python 进程,各自需要登记 SentryArmComponent
    - arris.registerBlock(...) 写入 CreateBlockInitComponent 字典,主 mod 客户端
      ModClientSystem 在 ModBlockEntityLoadedClientEvent 里 gate:
          if blockName not in CreateBlockInitComponent: return
      所以客户端这个字典必须有 create:sentry_mechanical_arm 条目,否则远端
      玩家放置 / 看到的哨戒动力臂方块永远不会创建客户端 ECS 实体,SentryArmRenderSystem
      也就永远不会被 onEntityAdded 通知 → 看不到旋转 / 武器 / 瞄准动画

服务端额外的注册项 (RuntimePoint / PlacementRules) 仍保留在 ModServerSystem。
"""

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"


def registerSentryArmEcs(arris, importMainModule):
    # type: (object, callable) -> object | None
    """
    定义并注册 SentryArmComponent,把哨戒臂方块加入 CreateBlockInitComponent。

    Args:
        arris: arrisCreateScripts.Api.ExtensionApi 模块(已 ImportModule)
        importMainModule: callable(relPath) → module, 形如 lambda p: serverApi.ImportModule("arrisCreateScripts." + p)

    Returns:
        SentryArmComponent class,或 None 表示主 mod 的稳定 Components 还没就绪。
    幂等:重复调用为 no-op。
    """
    # -------- Step 1: 定义并注册 SentryArmComponent --------
    componentClass = arris.World.getComponentClass("SentryArmComponent")
    if componentClass is None:

        @arris.registerComponent
        class SentryArmComponent(arris.Component):
            """哨戒机械臂 ECS 组件"""

            # ---- 状态机 ----
            state = arris.Field(default=0, persistent=True, synced=True)
            hasTarget = arris.Field(default=False, synced=True)
            targetX = arris.Field(default=0.0, synced=True)
            targetY = arris.Field(default=0.0, synced=True)
            targetZ = arris.Field(default=0.0, synced=True)

            # ---- 配置与锁定 ----
            scanRange = arris.Field(default=32, persistent=True, synced=True)
            redstoneLocked = arris.Field(default=False, persistent=True, synced=True)
            goggles = arris.Field(default=False, persistent=True, synced=True)

            # ---- 装备的枪械 ----
            weaponItemName = arris.Field(default="", persistent=True, synced=True)
            weaponCustomTips = arris.Field(default="", persistent=True)
            weaponExtraId = arris.Field(default="", persistent=True)

            # ---- 弹药系统 ----
            currentMagazine = arris.Field(default=0, persistent=True, synced=True)
            ammoReserve = arris.Field(default=0, persistent=True, synced=True)
            bulletType = arris.Field(default="", persistent=True, synced=True)

            # ---- 自定义索敌 ----
            # 0 = DEFAULT (按 _HOSTILE_FAMILIES 索敌)
            # 1 = CUSTOM  (按 customTargets 中的 typeStr 列表精确索敌)
            targetMode = arris.Field(default=0, persistent=True, synced=True)
            # 逗号分隔的 typeStr 列表;玩家用 "minecraft:player@<name>" 编码
            customTargets = arris.Field(default="", persistent=True, synced=True)

        componentClass = SentryArmComponent

    # -------- Step 2: 从主 mod 按需拉取稳定 Component --------
    facingMod = importMainModule("Content.Shared.Components.FacingComponent")
    networkMod = importMainModule("Content.Shared.Components.NetworkComponent")
    rpmMod = importMainModule("Content.Shared.Components.RPMComponent")
    stressConsumerMod = importMainModule("Content.Shared.Components.StressConsumerComponent")
    cogwheelTypeMod = importMainModule("Content.Shared.Components.CogwheelTypeComponent")

    if not all([facingMod, networkMod, rpmMod, stressConsumerMod, cogwheelTypeMod]):
        return None

    # -------- Step 3: 在 CreateBlockInitComponent 注册方块 ECS 配置 --------
    # arris.registerBlock 是先到先得 + warn,重复调用安全
    arris.registerBlock(
        SENTRY_ARM_BLOCK,
        components=[
            (facingMod.SixFacingComponent,),
            (networkMod.NetworkComponent,),
            (rpmMod.RPMComponent,),
            (stressConsumerMod.StressConsumerComponent, 3),  # 3 SU/RPM 消耗
            (cogwheelTypeMod.CogwheelTypeComponent, cogwheelTypeMod.CogSize.SMALL),
            (componentClass,),
        ],
    )

    return componentClass
