"""Design „Modern Dark“ (angelehnt an Linear / Vercel / Raycast).

Fast-schwarze Flächen, eine einzige Akzentfarbe (Indigo #5E6AD2), feine Haarlinien statt
Schatten, dezente Lichtkante oben auf Knöpfen, sofortige Hover-Zustände.

Performance: Die halbtransparenten Werte des Originalstils (z. B. weiß 5 %) sind hier bereits
in deckende Farben umgerechnet – es wird nichts durchscheinend gezeichnet.
"""
from __future__ import annotations

import ctypes

from PySide6.QtCore import QEvent, QLibraryInfo, QLocale, QObject, QPointF, QRect, QRectF, QSize, Qt, QTranslator
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QProxyStyle, QStyle, QStyleOptionButton,
                               QStyleOptionComboBox, QStyleOptionMenuItem, QStyleOptionSlider,
                               QStyleOptionToolButton, QToolBar, QWidget)

# ---------------------------------------------------------------- Farb-Token (deckend)
DEEP = QColor("#020203")            # tiefste Ebene
CANVAS = QColor("#050506")          # Fläche hinter den Seiten (background-base)
BG = QColor("#0a0a0c")              # Menüband, Leisten, Fenster (background-elevated)
SURFACE = QColor("#161618")         # Karten/Knöpfe (weiß 5 % auf BG)
SURFACE_HOVER = QColor("#1e1e20")   # weiß 8 %
INPUT = QColor("#0f0f12")           # Eingabefelder
BORDER = QColor("#19191b")          # Haarlinie (weiß 6 %)
BORDER_HOVER = QColor("#242426")    # weiß 10 %
BORDER_INPUT = QColor("#27272a")
HIGHLIGHT = QColor("#2b2b2e")       # Lichtkante oben auf Knöpfen (weiß 10 % auf SURFACE)
TEXT = QColor("#ededef")            # foreground
TEXT_DIM = QColor("#8a8f98")        # foreground-muted
TEXT_DISABLED = QColor("#4a4d53")
ACCENT = QColor("#5e6ad2")
ACCENT_BRIGHT = QColor("#7c86e6")   # Akzent für Text/Symbole auf dunklem Grund (bessere Lesbarkeit)
ACCENT_TINT = QColor("#17182a")     # Akzent 15 % auf BG: Füllung aktiver Elemente
ACCENT_BORDER = QColor("#2f3566")   # Akzent 40 % auf BG
ACCENT_HL = QColor("#8a93e6")       # Lichtkante auf Akzentknöpfen
ACCENT_PINK = QColor("#f2777a")     # zerstörerische Aktionen (Löschen)
LIGHT = BORDER_HOVER                # (Kompatibilität)
PAGE_EDGE = QColor("#3a3d44")       # Rand um jede Seite (auch schwarze Seiten heben sich vom Hintergrund ab)

RADIUS = 8        # Knöpfe, Eingabefelder
RADIUS_L = 16     # Karten, große Container
RADIUS_S = 6      # Menüeinträge, kleine Elemente


def surface_color(widget):
    w = widget
    while w is not None:
        if w.property("neuSurface") == "canvas":
            return CANVAS
        w = w.parentWidget()
    return BG


# ---------------------------------------------------------------- Zeichenhilfen
def draw_box(p: QPainter, rect, fill: QColor, border: QColor | None = None, radius=RADIUS,
             highlight: QColor | None = None):
    """Abgerundete Fläche mit 1-px-Haarlinie und optionaler Lichtkante oben (alles deckend)."""
    r = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    if r.width() <= 1 or r.height() <= 1:
        return
    radius = min(radius, r.height() / 2, r.width() / 2)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(border, 1) if border is not None else Qt.NoPen)
    p.setBrush(fill)
    p.drawRoundedRect(r, radius, radius)
    if highlight is not None and r.width() > 2 * radius:
        p.setPen(QPen(highlight, 1))
        p.drawLine(QPointF(r.left() + radius, r.top() + 1), QPointF(r.right() - radius, r.top() + 1))
    p.restore()


def draw_hairline(p: QPainter, a: QPointF, b: QPointF, color: QColor = BORDER):
    p.save()
    p.setRenderHint(QPainter.Antialiasing, False)
    p.setPen(QPen(color, 1))
    p.drawLine(a, b)
    p.restore()


