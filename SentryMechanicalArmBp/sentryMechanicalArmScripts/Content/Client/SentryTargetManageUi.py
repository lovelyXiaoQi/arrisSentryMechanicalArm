# -*- coding: utf-8 -*-
"""
SentryTargetManageUi - 哨戒动力臂自定义索敌设置 管理 UI

走 QuModLibs.UI.ScreenNodeWrapper 模板：autoRegister 装饰器在 UI 初始化阶段
通过 RegisterUI 把本类绑到 jsonui 中的 sentry_target_manage.sentry_screen。
打开时调用 SentryTargetManageUi.pushScreen()，UI 关闭时调 popScreen()。

数据源：直接读玩家手持 target_board 的 userData，避免 createParams 跨进程序列化。

新增交互：
- 输入框（#sentry_target_add_edit_box）回车/失焦提交一条手输匹配规则
  （支持 * 通配 / ! 取反，语义见 Shared/SentryTargetMatcher），
  客户端预检 + 乐观更新，服务端 sentryTargetBoardAddManual 权威写入登记板。
- 帮助按钮（#help_edit_box_button_pressed）显示内置帮助蒙层，
  蒙层上的返回按钮（#back_button_pressed）关闭。
"""

from ...QuModLibs.Client import Call, clientApi, levelId, playerId
from ...QuModLibs.UI import ScreenNodeWrapper
from ..Shared import SentryTargetMatcher as TargetMatcher
from ..Shared.ItemFactory import ItemFactory

ViewBinder = clientApi.GetViewBinderCls()
compFactory = clientApi.GetEngineCompFactory()
ItemPosType = clientApi.GetMinecraftEnum().ItemPosType

TARGET_BOARD = "create:target_board"

# common.base_screen 的内容挂载路径（与主包 PackageFilterUi 同源：
# $screen_content 面板的子控件直接挂在 root_screen_panel 下，不含面板自身名）
_ROOT_PATH = (
    "/variables_button_mappings_and_controls/safezone_screen_matrix/inner_matrix"
    "/safezone_screen_panel/root_screen_panel"
)
_EDIT_BOX = _ROOT_PATH + "/main_bg/stack_panel/top_panel/edit_panel/edit_box"


@ScreenNodeWrapper.autoRegister("sentry_target_manage.sentry_screen")
class SentryTargetManageUi(ScreenNodeWrapper):
    def __init__(self, namespace, name, param):
        ScreenNodeWrapper.__init__(self, namespace, name, param)
        self._targets = []  # type: list[dict]  # [{"typeStr","name"}, ...]
        self._helpVisible = False

    def Create(self):
        ScreenNodeWrapper.Create(self)
        # 帮助蒙层显隐由 #help_page_visible 绑定驱动（json 静态 visible/enabled
        # 均为 false 兜底首帧），_reloadFromHand 里的 UpdateScreen 会评估绑定
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

    def _tip(self, message):
        # type: (str) -> None
        """本地弹提示（服务端路径有自己的 SetOneTipMessage,这里只管客户端预检）"""
        compFactory.CreateGame(levelId).SetTipMessage(message)

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
        if typeStr == TargetMatcher.CUSTOM_PATTERN_TYPE:
            kind = "规则"
        elif typeStr == "minecraft:player":
            kind = "玩家"
        else:
            kind = "生物"
        return "类型: {}".format(kind)

    # ==================== 输入框：手输匹配规则 ====================

    @ViewBinder.binding(ViewBinder.BF_EditFinished, "#sentry_target_add_edit_box")
    def _onAddEditFinished(self, args):
        # type: (dict) -> None
        """
        回车/失焦提交手输规则。空内容静默忽略——提交成功后 SetEditText("")
        清空输入框会再触发一次本回调，空串分支保证不会循环。
        """
        text = TargetMatcher.toUtf8((args or {}).get("Text", ""))
        if not text.strip():
            return
        token, err = TargetMatcher.validateManualToken(text)
        if not token:
            self._tip("§c无效的索敌规则: §f{}".format(err))
            return
        if any(
            t.get("typeStr") == TargetMatcher.CUSTOM_PATTERN_TYPE and t.get("name") == token
            for t in self._targets
        ):
            self._tip("§e规则已存在: §f{}".format(token))
            return
        # 乐观更新本地列表（与删除按钮同风格），服务端权威写回手持登记板
        self._targets.append({"typeStr": TargetMatcher.CUSTOM_PATTERN_TYPE, "name": token})
        self.UpdateScreen(True)
        Call("sentryTargetBoardAddManual", token)
        editBox = self.GetBaseUIControl(_EDIT_BOX)
        if editBox:
            editBox.asTextEditBox().SetEditText("")

    # ==================== 帮助蒙层 ====================

    @ViewBinder.binding(ViewBinder.BF_BindBool, "#help_page_visible")
    def _bindHelpVisible(self):
        # type: () -> bool
        """
        帮助提示（help_bg 面板 + background 暗色蒙层）可见+可交互 双属性绑定
        （json 里两控件各挂 #visible / #enabled 一对，binding_condition 必须
        always——用 always_when_visible 隐藏后绑定不再评估，就永远显示不出来）。
        隐藏时同时 disabled，面板内的返回按钮不会残留可焦点状态。
        """
        return self._helpVisible

    @ViewBinder.binding(ViewBinder.BF_ButtonClickUp, "#help_edit_box_button_pressed")
    def _onHelpClick(self, args):
        # type: (dict) -> None
        self._helpVisible = True
        self.UpdateScreen(True)

    @ViewBinder.binding(ViewBinder.BF_ButtonClickUp, "#back_button_pressed")
    def _onHelpBackClick(self, args):
        # type: (dict) -> None
        self._helpVisible = False
        self.UpdateScreen(True)

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
