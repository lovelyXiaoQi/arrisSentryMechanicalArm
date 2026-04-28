# -*- coding: utf-8 -*-
"""
ModServerSystem - 哨戒机械臂服务端入口

这是 arrisCreate 扩展 API 的 **完整使用示例** —— 给其他开发者看的 canonical 模板。
配套文档: <主 mod 仓库>/docs/EXTENSION-API.md

职责:
    1. 通过 Api.ExtensionApi 向主包注册方块的 ECS 组件配置 (registerBlock)
    2. 定义并注册自定义 Component (SentryArmComponent) 到主 mod World 注册表
    3. 注册动力臂交互点 (RuntimePointRegistry，主 mod 内部 API 但路径稳定)
    4. 注册顶/底面放置规则 (PlacementRulesMeta，主 mod 内部 API)

主 mod 未安装时优雅降级：所有注册 skip，tick 阶段也跳过，方块仍可放置
但无 ECS 行为（和独立 mod 作者初衷一致）。
"""

import traceback

from ...QuModLibs.Server import Listen, regModLoadFinishHandler, serverApi

# 导入子模块（触发 @AllowCall / @Listen 装饰器注册）
from . import (
    SentryArmInteraction as _sentryInteraction,  # noqa: F401
    SentryArmTargeting as _sentryTargeting,
    TargetBoardServer as _targetBoardServer,  # noqa: F401  自定义索敌登记板事件 + RPC
)


# 在入口模块注册服务端 tick（子模块的 @Listen 在 QuMod 加载器里不生效）
@Listen("OnScriptTickServer")
def _onServerTick(args=None):
    _sentryTargeting._onServerTick(args)


# ==================== 常量 ====================

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_MAIN_PACK = "arrisCreateScripts"
_registered = False

compFactory = serverApi.GetEngineCompFactory()


# ==================== 主 mod 公共 API 引入 ====================
#
# 重点教学：扩展 mod 的对外入口是 Api.ExtensionApi（Phase A/B）。
# 通过 ImportModule 拿到的 arris 对象暴露：
#   - registerBlock / extendGearboxBlocks / ...     (方块注册 / 特性集合)
#   - registerComponent                              (自定义 Component 注册)
#   - Component / Field / Behaviour / System / World / registerSystem  (基类 re-export)
#   - onServerConfigFrozen / onClientConfigFrozen    (生命周期订阅)

try:
    arris = serverApi.ImportModule(_MAIN_PACK + ".Api.ExtensionApi")
except Exception:
    # 主 mod 未安装 or 版本不兼容 —— 优雅降级
    arris = None
    print("[sentry] arrisCreate ExtensionApi not available; block will have no ECS behavior")


def _importMainModule(path):
    # type: (str) -> object | None
    """Lazy import 主 mod 的其他稳定路径（Component / 内部 API）。"""
    try:
        return serverApi.ImportModule(_MAIN_PACK + "." + path)
    except Exception:
        return None


# ==================== 核心注册流程 ====================