def draw_chevron(p: QPainter, rect: QRectF, direction: str, color: QColor, size=4.0):
    c = QRectF(rect).center()
    s = size
    pts = {
        "down": [QPointF(-s, -s / 2), QPointF(0, s / 2), QPointF(s, -s / 2)],
        "up": [QPointF(-s, s / 2), QPointF(0, -s / 2), QPointF(s, s / 2)],
        "right": [QPointF(-s / 2, -s), QPointF(s / 2, 0), QPointF(-s / 2, s)],
        "left": [QPointF(s / 2, -s), QPointF(-s / 2, 0), QPointF(s / 2, s)],
    }[direction]
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    path = QPainterPath(c + pts[0])
    for q in pts[1:]:
        path.lineTo(c + q)
    p.drawPath(path)
    p.restore()


# ---------------------------------------------------------------- Palette
def palette() -> QPalette:
    pal = QPalette()
    for group in (QPalette.Active, QPalette.Inactive):
        for role, color in ((QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, INPUT),
                            (QPalette.AlternateBase, SURFACE), (QPalette.Text, TEXT), (QPalette.Button, SURFACE),
                            (QPalette.ButtonText, TEXT), (QPalette.BrightText, ACCENT_PINK),
                            (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, QColor("#ffffff")),
                            (QPalette.ToolTipBase, INPUT), (QPalette.ToolTipText, TEXT),
                            (QPalette.PlaceholderText, TEXT_DIM), (QPalette.Link, ACCENT_BRIGHT),
                            (QPalette.Light, BORDER_HOVER), (QPalette.Midlight, BORDER), (QPalette.Mid, BORDER),
                            (QPalette.Dark, DEEP), (QPalette.Shadow, DEEP)):
            pal.setColor(group, role, color)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, TEXT_DISABLED)
    for role, color in ((QPalette.Window, BG), (QPalette.Base, INPUT), (QPalette.Button, SURFACE),
                        (QPalette.Highlight, BORDER_HOVER)):
        pal.setColor(QPalette.Disabled, role, color)
    return pal


# ---------------------------------------------------------------- Stil
S = QStyle


def _in_toolbar(widget):
    w = widget.parentWidget() if widget is not None else None
    while w is not None:
        if isinstance(w, QToolBar) or w.property("ribbon"):
            return True
        w = w.parentWidget()
    return False


