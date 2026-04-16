# -*- coding: utf-8 -*-
"""
ModServerSystem - 哨戒机械臂服务端入口

职责:
1. 通过 ImportModule 向主包注册 ECS 组件配置
2. 注册动力臂交互点 (RuntimePointRegistry)
3. 注册放置规则 (PlacementRulesMeta)
"""

import traceback

from ...QuModLibs.Server import Listen, regModLoadFinishHandler, serverApi

# 导入交互模块（触发 @AllowCall 注册）
# 导入瞄准模块
from . import (
    SentryArmInteraction as _sentryInteraction,  # noqa: F401
    SentryArmTargeting as _sentryTargeting,
)


# 在入口模块注册服务端 tick（子模块的 @Listen 不生效）
@Listen("OnScriptTickServer")
def _onServerTick(args=None):
    _sentryTargeting._onServerTick(args)


SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_MAIN_PACK = "arrisCreateScripts"
_registered = False

compFactory = serverApi.GetEngineCompFactory()


def _importMain(path):
    # type: (str) -> object | None
    return serverApi.ImportModule(_MAIN_PACK + "." + path)


def _doRegister():
    # type: () -> bool
    """向主包注册 ECS 组件配置、交互点、放置规则。成功返回 True。"""
    global _registered
    if _registered:
        return True

    CreateConfig = _importMain("Content.Shared.Config.CreateConfig")
    Registry = _importMain("Content.Server.Helpers.RuntimePointRegistry")
    PlacementServer = _importMain("Content.Server.Placements.Server")
    SharedWorld = _importMain("Content.Shared.World")
    SharedComponent = _importMain("Content.Shared.Component")
    SharedField = _importMain("Content.Shared.Field")

    if not CreateConfig or not Registry or not SharedWorld or not SharedComponent or not SharedField:
        return False

    # 1. 动态创建 SentryArmComponent 并注册到主包 World
    World = SharedWorld.World
    Component = SharedComponent.Component
    Field = SharedField.Field

    # 防止重复定义（热重载）
    existing = World.getComponentClass("SentryArmComponent")
    if not existing:

        @World.registerComponent
        class SentryArmComponent(Component):
            """哨戒机械臂 ECS 组件"""

            state = Field(default=0, persistent=True, synced=True)
            hasTarget = Field(default=False, synced=True)
            targetX = Field(default=0.0, synced=True)
            targetY = Field(default=0.0, synced=True)
            targetZ = Field(default=0.0, synced=True)
            scanRange = Field(default=32, persistent=True, synced=True)
            redstoneLocked = Field(default=False, persistent=True, synced=True)
            goggles = Field(default=False, persistent=True, synced=True)

            # 装备的枪械（persistent 持久化 + synced 供客户端渲染）
            weaponItemName = Field(default="", persistent=True, synced=True)
            weaponCustomTips = Field(default="", persistent=True)
            weaponExtraId = Field(default="", persistent=True)

            # 弹药系统（synced 供 HUD 显示）
            currentMagazine = Field(default=0, persistent=True, synced=True)  # 当前弹匣剩余
            ammoReserve = Field(default=0, persistent=True, synced=True)      # 备用弹药储量
            bulletType = Field(default="", persistent=True, synced=True)      # 接受的弹药物品 ID

        existing = SentryArmComponent

    # 2. 获取主包组件类
    SixFacingComponent = getattr(CreateConfig, "SixFacingComponent", None)
    NetworkComponent = getattr(CreateConfig, "NetworkComponent", None)
    RPMComponent = getattr(CreateConfig, "RPMComponent", None)
    StressConsumerComponent = getattr(CreateConfig, "StressConsumerComponent", None)
    CogwheelTypeComponent = getattr(CreateConfig, "CogwheelTypeComponent", None)
    CogSize = getattr(CreateConfig, "CogSize", None)

    if not all(
        [
            SixFacingComponent,
            NetworkComponent,
            RPMComponent,
            StressConsumerComponent,
            CogwheelTypeComponent,
            CogSize,
        ]
    ):
        return False

    # 3. 注入 CreateBlockInitComponent
    SetCreateBlockInitComponents = getattr(CreateConfig, "SetCreateBlockInitComponents", None)
    if SetCreateBlockInitComponents:
        SetCreateBlockInitComponents(
            SENTRY_ARM_BLOCK,
            [
                (SixFacingComponent,),
                (NetworkComponent,),
                (RPMComponent,),
                (StressConsumerComponent, 3),
                (CogwheelTypeComponent, CogSize.SMALL),
                (existing,),
            ],
        )
    # 4. 注册动力臂交互点
    from .SentryArmRuntimePoint import SentryArmRuntimePoint

    Registry.registerBlockType(SENTRY_ARM_BLOCK, "take_deposit")
    Registry.registerRuntimePoint(SENTRY_ARM_BLOCK, SentryArmRuntimePoint())

    # 5. 注册放置规则
    if PlacementServer:
        from .SentryArmPlacement import SentryArmPlacementHandler

        registry = getattr(PlacementServer, "PlacementRulesMeta", None)
        if registry and hasattr(registry, "_registry"):
            if SENTRY_ARM_BLOCK not in registry._registry:
                registry._registry[SENTRY_ARM_BLOCK] = SentryArmPlacementHandler()

    _registered = True
    return True


# ==================== 双保险注册：模块级 + loadFinish 回调 ====================

# 尝试 1: 模块加载时立即注册（主包可能已就绪）
try:
    _doRegister()
except Exception:
    traceback.print_exc()


# 尝试 2: 所有 mod 加载完后再试（如果模块级注册失败）
@regModLoadFinishHandler
def _onAllModsLoaded():
    try:
        _doRegister()
    except Exception:
        traceback.print_exc()
