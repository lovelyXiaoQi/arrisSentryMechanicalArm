# -*- coding: utf-8 -*-
"""
TargetBoardServer - "哨戒动力臂自定义索敌设置" 物品 (create:target_board) 的服务端逻辑

四条事件 / RPC:
1. PlayerAttackEntityEvent       左键攻击实体 → 把 victim 的 typeStr 写入 board.userData
2. ServerItemUseOnEvent          右键哨戒臂方块 → 把 board.userData.targets 应用到 sentry ECS
3. sentryTargetBoardDelete       客户端 UI 删除按钮回调 → 删除 board 中第 index 项
4. sentryTargetBoardAddManual    客户端 UI 输入框回调 → 追加手输匹配规则（* 通配 / ! 取反）

userData 结构由 ItemFactory 管理: userData["ArrisCustomData"]["targets"] = list[dict]
手输规则条目: {"typeStr": TargetMatcher.CUSTOM_PATTERN_TYPE, "name": 规则文本}
"""

from ...QuModLibs.Server import AllowCall, InjectHttpPlayerId, Listen, serverApi
from ..Shared import SentryTargetMatcher as TargetMatcher
from ..Shared.ItemFactory import ItemFactory

TARGET_BOARD = "create:target_board"
SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_MAIN_PACK = "arrisCreateScripts"

compFactory = serverApi.GetEngineCompFactory()
levelId = serverApi.GetLevelId()
gameComp = compFactory.CreateGame(levelId)
ItemPosType = serverApi.GetMinecraftEnum().ItemPosType

_serverWorld = None


def _tip(playerId, message):
    # type: (str, str) -> None
    """通过 SetOneTipMessage 给指定玩家弹一条物品栏上方提示"""
    try:
        gameComp.SetOneTipMessage(playerId, message)
    except Exception:
        pass


def _getServerWorld():
    global _serverWorld
    if _serverWorld is not None:
        return _serverWorld
    SW = serverApi.ImportModule(_MAIN_PACK + ".Content.Server.ServerWorld")
    if SW:
        _serverWorld = SW.ServerWorld()
    return _serverWorld


def _getCarriedBoard(playerId):
    # type: (str) -> tuple
    """返回 (itemComp, slot, boardItem) 或 (itemComp, slot, None) 若手持非 board"""
    itemComp = compFactory.CreateItem(playerId)
    slot = itemComp.GetSelectSlotId()
    boardItem = itemComp.GetPlayerItem(ItemPosType.CARRIED, slot, True)
    if not boardItem or boardItem.get("newItemName") != TARGET_BOARD:
        return itemComp, slot, None
    return itemComp, slot, boardItem


def _readTargets(boardItem):
    # type: (dict) -> list
    """从 board.userData 解出 targets 列表（list[{typeStr, name}]），不存在返回 []"""
    factory = ItemFactory.fromDict(boardItem)
    customData = factory.getCustomData() or {}
    return customData.get("targets", []) or []


def _writeTargets(itemComp, playerId, boardItem, targets):
    # type: (object, str, dict, list) -> None
    """把 targets 写回 board.userData，并 SpawnItemToPlayerCarried 替换玩家手持"""
    factory = ItemFactory.fromDict(boardItem)
    if targets:
        factory.addCustomData("targets", targets)
    else:
        factory.removeCustomData("targets")
    itemComp.SpawnItemToPlayerCarried(factory.build(), playerId)


# ==================== 1. 左键攻击实体 → 登记目标 ====================


@Listen("PlayerAttackEntityEvent")
def _onAttackEntity(args):
    # type: (dict) -> None
    playerId = args.get("playerId")
    victimId = args.get("victimId")
    if not playerId or not victimId:
        return

    itemComp, slot, boardItem = _getCarriedBoard(playerId)
    if not boardItem:
        return

    # 取消伤害（不论是否成功登记）—— 持 board 攻击就不应该掉血
    args["cancel"] = True

    # 拿 victim typeStr
    typeStr = compFactory.CreateEngineType(victimId).GetEngineTypeStr()
    if not typeStr:
        return

    # 玩家用 GetName 拿用户名（CreateAttr.GetEntityName 拿不到玩家名，只能拿命名牌）
    # 非玩家走 GetEntityName + typeStr fallback
    name = ""
    if typeStr == "minecraft:player":
        try:
            name = compFactory.CreateName(victimId).GetName() or ""
        except Exception:
            pass
    else:
        try:
            name = compFactory.CreateAttr(victimId).GetEntityName() or ""
        except Exception:
            pass
        if not name:
            # fallback：拿 typeStr 冒号后面的部分作为名字
            name = typeStr.split(":")[-1] if ":" in typeStr else typeStr
    if not name:
        return  # 玩家名缺失（极端边界），放弃登记

    targets = _readTargets(boardItem)
    # 玩家按 (typeStr, name) 元组判重，非玩家按 typeStr
    def _isSame(t):
        if typeStr == "minecraft:player":
            return t.get("typeStr") == typeStr and t.get("name") == name
        return t.get("typeStr") == typeStr
    if any(_isSame(t) for t in targets):
        _tip(playerId, "§e不能重复添加: §f{}".format(name))
        return
    targets.append({"typeStr": typeStr, "name": name})
    _writeTargets(itemComp, playerId, boardItem, targets)
    _tip(playerId, "§a已添加目标: §f{}".format(name))


