# -*- coding: utf-8 -*-
"""
TargetBoardClient - "哨戒动力臂自定义索敌设置" 客户端事件钩子

监听 ClientItemTryUseEvent: 玩家手持 target_board 右键时,
通过 PickFacing 判定是否未命中方块/实体 (即"对空气右键"),
是 → push 管理 UI;否 → 不拦截,让事件继续走 ServerItemUseOnEvent (服务端有 handler)。
"""

from ...QuModLibs.Client import Listen, clientApi
from .SentryTargetManageUi import SentryTargetManageUi

TARGET_BOARD = "create:target_board"

compFactory = clientApi.GetEngineCompFactory()


@Listen("ClientItemTryUseEvent")
def _onItemTryUse(args):
    # type: (dict) -> None
    """
    客户端"右键尝试使用物品"事件 (引擎判定使用类型之前抛出)。
    手持 target_board + 准星未命中方块/实体 → 视为对空气右键 → 推 UI。

    备注:ClientItemTryUseEvent 不能取消"对方块/实体使用物品"
    (引擎对方块走另外的路径),所以右键方块仍会触发 ServerItemUseOnEvent。
    """
    itemDict = args.get("itemDict") or {}
    if itemDict.get("newItemName") != TARGET_BOARD:
        return

    playerId = clientApi.GetLocalPlayerId()
    cameraComp = compFactory.CreateCamera(playerId)
    if not cameraComp:
        return
    pickData = cameraComp.PickFacing()
    # 命中方块或实体 → 走主流程,不弹 UI
    if pickData and pickData.get("type") in ("Block", "Entity"):
        return

    SentryTargetManageUi.pushScreen()
    args["cancel"] = True  # 阻止后续物品使用网络包(纯打开 UI,无副作用)
