# -*- coding: utf-8 -*-
"""养成：饭碗、精力、饱食度、偷吃与沉睡。

数值规则集中在这里，方便对着「主人的要求」逐条核。"""
import argparse
import base64
import codecs
import html
import json
import math
import os
import queue
import random
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtCore import QTimer
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)      # 私有：import * 带不走
from pet_sound import play as play_sound      # 音效（异步、失败即静音）
from pet_store import (load_pet_state, save_pet_state)


class LifeMixin:
    def bowl_visible(self):
        """白饭只在**醒着**时画。

        睡姿是另一套构图（尺寸、姿态都不同），碗留在原地会像浮在半空；
        和呆毛/尾巴同一套规矩 —— 状态从画面推导，不另存标志。
        """
        return (self.bowl_full_pixmap is not None
                and not self.sleeping()
                and not self.showing_self_contained())

    def bowl_pixmap(self):
        """现在该画哪只碗：还有饭就满碗，吃空了换空碗。"""
        if self._rice_left <= 0 and self.bowl_empty_pixmap is not None:
            return self.bowl_empty_pixmap
        return self.bowl_full_pixmap

    def bowl_rect(self):
        """白饭在窗口里占的矩形（画和点判定共用）。"""
        from PySide6.QtCore import QRect
        pm = self.bowl_pixmap()
        if pm is None:
            return None
        x, y, w, _h = self.canvas_origin()
        k = w / float(AHOGE_CANVAS[0])
        cx = x + int(BOWL_BOTTOM_MID[0] * k)
        by = y + int(BOWL_BOTTOM_MID[1] * k)
        return QRect(cx - pm.width() // 2, by - pm.height(),
                     pm.width(), pm.height())

    def bowl_hit(self, pos):
        """点的地方是不是那碗白饭。

        判定比画面**向外扩一圈**（BOWL_HIT_PAD）：显示出来才 33px 宽，
        严格按轮廓判定几乎点不中，主人会以为"点了没反应"。

        注意必须把坐标转成 **QPoint**：鼠标事件给的是 QPointF，而
        QRect.contains() 在 PySide6 里**没有浮点重载**，直接吃 QPointF
        会 TypeError（自检当场抓到的）。
        """
        if not self.bowl_visible():
            return False
        rect = self.bowl_rect()
        if rect is None:
            return False
        point = pos.toPoint() if hasattr(pos, "toPoint") else pos
        return rect.adjusted(-BOWL_HIT_PAD, -BOWL_HIT_PAD,
                             BOWL_HIT_PAD, BOWL_HIT_PAD).contains(point)

    def feed_bowl(self):
        """点白饭：碗跳一下、她变开心、说一句"好好吃"。

        连点会一碗一碗吃掉（BOWL_RICE_CLICKS 次），吃空换空碗并起一个
        自动添饭的定时器。**不弹窗、不抢焦点** —— 就是她自己嘴馋。
        """
        if self._rice_left <= 0:
            # 空碗：还没添上饭呢。**不结算** —— 以前这一下会把 rice_left
            # 减成负数、然后被"吃空"分支当成又吃完一碗，有了精力/饱食度
            # 之后就是刷分漏洞（点着空碗能把饱食度刷满），顺手一起修掉。
            self.say(random.choice(LINES["rice_empty"]))
            return
        if self.deep_sleep():
            # 睡死了，扒不动饭（偷吃是"梦游"自动行为，不受这条限制）
            self.say(random.choice(LINES["deep_sleep"]))
            return
        self._bowl_hop = 1e-6                 # 起跳（0 表示没在跳）
        self._rice_left -= 1
        play_sound("feed")                    # 咀嚼音：碗跳的同一下响
        # 精力告急时**不换表情**：她连眼都睁不开了，硬切过去下一帧就会被
        # apply_tired_face 顶回眼困，中间白闪一下反而难看。台词照说。
        tired = self._energy < DROWSY_ENERGY
        if self._rice_left <= 0:
            self._rice_left = 0
            if not tired:
                self.set_expression("love")   # 吃空了：一脸满足
            self.say(random.choice(LINES["rice_done"]))
            self.eat_one_bowl()               # 一碗见底：精力 -5 / 饱食 +20
            self._refill_timer.start(
                int(random.uniform(*BOWL_REFILL_SEC) * 1000))
        else:
            if not tired:
                self.set_expression("happy")  # 主人的要求：变成开心的心情
            self.say(random.choice(LINES["rice"]))
        self.update()

    def refill_bowl(self):
        """自动添满。**静默** —— 不主动说话，免得跟待机闲话叠成噪音；
        主人看到的"碗又满了"本身就是反馈。"""
        self._rice_left = BOWL_RICE_CLICKS
        self.update()

    def load_state(self):
        """启动时读回精力/饱食度，**按上次退出时的样子接着走**。

        如果退出那一刻她是睡着的，这里要**静默躺回去** ——
        不然"深睡到一半关掉程序、再打开"就变成了清醒状态，
        精力还是那个低值却叫不醒的规则全部失效（主人实测报的 bug）。
        只在 `sleep_pixmap` 有素材时才躺；恢复用 quiet=True，不冒"晚安"台词。
        """
        self._energy, self._satiety, was_sleeping = \
            load_pet_state(self._config_path)
        self._state_ready = True        # 从这里开始才允许写盘（见 save_state）
        if was_sleeping and self.sleep_pixmap is not None:
            self.sleep(quiet=True)

    def save_state(self):
        """把当前状态落盘（定时落一次 + 睡/醒翻转时立刻落 + 退出前必定落）。

        `_state_ready` 的守卫不能省：构造完成到 main 调 load_state() 之间，
        `_config_path` 还是 None，这时写盘会往默认路径（源码目录）乱丢文件。
        """
        if not self._state_ready:
            return False
        return save_pet_state(self._config_path, self._energy, self._satiety,
                              self.sleeping())

    def update_bars(self):
        """把当前数值推给头顶那两条（没开能量条时什么都不做）。"""
        if self.bars is not None:
            self.bars.set_values(self._energy, self._satiety,
                                 deep=self.deep_sleep())

    def deep_sleep(self):
        """睡死了：**睡着** 且 **精力 <= 60**，这时谁也弄不醒她。

        判据是两条同时成立算出来的，不另存标志位 —— 和 sleeping() /
        showing_expression() 同一个原则。多存一个 `_deep` 标志，就会多
        一处"忘了同步"的机会（本项目在标志位上栽过两次）。
        """
        return self.sleeping() and self._energy <= DEEP_SLEEP_ENERGY

    def spend_energy(self, amount):
        self._energy = max(0.0, self._energy - float(amount))

    def gain_satiety(self, amount):
        self._satiety = min(SATIETY_MAX, self._satiety + float(amount))

    def eat_one_bowl(self):
        """**吃空一碗**白饭的结算：精力 -5、饱食 +20。

        点着碗一口一口吃出来的，和饿到 20 自动偷吃的，最后都汇到这里 ——
        结算法则只有一处，就不会出现"偷吃不算饭"这种分叉。
        """
        self.spend_energy(ENERGY_PER_MEAL)
        self.gain_satiety(SATIETY_PER_MEAL)
        self.update_bars()

    def advance_tiredness(self, seconds):
        """按经过的秒数推进精力与饱食度（_tick 每帧调）。

        用 dt 累加而不是读墙上时钟，有两个好处：
        ① 帧率无关（30fps 和 60fps 掉得一样快）；
        ② **桌宠没在跑的时间不算数** —— 关了程序就"冻结"，
           下次启动接着上次的数走，正好是主人要的"保持上次退出前的状态"。
        """
        if self.sleeping():
            self._energy = min(
                ENERGY_MAX,
                self._energy + ENERGY_RECOVER_PER_MIN * seconds / 60.0)
            if self._energy >= ENERGY_MAX:
                # 睡饱了，自己醒（forced：这一条不受"沉睡不许唤醒"的限制）
                self.wake_up(quiet=False, forced=True)
        else:
            self._energy = max(
                0.0, self._energy - ENERGY_MIN_PER_MIN * seconds / 60.0)
            # 精力耗尽当场趴下。多一条 sleep_pixmap 判断是必须的：
            # 没有睡姿素材时 sleep() 会直接 return，"画面仍是站姿"→
            # sleeping() 永远 False → 下一帧又进来睡一次，台词会刷屏。
            if (self._energy <= 0.0 and not self.sleeping()
                    and self.sleep_pixmap is not None):
                self.sleep(quiet=False)

        self._satiety = max(
            0.0, self._satiety - SATIETY_MIN_PER_MIN * seconds / 60.0)
        if self._satiety <= SATIETY_HUNGRY:
            self.start_stealing()

        # 精力告急 → 眼困（睡姿优先，见 apply_tired_face）
        self.apply_tired_face()

    def start_stealing(self):
        """饿到 20 以下 → 连续偷吃 3 碗（主人的规则）。

        `_steal_left` 是"这轮偷吃还剩几碗"。它**不是**影子标志 ——
        没有别的量能推出"这轮吃到第几碗了"，所以必须存；
        值为 0 就等于"没在偷吃"，不需要再来一个布尔开关。
        """
        if self._steal_left > 0:
            return                      # 已经在吃了，别叠加
        self._steal_left = STEAL_MEALS
        self._steal_refill()

    def _steal_refill(self):
        """偷吃第一步：先盛满一碗。

        为什么要这一步：三碗是**连着**吃的，中间不可能等 25~50 秒的自动添饭。
        先满后空循环三次，画面上才读得出"连吃三碗"这件事。
        """
        if self._steal_left <= 0:
            return
        self._rice_left = BOWL_RICE_CLICKS
        self.update()
        QTimer.singleShot(STEAL_SIP_MS, self._steal_bite)

    def _steal_bite(self):
        """偷吃第二步：一口闷掉这碗，结算，再排下一碗。"""
        if self._steal_left <= 0:
            return
        self._steal_left -= 1
        self._bowl_hop = 1e-6           # 碗跳一下
        self._rice_left = 0
        self.eat_one_bowl()
        # **睡着时换一套台词**（主人的要求）：梦游着扒饭，说法要含糊、
        # 像说梦话，跟醒着偷吃那种"心虚"的语气区分开。
        self.say(random.choice(
            LINES["steal_sleep"] if self.sleeping() else LINES["steal"]))
        self.update()
        if self._steal_left > 0:
            QTimer.singleShot(STEAL_GAP_MS, self._steal_refill)
        else:
            # 偷吃完了，把空碗交给平时的自动添饭（不然它就永远空着）
            self._refill_timer.start(
                int(random.uniform(*BOWL_REFILL_SEC) * 1000))
