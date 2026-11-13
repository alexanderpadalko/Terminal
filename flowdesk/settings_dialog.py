"""Scrollable v2.11 settings, Apply/OK/Cancel, and explicit default reset."""
from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QSlider, QSpinBox, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)
from .settings import (IndicatorSettings, ApplicationSettings, specifications,
                       CHART_COLOR_FIELDS, CHART_THEME_DEFAULTS)


def qcolor(value):
    return QColor(int(value[1:3],16),int(value[3:5],16),
                  int(value[5:7],16),int(value[7:9],16))


def opacity_percent(value):
    """Trading-style transparency: 0% opaque, 100% invisible."""
    return round((255-int(value[7:9],16))*100/255)


def rgba_value(rgb, transparency):
    """Keep the project's canonical #RRGGBBAA representation."""
    return rgb[:7].upper()+f"{round(255*(100-transparency)/100):02X}"


def palette_colors():
    """Ten-column swatch grid inspired by the TradingView color selector."""
    gray=("#FFFFFF","#E2E2E2","#C9C9C9","#AFAFAF","#969696",
          "#808080","#656565","#4D4D4D","#303030","#101010")
    hues=(0,25,47,139,162,187,222,258,289,331)
    rows=[gray]
    for saturation,value in ((235,250),(85,255),(125,245),
                             (170,230),(210,200),(235,160)):
        rows.append(tuple(QColor.fromHsv(hue,saturation,value).name().upper()
                          for hue in hues))
    return rows


class ColorPalettePopup(QFrame):
    """Compact non-modal palette + editable opacity, shared by color settings."""

    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Popup)
        self.owner=owner
        self.setObjectName("flowdeskColorPalette")
        self.setStyleSheet("""
            QFrame#flowdeskColorPalette {background:#171717; color:#e7e7e7;
                border:1px solid #494949; border-radius:7px;}
            QLabel {color:#a8a8a8; border:none; }
            QSpinBox {background:#202020; color:#f4f4f4; border:1px solid #505050;
                border-radius:4px; min-height:25px;}
            QSlider::groove:horizontal {background:#444; height:5px;
                border-radius:2px;}
            QSlider::sub-page:horizontal {background:#22a7a0;
                border-radius:2px;}
            QSlider::handle:horizontal {background:#fff; width:12px;
                margin:-5px 0; border-radius:6px;}
        """)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(11,11,11,11)
        layout.setSpacing(8)
        grid=QGridLayout()
        grid.setSpacing(3)
        self.swatches=[]
        for row, colors in enumerate(palette_colors()):
            for column,rgb in enumerate(colors):
                swatch=QToolButton(self)
                swatch.setObjectName("paletteSwatch")
                swatch.setFixedSize(23,23)
                swatch.setToolTip(rgb)
                swatch.setStyleSheet(
                    f"QToolButton {{background-color:{rgb}; border:1px solid #303030;"
                    "border-radius:4px;} QToolButton:hover {border:2px solid #fff;}")
                swatch.clicked.connect(
                    lambda checked=False, color=rgb: self._apply_rgb(color))
                grid.addWidget(swatch,row,column)
                self.swatches.append((swatch,rgb))
        layout.addLayout(grid)
        options=QHBoxLayout()
        custom=QPushButton("+",self)
        custom.setObjectName("customPaletteColor")
        custom.setFixedSize(29,26)
        custom.setToolTip("Другой цвет")
        custom.clicked.connect(self._custom_color)
        custom.setStyleSheet("QPushButton {background:#202020; color:white;"
                             "border:1px solid #444; border-radius:4px; font-size:18px;}")
        options.addWidget(custom)
        options.addStretch(1)
        layout.addLayout(options)
        separator=QFrame(self)
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("color:#494949;")
        layout.addWidget(separator)
        layout.addWidget(QLabel("Прозрачность",self))
        opacity_row=QHBoxLayout()
        self.transparency=QSlider(Qt.Orientation.Horizontal,self)
        self.transparency.setRange(0,100)
        self.transparency.setObjectName("paletteTransparency")
        self.percent=QSpinBox(self)
        self.percent.setRange(0,100)
        self.percent.setSuffix("%")
        self.percent.setFixedWidth(64)
        self.percent.setObjectName("paletteTransparencyPercent")
        self.transparency.valueChanged.connect(self.percent.setValue)
        self.percent.valueChanged.connect(self.transparency.setValue)
        self.transparency.valueChanged.connect(self._apply_transparency)
        opacity_row.addWidget(self.transparency,1)
        opacity_row.addWidget(self.percent)
        layout.addLayout(opacity_row)
        self.setFixedWidth(282)
        self.sync_from_owner()

    def sync_from_owner(self):
        percent=opacity_percent(self.owner.value)
        self.transparency.blockSignals(True)
        self.percent.blockSignals(True)
        self.transparency.setValue(percent)
        self.percent.setValue(percent)
        self.transparency.blockSignals(False)
        self.percent.blockSignals(False)

    def _apply_rgb(self, rgb):
        self.owner.set_value(rgba_value(rgb,self.transparency.value()))

    def _apply_transparency(self, transparency):
        self.owner.set_value(rgba_value(self.owner.value,transparency))

    def _custom_color(self):
        color=QColorDialog.getColor(qcolor(self.owner.value),self.owner,
                                    "Другой цвет")
        if color.isValid():
            self._apply_rgb(color.name().upper())


