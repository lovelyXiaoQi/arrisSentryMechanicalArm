# -*- coding: utf-8 -*-
"""
ModServerSystem - 哨戒机械臂服务端入口

这是 arrisCreate 扩展 API 的 **完整使用示例** —— 给其他开发者看的 canonical 模板。
配套文档: <主 mod 仓库>/docs/EXTENSION-API.md

职责:
    1. 通过 Api.ExtensionApi 向主包注册方块的 ECS 组件配置 (registerBlock)
    2. 定义并注册自定义 Component (SentryArmComponent) 到主 mod World 注册表
    3. 注册动力臂交互点 (ext.registerArmPoint，v3 公共接口，capability "arm_points")
    4. 注册顶/底面放置规则 (PlacementRulesMeta，主 mod 内部 API)

入口方式: 监听主 mod 的 ServerExtensionApiReady 事件,handler 内通过 args["extension"]
拿到 ExtensionApiFacade 完成所有注册。事件订阅 + isFrozen() 兜底保证不会错过。

主 mod 未安装时优雅降级:订阅失败 / facade 不存在 → 全部 skip,tick 阶段也跳过,
方块仍可放置但无 ECS 行为(和独立 mod 作者初衷一致)。
"""

from ...QuModLibs.Server import Listen, serverApi

# 导入子模块（触发 @AllowCall / @Listen 装饰器注册）
from . import (
    SentryArmInteraction as _sentryInteraction,  # noqa: F401
)
from . import (
    SentryArmTargeting as _sentryTargeting,
)
from . import (
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


def _importMainModule(path):
    # type: (str) -> object | None
    """Lazy import 主 mod 的其他稳定路径（Component / 内部 API）。"""
    return serverApi.ImportModule(_MAIN_PACK + "." + path)


# ==================== 核心注册流程 ====================


def _doRegister(ext):
    # type: (object) -> bool
    """
    向主 mod 注册 ECS 配置 / 动力臂交互点 / 放置规则。

    Args:
        ext: ExtensionApiFacade 实例 (或旧版 arris 模块,接口同形)

    幂等:重复调用是 no-op。
    """
    global _registered
    if _registered:
        return True
    if ext is None:
        return False

    # -------- Step 1+2+3: ECS Component 定义 + 方块 ECS 配置注册 --------
    # 抽到 Shared 模块,使客户端 ModClientSystem 也能调用同一份 (联机时,
    # 加入玩家的客户端必须独立注册 SentryArmComponent + 把方块加入
    # CreateBlockInitComponent,否则主 mod 的客户端 BlockEntity hook 会
    # 提前 return,渲染系统永远不会被通知)。
    from ..Shared.SentryArmRegistration import registerSentryArmEcs

    componentClass = registerSentryArmEcs(ext)
    if componentClass is None:
        return False

    # -------- Step 4: 注册动力臂交互点（v3 公共接口 arm_points） --------
    # 一次调用合并原本分开的"方块类型 + 运行时交互点"两处注册，让普通动力臂
    # 把哨戒臂识别为 "take_deposit" 交互点（填弹 / 取弹）。
    # ⚠ 双端各注册一次：这里是服务端（决定真的能搬运），客户端那份在
    # ModClientSystem._doRegisterClient（决定玩家手持动力臂能否点中哨戒臂），
    # 见主包 docs/EXTENSION-API.md §15.8。
    if ext.hasCapability("arm_points"):
        from .SentryArmRuntimePoint import SentryArmRuntimePoint

        result = ext.registerArmPoint(SENTRY_ARM_BLOCK, "take_deposit", SentryArmRuntimePoint())
        if not result.get("ok"):
            print("[sentry] registerArmPoint failed: {}".format(result.get("error")))

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


# ==================== 事件驱动入口 ====================
#
# arrisCreate Api 推荐流程(详见主 mod docs/EXTENSION-API.md §2):
#   1. 订阅 ServerExtensionApiReady 事件
#   2. handler 收到 args["extension"] (ExtensionApiFacade) 后调注册
#   3. isFrozen() 兜底:订阅时主 mod 已经 freeze 完了 → 立即调一次
#
# 旧版 arrisCreate(没有 getServerExtensionApi)→ 优雅降级 no-op,
# 方块仍可放置但无 ECS 行为(和独立 mod 作者初衷一致)。


def _onArrisCreateReady(args):
    ext = args.get("extension") if isinstance(args, dict) else None
    if ext is None:
        return
    _doRegister(ext)


def _setupRegistration():
    arrisMod = serverApi.ImportModule(_MAIN_PACK + ".Api.ExtensionApi")
    if arrisMod is None:
        return  # 主 mod 未安装,优雅降级

    # 新版 API 路径:facade + 事件订阅
    if hasattr(arrisMod, "getServerExtensionApi"):
        ext = arrisMod.getServerExtensionApi()

        # 订阅 ServerExtensionApiReady,主 mod 之后 broadcast 时会调 _onArrisCreateReady
        eventName = getattr(arrisMod, "SERVER_EXTENSION_API_READY_EVENT", None)
        if eventName:
            namespace = arrisMod.EXTENSION_API_NAMESPACE
            systemName = arrisMod.EXTENSION_API_SYSTEM_NAME
            from ...QuModLibs.Systems.Loader.Server import LoaderSystem

            loader = LoaderSystem.getSystem()
            if loader is not None:
                # NetEase ListenForEvent 通过 getattr(parent, func.__name__) 查回调,
                # 模块级函数必须先挂到 parent(loader) 上。QuMod 的 _allocMethodWithOUTFunction
                # 用随机名做 setattr 并返回包装方法,避免重名冲突。
                wrappedFunc = loader._allocMethodWithOUTFunction(_onArrisCreateReady)
                loader.ListenForEvent(namespace, systemName, eventName, loader, wrappedFunc)

        # 兜底:若主 mod 已经 freeze(订阅来得晚),立刻拿 facade 跑一次
        if ext.isFrozen():
            _doRegister(ext)


_setupRegistration()
