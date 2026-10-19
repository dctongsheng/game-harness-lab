"""Bind the shared engine to BetterGI's wire-compatible account scope."""
from ...core.engine import AuditLog, ExecutionEngine
from .contracts import ActionCommand

class RealtimeRunner(ExecutionEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, command_type=ActionCommand, **kwargs)
