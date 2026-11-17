"""Measured job progress for the watcher.

Each stage owns a fixed percentage band. Inside a band, progress only moves when real work is
measured: a counted unit finished (a step of a known list) or a measured fraction (for example
Ollama output characters received against the size of the text it must return). Work whose
remaining amount cannot be known is reported as *unmeasured*: the percentage stays where it is and
the record carries the elapsed time and the last activity instead. Progress never decreases and
never reaches 100 until :meth:`Tracker.complete` is called after final verification.
"""
import time

# key, label, first percent, last percent, expected seconds before "taking longer than expected"
STAGES = (
    ('validate', 'Validate candidate information', 1, 5, 15),
    ('job', 'Create and verify job', 6, 10, 15),
    ('source', 'Process source resume', 11, 20, 30),
    ('ai', 'Ollama AI resume generation', 21, 45, 240),
    ('checks', 'Validate AI content and ATS checks', 46, 55, 20),
    ('latex', 'Generate LaTeX', 56, 70, 15),
    ('pdf', 'Compile and validate local PDF', 71, 80, 90),
    ('overleaf', 'Upload and compile on Overleaf', 81, 92, 240),
    ('verify', 'Verify final outputs and save evidence', 93, 99, 30),
)
BANDS = {key: (label, low, high, expected) for key, label, low, high, expected in STAGES}
ORDER = [key for key, *_ in STAGES]


class Tracker:
    """Collects measured progress and hands a JSON-serialisable snapshot to ``emit``."""

    def __init__(self, emit=lambda snapshot: None, clock=time.time):
        self.emit = emit
        self.clock = clock
        self.started = clock()
        self.percent = 0
        self.stage = ''
        self.step = ''
        self.measured = True
        self.units = None
        self.detail = ''
        self.stage_started = self.started
        self.activity = self.started
        self.completed = []
        self.skipped = []
        self.candidate = None

    # -- stage control -------------------------------------------------------------------
    def enter(self, key, step, candidate=None):
        """Start a stage. Earlier stages are marked complete; the floor is the band start."""
        if key not in BANDS:
            raise ValueError(f'Unknown progress stage {key}')
        if candidate is not None:
            self.candidate = candidate
        if key != self.stage:
            index = ORDER.index(key)
            for earlier in ORDER[:index]:
                if earlier not in self.completed and earlier not in self.skipped:
                    self.completed.append(earlier)
            self.stage = key
            self.stage_started = self.clock()
            self.units = None
        self.step = step
        self.measured = True
        self.detail = ''
        self._advance(BANDS[key][1])

    def skip(self, key, reason):
        if key not in self.skipped and key not in self.completed:
            self.skipped.append(key)
        self.step = reason
        self._publish()

    def units_done(self, done, total, step, unit='steps'):
        """A counted step of a known list finished: ``done`` of ``total``."""
        self.units = {'done': done, 'total': total, 'unit': unit}
        self._fraction(done / total if total else 1, step)

    def fraction(self, value, step, detail=''):
        """A measured fraction of the current stage (clamped to 0..1)."""
        self.detail = detail
        self._fraction(value, step)

    def unmeasured(self, step, detail=''):
        """Work is happening but its remaining amount is unknown: keep the percentage."""
        self.step = step
        self.detail = detail
        self.measured = False
        self.activity = self.clock()
        self._publish()

    def complete(self, step='Completed successfully'):
        for key in ORDER:
            if key not in self.completed and key not in self.skipped:
                self.completed.append(key)
        self.stage = 'done'
        self.step = step
        self.measured = True
        self.detail = ''
        self.activity = self.clock()
        self.percent = 100
        self._publish()

    # -- internals -----------------------------------------------------------------------
    def _fraction(self, value, step):
        low, high = BANDS[self.stage][1:3]
        value = min(1.0, max(0.0, value))
        self.step = step
        self.measured = True
        self._advance(low + int((high - low) * value))

    def _advance(self, percent):
        # Monotonic and never 100 before complete().
        self.percent = max(self.percent, min(99, percent))
        self.activity = self.clock()
        self._publish()

    def snapshot(self):
        label = BANDS[self.stage][0] if self.stage in BANDS else (
            'Completed successfully' if self.stage == 'done' else '')
        return {
            'percent': self.percent, 'stage': self.step or label, 'stage_key': self.stage,
            'stage_label': label, 'measured': self.measured, 'units': self.units,
            'detail': self.detail, 'started': self.started, 'stage_started': self.stage_started,
            'activity': self.activity, 'expected_seconds': BANDS[self.stage][3] if self.stage in BANDS else 0,
            'completed_stages': list(self.completed), 'skipped_stages': list(self.skipped),
            'updated': self.activity,
        }

    def _publish(self):
        snapshot = self.snapshot()
        if self.candidate is not None:
            snapshot['candidate'] = self.candidate
        self.emit(snapshot)


class LegacyTracker(Tracker):
    """Adapter for callers that pass the old ``progress(percent, stage, candidate)`` function."""

    def __init__(self, callback):
        super().__init__(lambda snapshot: callback(snapshot['percent'], snapshot['stage'],
                                                    snapshot.get('candidate')))
