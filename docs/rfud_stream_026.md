# FlowDesk 0.2.26: RFUD WebSocket

The RFUD screenshot showed saved MXZ6 candles and “stream not supported” with HTTP disabled. That was an application restriction: the controller explicitly skipped RFUD, while the transport hardcoded MXSE subscriptions and the badge forced RFUD into HTTP mode.

Production routing now uses FORTS.securities / FORTS.candles, lowercase ticker/language selectors and native Si/Eu symbol case. Other RFUD symbols keep their exchange case. M1, H1, D1, W1 and m1 are requested; M5/H4 retain existing aggregation. Candle labels still use exchange FROM, not receipt time. HTTP opt-in, caches, session/auction policy, indicator calculations, chart optimizations and version number remain unchanged.

Protocol reference: https://moexalgo.github.io/docs/websocket/docs/websocket-en/ (FORTS section). Documentation is not evidence of DEMO access. Prior actual diagnostic run 43057673638 (2026-11-11) received FORTS.RFUD.SiZ6 quotes and automatic M1 updates; FORTS.FORTS.SiZ6 returned unknown board FORTS.

Current validation:

- Unit/Qt checks cover both markets' selectors, native ticker case, exchange candle times, RFUD reconnect, cache upserts/views and a stock-to-futures switch with HTTP disabled.
- tools/check_rfud_stream.py uses actual DEMO/guest, checks all six subscriptions on the ten watchlist futures, then observes MXZ6 M1 changes and deliberately breaks/reconnects the production client. It makes no market HTTP requests. Snapshots are not counted as proof of automatic updates.
- The packaged Windows executable runs --diagnose-stream for both SBER/TQBR and MXZ6/RFUD. It must receive all subscriptions and repeated M1/quote messages without market HTTP.
- Live results, timestamps and delays are saved as issplus-rfud-result.json and issplus-exe-rfud-result.json artifacts. Actual run results must be inspected before claiming current access or releasing a download link.

New history cannot be recovered from a quantity=2 stream snapshot. Initial full history and gap recovery still need the user's separate HTTP permission; an unavailable stream never enables it.
