"""Active terminal scope; legacy formats remain readable for offline reference."""

DISPLAY_TIMEFRAMES = ('M5', 'H4', 'D1', 'W1', 'MN1')
SOURCE_TIMEFRAMES = ('M5', 'H4', 'D1', 'W1', 'MN1')
# Offline legacy fixtures may still read past H1, but production never does.
LEGACY_SOURCE_TIMEFRAMES = ('M5', 'H1', 'H4', 'D1', 'W1', 'MN1')
FLOW_TIMEFRAMES = ('H4', 'D1', 'W1', 'MN1')
SUMMARY_LABELS = ('H4', 'D', 'W', 'M')
HTTP_REFRESH_SECONDS = 300
POLICY_VERSION = 'terminal-h4-d-w-m-3-all-finam-native'


def retired_setting(name):
    return (name.startswith('market') or name.startswith('showMarket') or
            name in ('enableMarketStructureInput', 'flowH1OnM5', 'showH1OnM5',
                     'fvgShowM1', 'fvgFontM1', 'fvgFontH1'))
