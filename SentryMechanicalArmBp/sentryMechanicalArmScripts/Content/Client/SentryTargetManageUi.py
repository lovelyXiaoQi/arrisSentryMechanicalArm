# -*- coding: utf-8 -*-
"""
SentryTargetManageUi - 哨戒动力臂自定义索敌设置 管理 UI

走 QuModLibs.UI.ScreenNodeWrapper 模板：autoRegister 装饰器在 UI 初始化阶段
通过 RegisterUI 把本类绑到 jsonui 中的 sentry_target_manage.sentry_screen。
打开时调用 SentryTargetManageUi.pushScreen()，UI 关闭时调 popScreen()。

数据源：直接读玩家手持 target_board 的 userData，避免 createParams 跨进程序列化。
"""

from ...QuModLibs.Client import Call, clientApi, playerId
from ...QuModLibs.UI import ScreenNodeWrapper
from ..Shared.ItemFactory import ItemFactory

ViewBinder = clientApi.GetViewBinderCls()
compFactory = clientApi.GetEngineCompFactory()
ItemPosType = clientApi.GetMinecraftEnum().ItemPosType

TARGET_BOARD = "create:target_board"


@ScreenNodeWrapper.autoRegister("sentry_target_manage.sentry_screen")
class SentryTargetManageUi(ScreenNodeWrapper):
    def __init__(self, namespace, name, param):
        ScreenNodeWrapper.__init__(self, namespace, name, param)
        self._targets = []  # type: list[dict]  # [{"typeStr","name"}, ...]

    def Create(self):
        ScreenNodeWrapper.Create(self)
        self._reloadFromHand()

    def _reloadFromHand(self):
        """从玩家手持 board 读 userData,刷新 collection 数据"""
        itemComp = compFactory.CreateItem(playerId)
        # 客户端 ItemCompClient 没有 GetSelectSlotId(那是服务端 API)。
        # ItemPosType.CARRIED 客户端只有一个槽位 0(当前手持物)。
        boardItem = itemComp.GetPlayerItem(ItemPosType.CARRIED, 0, True)
        if not boardItem or boardItem.get("newItemName") != TARGET_BOARD:
            self._targets = []
        else:
            customData = ItemFactory.fromDict(boardItem).getCustomData() or {}
            self._targets = customData.get("targets", []) or []
        self.UpdateScreen(True)

    # ==================== Grid 大小动态绑定 ====================

    @ViewBinder.binding(ViewBinder.BF_BindInt, "#maximum_grid_items")
    def _bindGridSize(self):
        # type: (int) -> int
        """
        根据当前 targets 数量动态返回 grid 项数。
        """
        return len(self._targets)

    # ==================== Collection 数据绑定 ====================

    @ViewBinder.binding_collection(ViewBinder.BF_BindString, "target_entity_grid", "#target_name")
    def _bindName(self, index):
        # type: (int) -> str
        if 0 <= index < len(self._targets):
            return "目标: {}".format(self._targets[index].get("name", ""))
        return ""

    @ViewBinder.binding_collection(ViewBinder.BF_BindString, "target_entity_grid", "#target_type")
    def _bindType(self, index):
        # type: (int) -> str
        if not (0 <= index < len(self._targets)):
            return ""
        typeStr = self._targets[index].get("typeStr", "")
        kind = "玩家" if typeStr == "minecraft:player" else "生物"
        return "类型: {}".format(kind)

    # ==================== 按钮事件 ====================

    @ViewBinder.binding(ViewBinder.BF_ButtonClickUp, "#delete_target_button")
    def _onDeleteClick(self, args):
        # type: (dict) -> None
        """删除指定行的目标。args 含 #collection_index"""
        index = args.get("#collection_index", -1)
        if not (0 <= index < len(self._targets)):
            return
        # 客户端先乐观更新 UI（更顺滑），再 RPC 同步服务端 userData
        del self._targets[index]
        self.UpdateScreen(True)
        Call("sentryTargetBoardDelete", index)

    @ViewBinder.binding(ViewBinder.BF_ButtonClickUp, "#close_sentry_screen")
    def _onCloseClick(self, args):
        # type: (dict) -> None
        clientApi.PopScreen()