# ==================== 2. 右键哨戒臂方块 → 应用配置 ====================


@Listen("ServerItemUseOnEvent")
def _onItemUseOnBlock(args):
    # type: (dict) -> None
    if args.get("blockName") != SENTRY_ARM_BLOCK:
        return
    itemDict = args.get("itemDict") or {}
    if itemDict.get("newItemName") != TARGET_BOARD:
        return

    playerId = args.get("entityId")
    blockPos = (int(args["x"]), int(args["y"]), int(args["z"]))
    dimensionId = int(args.get("dimensionId", 0))

    world = _getServerWorld()
    if not world:
        return
    entity = world.getEntityByPos(blockPos, dimensionId)
    if not entity:
        return
    comp = entity.getComponent("SentryArmComponent")
    if not comp:
        return

    targets = _readTargets(itemDict)
    if targets:
        # 编码规则:
        #   非玩家     → "<typeStr>"          (例: "minecraft:zombie")
        #   玩家       → "<typeStr>@<name>"   (例: "minecraft:player@Alice")
        #   手输规则   → 规则文本原样          (例: "1234*" / "!ep_jxk:*")
        # 玩家加上 name 是为了能区分具体玩家(同 typeStr 多个实例)。
        encoded = []
        for t in targets:
            ts = t.get("typeStr", "")
            if not ts:
                continue
            if ts == "minecraft:player":
                pname = t.get("name", "")
                if not pname:
                    continue
                encoded.append("{}@{}".format(ts, pname))
            elif ts == TargetMatcher.CUSTOM_PATTERN_TYPE:
                pattern, _err = TargetMatcher.validateManualToken(t.get("name", ""))
                if pattern:
                    encoded.append(pattern)
            else:
                encoded.append(ts)
        comp.customTargets = ",".join(encoded)
        comp.targetMode = 1
        if playerId:
            _tip(playerId, "§a哨戒动力臂已写入自定义索敌设置 §7({} 个目标)".format(len(targets)))
    else:
        comp.customTargets = ""
        comp.targetMode = 0
        if playerId:
            _tip(playerId, "§e哨戒动力臂已恢复默认敌对索敌")

    # 新配置立即生效：丢弃当前索敌目标与冷却，下一 tick 按新配置重新扫描
    from .SentryArmTargeting import resetTargeting

    resetTargeting(entity.id, comp)

    # 阻止默认放置（登记板不应作为方块放进世界）
    args["ret"] = True


# ==================== 3. UI 删除按钮 RPC ====================


@AllowCall
@InjectHttpPlayerId
def sentryTargetBoardDelete(playerId, index):
    # type: (str, int) -> None
    """客户端 UI 删除按钮回调：从手持 board 的 targets 列表删除第 index 项"""
    itemComp, slot, boardItem = _getCarriedBoard(playerId)
    if not boardItem:
        return

    targets = _readTargets(boardItem)
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return
    if not (0 <= idx < len(targets)):
        return

    del targets[idx]
    _writeTargets(itemComp, playerId, boardItem, targets)


# ==================== 4. UI 输入框手输规则 RPC ====================


@AllowCall
@InjectHttpPlayerId
def sentryTargetBoardAddManual(playerId, text):
    # type: (str, str) -> None
    """
    客户端 UI 输入框回调：把手输的匹配规则追加进手持 board。
    服务端权威校验（客户端预检只是省一次 RPC），规则语义见 Shared/SentryTargetMatcher。
    """
    itemComp, slot, boardItem = _getCarriedBoard(playerId)
    if not boardItem:
        return

    token, err = TargetMatcher.validateManualToken(text)
    if not token:
        _tip(playerId, "§c无效的索敌规则: §f{}".format(err))
        return

    targets = _readTargets(boardItem)
    if any(
        t.get("typeStr") == TargetMatcher.CUSTOM_PATTERN_TYPE and t.get("name") == token
        for t in targets
    ):
        _tip(playerId, "§e规则已存在: §f{}".format(token))
        return

    targets.append({"typeStr": TargetMatcher.CUSTOM_PATTERN_TYPE, "name": token})
    _writeTargets(itemComp, playerId, boardItem, targets)
    _tip(playerId, "§a已添加索敌规则: §f{}".format(token))
