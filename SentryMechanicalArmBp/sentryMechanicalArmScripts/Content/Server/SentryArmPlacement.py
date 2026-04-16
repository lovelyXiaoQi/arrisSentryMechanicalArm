# -*- coding: utf-8 -*-
"""
SentryArmPlacement - 哨戒机械臂放置规则

对齐 Java SentryArmBlock.getStateForPlacement():
- 点击方块下表面 → ceiling 模式
- 其他面 → floor 模式

通过 ModServerSystem._onAllModsLoaded 手动注入到主包
PlacementRulesMeta._registry，而非元类自动注册。
"""

from ...QuModLibs.Server import serverApi

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_MAIN_PACK = "arrisCreateScripts"


class SentryArmPlacementHandler(object):
    """哨戒臂放置规则处理器"""

    BLOCK_ID = SENTRY_ARM_BLOCK

    def onPlace(self, args):
        # type: (dict) -> None
        fullName = args.get("fullName", "")
        if fullName != SENTRY_ARM_BLOCK:
            return

        # 对齐 Java SentryArmBlock: 点击下表面 (face=0=DOWN) → ceiling
        clickedFace = args.get("face", 1)
        isCeiling = clickedFace == 0

        BlockStateApi = serverApi.ImportModule(
            _MAIN_PACK + ".Content.Server.BlockStateApi"
        )
        if BlockStateApi:
            auxData = BlockStateApi.getAuxFromBlockStates(
                SENTRY_ARM_BLOCK,
                {"create:ceiling": isCeiling, "create:mode": "block"},
            )
            if auxData is not None:
                args["auxData"] = auxData
