# -*- coding: utf-8 -*-
"""
ModClientSystem - 哨戒机械臂客户端入口

职责:
1. 注册 Molang query 命名空间 (query.mod.arm_*)
2. 注册 HUD 代理 (SentryArmHudProxy 修改 hud_screen)
3. 接收服务端 RPC:
    - sentryArmPlayReloadSound: 装填音效广播
    - sentryArmFetchGunInfo:    枪械数据请求 (含配件加成)
4. 通过 import 触发其他子模块的客户端注册:
    - SentryArmInteraction:  准星 HUD + K 键
    - SentryTargetManageUi:  哨戒动力臂自定义索敌 UI
    - TargetBoardClient:     右键空气打开 UI
    - SentryArmRenderSystem: 客户端实体生命周期 + 渲染 (主包 ECS @registerSystem 模式)

注: 实体生命周期 / RPM uniform / 武器绑定 / IK 瞄准角度等已迁移至 SentryArmRenderSystem,
    使用主包 ECS 的 onEntityAdded/onEntityRemoved 回调,远端客户端可靠看到模型。
"""

from ...QuModLibs.Client import AllowCall, Call, clientApi

# 子模块导入(触发各自的注册装饰器/事件订阅)
from . import SentryArmInteraction as _sentryInteraction  # noqa: F401
from . import SentryTargetManageUi as _sentryTargetManageUi  # noqa: F401
from . import TargetBoardClient as _targetBoardClient  # noqa: F401
from . import SentryArmRenderSystem as _sentryArmRenderSystem  # noqa: F401

_MAIN_PACK = "arrisCreateScripts"
_EP_PACK = "EpJxkScript"

compFactory = clientApi.GetEngineCompFactory()
levelId = clientApi.GetLevelId()


# ==================== 客户端 ECS 注册 ====================
# 联机时,加入玩家的客户端是独立 Python 进程,arris.World._componentRegistry
# 和 CreateBlockInitComponent 都是 per-process 的,需要客户端独立注册。
# 不然主 mod 在 ModBlockEntityLoadedClientEvent 中:
#   if blockName not in CreateBlockInitComponent: return
# 远端玩家就永远拿不到客户端 ECS 实体 → SentryArmRenderSystem 不会收到
# onEntityAdded → 看不到旋转 / 武器 / 瞄准动画。
#
# 入口走主 mod 的 ClientExtensionApiReady 事件:
#   1. ImportModule 拿到 ExtensionApi 模块
#   2. getClientExtensionApi() 拿 ExtensionApiFacade
#   3. ListenForEvent 订阅 ready 事件,handler 内调 _doRegisterClient(ext)
#   4. ext.isFrozen() 兜底:订阅时主 mod 已 freeze 完 → 立即跑一次
# 主 mod 未安装 / 事件订阅失败 → 全部跳过,优雅降级。

_clientRegistered = False


def _importMainModuleClient(path):
    return clientApi.ImportModule(_MAIN_PACK + "." + path)


def _doRegisterClient(ext):
    # type: (object) -> bool
    """客户端 ECS 注册。幂等。"""
    global _clientRegistered
    if _clientRegistered:
        return True
    if ext is None:
        return False
    from ..Shared.SentryArmRegistration import registerSentryArmEcs

    componentClass = registerSentryArmEcs(ext, _importMainModuleClient)
    if componentClass is None:
        return False
    _clientRegistered = True
    print("[sentry] client registered via arrisCreate ExtensionApi")
    return True


def _onArrisCreateReady(args):
    ext = args.get("extension") if isinstance(args, dict) else None
    if ext is None:
        return
    _doRegisterClient(ext)


def _setupRegistration():
    arrisMod = clientApi.ImportModule(_MAIN_PACK + ".Api.ExtensionApi")
    if arrisMod is None:
        return  # 主 mod 未安装,优雅降级

    # 新版 API 路径:facade + 事件订阅
    if hasattr(arrisMod, "getClientExtensionApi"):
        ext = arrisMod.getClientExtensionApi()

        # 订阅 ClientExtensionApiReady,主 mod 之后 broadcast 时会调 _onArrisCreateReady
        eventName = getattr(arrisMod, "CLIENT_EXTENSION_API_READY_EVENT", None)
        if eventName:
            namespace = arrisMod.EXTENSION_API_NAMESPACE
            systemName = arrisMod.EXTENSION_API_SYSTEM_NAME
            from ...QuModLibs.Systems.Loader.Client import LoaderSystem

            loader = LoaderSystem.getSystem()
            if loader is not None:
                # NetEase ListenForEvent 通过 getattr(parent, func.__name__) 查回调,
                # 模块级函数必须先挂到 parent(loader) 上。QuMod 的 _allocMethodWithOUTFunction
                # 用随机名做 setattr 并返回包装方法,避免重名冲突。
                wrappedFunc = loader._allocMethodWithOUTFunction(_onArrisCreateReady)
                loader.ListenForEvent(namespace, systemName, eventName, loader, wrappedFunc)

        # 兜底:若主 mod 已经 freeze(订阅来得晚),立刻拿 facade 跑一次
        if ext.isFrozen():
            _doRegisterClient(ext)


