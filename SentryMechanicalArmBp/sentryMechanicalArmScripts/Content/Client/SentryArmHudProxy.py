# -*- coding: utf-8 -*-
"""
SentryArmHudProxy - 哨戒臂 HUD 代理

注册到 hud.hud_screen，管理"装备枪械"按钮面板的显隐和点击回调。
通过 ViewBinder 绑定 #createEquipGunButton 按钮，点击后 RPC 到服务端。
"""

from ...QuModLibs.Client import clientApi

CustomUIScreenProxy = clientApi.GetUIScreenProxyCls()
ViewBinder = clientApi.GetViewBinderCls()

UI_ROOT_PANEL = (
    "/variables_button_mappings_and_controls/safezone_screen_matrix/inner_matrix/safezone_screen_panel"
)
BUTTON_PANEL_PATH = UI_ROOT_PANEL + "/root_screen_panel/use_button_panel"
BUTTON_TEXT_PATH = BUTTON_PANEL_PATH + "/use_button/button_text"


_instance = None


def getHudProxy():
    # type: () -> SentryArmHudProxy | None
    return _instance


class SentryArmHudProxy(CustomUIScreenProxy):
    def __init__(self, screenName, screenNode):
        CustomUIScreenProxy.__init__(self, screenName, screenNode)
        self._buttonPanelCtrl = None
        self._buttonTextCtrl = None
        self._targetBlockPos = None  # type: tuple | None
        self._targetDimId = 0

    def OnCreate(self):
        global _instance
        _instance = self
        self._buttonPanelCtrl = self.screenNode.GetBaseUIControl(BUTTON_PANEL_PATH)
        self._buttonTextCtrl = self.screenNode.GetBaseUIControl(BUTTON_TEXT_PATH)

    def OnRemove(self):
        global _instance
        _instance = None

    @ViewBinder.binding(ViewBinder.BF_ButtonClickUp, "#createEquipGunButton")
    def onEquipButtonClicked(self, args):
        # type: (dict) -> None
        """按钮点击 → RPC 到服务端装备/取出枪械"""
        if not self._targetBlockPos:
            return
        from ...QuModLibs.Client import Call

        Call(
            "equipGunToSentry",
            {
                "posX": self._targetBlockPos[0],
                "posY": self._targetBlockPos[1],
                "posZ": self._targetBlockPos[2],
                "dimensionId": self._targetDimId,
            },
        )
        self.hideButton()

    def showButton(self, blockPos, dimId, text="装备枪械"):
        # type: (tuple, int, str) -> None
        self._targetBlockPos = blockPos
        self._targetDimId = dimId
        if self._buttonPanelCtrl:
            self._buttonPanelCtrl.SetVisible(True)
        if self._buttonTextCtrl and text:
            self._buttonTextCtrl.asLabel().SetText(text)

    def hideButton(self):
        # type: () -> None
        self._targetBlockPos = None
        if self._buttonPanelCtrl:
            self._buttonPanelCtrl.SetVisible(False)
