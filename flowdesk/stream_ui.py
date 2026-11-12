"""GUI-thread stream coordination; market mutation remains in MarketWorker."""
from dataclasses import replace


class StreamController:
    def _init_stream(self, factory):
        self.stream_factory = factory
        self.stream_worker = None
        self.retiring_streams = []
        self.stream_key = None
        self.stream_epoch = 0
        self.stream_ready = False
        self.stream_needs_repair = True
        self.stream_repair_pending = False
        self.stream_disabled_key = None
        self.stream_buffer = {}
        self.stream_state = ''
        self.stream_quote = {}
        self.stream_error = ''

    def _auto_toggled(self, enabled):
        if enabled:
            self.stream_disabled_key = None
            self._ensure_stream()
        else:
            self._stop_stream()
            self.stream_state = 'Пауза'
            self._feed_text()
        self._schedule_auto_refresh()
        self._update_channel_indicator()

    def _stop_stream(self):
        self.stream_timer.stop()
        if hasattr(self,'stream_display_timer'):self.stream_display_timer.stop()
        self.stream_buffer.clear()
        self.stream_ready = False
        self.stream_needs_repair = True
        self.stream_repair_pending = False
        self.stream_key = None
        self.stream_quote = {}
        if self.stream_worker is not None:
            worker, self.stream_worker = self.stream_worker, None
            self.retiring_streams.append(worker)
            worker.stop()
        self._update_channel_indicator()

    def _stream_finished(self):
        worker = self.sender()
        if worker is self.stream_worker:
            self.stream_worker = None
            self.stream_ready = False
            self.stream_state = ('HTTP · поток недоступен' if self.http_checkbox.isChecked()
                                 else 'ISS+ недоступен · HTTP выключен')
            self._feed_text()
        if worker in self.retiring_streams:
            self.retiring_streams.remove(worker)
        worker.deleteLater()
        if self.closing_after_load:
            self.close()

    def _ensure_stream(self):
        if (not self.stream_factory or not self.auto_checkbox.isChecked() or self.closing_after_load or
                not (self.loaded_market or self.desired_market) or
                not self.source.startswith('MOEX:') or self.csv_overrides):
            return
        key = (self.loaded_market or self.desired_market)[:2]
        if key[1] == 'RFUD':
            seconds=getattr(getattr(self,'application_settings',None),'refresh_seconds',60)
            self.stream_state = (f'MOEX ISS · RFUD · HTTP, обновление {seconds} с'
                                 if self.http_checkbox.isChecked() else
                                 'RFUD · HTTP выключен; поток пока не поддерживается')
            self.feed_label.show()
            self._feed_text()
            return
        if key[1] != 'TQBR' or key == self.stream_disabled_key:
            return
        if self.stream_worker is not None and self.stream_key == key:
            return
        self._stop_stream()
        self.stream_key = key
        self.stream_epoch = 0
        self.stream_state = 'Подключение ISS+ DEMO'
        self.feed_label.show()
        self._feed_text()
        worker = self.stream_factory(*key)
        self.stream_worker = worker
        worker.event.connect(self._stream_event)
        worker.finished.connect(self._stream_finished)
        worker.start()
        self._update_channel_indicator()

    def _feed_text(self):
        text = self.stream_state
        if self.stream_ready and self.stream_needs_repair:
            text = ('ISS+ DEMO · требуется восстановление HTTP' if self.http_checkbox.isChecked()
                    else 'ISS+ DEMO · требуется сверка, HTTP выключен')
        q = self.stream_quote
        if q.get('LAST') is not None:
            text += f" · Сделка {q['LAST']:g} · {q.get('TIME', '—')} · статус {q.get('TRADINGSTATUS', '—')}"
        self.feed_label.setText(text)
        self.feed_label.setToolTip('Сделка не заменяет OHLC свечи. Д/Н/М могут обновляться быстрее внутридневного канала. Граница подтверждения свечей указана отдельно.')
        self._update_channel_indicator()

    def _stream_event(self, event):
        if (self.sender() is not self.stream_worker or self.closing_after_load or
                not self.auto_checkbox.isChecked() or (event.symbol, event.board) != self.stream_key):
            return
        if event.epoch < self.stream_epoch:
            return
        if event.epoch > self.stream_epoch:
            self.stream_epoch = event.epoch
            self.stream_buffer.clear()
        if event.kind == 'status':
            if event.state == 'resync':
                self.stream_needs_repair = self.stream_repair_pending = True
                self.stream_ready = False
            elif event.state == 'ready':
                self.stream_ready = True
            elif event.state in ('reconnecting', 'unavailable'):
                self.stream_ready = False
                self.stream_needs_repair = True
                self.stream_buffer.clear()
                if event.state == 'unavailable':
                    self.stream_disabled_key = self.stream_key
            approved = self.http_checkbox.isChecked()
            self.stream_state = {'connecting': 'Подключение ISS+ DEMO',
                'resync': 'Восстановление диапазона HTTP' if approved else 'Сверка ISS+ без HTTP',
                'ready': 'ISS+ DEMO · внутридневные данные ≈15 мин',
                'reconnecting': 'HTTP · переподключение потока' if approved else 'Переподключение ISS+',
                'unavailable': 'HTTP · поток недоступен' if approved else 'ISS+ недоступен · HTTP выключен'
                }.get(event.state, event.state)
            if event.detail:
                self.stream_error = event.detail
                self.status.setText(event.detail)
            elif event.state == 'ready':
                self.stream_error = ''
        elif event.kind == 'quote':
            self.stream_quote = event.quote
            if not self.stream_display_timer.isActive():self.stream_display_timer.start()
            return
        elif event.kind == 'candles':
            for bar in event.candles:
                self.stream_buffer[(event.timeframe, bar.time)] = (event, bar)
            if len(self.stream_buffer) > 512:
                self.stream_buffer.clear()
                self.stream_needs_repair = self.stream_repair_pending = True
        self._feed_text()
        if not self.stream_timer.isActive():
            self.stream_timer.start()

    def _flush_stream(self):
        if (self.worker is not None or not self.stream_worker or not self.loaded_market or
                self.loaded_market[:2] != self.stream_key or not self.auto_checkbox.isChecked() or
                self.csv_overrides or self.closing_after_load):
            return
        if self.stream_repair_pending and self.http_checkbox.isChecked():
            self.stream_repair_pending = False
            self._start_market((*self.loaded_market, True), automatic=True, repair=True)
        elif (self.stream_ready and self.stream_buffer and
              (not self.stream_needs_repair or not self.http_checkbox.isChecked())):
            batch = tuple(replace(e, candles=(bar,)) for e, bar in self.stream_buffer.values())
            self.stream_buffer.clear()
            self._start_market((*self.loaded_market, False), automatic=True, stream_batch=batch)
