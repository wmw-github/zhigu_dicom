# -*- coding: utf-8 -*-
"""质控界面的取色小工具。

为什么需要它
------------
质控的四个对话框（设置 / 历史 / 详情 / PDF 导出）原来把配色写死成
``C_BG = "#1e2022"`` 这类模块级常量。加了皮肤系统之后，主程序切到浅色
皮肤，这几个窗口还是深色，一打开就像从别的软件里弹出来的。

这里用「懒取色」把常量接回 ``theme``：

* 常量在**模块导入时**创建，但真正的颜色要等到 ``str()`` 那一刻才去读
  ``theme.token()`` —— 对话框构造时才拼 QSS 字符串，所以**打开时拿到
  的一定是当前皮肤的颜色**，切换皮肤后再打开就是新配色；
* 用法保持不变（``"color:%s" % C_TEXT``），不用改几十处调用；
* ``theme`` 读不到（例如单独跑质控的测试脚本）就退回原来的深色，
  保证质控模块在脱离主程序时行为不变。

只适合「拼字符串」的用处：不要拿它去做 ``QColor()`` 参数或比较，
需要真实颜色时用 ``str(C_TEXT)``。
"""


class LazyColor(object):
    """延迟解析的皮肤颜色（格式化时才去读当前主题）。"""

    __slots__ = ("_token", "_fallback")

    def __init__(self, token, fallback):
        self._token = token
        self._fallback = fallback

    def value(self):
        try:
            import theme
            v = theme.token(self._token, "")
            return v or self._fallback
        except Exception:              # noqa: BLE001
            return self._fallback

    # %s / str() / print 都走这里
    def __str__(self):
        return self.value()

    def __repr__(self):
        return self.value()

    # f-string 与 "{}".format() 走这里
    def __format__(self, spec):
        return format(self.value(), spec or "s")


def lazy_color(token, fallback):
    return LazyColor(token, fallback)
