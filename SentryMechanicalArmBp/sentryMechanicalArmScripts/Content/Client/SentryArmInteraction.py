# -*- coding: utf-8 -*-
"""
SentryArmInteraction - 哨戒臂枪械装备交互（客户端）

每 tick 通过 PickFacing 检测准星是否看向哨戒臂：
- 手持枪械 → 显示"装备枪械"按钮
- 空手 + 哨戒臂有武器 → 显示"取出枪械"按钮
- 其他 → 隐藏按钮
"""

from ...QuModLibs.Client import Listen, clientApi

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_EP_PACK = "EpJxkScript"
_MAIN_PACK = "arrisCreateScripts"

compFactory = clientApi.GetEngineCompFactory()
levelId = clientApi.GetLevelId()

# 缓存 EpApiClient 模块引用
_epApi = None
_epApiChecked = False

# 上一次看向的哨戒臂位置（避免重复显隐调用）
_lastTargetPos = None


def _getEpApiInstance():
    # type: () -> object | None
    """获取 EpApiClient 的全局实例（不是模块，是类实例）"""
    global _epApi, _epApiChecked
    if not _epApiChecked:
        mod = clientApi.ImportModule(_EP_PACK + ".Api.EpApiClient")
        if mod:
            # EpApiClient.py 第 10 行: epApiClient = None（在类 __init__ 时被赋值为实例）
            _epApi = getattr(mod, "epApiClient", None)
        _epApiChecked = True
    return _epApi


def _isGunClient(itemName):
    # type: (str) -> bool
    if not itemName:
        return False
    instance = _getEpApiInstance()
    if instance and hasattr(instance, "IsGun"):
        return instance.IsGun(itemName)
    return False


@Listen("OnScriptTickClient")
def _onTickCheckCrosshair(args=None):
    # type: (dict | None) -> None
    """每 tick 检测准星是否看向哨戒臂，控制 HUD 按钮显隐"""
    global _lastTargetPos

    from .SentryArmHudProxy import getHudProxy

    proxy = getHudProxy()
    if not proxy:
        return

    playerId = clientApi.GetLocalPlayerId()
    cameraComp = compFactory.CreateCamera(playerId)
    if not cameraComp:
        if _lastTargetPos is not None:
            proxy.hideButton()
            _lastTargetPos = None
        return

    # PickFacing 获取准星方块
    # 返回格式: {"type": "Block", "x": int, "y": int, "z": int, "face": int} 或 {"type": "None"}
    result = cameraComp.PickFacing()
    if not result or result.get("type") != "Block":
        if _lastTargetPos is not None:
            proxy.hideButton()
            _lastTargetPos = None
        return

    blockPos = (result["x"], result["y"], result["z"])
    # PickFacing 不返回 blockName，需要通过客户端 GetBlock 查询
    # 客户端 API: GetBlock(pos) -> (blockName, auxValue)
    blockInfoComp = compFactory.CreateBlockInfo(levelId)
    blockResult = blockInfoComp.GetBlock(blockPos)
    blockName = blockResult[0] if blockResult else ""

    if blockName != SENTRY_ARM_BLOCK:
        if _lastTargetPos is not None:
            proxy.hideButton()
            _lastTargetPos = None
        return

    # 看向哨戒臂 — 检查手持物品决定按钮文本
    itemComp = compFactory.CreateItem(playerId)
    carriedItem = itemComp.GetPlayerItem(clientApi.GetMinecraftEnum().ItemPosType.CARRIED, 0, True)
    itemName = carriedItem.get("newItemName", "") if carriedItem else ""

    # 读哨戒臂 Component（武器 + 弹药状态）
    sentryComp = _getSentryComp(blockPos)
    hasWeapon = bool(sentryComp and sentryComp.weaponItemName)

    # 主提示：装/取枪
    if itemName and _isGunClient(itemName):
        text = "[K]装备枪械"
    elif not itemName and hasWeapon:
        text = "[K]取出枪械"
    else:
        # 非枪械且非空手取出场景 → 不显示
        if _lastTargetPos is not None:
            proxy.hideButton()
            _lastTargetPos = None
        return

    # 有枪时附加弹药信息行
    if sentryComp and sentryComp.weaponItemName and sentryComp.bulletType:
        mag = int(sentryComp.currentMagazine or 0)
        reserve = int(sentryComp.ammoReserve or 0)
        text = "{}\n弹匣:{} 备用:{}".format(text, mag, reserve)

    # 目标或文本变化时更新（弹药数变化也刷新）
    stateKey = (blockPos, text)
    if _lastTargetPos != stateKey:
        proxy.showButton(blockPos, 0, text)
        _lastTargetPos = stateKey


