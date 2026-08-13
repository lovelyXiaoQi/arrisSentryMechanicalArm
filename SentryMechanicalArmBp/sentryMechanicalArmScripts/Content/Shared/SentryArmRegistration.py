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


def registerSentryArmEcs(arris):
    # type: (object) -> object | None
    """
    定义并注册 SentryArmComponent,把哨戒臂方块加入 CreateBlockInitComponent。

    Args:
        arris: ExtensionApiFacade 实例(由 arrisMod.getServerExtensionApi() /
            getClientExtensionApi() 取得)。本函数只用到 facade 的稳定公共面:
            World / registerComponent / Component / Field / registerBlock,
            以及 §5.1 稳定符号直取(ext.SixFacingComponent 等,由 facade
            __getattr__ 按 components → base → behaviours 的 __all__ 查找)。

    Returns:
        SentryArmComponent class,或 None 表示 facade 不可用。
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
            # 枪械物品 userData 的 JSON 快照(bullet_list / bullet_priority /
            # ep_skin 等 EP 侧状态)，取出/掉落时原样写回，避免弹药等级与皮肤丢失
            weaponUserData = arris.Field(default="", persistent=True)

            # ---- 弹药系统 ----
            # 注:新增字段后热重载会复用旧 Component 类(见上方 getComponentClass
            # 判重)，旧类实例上读这些字段需 getattr 兜底、写入仅本次会话有效；
            # 重进世界后按新类注册即恢复完整 persistent/synced 语义
            currentMagazine = arris.Field(default=0, persistent=True, synced=True)
            ammoReserve = arris.Field(default=0, persistent=True, synced=True)
            bulletType = arris.Field(default="", persistent=True, synced=True)
            # 装枪时同步持久化的弹匣容量(gun data.magazine)。库存容量 =
            # magazineSize × 5。必须是持久字段而非异步 gunInfo 缓存——
            # 动力臂 collect 的 simulate 预算和 deposit 的真实入库若读到
            # 不同容量,差额会永久滞留在动力臂爪子里(玩家视角=吞子弹)
            magazineSize = arris.Field(default=0, persistent=True)
            # EP+ 子弹等级支持:
            # reserveBulletType: 库存(ammoReserve)实际存放的子弹物品名，可为
            #   高等级变体；空串 = 按 bulletType(基础弹)处理。库存同时只存一种。
            reserveBulletType = arris.Field(default="", persistent=True, synced=True)
            # magazineBulletList: 弹匣逐发等级记录，EP bullet_list 规范数字串
            #   (每位 = 弹药序列下标，降序排列，开火从末尾消耗)
            magazineBulletList = arris.Field(default="", persistent=True, synced=True)

            # ---- 主人（放置者）----
            # 放置方块时由 SentryArmPlacement 暂存、Targeting 首个 tick 写入。
            # ownerId 是运行时实体 id（仅当前会话可靠），ownerName 跨会话稳定；
            # 索敌无条件绕过主人与创造模式玩家（优先级高于自定义规则）
            ownerId = arris.Field(default="", persistent=True, synced=True)
            ownerName = arris.Field(default="", persistent=True, synced=True)

            # ---- 自定义索敌 ----
            # 0 = DEFAULT (按 _HOSTILE_FAMILIES 索敌)
            # 1 = CUSTOM  (按 customTargets 中的 typeStr 列表精确索敌)
            targetMode = arris.Field(default=0, persistent=True, synced=True)
            # 逗号分隔的 typeStr 列表;玩家用 "minecraft:player@<name>" 编码
            customTargets = arris.Field(default="", persistent=True, synced=True)

        componentClass = SentryArmComponent

    # -------- Step 2+3: 在 CreateBlockInitComponent 注册方块 ECS 配置 --------
    # 主包稳定 Component 全部走 facade 直取(§5.1 稳定符号清单,
    # 不再 ImportModule 内部子模块路径)。
    # arris.registerBlock 是先到先得 + warn,重复调用安全
    arris.registerBlock(
        SENTRY_ARM_BLOCK,
        components=[
            (arris.SixFacingComponent,),
            (arris.NetworkComponent,),
            (arris.RPMComponent,),
            (arris.StressConsumerComponent, 3),  # 3 SU/RPM 消耗
            (arris.CogwheelTypeComponent, arris.CogSize.SMALL),
            (componentClass,),
        ],
    )

    return componentClass
