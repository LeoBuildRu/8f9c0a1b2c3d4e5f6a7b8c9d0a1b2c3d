"""QThread-обёртка над TraderAgent — только для desktop UI."""

from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from app.agent.trader_agent import TraderAgent


class TraderAgentWorker(QThread):
    tick_finished = pyqtSignal(object)   # AgentTickReport
    error_occurred = pyqtSignal(str)

    def __init__(self, agent: TraderAgent, tickers: Optional[List[str]] = None):
        super().__init__()
        self.agent = agent
        self.tickers = tickers

    def run(self):
        try:
            report = self.agent.tick(self.tickers)
            self.tick_finished.emit(report)
        except Exception as exc:
            self.error_occurred.emit(f"{type(exc).__name__}: {exc}")
