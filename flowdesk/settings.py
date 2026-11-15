"""Typed, persisted settings matching every input of the pinned Pine v2.11."""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

REFERENCE_SHA256='CC2E1422F6D865C7D4814B7FF7CD4EEA82F87EB89642E8EF5F158A47B85D057F'
SOURCES=('open','high','low','close','hl2','hlc3','ohlc4','volume')
SIZES={'tiny':8,'small':10,'normal':14,'large':20,'huge':36}
CALCULATION_KEYS=('fvgExcludeGaps','fvgMaxZones','liquidityHistoryBars','enableMarketStructureInput','showMarketOnlyOnH1')
# Chart-only appearance is separate from the 74 pinned Pine inputs. None means
# the palette selected by the chart theme, preserving legacy settings.
CHART_COLOR_FIELDS = (
    ('chart_candle_up', 'Восходящая свеча'),
    ('chart_candle_down', 'Нисходящая свеча'),
    ('chart_outline_up', 'Контур восходящей свечи'),
    ('chart_outline_down', 'Контур нисходящей свечи'),
    ('chart_wick_up', 'Фитиль восходящей свечи'),
    ('chart_wick_down', 'Фитиль нисходящей свечи'),
    ('chart_background', 'Фон графика'),
    ('chart_grid', 'Сетка графика'),
    ('chart_axis_text', 'Текст шкалы'),
)
# One base appearance for both Finam and legacy UI entrypoints. Users can
# override every chart color below in the settings dialog; there is no
# hidden light/dark preset selected by the application entrypoint.
CHART_THEME_DEFAULTS = {
    "user": {
        'chart_candle_up': '#20C997FF',
        'chart_candle_down': '#F46B82FF',
        'chart_outline_up': '#20C997FF',
        'chart_outline_down': '#F46B82FF',
        'chart_wick_up': '#20C997FF',
        'chart_wick_down': '#F46B82FF',
        'chart_background': '#0C131EFF',
        'chart_grid': '#202B3BFF',
        'chart_axis_text': '#B7C3D5FF',
    },
}

@lru_cache(maxsize=1)
def specifications():
    payload=json.loads((Path(__file__).parent/'data/settings_v211.json').read_text(encoding='utf-8'))
    if payload['sha256']!=REFERENCE_SHA256 or len(payload['inputs'])!=74:
        raise ValueError('Invalid v2.11 settings manifest')
    return tuple(payload['inputs'])


def validate(spec,value):
    kind=spec['type']
    if kind=='bool':
        if type(value) is not bool:raise ValueError(spec['title'])
    elif kind=='int':
        if type(value) is not int:raise ValueError(spec['title'])
    elif kind=='float':
        if isinstance(value,bool) or not isinstance(value,(float,int)):raise ValueError(spec['title'])
        value=float(value)
    elif kind=='color':
        if not isinstance(value,str) or not re.fullmatch(r'#[0-9A-Fa-f]{8}',value):raise ValueError(spec['title'])
        value=value.upper()
    elif kind in ('string','source'):
        if value not in spec['options']:raise ValueError(spec['title'])
    else:raise ValueError(kind)
    if kind in ('int','float'):
        if not spec['min']<=value<=spec['max']:raise ValueError(spec['title'])
    return value


class IndicatorSettings:
    def __init__(self,values=None):
        self._values={spec['variable']:validate(spec,(values or {}).get(spec['variable'],spec['default'])) for spec in specifications()}
        # Last forming leg is a fixed display rule, including when opening
        # older settings files that stored this option as false.
        self._values["showDevelopingLeg"]=True
        # Debug overlays are not user-facing features. Keep the 74-field
        # manifest for old saved files, but never activate these two flags.
        self._values["showMarketStatusInput"]=False
        self._values["fvgMtfDiagnostics"]=False

    def __getattr__(self,key):
        values=self.__dict__.get('_values',{})
        if key in values:return values[key]
        raise AttributeError(key)

    def as_dict(self):return dict(self._values)

    def changed(self,**values):return IndicatorSettings({**self._values,**values})

    def calculation_key(self):
        raw=json.dumps({k:self._values[k] for k in CALCULATION_KEYS},sort_keys=True)
        return hashlib.sha256((REFERENCE_SHA256+raw).encode()).hexdigest()


@dataclass(frozen=True)
class ApplicationSettings:
    auto_updates: bool = True
    refresh_seconds: int = 60
    show_panel: bool = False
    allow_http: bool = False  # Explicit opt-in for network-based candle history.
    chart_candle_up: str | None = None
    chart_candle_down: str | None = None
    chart_outline_up: str | None = None
    chart_outline_down: str | None = None
    chart_wick_up: str | None = None
    chart_wick_down: str | None = None
    chart_background: str | None = None
    chart_grid: str | None = None
    chart_axis_text: str | None = None

    def __post_init__(self):
        if (type(self.auto_updates) is not bool or type(self.show_panel) is not bool
                or type(self.allow_http) is not bool):
            raise ValueError('Invalid application settings')
        if type(self.refresh_seconds) is not int or not 10<=self.refresh_seconds<=3600:
            raise ValueError('Интервал обновления должен быть от 10 до 3600 секунд')
        for key,_ in CHART_COLOR_FIELDS:
            value=getattr(self,key)
            if value is not None:
                if not isinstance(value,str) or not re.fullmatch(r'#[0-9A-Fa-f]{8}',value):
                    raise ValueError('Неверный цвет графика: '+key)
                object.__setattr__(self,key,value.upper())


class SettingsStore:
    def __init__(self,path=None):
        self.path=Path(path or Path.home()/'.flowdesk/settings_v211.json')
        self.warning=''

    def read(self):
        try:
            data=json.loads(self.path.read_text(encoding='utf-8-sig'))
            if data.get('schema')!=1:raise ValueError('Unsupported settings schema')
            return IndicatorSettings(data['indicator']),ApplicationSettings(**data.get('application',{}))
        except FileNotFoundError:
            pass
        except (OSError,ValueError,TypeError,KeyError) as error:
            self.warning=f'Не удалось прочитать настройки: {error}. Использованы умолчания v2.11.'
        return IndicatorSettings(),ApplicationSettings()

    def write(self,indicator,application):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        data={'schema':1,'reference':'v2.11 H1 + H4','reference_sha256':REFERENCE_SHA256,
              'indicator':indicator.as_dict(),'application':vars(application)}
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(self.path)


class MemorySettingsStore:
    warning=''
    def __init__(self):self.values=(IndicatorSettings(),ApplicationSettings())
    def read(self):return self.values
    def write(self,indicator,application):self.values=(indicator,application)
