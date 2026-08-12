# -*- coding: utf-8 -*-
"""
SentryArmInteraction - 哨戒臂枪械装备交互（客户端）

每 tick 通过 PickFacing 检测准星是否看向哨戒臂：
- 手持枪械 → 显示"装备枪械"按钮
- 空手 + 哨戒臂有武器 → 显示"取出枪械"按钮
- 其他 → 隐藏按钮
"""

from ...QuModLibs.Client import Listen, clientApi
from ..Shared import SentryArmEpCompat as EpCompat

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

# 玩家拾取距离平方上限（8^2=64），对齐主包 GogglesOverlaySystem._pickFacingBlock：
# PickFacing 本身不限距，必须用玩家位置二次校验，否则会"隔着房间锁定"。
_PLAYER_REACH_SQ = 64


def _pickFacingSentry(playerId):
    # type: (str) -> tuple | None
    """
    准星 → 哨戒动力臂方块的安全拾取：含距离限制 + 方块名校验。
    返回 blockPos 或 None。模式与 arrisCreate.GogglesOverlaySystem._pickFacingBlock 对齐。
    """
    cameraComp = compFactory.CreateCamera(playerId)
    if not cameraComp:
        return None
    pickData = cameraComp.PickFacing()
    if not pickData or pickData.get("type") != "Block":
        return None
    blockPos = (pickData.get("x"), pickData.get("y"), pickData.get("z"))
    pX, pY, pZ = compFactory.CreatePos(playerId).GetPos()
    dx = blockPos[0] - pX
    dy = blockPos[1] - pY
    dz = blockPos[2] - pZ
    if dx * dx + dy * dy + dz * dz > _PLAYER_REACH_SQ:
        return None
    blockInfo = compFactory.CreateBlockInfo(levelId).GetBlock(blockPos)
    if not blockInfo or blockInfo[0] != SENTRY_ARM_BLOCK:
        return None
    return blockPos


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


_epBullet = None
_epBulletChecked = False


def _getEpBullet():
    # type: () -> object | None
    """EP 子弹等级数据模块（纯数据，客户端进程同样加载）"""
    global _epBullet, _epBulletChecked
    if not _epBulletChecked:
        _epBullet = clientApi.ImportModule(_EP_PACK + ".modCommon.epBullet")
        _epBulletChecked = True
    return _epBullet


def _isGunClient(itemName):
    # type: (str) -> bool
    """枪械判定（含 bind 变体枪兜底，与服务端 _isGun 一致）"""
    if not itemName:
        return False
    return EpCompat.isGunWithBind(_getEpApiInstance(), itemName)


def _canLoadBullet(sentryComp, itemName):
    # type: (object, str) -> bool
    """手持物品可否装入哨戒臂库存：本枪弹药序列内任意等级子弹，
    且库存为空或与已存等级同名（与服务端 RuntimePoint.insert 门禁一致）"""
    if not sentryComp or not itemName:
        return False
    if not sentryComp.weaponItemName or not sentryComp.bulletType:
        return False
    if not EpCompat.isBulletAccepted(_getEpBullet(), sentryComp.bulletType, itemName):
        return False
    reserve = int(sentryComp.ammoReserve or 0)
    storedType = getattr(sentryComp, "reserveBulletType", "") or sentryComp.bulletType
    return reserve <= 0 or itemName == storedType


def _bulletLevelTag(bulletName):
    # type: (str) -> str
    """HUD 等级标注：有等级数据的子弹显示 (LvN)，否则空串"""
    level = EpCompat.bulletLevel(_getEpBullet(), bulletName)
    return "(Lv{})".format(level) if level else ""


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
    blockPos = _pickFacingSentry(playerId)
    if not blockPos:
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

    # 主提示：装枪 / 装填子弹 / 取枪（等级弹判定与服务端 insert 门禁一致）
    canLoadAmmo = _canLoadBullet(sentryComp, itemName)

    if itemName and _isGunClient(itemName):
        text = "[K]装备枪械"
    elif canLoadAmmo:
        text = "[K]装填子弹"
    elif not itemName and hasWeapon:
        text = "[K]取出枪械"
    else:
        # 非枪械且非空手取出且非匹配子弹 → 不显示
        if _lastTargetPos is not None:
            proxy.hideButton()
            _lastTargetPos = None
        return

    # 有枪时附加弹药信息行（含 EP+ 子弹等级标注）
    if sentryComp and sentryComp.weaponItemName and sentryComp.bulletType:
        mag = int(sentryComp.currentMagazine or 0)
        reserve = int(sentryComp.ammoReserve or 0)
        epBulletMod = _getEpBullet()
        magBullet = ""
        if mag > 0:
            magList = EpCompat.parseMagList(getattr(sentryComp, "magazineBulletList", "") or "", mag)
            magBullet = EpCompat.bulletAtIndex(epBulletMod, sentryComp.bulletType, magList[mag - 1])
        reserveBullet = ""
        if reserve > 0:
            reserveBullet = getattr(sentryComp, "reserveBulletType", "") or sentryComp.bulletType
        text = "{}\n弹匣:{}{} 备用:{}{}".format(
            text, mag, _bulletLevelTag(magBullet), reserve, _bulletLevelTag(reserveBullet)
        )

    # 目标或文本变化时更新（弹药数变化也刷新）
    stateKey = (blockPos, text)
    if _lastTargetPos != stateKey:
        proxy.showButton(blockPos, _getCurrentDim(), text)
        _lastTargetPos = stateKey


def _getCurrentDim():
    # type: () -> int
    """获取玩家当前所在维度（主世界 0、下界 1、末路 2 等）"""
    return compFactory.CreateGame(levelId).GetCurrentDimension()


def _getSentryComp(blockPos):
    # type: (tuple) -> object | None
    """读取哨戒臂 ECS Component（按当前维度查询，支持非主世界）"""
    ClientWorldMod = clientApi.ImportModule(_MAIN_PACK + ".Content.Client.ClientWorld")
    if not ClientWorldMod:
        return None
    world = ClientWorldMod.ClientWorld()
    entity = world.getEntityByPos(blockPos, _getCurrentDim())
    if not entity:
        return None
    return entity.getComponent("SentryArmComponent")


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

    # 准星检测（含玩家距离限制，对齐主包护目镜模式）
    playerId = clientApi.GetLocalPlayerId()
    blockPos = _pickFacingSentry(playerId)
    if not blockPos:
        return

    # 手持枪械 / 空手哨戒臂有武器 / 手持匹配子弹时才响应（服务端会再校验一次）
    itemComp = compFactory.CreateItem(playerId)
    carriedItem = itemComp.GetPlayerItem(clientApi.GetMinecraftEnum().ItemPosType.CARRIED, 0, True)
    itemName = carriedItem.get("newItemName", "") if carriedItem else ""

    sentryComp = _getSentryComp(blockPos)
    hasWeapon = bool(sentryComp and sentryComp.weaponItemName)

    canEquip = itemName and _isGunClient(itemName)
    canTake = not itemName and hasWeapon
    canLoadAmmo = _canLoadBullet(sentryComp, itemName)
    if not (canEquip or canTake or canLoadAmmo):
        return

    # 触发服务端 RPC
    from ...QuModLibs.Client import Call

    Call(
        "equipGunToSentry",
        {
            "posX": blockPos[0],
            "posY": blockPos[1],
            "posZ": blockPos[2],
            "dimensionId": _getCurrentDim(),
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