class ColorButton(QPushButton):
    """Small square color chip; no hex/opacity text in the settings form."""

    def __init__(self,value,parent=None):
        super().__init__(parent)
        self._palette=None
        self.setFixedSize(36,32)
        self.setText("")
        self.set_value(value)
        self.clicked.connect(self.choose)

    def set_value(self,value):
        self.value=value.upper()
        self.setToolTip(self.value[:7]+f" · Прозрачность {opacity_percent(self.value)}%")
        self.setAccessibleName("Цвет "+self.value[:7])
        self.update()

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=self.rect().adjusted(3,3,-3,-3)
        painter.fillRect(rect,QColor("#ededed"))
        for y in range(rect.top(),rect.bottom()+1,7):
            for x in range(rect.left(),rect.right()+1,7):
                if ((x-rect.left())//7+(y-rect.top())//7)%2:
                    painter.fillRect(x,y,min(7,rect.right()-x+1),
                                     min(7,rect.bottom()-y+1),QColor("#9f9f9f"))
        painter.fillRect(rect,qcolor(self.value))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#666666"),1))
        painter.drawRoundedRect(rect,4,4)
        painter.end()

    def choose(self):
        if self._palette is None:
            self._palette=ColorPalettePopup(self)
        self._palette.sync_from_owner()
        self._palette.adjustSize()
        anchor=self.mapToGlobal(QPoint(0,self.height()+3))
        screen=QGuiApplication.screenAt(anchor)
        if screen is not None:
            area=screen.availableGeometry()
            if anchor.y()+self._palette.height()>area.bottom():
                anchor.setY(self.mapToGlobal(QPoint(0,0)).y()-
                            self._palette.height()-3)
            anchor.setX(max(area.left(),min(anchor.x(),
                          area.right()-self._palette.width())))
            anchor.setY(max(area.top(),anchor.y()))
        self._palette.move(anchor)
        self._palette.show()
        self._palette.raise_()


class SettingsDialog(QDialog):
    applied=Signal(object,object)

    def __init__(self,indicator,application,parent=None,*,finam_only=False,token_action=None):
        super().__init__(parent)
        self.finam_only=finam_only
        self.setWindowTitle('Настройки FlowDesk')
        self.resize(860,690)
        self.controls={}
        layout=QVBoxLayout(self)
        self.tabs=QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        layout.addWidget(self.tabs,1)
        # Presentation colors are not Pine inputs and are stored separately
        # in ApplicationSettings. This tab always precedes structure settings.
        colors_widget=QWidget()
        colors_form=QFormLayout(colors_widget)
        colors_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.chart_controls={}
        theme='dark' if finam_only else 'tv'
        for field,label in CHART_COLOR_FIELDS:
            value=getattr(application,field) or CHART_THEME_DEFAULTS[theme][field]
            button=ColorButton(value,colors_widget)
            button.setObjectName('chart_'+field)
            self.chart_controls[field]=button
            colors_form.addRow(label,button)
        self.tabs.addTab(colors_widget,'Настройки графика')
        for group in dict.fromkeys(x['group'] for x in specifications()):
            widget=QWidget()
            form=QFormLayout(widget)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            for spec in specifications():
                if spec['group']!=group or spec['variable']=="showDevelopingLeg":
                    continue  # This structural leg is permanently enabled.
                control=self._control(spec,getattr(indicator,spec['variable']))
                control.setObjectName('setting_'+spec['variable'])
                control.setToolTip(spec['tooltip'].replace('TradingView','FlowDesk'))
                self.controls[spec['variable']]=control
                form.addRow(spec['title'],control)
            scroll=QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            self.tabs.addTab(scroll,group)
        app_widget=QWidget()
        form=QFormLayout(app_widget)
        self.auto=None
        self.http=None
        if not finam_only:
            # Older local/CSV window keeps its original configurable options.
            self.auto=QCheckBox()
            self.auto.setChecked(application.auto_updates)
            form.addRow('Автоматическое обновление Finam Trade API',self.auto)
            self.http=QCheckBox()
            self.http.setChecked(application.allow_http)
            self.http.setToolTip('Разрешить HTTP-загрузку истории и резервные свечи; без галочки — WebSocket/кэш.')
            form.addRow('Разрешить HTTP (история и резерв)',self.http)
        if finam_only and token_action is not None:
            self.token_button=QPushButton('Изменить токен Finam')
            self.token_button.setToolTip(
                'Secret key хранится в хранилище Windows; в настройках не отображается.')
            self.token_button.clicked.connect(lambda: token_action(self))
            form.addRow('Доступ к Finam Trade API',self.token_button)
        self.refresh=QSpinBox()
        self.refresh.setRange(10,3600)
        self.refresh.setSuffix(' с')
        self.refresh.setValue(application.refresh_seconds)
        form.addRow('Интервал резервного HTTP-обновления',self.refresh)
        self.panel=QCheckBox()
        self.panel.setChecked(application.show_panel)
        form.addRow('Показывать боковую панель',self.panel)
        form.addRow(QLabel('Время свечи — время её начала.\nД/Н/М: готовые свечи Finam Trade API.'))
        self.tabs.addTab(app_widget,'Приложение')
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Apply|
                                QDialogButtonBox.StandardButton.Cancel|QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('ОК')
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText('Применить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText('По умолчанию')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        layout.addWidget(buttons)

    @staticmethod
    def _control(spec,value):
        if spec['type']=='bool':
            result=QCheckBox()
            result.setChecked(value)
        elif spec['type'] in ('int','float'):
            result=QSpinBox() if spec['type']=='int' else QDoubleSpinBox()
            result.setRange(spec['min'],spec['max'])
            result.setSingleStep(spec.get('step',1))
            result.setValue(value)
        elif spec['type']=='color':result=ColorButton(value)
        else:
            result=QComboBox()
            result.addItems(spec['options'])
            result.setCurrentText(value)
        return result

    def values(self):
        values={}
        for spec in specifications():
            if spec['variable']=="showDevelopingLeg":
                values[spec['variable']]=True
                continue
            control=self.controls[spec['variable']]
            if spec['type']=='bool':value=control.isChecked()
            elif spec['type'] in ('int','float'):value=control.value()
            elif spec['type']=='color':value=control.value
            else:value=control.currentText()
            values[spec['variable']]=value
        colors={field:button.value for field,button in self.chart_controls.items()}
        return IndicatorSettings(values),ApplicationSettings(
            True if self.finam_only else self.auto.isChecked(),
            self.refresh.value(),self.panel.isChecked(),
            True if self.finam_only else self.http.isChecked(),
            **colors)

    def apply(self):
        self.applied.emit(*self.values())

    def accept(self):
        self.apply()
        super().accept()

    def restore_defaults(self):
        theme='dark' if self.finam_only else 'tv'
        for field,button in self.chart_controls.items():
            button.set_value(CHART_THEME_DEFAULTS[theme][field])
        for spec in specifications():
            if spec['variable']=="showDevelopingLeg":
                continue
            control=self.controls[spec['variable']]
            value=spec['default']
            if spec['type']=='bool':control.setChecked(value)
            elif spec['type'] in ('int','float'):control.setValue(value)
            elif spec['type']=='color':control.set_value(value)
            else:control.setCurrentText(value)
        if self.auto is not None:
            self.auto.setChecked(True)
        self.refresh.setValue(60)
        self.panel.setChecked(False)
        if self.http is not None:
            self.http.setChecked(False)
