# FlowDesk 0.2.26: RFUD WebSocket

The RFUD screenshot showed saved MXZ6 candles and “stream not supported” with HTTP disabled. That was an application restriction: the controller explicitly skipped RFUD, while the transport hardcoded MXSE subscriptions and the badge forced RFUD into HTTP mode.

Production routing now uses FORTS.securities / FORTS.candles, lowercase ticker/language selectors and native Si/Eu symbol case. Other RFUD symbols keep their exchange case. M1, H1, D1, W1 and m1 are requested; M5/H4 retain existing aggregation. Candle labels still use exchange FROM, not receipt time. HTTP opt-in, caches, session/auction policy, indicator calculations, chart optimizations and version number remain unchanged.

Protocol reference: https://moexalgo.github.io/docs/websocket/docs/websocket-en/ (FORTS section). Documentation is not evidence of DEMO access. Prior actual diagnostic run 43057673638 (2026-11-11) received FORTS.RFUD.SiZ6 quotes and automatic M1 updates; FORTS.FORTS.SiZ6 returned unknown board FORTS.

Current validation:

- Unit/Qt checks cover both markets' selectors, native ticker case, exchange candle times, RFUD reconnect, cache upserts/views and a stock-to-futures switch with HTTP disabled.
- tools/check_rfud_stream.py uses actual DEMO/guest, checks all six subscriptions on the ten watchlist futures, then observes MXZ6 M1 changes and deliberately breaks/reconnects the production client. It makes no market HTTP requests. Snapshots are not counted as proof of automatic updates.
- The packaged Windows executable runs --diagnose-stream for both SBER/TQBR and MXZ6/RFUD. It must receive all subscriptions and repeated M1 messages within the same connection epoch, without market HTTP. Quote updates are reported separately; absence of a second quote does not invalidate automatic candle delivery.
- Live results, timestamps and delays are saved as issplus-rfud-result.json and issplus-exe-rfud-result.json artifacts. Actual run results must be inspected before claiming current access or releasing a download link.

New history cannot be recovered from a quantity=2 stream snapshot. Initial full history and gap recovery still need the user's separate HTTP permission; an unavailable stream never enables it.

Actual run 43303772039 on 2026-11-12: all ten RFUD instruments supplied quotes and M1/H1/D/W/M snapshots. The production MXZ6 client received a new M1 key and recovered from a forced disconnect (ready epochs 1 and 2); zero market HTTP requests. M1 begin age was 960.609 seconds, equivalent to about 900.609 seconds after that minute ended. Packaged MXZ6 also reached ready and received two M1 messages, but the original probe failed solely because it required a second quote (only one was observed). The bounded probe now records quote-update evidence separately. A transient opening-handshake timeout recovered on the next attempt.

Run 43305334952 again received all sixty RFUD snapshots, then the FORTS gateway sent “Таймаут операции” during the production subscription phase. This observed transient STOMP error now follows bounded reconnect backoff, rather than permanently disabling the stream. Unrecognized resource/permission errors still stop retries. A regression test reproduces the exact timeout frame and verifies re-subscription.

Probes distinguish automatic updates from snapshots outside the known delayed session. No new M1 is expected after the 23:50 close plus 15-minute delay, or before the next known opening. In that case all subscriptions and (in the RFUD live test) forced reconnection remain required, but automatic_updates is explicitly false and validation says SNAPSHOTS_ONLY_OUTSIDE_SESSION. Unknown schedules remain strict. This diagnostic policy does not change application candle times, indicator confirmation or session rules. Actual new M1 delivery was already observed in run 43303772039; it must not be claimed from an after-hours snapshot-only run.
