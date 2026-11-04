"""Persisted installation checkpoints; completed work is skipped only after validation."""

from datetime import datetime, timezone
from .core import InstallerError


class Phases:
    def __init__(self, installer, state, save):
        self.i, self.state, self.save = installer, state, save

    def run(self, name, operation, validate=None):
        phases = self.state.setdefault('phases', {})
        previous = phases.get(name, {})
        if previous.get('status') == 'done' and previous.get('version') == self.state.get('version') and validate:
            try:
                if validate():
                    self.i.ui.say(self.i.ui.tr('Validated completed phase: {value}').format(value=self.i.ui.tr(name)), 'muted')
                    return
            except (OSError, ValueError, InstallerError):
                pass
        phase = {'status': 'running', 'version': self.state.get('version'), 'started': datetime.now(timezone.utc).isoformat()}
        phases[name] = phase
        self.save()
        try:
            result = operation()
        except BaseException:
            # Never persist exceptions: they can contain user credentials.
            phase['status'] = 'failed'
            self.save()
            raise
        phase.update(status='done', finished=datetime.now(timezone.utc).isoformat())
        self.save()
        return result
