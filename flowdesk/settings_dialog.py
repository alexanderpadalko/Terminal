"""Scrollable v2.11 settings, Apply/OK/Cancel, and explicit default reset."""
from PySide6.QtCore import Qt,Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox,QColorDialog,QComboBox,QDialog,QDialogButtonBox,
    QDoubleSpinBox,QFormLayout,QLabel,QPushButton,QScrollArea,QSpinBox,QTabWidget,QVBoxLayout,QWidget)
from .settings import IndicatorSettings,ApplicationSettings,specifications


def qcolor(value):
    return QColor(int(value[1:3],16),int(value[3:5],16),int(value[5:7],16),int(value[7:9],16))


class ColorButton(QPushButton):
    def __init__(self,value,parent=None):
        super().__init__(parent)
        self.set_value(value)
        self.clicked.connect(self.choose)

    def set_value(self,value):
        self.value=value
        color=qcolor(value)
        self.setText(value[:7]+f' · прозрачность {round(100*(1-color.alphaF()))}%')
        self.setStyleSheet(f'border-left: 24px solid {value[:7]}; padding: 5px; text-align: left;')

    def choose(self):
        color=QColorDialog.getColor(qcolor(self.value),self,'Цвет и прозрачность',QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            self.set_value(f'#{color.red():02X}{color.green():02X}{color.blue():02X}{color.alpha():02X}')


class SettingsDialog(QDialog):
    applied=Signal(object,object)

    def __init__(self,indicator,application,parent=None):
        super().__init__(parent)
        self.setWindowTitle('Настройки FlowDesk · TradingView v2.11')
        self.resize(860,690)
        self.controls={}
        layout=QVBoxLayout(self)
        note=QLabel('Эталон: v2.11 H1 + H4 · настройки сохраняются между запусками')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs=QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        layout.addWidget(self.tabs,1)
        for group in dict.fromkeys(x['group'] for x in specifications()):
            widget=QWidget()
            form=QFormLayout(widget)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            for spec in specifications():
                if spec['group']!=group:continue
                control=self._control(spec,getattr(indicator,spec['variable']))
                control.setObjectName('setting_'+spec['variable'])
                control.setToolTip(spec['tooltip'])
                self.controls[spec['variable']]=control
                form.addRow(spec['title'],control)
            scroll=QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            self.tabs.addTab(scroll,group)
        app_widget=QWidget()
        form=QFormLayout(app_widget)
        self.auto=QCheckBox()
        self.auto.setChecked(application.auto_updates)
        form.addRow('Автоматическое обновление MOEX',self.auto)
        self.refresh=QSpinBox()
        self.refresh.setRange(10,3600)
        self.refresh.setSuffix(' с')
        self.refresh.setValue(application.refresh_seconds)
        form.addRow('Интервал резервного HTTP-обновления',self.refresh)
        self.panel=QCheckBox()
        self.panel.setChecked(application.show_panel)
        form.addRow('Показывать боковую панель',self.panel)
        form.addRow(QLabel('Часовой пояс: Москва · время свечи — время её начала.\nД/Н/М: готовые свечи MOEX, метка 00:00.'))
        self.tabs.addTab(app_widget,'Приложение')
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Apply|
                                QDialogButtonBox.StandardButton.Cancel|QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('ОК')
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText('Применить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText('Умолчания v2.11')
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
            control=self.controls[spec['variable']]
            if spec['type']=='bool':value=control.isChecked()
            elif spec['type'] in ('int','float'):value=control.value()
            elif spec['type']=='color':value=control.value
            else:value=control.currentText()
            values[spec['variable']]=value
        return IndicatorSettings(values),ApplicationSettings(self.auto.isChecked(),self.refresh.value(),self.panel.isChecked())

    def apply(self):
        self.applied.emit(*self.values())

    def accept(self):
        self.apply()
        super().accept()

    def restore_defaults(self):
        for spec in specifications():
            control=self.controls[spec['variable']]
            value=spec['default']
            if spec['type']=='bool':control.setChecked(value)
            elif spec['type'] in ('int','float'):control.setValue(value)
            elif spec['type']=='color':control.set_value(value)
            else:control.setCurrentText(value)
        self.auto.setChecked(True)
        self.refresh.setValue(60)
        self.panel.setChecked(False)