def _doRegister():
    # type: () -> bool
    """向主 mod 注册 ECS 配置 / 动力臂交互点 / 放置规则。

    幂等：重复调用是 no-op。
    返回 True 表示成功；False 表示主 mod 还未就绪（由 onServerConfigFrozen 重试）。
    """
    global _registered
    if _registered:
        return True
    if arris is None:
        return False

    # -------- Step 1: 定义并注册自定义 Component --------
    # arris.Component / arris.Field 是主 mod 基类的 re-export（Phase B）
    # @arris.registerComponent 把类挂到主 mod 的 World._componentRegistry，
    # 使得 Bridge 层能通过类名查找此组件以驱动持久化/同步。
    #
    # 防止热重载重复定义：World.getComponentClass 命中已存在的类时复用。
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
            # 逗号分隔的 typeStr 列表，例如 "minecraft:zombie,minecraft:skeleton"
            # ECS Field 不支持 list，用字符串编码后再 split
            customTargets = arris.Field(default="", persistent=True, synced=True)

        componentClass = SentryArmComponent

    # -------- Step 2: 从主 mod 按需拉取稳定 Component --------
    # 这些 Component 在 EXTENSION-API.md "稳定 Component 清单" 中列为 public stable。
    facingMod = _importMainModule("Content.Shared.Components.FacingComponent")
    networkMod = _importMainModule("Content.Shared.Components.NetworkComponent")
    rpmMod = _importMainModule("Content.Shared.Components.RPMComponent")
    stressConsumerMod = _importMainModule("Content.Shared.Components.StressConsumerComponent")
    cogwheelTypeMod = _importMainModule("Content.Shared.Components.CogwheelTypeComponent")

    if not all([facingMod, networkMod, rpmMod, stressConsumerMod, cogwheelTypeMod]):
        # 主 mod 某个 Component 模块还没加载（极少见）
        return False

    # -------- Step 3: 注册方块 ECS 配置 --------
    # arris.registerBlock 取代老版本的 SetCreateBlockInitComponents 调用。
    # 冲突策略：先到先得 + warn（同名方块不会被覆盖）。
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

    # -------- Step 4: 注册动力臂交互点（主 mod 内部 API） --------
    # RuntimePointRegistry 不在 Api.ExtensionApi 公共面中，但模块路径稳定。
    # 让普通机械臂把哨戒臂识别为 "take_deposit" 交互点（填弹 / 取弹）。
    runtimePointRegistry = _importMainModule("Content.Server.Helpers.RuntimePointRegistry")
    if runtimePointRegistry is not None:
        from .SentryArmRuntimePoint import SentryArmRuntimePoint

        runtimePointRegistry.registerBlockType(SENTRY_ARM_BLOCK, "take_deposit")
        runtimePointRegistry.registerRuntimePoint(SENTRY_ARM_BLOCK, SentryArmRuntimePoint())

    # -------- Step 5: 注册放置规则（主 mod 内部 API） --------
    # 顶/底面放置走主 mod 的 PlacementRulesMeta._registry（当前是私有属性，
    # 未来主 mod 可能提供公开 API，届时这里会更新）。
    placementServer = _importMainModule("Content.Server.Placements.Server")
    if placementServer is not None:
        from .SentryArmPlacement import SentryArmPlacementHandler

        placementRegistry = getattr(placementServer, "PlacementRulesMeta", None)
        if placementRegistry and hasattr(placementRegistry, "_registry"):
            if SENTRY_ARM_BLOCK not in placementRegistry._registry:
                placementRegistry._registry[SENTRY_ARM_BLOCK] = SentryArmPlacementHandler()

    _registered = True
    print("[sentry] registered via arrisCreate ExtensionApi")
    return True


# ==================== 双保险注册 ====================
#
# 时序挑战：本 mod 的 ModServerSystem 和主 mod 的 ModServerSystem 哪个先被 import
# 取决于网易 SDK 的 mod 加载顺序（无保证）。两种情况都要兼容：
#
#   case A：本 mod 先于主 mod → 模块加载时 arris=None，需要兜底
#   case B：主 mod 先于本 mod → 模块加载时 arris 已可用，立即注册即可
#
# 策略：尝试立即注册；无论成功失败，再挂 onServerConfigFrozen / regModLoadFinishHandler
# 兜底一次。_doRegister 幂等，重复调用无害。

# 尝试 1：模块加载时立即注册（case B）
try:
    _doRegister()
except Exception:
    traceback.print_exc()


# 尝试 2：所有 mod 加载完毕后兜底（case A 的解药）
if arris is not None:
    # 推荐写法：订阅主 mod 的 onServerConfigFrozen（语义明确 + 与主 mod 生命周期对齐）
    arris.onServerConfigFrozen(_doRegister)
else:
    # 主 mod 在本模块加载时仍然不可达 —— 用 SDK 级 regModLoadFinishHandler 做最终兜底，
    # 等所有 mod 加载完后再次尝试 ImportModule
    @regModLoadFinishHandler
    def _onAllModsLoaded():
        global arris
        if arris is None:
            try:
                arris = serverApi.ImportModule(_MAIN_PACK + ".Api.ExtensionApi")
            except Exception:
                return  # 主 mod 未安装，优雅降级
        try:
            _doRegister()
        except Exception:
            traceback.print_exc()