def _getSentryComp(blockPos):
    # type: (tuple) -> object | None
    """读取哨戒臂 ECS Component"""
    ClientWorldMod = clientApi.ImportModule(_MAIN_PACK + ".Content.Client.ClientWorld")
    if not ClientWorldMod:
        return None
    world = ClientWorldMod.ClientWorld()
    entityId = "{}_{}_{}_{}".format(blockPos[0], blockPos[1], blockPos[2], 0)
    entity = world.getEntity(entityId)
    if not entity:
        return None
    return entity.getComponent("SentryArmComponent")


def _checkSentryHasWeapon(blockPos):
    # type: (tuple) -> bool
    """检查哨戒臂 ECS 是否已装备武器（给按键 handler 用）"""
    comp = _getSentryComp(blockPos)
    return bool(comp and comp.weaponItemName)


# ==================== 自定义按键：装备/取出枪械 ====================

SENTRY_KEY_NAME = "装备/取出枪械"
SENTRY_KEY_CATEGORY = "哨戒动力臂"  # "哨戒动力臂"
SENTRY_KEY_DEFAULT = 75  # K 键（KeyBoardType.K）


def _registerSentryKey():
    """注册自定义按键（模块加载时调用）"""
    try:
        playerViewComp = compFactory.CreatePlayerView(levelId)
        if playerViewComp and hasattr(playerViewComp, "RegisterCustomKeyMapping"):
            playerViewComp.RegisterCustomKeyMapping(SENTRY_KEY_NAME, SENTRY_KEY_DEFAULT, SENTRY_KEY_CATEGORY)
    except Exception:
        pass


_registerSentryKey()


@Listen("OnCustomKeyPressInGame")
def _onSentryKeyPress(args):
    # type: (dict) -> None
    """
    玩家按下自定义按键：若准星看向哨戒臂 → 触发装备/取出
    args: {name, key, category, isDown, screenName}
    """
    if not args:
        return
    if args.get("name") != SENTRY_KEY_NAME:
        return
    if args.get("isDown") != "1":
        return  # 仅按下触发，抬起忽略

    # 准星检测（复用 _onTickCheckCrosshair 的逻辑）
    playerId = clientApi.GetLocalPlayerId()
    cameraComp = compFactory.CreateCamera(playerId)
    if not cameraComp:
        return
    result = cameraComp.PickFacing()
    if not result or result.get("type") != "Block":
        return
    blockPos = (result["x"], result["y"], result["z"])

    blockInfoComp = compFactory.CreateBlockInfo(levelId)
    blockResult = blockInfoComp.GetBlock(blockPos)
    blockName = blockResult[0] if blockResult else ""
    if blockName != SENTRY_ARM_BLOCK:
        return

    # 手持枪械 or 空手哨戒臂有武器时才响应（服务端会再校验一次）
    itemComp = compFactory.CreateItem(playerId)
    carriedItem = itemComp.GetPlayerItem(clientApi.GetMinecraftEnum().ItemPosType.CARRIED, 0, True)
    itemName = carriedItem.get("newItemName", "") if carriedItem else ""
    hasWeapon = _checkSentryHasWeapon(blockPos)

    canEquip = itemName and _isGunClient(itemName)
    canTake = not itemName and hasWeapon
    if not (canEquip or canTake):
        return

    # 触发服务端 RPC
    from ...QuModLibs.Client import Call

    Call(
        "equipGunToSentry",
        {
            "posX": blockPos[0],
            "posY": blockPos[1],
            "posZ": blockPos[2],
            "dimensionId": 0,
        },
    )

    # 按下后立即隐藏 HUD 按钮（装备状态会变，下一 tick 会根据新状态重算）
    try:
        from .SentryArmHudProxy import getHudProxy

        proxy = getHudProxy()
        if proxy:
            proxy.hideButton()
        global _lastTargetPos
        _lastTargetPos = None
    except Exception:
        pass
