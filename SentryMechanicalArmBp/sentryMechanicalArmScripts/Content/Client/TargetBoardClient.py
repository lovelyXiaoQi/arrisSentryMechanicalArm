# -*- coding: utf-8 -*-
"""
TargetBoardClient - "哨戒动力臂自定义索敌设置" 客户端事件钩子

打开 UI 的策略对齐主包列表过滤器 (FilterInteraction):
  - ClientItemTryUseEvent (右键空气/方块都触发) → 默认打开 UI
  - ClientItemUseOnEvent  (右键方块时先于 TryUse 触发) → 若目标是哨戒动力臂方块,
    设置抑制时间戳让随后 TryUse 跳过 UI (服务端 ServerItemUseOnEvent 仍照常应用配置)
  - 不再用 PickFacing 距离/类型判断 —— 对远处方块/边角情况会误判
"""

import time as _timeModule

from ...QuModLibs.Client import Listen, clientApi  # noqa: F401
from .SentryTargetManageUi import SentryTargetManageUi

TARGET_BOARD = "create:target_board"
SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"

# 抑制 UI 时间戳:右键哨戒臂方块时由 ClientItemUseOnEvent 设置,
# 紧接的 ClientItemTryUseEvent 里看到 0.5s 内的标志 → 跳过 UI。
_suppressUITs = [0.0]


@Listen("ClientItemUseOnEvent")
def _onItemUseOnBlock(args):
    # type: (dict) -> None
    """
    手持 board 右键方块时优先触发 (早于 ClientItemTryUseEvent)。
    若目标是哨戒动力臂方块 → 设置抑制时间戳,防止后续 TryUse 误开 UI。
    """
    itemDict = args.get("itemDict") or {}
    if itemDict.get("newItemName") != TARGET_BOARD:
        return
    if args.get("blockName") != SENTRY_ARM_BLOCK:
        return
    _suppressUITs[0] = _timeModule.time()


@Listen("ClientItemTryUseEvent")
def _onItemTryUse(args):
    # type: (dict) -> None
    """
    手持 board 右键 → 打开管理 UI。
    若 0.5 秒内有 ClientItemUseOnEvent 抑制(右键的是哨戒臂方块),则跳过。
    """
    itemDict = args.get("itemDict") or {}
    if itemDict.get("newItemName") != TARGET_BOARD:
        return
    elapsed = _timeModule.time() - _suppressUITs[0]
    if elapsed < 0.5:
        return
    SentryTargetManageUi.pushScreen()
