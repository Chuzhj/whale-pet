# -*- coding: utf-8 -*-
"""画面：每帧绘制、相位推进、表情切换、睡眠与眨眼。

这些方法都只依赖 self 上的状态与 pet_constants 里的几何/动画参数，所以能独立成一层。"""
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
from PySide6.QtGui import QImage
from PySide6.QtGui import QPainter
from PySide6.QtCore import QTimer
from pet_constants import *          # noqa: F401,F403
from pet_constants import (_CANCEL_INTENT_RE, _CANCEL_NEG_RE, _CANCEL_STRIP_RE, _CLOCK_BEFORE_RE, _CN_DIGITS, _DURATION_COMPOUND_RE, _DURATION_RE, _QUARTER_RE, _REMIND_ASK_RE, _SECRET_PREFIX, _TAG_KEYWORDS, _TAG_TAIL_RE)      # 私有：import * 带不走
from pet_sound import play as play_sound      # 音效（异步、失败即静音）


class AnimMixin:
    def current_scale(self):
        # 只有点击时的弹跳会改变缩放；不做呼吸动画
        return 1.0 + BOUNCE_GAIN * self.bounce

    def ahoge_visible(self):
        """是否该叠加呆毛层。

        只要醒着就一直叠 —— 眨眼帧和表情帧里原本烘焙着各自固定角度的呆毛，
        已经在素材阶段抹干净了（见 strip_baked_ahoge.py），
        所以这三种状态可以共用同一层呆毛，动作才连得上、不会一隐一现。

        睡姿例外：那是另一套姿势、尺寸也不同，位置对不上，所以睡着时不叠。
        特写表情（自带呆毛）也不叠 —— 否则两根呆毛。
        """
        return (self.ahoge_pixmap is not None and not self.sleeping()
                and not self.showing_self_contained())

    def tail_visible(self):
        """是否该叠加尾巴层。规则和呆毛一致：醒着就一直叠，睡姿不叠。

        同样因为眨眼帧 / 表情帧里烘焙的尾巴已经在素材阶段抹掉了
        （见 extract_tail.py 与 strip_baked_ahoge.py），
        所以醒着的三种状态共用这一层尾巴，摆动才连得上。
        特写表情同理不叠（自带尾巴，叠了就是浮空的一条）。
        """
        return (self.tail_pixmap is not None and not self.sleeping()
                and not self.showing_self_contained())

    def draw_part(self, painter, pixmap, origin, pivot, angle, x, y, w):
        """把一层素材按原图坐标贴进窗口，并绕 pivot 旋转 angle 度。

        呆毛和尾巴共用这段：两者都是"原尺寸图层 + 原图坐标 + 绕根部转"。
        图层保持原尺寸不预缩放，绘制时按 k（显示尺寸 / 原图宽）换算 ——
        这样它和底图的缩放比例严格一致，不会错位。
        """
        from PySide6.QtCore import QRect
        k = w / float(AHOGE_CANVAS[0])        # AHOGE_CANVAS 是两层共用的原图尺寸
        ax = x + int(origin[0] * k)
        ay = y + int(origin[1] * k)
        aw = max(1, int(pixmap.width() * k))
        ah = max(1, int(pixmap.height() * k))
        px = x + pivot[0] * k
        py = y + pivot[1] * k
        painter.save()
        painter.translate(px, py)
        painter.rotate(angle)
        painter.translate(-px, -py)
        painter.drawPixmap(QRect(ax, ay, aw, ah), pixmap)
        painter.restore()

    def canvas_origin(self):
        """底图在窗口里的落点，返回 (x, y, 显示宽, 显示高)。

        paintEvent、部件绘制、白饭的点击判定**共用这一处** ——
        底边对齐、呼吸起伏、点击弹跳三件事叠在一起，各算一遍迟早会分叉。
        """
        scale = self.current_scale()
        w = int(self.pixmap.width() * scale)
        h = int(self.pixmap.height() * scale)

        if self.sleeping():
            # 呼吸：以"贴地的下边缘"为锚点，让上半身随时间鼓起再落下。
            # 底边固定不动，所以这既不是整体位移（上下浮动），
            # 也不是以中心为锚点的缩放（那会上下一起动、像吹气球）。
            wave = (1.0 - math.cos(self._breath_phase)) / 2.0    # 0 → 1 → 0
            h = int(h * (1.0 + SLEEP_BREATH_AMP * wave))

        x = (self.width() - w) // 2
        y = self.height() - h          # 底边对齐：贴地那一侧不动
        return x, y, w, h

    def paintEvent(self, _event):
        from PySide6.QtCore import QRect
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.Antialiasing, True)

        x, y, w, h = self.canvas_origin()

        # 尾巴画在**底图前面不行** —— 尾巴长在身体后侧，一摆就会盖到裙摆上。
        # 画在底图后面：裙摆、腿这些不透明像素天然遮住它，
        # 根部那截在裙摆下的部分怎么转都不会跑到裙子前面来。
        if self.tail_visible():
            self.draw_part(painter, self.tail_pixmap,
                           TAIL_ORIGIN, TAIL_PIVOT,
                           self._tail_angle, x, y, w)

        painter.drawPixmap(QRect(x, y, w, h), self.pixmap)

        # 白饭画在底图**之上** —— 它摆在桌面靠近观众的一侧，压住裙摆边缘才对
        if self.bowl_visible():
            self.draw_bowl(painter, x, y, w)

        # 呆毛在头发前面，最后画
        if self.ahoge_visible():
            self.draw_part(painter, self.ahoge_pixmap,
                           AHOGE_ORIGIN, AHOGE_PIVOT,
                           self._ahoge_angle, x, y, w)

        painter.end()

    def _tick(self):
        """每一帧的推进。dt 取**真实经过的时间**，不是标称的 1/fps。

        以前固定用 1/fps：主线程被阻塞时（写盘、右键菜单 exec()、网络调用）
        QTimer 会漏拍，而按标称值累加等于把漏掉的时间当没发生 ——
        精力/饱食度的推进比墙上时钟慢，"精力耗尽就睡"因此晚于预期。
        现在读 QElapsedTimer 的真实差值（单调时钟，不怕系统时间被改）。

        两个边界：
        * 下限 1/fps —— 自检里是**连续空转调用** _tick()，真实差值是 0，
          没有这个下限的话所有相位都不会动，自检会整片误报；
        * 上限 0.5s —— 休眠/长时间卡顿之后不该一次扣掉一大截精力，
          这和旧行为（休眠期间定时器压根不触发）也一致。
        """
        dt = min(0.5, max(1.0 / self.fps,
                          self._clock.restart() / 1000.0))

        # 只有弹跳在衰减；用 dt 归一化，保证不同帧率下手感一致
        self.bounce *= BOUNCE_DECAY ** (dt * 60.0)
        if self.bounce < 0.01:
            self.bounce = 0.0

        if self.sleeping():
            # 呼吸相位同样按"弧度/秒"推进，与帧率解耦
            self._breath_phase += (2.0 * math.pi / SLEEP_BREATH_PERIOD) * dt

        # 呆毛相位**始终**推进，中途不归零 ——
        # 以前眨眼时会被归零，眨完再瞬间跳回原角度，那一下就是那个"顿"。
        # 睡觉时照推不误（只是不画出来），醒来时角度才是连续的。
        self._ahoge_phase += (2.0 * math.pi / AHOGE_PERIOD) * dt
        self._ahoge_angle = AHOGE_SWING * math.sin(self._ahoge_phase)

        # 尾巴同理：相位始终推进。周期与呆毛错开（3.0 vs 3.5 秒），
        # 免得两根一起摆得像机械装置。
        self._tail_phase += (2.0 * math.pi / TAIL_PERIOD) * dt
        self._tail_angle = TAIL_SWING * math.sin(self._tail_phase)

        # 白饭的"跳一下"是个**有限进度**（0→1），不是周期运动，
        # 所以跑完就归零 —— 归零等于"没在跳"，不用另存一个布尔开关。
        if self._bowl_hop > 0.0:
            self._bowl_hop += dt / BOWL_HOP_SECONDS
            if self._bowl_hop >= 1.0:
                self._bowl_hop = 0.0

        # 精力/饱食度按真实经过的时间推进（与帧率无关）
        self.advance_tiredness(dt)
        self.update_bars()

        self.update()

    def alpha_top(self, pixmap):
        """一张图里最上面一行"有内容"的像素，返回它自己的像素坐标 y。

        逐行扫、找到就停，所以通常只扫几十行；结果按图片缓存（cacheKey），
        同一个状态反复冒台词不会重复计算。
        """
        key = pixmap.cacheKey()
        cached = self._alpha_top_cache.get(key)
        if cached is not None:
            return cached
        image = pixmap.toImage()
        for y in range(image.height()):
            hit = False
            # 横向跳着采样：顶端是刘海/发箍这类连续色块，不用逐像素确认
            for x in range(0, image.width(), 2):
                if ((image.pixel(x, y) >> 24) & 0xFF) > ALPHA_TOP_MIN:
                    hit = True
                    break
            if hit:
                self._alpha_top_cache[key] = y
                return y
        self._alpha_top_cache[key] = 0
        return 0

    def alpha_span(self, pixmap):
        """一张图里"有内容"的左右边界（像素坐标），返回 (left, right)。

        和 alpha_top 同一个用途：素材四周的透明留白左右**不一样宽** ——
        她那条尾巴长在右侧，右留白就比左边窄。拿图片宽度居中算出来的
        头顶能量条会明显偏左，所以得用真正的内容边界。

        结果按 cacheKey 缓存；一列一列扫（竖向跳着采样）比逐像素快得多，
        而且这几张图一辈子只算一次。
        """
        key = pixmap.cacheKey()
        cached = self._alpha_span_cache.get(key)
        if cached is not None:
            return cached
        image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
        w, h = image.width(), image.height()
        buf = memoryview(image.bits()).cast("B")   # ARGB32 小端：每像素 BGRA
        left, right = w, -1
        for x in range(w):
            hit = False
            for y in range(0, h, 3):
                if buf[(y * w + x) * 4 + 3] > ALPHA_TOP_MIN:
                    hit = True
                    break
            if hit:
                if x < left:
                    left = x
                right = x
        if right < 0:                    # 整张全透明：退回图片边界
            left, right = 0, w - 1
        self._alpha_span_cache[key] = (left, right)
        return left, right

    def head_top(self):
        """当前这一帧"看起来最高的东西"距底图顶边多少**显示像素**。

        要连呆毛、尾巴这些叠层一起算：气泡该贴在这一帧最高的东西上面，
        而不是贴在图片的上边缘。
        睡姿那张是横躺的、图上方一多半是透明留白 ——
        照图片上沿贴，气泡会飘到她头顶几十像素开外去（实测就是这样）。
        """
        w = int(self.pixmap.width() * self.current_scale())
        k = w / float(AHOGE_CANVAS[0])       # 叠层是原图尺寸，得换算到显示尺寸
        tops = [self.alpha_top(self.pixmap)]
        if self.ahoge_visible():
            tops.append((AHOGE_ORIGIN[1] + self.alpha_top(self.ahoge_pixmap)) * k)
        if self.tail_visible():
            tops.append((TAIL_ORIGIN[1] + self.alpha_top(self.tail_pixmap)) * k)
        return int(min(tops))

    def char_rect(self):
        """角色当前在屏幕上的外接矩形。

        这里是**照着 paintEvent 的算法重算一遍**，而不是加一个"气泡定位缓存"——
        同一条摆法写两处确实有走样的风险，但缓存一旦和画面不同步更糟。
        缩放和呼吸高度都跟 paintEvent 取同一组参数，所以两者不会漂移。
        区别只有一个：上边界取 head_top()（真实的视觉顶端），不是图片顶边。
        """
        from PySide6.QtCore import QRect

        scale = self.current_scale()
        w = int(self.pixmap.width() * scale)
        h = int(self.pixmap.height() * scale)
        if self.sleeping():
            wave = (1.0 - math.cos(self._breath_phase)) / 2.0
            h = int(h * (1.0 + SLEEP_BREATH_AMP * wave))
        top = self.head_top()
        x = self.x() + (self.width() - w) // 2
        y = self.y() + self.height() - h + top
        return QRect(x, y, w, h - top)

    def draw_bowl(self, painter, x, y, w):
        """画那碗白饭。跳起来时向上平移，同时朝上拉长一点点（起跳的拉伸感）。"""
        from PySide6.QtCore import QRect
        pm = self.bowl_pixmap()
        if pm is None:
            return
        k = w / float(AHOGE_CANVAS[0])
        cx = x + int(BOWL_BOTTOM_MID[0] * k)
        by = y + int(BOWL_BOTTOM_MID[1] * k)
        bw, bh = pm.width(), pm.height()
        top = by - bh
        if self._bowl_hop > 0.0:
            lift = math.sin(math.pi * self._bowl_hop)     # 0 → 1 → 0
            extra = int(bh * 0.18 * lift)
            bh += extra
            top -= int(BOWL_HOP_H * lift) + extra
        painter.drawPixmap(QRect(cx - bw // 2, top, bw, bh), pm)

    def set_expression(self, name):
        """按**名字**切表情（名字不认识就什么都不做，绝不抛异常）。

        心情落地、点白饭、右键菜单都走这里 —— "名字→pixmap"的查表只有一处，
        免得三处各写一遍 index() 然后某处忘了防 ValueError。

        **沉睡守卫不写在这儿**，统一放在 show_expression（所有换表情路径的
        最后一道闸）。两个地方各写一遍，就一定会有一条路径被漏掉 ——
        右键菜单当初就是这么漏的。
        """
        if name in self.expr_names:
            return self.show_expression(
                self.expressions[self.expr_names.index(name)])
        return False

    def apply_mood(self, mood):
        """把模型报来的心情落到画面上。

        - sleep：趴下睡（静默）；
        - happy/angry/shy/surprised：切对应表情并保持（正睡着就等于直接醒来做表情，
          状态全从画面推导，不需要先 wake_up 再切）；
        - idle / 认不出的值：**不动画面** —— 平静的回复不该打断她现在的样子，
          上一轮的生气表情保持到主人来点她，反而是自然的。
        """
        if mood == "sleep":
            self.sleep(quiet=True)
        elif mood in self.expr_names:
            self.set_expression(mood)

    def show_expression(self, pixmap):
        """切到某张表情图，并保持住。返回是否真的切了。

        这里**不需要**再手动清"睡觉"状态 —— 换成站姿表情图之后，
        sleeping() 自然就不再成立了。以前正是靠一个布尔标志来记这件事，
        切表情时忘了清，就落进"画面是站姿、状态却算作睡着"的矛盾态：
        呆毛层被当成睡觉藏起来，整根消失（"先点睡觉、再点做个表情"那次）。

        左侧单击睡着的小家伙也是先叫醒再回应，两条路径保持一致。

        **沉睡守卫放在这里，而不是只放在 set_expression 里**：这是所有
        换表情路径的最后一道闸。右键菜单「做个表情」原先是直接拿 pixmap
        调本函数的（绕过了 set_expression 的守卫），等于给"谁也弄不醒"
        开了个后门 —— 主人实测点了菜单就把她弄醒了。守卫放在底层，以后
        再加菜单项或新入口也漏不掉。

        **眼困守卫同样放这里**（精力 < 20 时只认眼困图）：
        apply_tired_face 每帧都会把画面拉回眼困，所以别的表情切上去
        活不过 1/30 秒 —— 表现就是"低精力档位心情系统整个失效"，
        回复里的 [mood:love] 一帧都看不到。与其闪一下，不如在闸上认下来：
        这会儿她困得做不出别的表情。
        """
        if self.deep_sleep():
            self.say(random.choice(LINES["deep_sleep"]))
            return False
        name = self.expr_name_of(pixmap)
        if (self.do_expr and name is not None and name != DROWSY_EXPR
                and not self.sleeping()
                and self._energy < DROWSY_ENERGY):
            return False
        self.pixmap = pixmap
        self.update()
        return True

    def showing_expression(self):
        """当前画面是不是某张表情图。

        用"画面本身"当唯一依据，而不是另外维护一个布尔标志 ——
        标志一旦因异常路径没被复位，眨眼就会永久失效。
        """
        return any(p is self.pixmap for p in self.expressions)

    def showing_named(self, name):
        """当前画面是不是**某个具名表情**（名字不存在就 False）。

        和 showing_expression / sleeping 同一套原则：只看"现在画的是哪张"，
        不去维护"现在处于什么状态"。
        """
        if name not in self.expr_names:
            return False
        return self.pixmap is self.expressions[self.expr_names.index(name)]

    def showing_self_contained(self):
        """当前画面是不是"自带部件"的特写表情。

        和 showing_expression / sleeping 同一个原则：**从画面推导**，
        不另存标志位 —— 标志一旦漏复位就会造出矛盾态（本项目栽过两次）。
        """
        for i, pixmap in enumerate(self.expressions):
            if pixmap is self.pixmap:
                return self.expr_names[i] in SELF_CONTAINED_EXPRESSIONS
        return False

    def expr_name_of(self, pixmap):
        """把一张表情图反查成名字（不在表情表里就返回 None）。

        给"眼困守卫"用：守卫拿到的是一张 pixmap，得知道它是不是眼困图。
        """
        for i, item in enumerate(self.expressions):
            if item is pixmap:
                return self.expr_names[i]
        return None

    def back_to_idle(self):
        """手动回到待机形象。返回是否真的切了。

        沉睡中直接不动 —— "回到待机"是把她从睡姿拽回站姿的一条捷径
        （右键菜单里就点得到），同样算"叫醒"，主人的规则里不许。
        内部路径（apply_tired_face 退出眼困）只会在**清醒**时走到这儿，
        所以不需要 forced 之类的开关。
        """
        if self.deep_sleep():
            self.say(random.choice(LINES["deep_sleep"]))
            return False
        self.pixmap = self.frames[self.index]
        self.update()
        return True

    def react(self):
        """点击反应：按顺序切到下一个表情，点满一圈再从头开始。

        切过去就一直保持，直到再点一次或手动回待机 —— 不做自动收回。
        """
        if not self.do_expr:
            return
        pixmap = self.expressions[self._expr_cursor % len(self.expressions)]
        self._expr_cursor = (self._expr_cursor + 1) % len(self.expressions)
        self.show_expression(pixmap)

    def sleep(self, quiet=False):
        """趴下睡觉。

        睡着时眼睛本来就是闭的，所以跳过眨眼；
        呼吸起伏由 paintEvent 按 _breath_phase 直接绘制，不换图、不位移。
        quiet=True 用于**对话触发**（她回复说困了）：回复本身就挂在
        气泡上，再插一句睡前台词会把刚收到的回答顶掉。
        """
        if self.sleep_pixmap is None:
            return
        self._breath_phase = 0.0
        self.pixmap = self.sleep_pixmap
        if not quiet:
            self.say(random.choice(LINES["sleep"]))
        play_sound("sleep")                   # 入睡音（柔和下滑）
        self.update()
        # 睡/醒是**必须立刻落盘**的两个时刻：万一接下来被强杀（任务管理器 /
        # 断电），15 秒的定时器还没轮到，状态文件里就还是"醒着" ——
        # 下次启动她就白醒一回。写一次 JSON 是毫秒级的事，这里不值得省。
        self.save_state()

    def wake_up(self, quiet=False, forced=False):
        """醒过来，回到待机形象。返回是否真的醒了。

        `forced` 只给**内部**用（精力睡回 200 时自己醒）：精力 <= 60 的
        "沉睡"状态下，右键「叫醒」、点她、跟她说话统统叫不醒（主人的规则），
        所以用户能碰到的每条路径都不传 forced —— 想叫醒她只有一个办法，
        就是等她精力涨回 60 以上。
        """
        if not self.sleeping():
            return False
        if not forced and self.deep_sleep():
            if not quiet:
                self.say(random.choice(LINES["deep_sleep"]))
            return False
        self.pixmap = self.frames[self.index]
        self.flash()
        if not quiet:
            self.say(random.choice(LINES["wake"]))
        self.update()
        self.save_state()       # 醒来同样是"必须立刻落盘"的时刻（见 sleep）
        return True

    def toggle_sleep(self):
        if self.sleeping():
            self.wake_up()
        else:
            self.sleep()

    def sleeping(self):
        """当前是不是睡姿。

        同样以"画面本身"为唯一依据，不存布尔标志。
        踩过的坑：以前有个 `_sleeping` 标志，切表情时忘了清，
        就落进"画面是站姿表情、状态却算作睡着"的矛盾态 ——
        呆毛层被当成睡觉藏起来，整根消失。
        改成从画面推导之后，这种状态**根本无法构造**。
        """
        return (self.sleep_pixmap is not None
                and self.pixmap is self.sleep_pixmap)

    def apply_tired_face(self):
        """精力告急时自动换上"眼困"的样子，缓过来再放下（主人的要求）。

        - **睡着时一律不管**：睡姿本身就是闭眼的，再叠一个眼困没意义，
          主人也明确要求"睡觉时保持睡觉状态，不会眼困"。
        - 退出眼困时回到的是**待机**，不是"她之前那个表情" —— 想记住
          "被顶掉之前是什么表情"就得再存一份状态，那正是本项目栽过两次的
          影子标志。累到睁不开眼、缓过来回到平常的样子，本来就说得通。
        - 用 `showing_named` 判断"要不要动手"，所以每帧调用也不会反复
          触发换图和重绘（只在状态真的翻转时切一次）。
        """
        if self.sleeping() or not self.do_expr:
            return
        if self._energy < DROWSY_ENERGY:
            if not self.showing_named(DROWSY_EXPR):
                self.set_expression(DROWSY_EXPR)
        elif self.showing_named(DROWSY_EXPR):
            self.back_to_idle()

    def next_blink_gap(self):
        """下一次眨眼的间隔，取随机值，避免看出规律。"""
        return int(random.uniform(BLINK_GAP_MIN, BLINK_GAP_MAX) * 1000)

    def current_blink_pixmap(self):
        """按"当前正在显示哪张图"，取它对应的闭眼帧。

        没配到闭眼帧就返回 None —— 那这次就不眨。
        """
        for i, pixmap in enumerate(self.expressions):
            if pixmap is self.pixmap:
                return self.expr_blinks[i]
        for i, pixmap in enumerate(self.frames):
            if pixmap is self.pixmap:
                return self.idle_blinks[i]
        return None

    def blink(self):
        """眨一次眼：贴上闭眼帧，几十毫秒后回到原来的画面，再排下一次。"""
        target = self.current_blink_pixmap()
        if target is None or self._dragging or self.sleeping():
            # 拖拽中、睡着了、或这张图没配闭眼帧时跳过这一次；
            # 但定时器必须续上，否则眨眼会从此永久停掉
            self.blink_timer.start(self.next_blink_gap())
            return

        self._blink_token += 1
        token = self._blink_token
        self._blink_target = target
        self._return_pixmap = self.pixmap      # 记住眨眼前的画面
        self.pixmap = target
        self.update()
        QTimer.singleShot(int(BLINK_DURATION * 1000),
                          lambda: self.end_blink(token))

    def end_blink(self, token):
        if token != self._blink_token:
            return
        # 只有画面还停在本次贴的闭眼帧时才回退；
        # 若这期间表情超时收回、画面已被换过，就别去覆盖它
        if self.pixmap is self._blink_target:
            self.pixmap = self._return_pixmap
        self._blink_target = None
        self.update()
        self.blink_timer.start(self.next_blink_gap())

    def flash(self):
        """短暂压低透明度，用来掩盖换图那一瞬间的突兀感。"""
        self.setWindowOpacity(self.base_opacity * 0.62)
        QTimer.singleShot(110, lambda: self.setWindowOpacity(self.base_opacity))