class ModernStyle(QProxyStyle):
    def __init__(self):
        super().__init__("Fusion")

    def polish(self, arg):
        if isinstance(arg, QPalette):
            return palette()
        if isinstance(arg, QWidget):
            arg.setAttribute(Qt.WA_Hover, True)
        return super().polish(arg)

    def standardPalette(self):
        return palette()

    def pixelMetric(self, metric, option=None, widget=None):
        m = {
            S.PM_ScrollBarExtent: 12,
            S.PM_ScrollBarSliderMin: 40,
            S.PM_ToolBarItemSpacing: 2,
            S.PM_ToolBarSeparatorExtent: 13,
            S.PM_ToolBarFrameWidth: 0,
            S.PM_ToolBarItemMargin: 3,
            S.PM_ToolBarHandleExtent: 0,
            S.PM_MenuHMargin: 6,
            S.PM_MenuVMargin: 6,
            S.PM_MenuPanelWidth: 1,
            S.PM_MenuBarVMargin: 2,
            S.PM_MenuBarHMargin: 4,
            S.PM_IndicatorWidth: 18,
            S.PM_IndicatorHeight: 18,
            S.PM_ExclusiveIndicatorWidth: 18,
            S.PM_ExclusiveIndicatorHeight: 18,
            S.PM_SliderThickness: 22,
            S.PM_SliderLength: 18,
            S.PM_SliderControlThickness: 18,
            S.PM_ButtonMargin: 12,
            S.PM_DefaultFrameWidth: 1,
            S.PM_ComboBoxFrameWidth: 4,
            S.PM_SpinBoxFrameWidth: 4,
            S.PM_ToolTipLabelFrameWidth: 7,
            S.PM_SplitterWidth: 10,
            S.PM_FocusFrameHMargin: 0,
            S.PM_FocusFrameVMargin: 0,
            S.PM_ButtonShiftHorizontal: 0,
            S.PM_ButtonShiftVertical: 0,
            S.PM_CheckBoxLabelSpacing: 8,
            S.PM_RadioButtonLabelSpacing: 8,
            S.PM_ToolBarIconSize: 20,
            S.PM_SmallIconSize: 18,
        }.get(metric)
        return m if m is not None else super().pixelMetric(metric, option, widget)

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint in (S.SH_ComboBox_Popup, S.SH_Menu_Scrollable, S.SH_ScrollBar_LeftClickAbsolutePosition):
            return 1
        if hint == S.SH_DialogButtonBox_ButtonsHaveIcons:
            return 0
        if hint == S.SH_ToolTipLabel_Opacity:
            return 255  # deckend
        return super().styleHint(hint, option, widget, returnData)

    def sizeFromContents(self, ct, option, size, widget=None):
        sz = super().sizeFromContents(ct, option, size, widget)
        if ct == S.CT_ToolButton:
            return QSize(sz.width() + 10, max(sz.height() + 8, 38))
        if ct == S.CT_PushButton:
            return QSize(max(sz.width() + 12, 88), max(sz.height(), 36))
        if ct == S.CT_ComboBox:
            return QSize(sz.width() + 10, max(sz.height(), 36))
        if ct in (S.CT_LineEdit, S.CT_SpinBox):
            return QSize(sz.width() + 6, max(sz.height() + 2, 34))
        if ct == S.CT_MenuItem and isinstance(option, QStyleOptionMenuItem):
            if option.menuItemType == QStyleOptionMenuItem.Separator:
                return QSize(sz.width(), 9)
            return QSize(sz.width() + 20, max(sz.height() + 8, 36))
        if ct == S.CT_MenuBarItem:
            return QSize(sz.width() + 10, max(sz.height() + 6, 30))
        if ct == S.CT_ItemViewItem:
            return QSize(sz.width(), max(sz.height() + 6, 32))
        return sz

    def generatedIconPixmap(self, mode, pixmap, option):
        if mode in (QIcon.Selected, QIcon.Active):
            return pixmap  # ausgewählte Symbole/Miniaturen nicht einfärben
        return super().generatedIconPixmap(mode, pixmap, option)

    # ---------------------------------------------------------- Bildlaufleiste ohne Pfeile
    def subControlRect(self, cc, option, sc, widget=None):
        if cc == S.CC_ScrollBar and isinstance(option, QStyleOptionSlider):
            r = option.rect
            horizontal = option.orientation == Qt.Horizontal
            length = r.width() if horizontal else r.height()
            span = option.maximum - option.minimum
            if sc in (S.SC_ScrollBarAddLine, S.SC_ScrollBarSubLine):
                return QRect()
            if sc == S.SC_ScrollBarGroove:
                return QRect(r)
            slen = length if span <= 0 else max(self.pixelMetric(S.PM_ScrollBarSliderMin),
                                                int(length * option.pageStep / (span + option.pageStep)))
            slen = min(slen, length)
            pos = QStyle.sliderPositionFromValue(option.minimum, option.maximum, option.sliderPosition,
                                                 length - slen, option.upsideDown)
            if horizontal:
                slider = QRect(r.x() + pos, r.y(), slen, r.height())
                sub = QRect(r.x(), r.y(), pos, r.height())
                add = QRect(slider.right() + 1, r.y(), r.right() - slider.right(), r.height())
            else:
                slider = QRect(r.x(), r.y() + pos, r.width(), slen)
                sub = QRect(r.x(), r.y(), r.width(), pos)
                add = QRect(r.x(), slider.bottom() + 1, r.width(), r.bottom() - slider.bottom())
            return {S.SC_ScrollBarSlider: slider, S.SC_ScrollBarSubPage: sub,
                    S.SC_ScrollBarAddPage: add}.get(sc, QRect(r))
        return super().subControlRect(cc, option, sc, widget)

    # ---------------------------------------------------------- Knopfflächen
    def _button_panel(self, p, rect, st, widget, ghost):
        """ghost: im Menüband/in Leisten nur bei Hover/aktiv sichtbar (wie bei Linear)."""
        hover = bool(st & S.State_MouseOver) and bool(st & S.State_Enabled)
        on = bool(st & S.State_On)
        sunken = bool(st & S.State_Sunken)
        r = QRectF(rect).adjusted(2, 2, -2, -2)
        if on:
            draw_box(p, r, ACCENT_TINT, ACCENT_BORDER, RADIUS)
        elif sunken:
            draw_box(p, r, SURFACE, BORDER_HOVER, RADIUS)
        elif hover:
            draw_box(p, r, SURFACE_HOVER, BORDER_HOVER, RADIUS, HIGHLIGHT)
        elif not ghost:
            draw_box(p, r, SURFACE, BORDER, RADIUS, HIGHLIGHT)

    # ---------------------------------------------------------- Grundelemente
    def drawPrimitive(self, pe, option, p, widget=None):
        st = option.state
        r = QRectF(option.rect)
        if pe in (S.PE_PanelButtonTool, S.PE_PanelButtonCommand, S.PE_PanelButtonBevel):
            self._button_panel(p, r, st, widget, ghost=_in_toolbar(widget))
            return
        if pe in (S.PE_FrameButtonTool, S.PE_FrameDefaultButton, S.PE_FrameFocusRect, S.PE_FrameButtonBevel,
                  S.PE_IndicatorToolBarHandle, S.PE_FrameLineEdit, S.PE_Frame, S.PE_FrameStatusBarItem,
                  S.PE_FrameTabBarBase, S.PE_PanelScrollAreaCorner, S.PE_IndicatorButtonDropDown):
            return
        if pe == S.PE_PanelLineEdit:
            parent = widget.parentWidget() if widget is not None else None
            if isinstance(parent, (QAbstractSpinBox, QComboBox)):
                return
            focus = bool(st & S.State_HasFocus)
            draw_box(p, r, INPUT, ACCENT if focus else BORDER_INPUT, RADIUS)
            return
        if pe in (S.PE_PanelToolBar, S.PE_PanelStatusBar, S.PE_PanelMenuBar):
            p.fillRect(option.rect, BG)
            return
        if pe in (S.PE_PanelMenu, S.PE_FrameMenu, S.PE_PanelTipLabel):
            p.fillRect(option.rect, INPUT)
            p.setPen(QPen(BORDER_HOVER, 1))
            p.drawRect(option.rect.adjusted(0, 0, -1, -1))
            return
        if pe == S.PE_IndicatorToolBarSeparator:
            c = r.center()
            if st & S.State_Horizontal:
                draw_hairline(p, QPointF(c.x(), r.top() + 6), QPointF(c.x(), r.bottom() - 6))
            else:
                draw_hairline(p, QPointF(r.left() + 6, c.y()), QPointF(r.right() - 6, c.y()))
            return
        if pe in (S.PE_IndicatorCheckBox, S.PE_IndicatorItemViewItemCheck):
            box = QRectF(r.center().x() - 9, r.center().y() - 9, 18, 18)
            if st & (S.State_On | S.State_NoChange):
                draw_box(p, box, ACCENT, ACCENT, 5, ACCENT_HL)
                p.save()
                p.setRenderHint(QPainter.Antialiasing)
                p.setPen(QPen(QColor("#ffffff"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                if st & S.State_On:
                    path = QPainterPath(QPointF(box.left() + 4.5, box.center().y()))
                    path.lineTo(QPointF(box.left() + 7.8, box.bottom() - 5))
                    path.lineTo(QPointF(box.right() - 4.5, box.top() + 5))
                    p.drawPath(path)
                else:
                    p.drawLine(QPointF(box.left() + 5, box.center().y()), QPointF(box.right() - 5, box.center().y()))
                p.restore()
            else:
                hover = bool(st & S.State_MouseOver)
                draw_box(p, box, INPUT, BORDER_HOVER if hover else BORDER_INPUT, 5)
            return
        if pe == S.PE_IndicatorRadioButton:
            box = QRectF(r.center().x() - 9, r.center().y() - 9, 18, 18)
            p.save()
            p.setRenderHint(QPainter.Antialiasing)
            if st & S.State_On:
                p.setPen(QPen(ACCENT, 1))
                p.setBrush(ACCENT)
                p.drawEllipse(box.adjusted(0.5, 0.5, -0.5, -0.5))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#ffffff"))
                p.drawEllipse(box.center(), 3.5, 3.5)
            else:
                p.setPen(QPen(BORDER_INPUT, 1))
                p.setBrush(INPUT)
                p.drawEllipse(box.adjusted(0.5, 0.5, -0.5, -0.5))
            p.restore()
            return
        if pe == S.PE_PanelItemViewItem:
            if st & S.State_Selected:
                draw_box(p, r.adjusted(2, 1, -2, -1), ACCENT_TINT, ACCENT_BORDER, RADIUS_S)
            elif st & S.State_MouseOver:
                draw_box(p, r.adjusted(2, 1, -2, -1), SURFACE, None, RADIUS_S)
            return
        arrows = {S.PE_IndicatorArrowDown: "down", S.PE_IndicatorArrowUp: "up",
                  S.PE_IndicatorArrowRight: "right", S.PE_IndicatorArrowLeft: "left",
                  S.PE_IndicatorSpinDown: "down", S.PE_IndicatorSpinUp: "up"}
        if pe in arrows:
            draw_chevron(p, r, arrows[pe], TEXT_DIM if st & S.State_Enabled else TEXT_DISABLED,
                         size=min(4.0, r.height() / 3))
            return
        super().drawPrimitive(pe, option, p, widget)

    # ---------------------------------------------------------- Steuerelemente
    def drawControl(self, ce, option, p, widget=None):
        st = option.state
        if ce == S.CE_MenuItem and isinstance(option, QStyleOptionMenuItem):
            r = QRectF(option.rect)
            p.fillRect(option.rect, INPUT)
            if option.menuItemType == QStyleOptionMenuItem.Separator:
                y = r.center().y()
                draw_hairline(p, QPointF(r.left() + 8, y), QPointF(r.right() - 8, y), BORDER_HOVER)
                return
            opt = QStyleOptionMenuItem(option)
            if st & S.State_Selected and st & S.State_Enabled:
                draw_box(p, r.adjusted(4, 1, -4, -1), SURFACE_HOVER, None, RADIUS_S)
                opt.state &= ~S.State_Selected
            super().drawControl(ce, opt, p, widget)
            return
        if ce == S.CE_MenuBarItem and isinstance(option, QStyleOptionMenuItem):
            p.fillRect(option.rect, BG)
            opt = QStyleOptionMenuItem(option)
            if st & (S.State_Selected | S.State_Sunken) and st & S.State_Enabled:
                draw_box(p, QRectF(option.rect).adjusted(1, 2, -1, -2), SURFACE_HOVER, None, RADIUS_S)
            opt.state &= ~(S.State_Selected | S.State_Sunken)
            super().drawControl(ce, opt, p, widget)
            return
        if ce in (S.CE_MenuBarEmptyArea, S.CE_ToolBar):
            p.fillRect(option.rect, BG)
            return
        if ce == S.CE_MenuEmptyArea:
            p.fillRect(option.rect, INPUT)
            return
        if ce == S.CE_PushButtonBevel:
            default = isinstance(option, QStyleOptionButton) and bool(option.features & QStyleOptionButton.DefaultButton)
            if default:  # Primärknopf: volle Akzentfarbe mit Lichtkante
                r = QRectF(option.rect).adjusted(2, 2, -2, -2)
                hover = bool(st & S.State_MouseOver)
                fill = QColor("#6872d9") if hover else ACCENT
                if st & S.State_Sunken:
                    fill = QColor("#5560c4")
                draw_box(p, r, fill, fill, RADIUS, ACCENT_HL)
            else:
                self._button_panel(p, option.rect, st, widget, ghost=False)
            return
        if ce == S.CE_PushButtonLabel:
            default = isinstance(option, QStyleOptionButton) and bool(option.features & QStyleOptionButton.DefaultButton)
            if default:
                opt = QStyleOptionButton(option)
                opt.palette.setColor(QPalette.ButtonText, QColor("#ffffff"))
                super().drawControl(ce, opt, p, widget)
                return
        if ce == S.CE_Splitter:
            p.fillRect(option.rect, BG)
            return
        if ce == S.CE_ShapedFrame:
            return
        super().drawControl(ce, option, p, widget)

    def drawComplexControl(self, cc, option, p, widget=None):
        st = option.state
        r = QRectF(option.rect)
        if cc == S.CC_ToolButton and isinstance(option, QStyleOptionToolButton):
            self._button_panel(p, r, st, widget, ghost=_in_toolbar(widget) or option.state & S.State_AutoRaise)
            opt = QStyleOptionToolButton(option)
            on = bool(st & S.State_On)
            opt.state &= ~(S.State_Sunken | S.State_Raised | S.State_MouseOver)
            if on:
                opt.palette.setColor(QPalette.ButtonText, ACCENT_BRIGHT)
            if option.features & QStyleOptionToolButton.MenuButtonPopup:
                menu = super().subControlRect(cc, option, S.SC_ToolButtonMenu, widget)
                draw_chevron(p, QRectF(menu).adjusted(-2, 0, -4, 0), "down", TEXT_DIM, 3.2)
                opt.subControls &= ~S.SC_ToolButtonMenu
                opt.features &= ~QStyleOptionToolButton.MenuButtonPopup
                opt.rect = option.rect.adjusted(0, 0, -menu.width() + 4, 0)
            opt.features &= ~QStyleOptionToolButton.HasMenu
            super().drawComplexControl(cc, opt, p, widget)
            return
        if cc == S.CC_ComboBox and isinstance(option, QStyleOptionComboBox):
            hover = bool(st & S.State_MouseOver)
            if option.editable:
                draw_box(p, r, INPUT, ACCENT if st & S.State_HasFocus else BORDER_INPUT, RADIUS)
            else:
                draw_box(p, r.adjusted(1, 1, -1, -1), SURFACE_HOVER if hover else SURFACE,
                         BORDER_HOVER if hover or st & S.State_On else BORDER, RADIUS, HIGHLIGHT)
            draw_chevron(p, QRectF(r.right() - 28, r.top(), 20, r.height()), "down",
                         TEXT_DIM if st & S.State_Enabled else TEXT_DISABLED, 3.6)
            return
        if cc == S.CC_SpinBox:
            draw_box(p, r, INPUT, ACCENT if st & S.State_HasFocus else BORDER_INPUT, RADIUS)
            up = QRectF(super().subControlRect(cc, option, S.SC_SpinBoxUp, widget))
            dn = QRectF(super().subControlRect(cc, option, S.SC_SpinBoxDown, widget))
            col = TEXT_DIM if st & S.State_Enabled else TEXT_DISABLED
            draw_chevron(p, up.adjusted(-3, 2, -3, 0), "up", col, 3.2)
            draw_chevron(p, dn.adjusted(-3, 0, -3, -2), "down", col, 3.2)
            return
        if cc == S.CC_ScrollBar and isinstance(option, QStyleOptionSlider):
            p.fillRect(option.rect, surface_color(widget) if widget is not None else BG)
            sl = QRectF(self.subControlRect(cc, option, S.SC_ScrollBarSlider, widget))
            if not sl.isEmpty() and option.maximum > option.minimum:
                knob = sl.adjusted(3, 3, -3, -3)
                active = bool(option.activeSubControls & S.SC_ScrollBarSlider) and bool(st & S.State_Sunken)
                hover = bool(st & S.State_MouseOver)
                color = QColor("#4b4f58") if active else (QColor("#3a3d44") if hover else QColor("#2a2c31"))
                draw_box(p, knob, color, None, min(knob.width(), knob.height()) / 2)
            return
        if cc == S.CC_Slider and isinstance(option, QStyleOptionSlider):
            p.fillRect(option.rect, BG)
            groove = QRectF(super().subControlRect(cc, option, S.SC_SliderGroove, widget))
            handle = QRectF(super().subControlRect(cc, option, S.SC_SliderHandle, widget))
            if option.orientation == Qt.Horizontal:
                g = QRectF(groove.left(), groove.center().y() - 2, groove.width(), 4)
                fill = QRectF(g.left(), g.top(), handle.center().x() - g.left(), g.height())
            else:
                g = QRectF(groove.center().x() - 2, groove.top(), 4, groove.height())
                fill = QRectF(g.left(), handle.center().y(), g.width(), g.bottom() - handle.center().y())
            draw_box(p, g, SURFACE_HOVER, None, 2)
            draw_box(p, fill, ACCENT, None, 2)
            knob = QRectF(handle.center().x() - 8, handle.center().y() - 8, 16, 16)
            p.save()
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(ACCENT, 2))
            p.setBrush(TEXT)
            p.drawEllipse(knob)
            p.restore()
            return
        super().drawComplexControl(cc, option, p, widget)


# ---------------------------------------------------------------- dunkle Titelleiste (Windows)
class _DarkTitleBars(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
            set_dark_titlebar(obj)
        return False


def set_dark_titlebar(widget):
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def apply(app: QApplication):
    """Design auf die ganze Anwendung anwenden (mehrfacher Aufruf ist unschädlich)."""
    if isinstance(app.style(), ModernStyle):
        return
    app.setStyle(ModernStyle())
    app.setPalette(palette())
    font = QFont("Segoe UI Variable Text")
    if not font.exactMatch():
        font = QFont("Segoe UI")
    font.setPointSizeF(9.75)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)
    app._dark_titles = _DarkTitleBars(app)
    app.installEventFilter(app._dark_titles)
    tr = QTranslator(app)
    if tr.load(QLocale(QLocale.German), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(tr)
        app._qt_de = tr