_setupRegistration()


# ==================== Molang 查询注册 ====================


def _registerMolangQueries():
    """变量名对齐资源包 animation: arm_* 前缀(per-entity, 不与主包冲突)"""
    queryComp = compFactory.CreateQueryVariable(levelId)
    queryComp.Register("query.mod.arm_base_angle", 0.0)
    queryComp.Register("query.mod.arm_lower_angle", 0.0)
    queryComp.Register("query.mod.arm_upper_angle", 0.0)
    queryComp.Register("query.mod.arm_claw_angle", 0.0)
    queryComp.Register("query.mod.arm_ceiling", 0.0)
    queryComp.Register("query.mod.arm_z_rotation", 0.0)
    queryComp.Register("query.mod.arm_claw_grip", 0.0)


_registerMolangQueries()


# ==================== HUD 代理注册 ====================
# 附属包的按钮面板通过 ui/hud_screen.json modifications 注入到 hud_screen
NativeScreenManager = clientApi.GetNativeScreenManagerCls()
NativeScreenManager.instance().RegisterScreenProxy(
    "hud.hud_screen",
    "sentryMechanicalArmScripts.Content.Client.SentryArmHudProxy.SentryArmHudProxy",
)


# ==================== 服务端 RPC 入口 ====================


@AllowCall
def sentryArmPlayReloadSound(soundName, x, y, z, dimensionId):
    # type: (str, float, float, float, int) -> None
    """
    服务端广播装填音效 → 本地客户端用 PlayCustomMusic 在世界坐标播放。
    EP 的 reloadSound 是 CustomAudio 注册的自定义事件,服务端 /playsound 找不到。
    """
    if not soundName:
        return
    if dimensionId != compFactory.CreateGame(levelId).GetCurrentDimension():
        return  # 不在同一维度,忽略
    compFactory.CreateCustomAudio(levelId).PlayCustomMusic(
        soundName, (x, y, z), 1.0, 1.0, False, None
    )


@AllowCall
def sentryArmFetchGunInfo(entityId, itemName, customTips, extraId):
    # type: (str, str, str, str) -> None
    """
    服务端请求完整枪械数据(含配件加成)。
    本地客户端走 EP 的 GetEplisItemData 拿到真实 reloadSound/shootSound/damage 等字段,
    扁平化后 Call 回报服务端 sentryArmReportGunInfo。
    """
    if not itemName:
        return
    epSystem = clientApi.GetSystem(_EP_PACK, "EpJxkScriptClientSystem")
    if not epSystem or not hasattr(epSystem, "GetEplisItemData"):
        return
    try:
        allData = epSystem.GetEplisItemData(
            {
                "newItemName": itemName,
                "customTips": customTips or "",
                "extraId": extraId or "",
            }
        )
    except Exception:
        return
    if not allData or "data" not in allData:
        return

    d = allData["data"]
    shootSound = d.get("shootSound", [])
    shootSoundX = d.get("shootSoundX", [])
    hasShootX = d.get("shootX", False)
    soundList = shootSoundX if hasShootX and shootSoundX else shootSound

    # reloadSound 在 GetEplisItemData 的配件加成路径里会被覆盖成 ['', '']。
    # 兜底:从未经配件处理的 EpApiClient.GetGunInfo 读原始 JSON 值。
    reloadSound = d.get("reloadSound", [])
    if not reloadSound or all(not s for s in reloadSound):
        mod = clientApi.ImportModule(_EP_PACK + ".Api.EpApiClient")
        if mod and hasattr(mod, "GetGunInfo"):
            rawInfo = mod.GetGunInfo(itemName)
            if rawInfo:
                reloadSound = rawInfo.get("reloadSound", []) or reloadSound

    gunInfo = {
        "name": itemName,
        "damage": d.get("danger", 0),
        "fireSpeed": d.get("fireSpeed", 4),
        "boltSpeed": d.get("boltSpeed", 0),
        "shootCount": d.get("shootCount", 1),
        "fireType": d.get("fireType", 0),
        "magazine": d.get("magazine", 30),
        "reloadEmptyTick": d.get("reloadEmptyTick", 2.0),
        "reloadTacticalTick": d.get("reloadTacticalTick", 2.0),
        "dangerType": d.get("dangerType", "projectile"),
        "reloadSound": reloadSound,
        "bulletSpeed": d.get("bulletSpeed", 100),
        "useBullet": d.get("useBullet", ""),
        "count": d.get("count", 1),
        "spread": d.get("spread", 0),
        "distance": d.get("distance", 100),
        "crit": d.get("crit", 0),
        "critDamage": d.get("critDabger", 1.5),
        "shootSound": soundList,
        "fireFlash": d.get("fire_flash", ""),
        "hitPartic": d.get("hitPartic", ""),
        "fireParts": d.get("fireParts", ""),
    }
    Call("sentryArmReportGunInfo", entityId, gunInfo)
