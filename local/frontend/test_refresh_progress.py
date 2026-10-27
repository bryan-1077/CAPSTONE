"""Manual finite-state interpretations of the saved command-engine contract.

This is a test-only reasoning aid. It neither parses contract prose nor executes
target RTL, and cannot issue an approval. The reference sequencing is supplied
explicitly to test whether each interpretation of the formation gate permits
the required paths. A successful chosen interpretation is not an exhaustive
proof of the source specification.
"""
from dataclasses import asdict, dataclass
import copy
from pathlib import Path
import unittest

from specification import read_document


@dataclass(frozen=True)
class Request:
    bank: int
    row: int
    operation: str = 'READ'
    token: str = 'original-request'


@dataclass(frozen=True)
class Command:
    operation: str
    bank: int | None = None
    row: int | None = None


MODES = ('pending_blocks_formation', 'service_only_without_preparation', 'service_only_with_preparation')


class ProgressModel:
    """Selected edge semantics, not an implementation extracted from the artifact.

    Initial open banks have mature ACT history. No additional host work is
    injected. Dispatch permission is observed, but FIFO/memory/response delivery
    are outside this model. Command stalls eventually clear unless a test
    explicitly supplies an indefinitely blocked environment. tFAW is outside
    this model (the chosen paths contain at most one new ACT).
    """
    def __init__(self, mode, rows, *, active=None, candidate=None, trcd=2, tras=3,
                 trfc=4, pending=True, busy_until=None):
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        self.rows = list(rows)
        self.active = active
        self.candidate = candidate
        self.trcd, self.tras, self.trfc = trcd, tras, trfc
        self.pending = pending
        self.busy_until = busy_until
        self.last_act = [-max(trcd, tras) if r is not None else None for r in rows]
        self.edge = 0
        self.trace = []

    def busy(self):
        return self.busy_until is not None and self.edge < self.busy_until

    def choose(self):
        if self.active is not None:
            bank, row = self.active.bank, self.active.row
            if self.rows[bank] is None:
                return Command('ACT', bank, row)
            if self.rows[bank] != row:
                return Command('PRE', bank)
            return Command(self.active.operation, bank, row)
        if self.pending and self.mode == 'service_only_with_preparation':
            opened = [bank for bank, row in enumerate(self.rows) if row is not None]
            return Command('PRE', min(opened)) if opened else Command('REF')
        return None

    def permitted(self, command):
        if self.busy():
            return False
        op, bank = command.operation, command.bank
        if op == 'REF':
            return self.pending and self.active is None and all(row is None for row in self.rows)
        if op == 'ACT':
            return self.active is not None and self.rows[bank] is None
        if op == 'PRE':
            return self.rows[bank] is not None and self.edge - self.last_act[bank] >= self.tras
        if op in ('READ', 'WRITE'):
            return (self.active is not None and self.rows[bank] == self.active.row
                    and command.row == self.active.row and self.edge - self.last_act[bank] >= self.trcd)
        return False

    def state(self):
        return {'edge': self.edge, 'rows': list(self.rows), 'pending': self.pending,
                'busy': self.busy(), 'active': asdict(self.active) if self.active else None,
                'candidate': asdict(self.candidate) if self.candidate else None,
                'dispatch_allowed': self.active is None and not self.pending and not self.busy()}

    def step(self, *, due=False, reset=False, blocked=False):
        before = self.state()
        action, committed = 'idle', None
        if reset:
            self.rows = [None] * len(self.rows)
            self.last_act = [None] * len(self.rows)
            self.pending, self.active, self.candidate, self.busy_until = False, None, None, None
            action = 'reset'
        elif self.candidate is not None:
            if blocked or not self.permitted(self.candidate):
                action = 'hold'
            else:
                command = self.candidate
                committed = asdict(command)
                action = 'commit'
                if command.operation == 'ACT':
                    self.rows[command.bank] = command.row
                    self.last_act[command.bank] = self.edge
                elif command.operation == 'PRE':
                    self.rows[command.bank] = None
                elif command.operation in ('READ', 'WRITE'):
                    self.active = None
                elif command.operation == 'REF':
                    self.pending = False
                    self.busy_until = self.edge + self.trfc
                self.candidate = None  # No same-edge replacement formation.
        elif not self.busy() and not (self.mode == 'pending_blocks_formation' and self.pending):
            self.candidate = self.choose()
            if self.candidate is not None:
                action = 'form'
        if due and not reset:
            self.pending = True  # Due wins acknowledgment, using pre-edge state.
        after = self.state()
        row = {'before': before, 'action': action, 'committed': committed, 'due': due, 'after': after}
        self.trace.append(row)
        self.edge += 1
        return row

    def run(self, *, limit=2048, blocked_edges=(), due_edges=()):
        for _ in range(limit):
            row = self.step(blocked=self.edge in blocked_edges, due=self.edge in due_edges)
            if row['committed'] and row['committed']['operation'] == 'REF':
                return 'ref_committed'
            # This is a fixed point in the progress-relevant projection of THIS
            # model, irrespective of further age advancement. It is not a claim
            # that every interpretation/implementation of the prose deadlocks.
            if self.candidate is None and self.pending and not self.busy():
                if self.mode == 'pending_blocks_formation' or (
                        self.mode == 'service_only_without_preparation' and self.active is None):
                    return 'stalled_in_chosen_interpretation'
        return 'bounded_timeout'

    def commits(self):
        return [(r['before']['edge'], r['committed']) for r in self.trace if r['committed']]


def scenarios():
    return {
        'pending_with_active_work': {'rows': (4, None, 8, None), 'active': Request(0, 9)},
        'pending_idle_open_banks': {'rows': (4, None, 8, 2)},
        'pending_idle_closed_banks': {'rows': (None, None, None, None)},
    }


class RefreshProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = read_document(Path(__file__).parent / 'testdata/refresh_preparation_progress.json')

    def test_saved_evidence_distinguishes_dispatch_gate_from_ambiguous_candidate_gate(self):
        engine = self.fixture['contracts']['command_engine']
        self.assertIn('refresh is inactive', engine['update_events']['every_clock'])
        self.assertIn('refresh is neither pending nor in service', engine['handshakes']['dispatch'])
        self.assertIn('may finish, then all open banks are closed in ascending bank order',
                      self.fixture['system_rules']['SYS_REFRESH']['statement'])
        self.assertIn('refresh_preparation', engine['owned_state'])

    def test_three_requested_paths_stall_if_pending_blocks_candidate_formation(self):
        for name, initial in scenarios().items():
            with self.subTest(name=name):
                model = ProgressModel('pending_blocks_formation', **initial)
                self.assertEqual(model.run(), 'stalled_in_chosen_interpretation')
                self.assertEqual(model.commits(), [])
                self.assertTrue(model.pending)

    def test_busy_only_gate_without_preparation_entry_drains_but_cannot_refresh(self):
        for name, initial in scenarios().items():
            with self.subTest(name=name):
                model = ProgressModel('service_only_without_preparation', **initial)
                self.assertEqual(model.run(), 'stalled_in_chosen_interpretation')
                operations = [c['operation'] for _, c in model.commits()]
                self.assertEqual(operations, ['PRE', 'ACT', 'READ'] if initial.get('active') else [])
                self.assertIsNone(model.active)
                self.assertTrue(model.pending)

    def test_three_paths_progress_with_explicit_drain_and_preparation_selection(self):
        expected = {
            'pending_with_active_work': [('PRE', 0), ('ACT', 0), ('READ', 0), ('PRE', 0), ('PRE', 2), ('REF', None)],
            'pending_idle_open_banks': [('PRE', 0), ('PRE', 2), ('PRE', 3), ('REF', None)],
            'pending_idle_closed_banks': [('REF', None)],
        }
        for name, initial in scenarios().items():
            with self.subTest(name=name):
                model = ProgressModel('service_only_with_preparation', **initial)
                self.assertEqual(model.run(), 'ref_committed')
                self.assertEqual([(c['operation'], c['bank']) for _, c in model.commits()], expected[name])
                self.assertTrue(all(r['before']['pending'] and not r['before']['dispatch_allowed'] for r in model.trace))
                self.assertFalse(model.pending)
                self.assertIsNone(model.active)
                self.assertEqual(model.rows, [None] * 4)

    def test_saved_timing_values_preserve_progress_and_minimum_boundaries(self):
        timing = self.fixture['timing_cycles']
        model = ProgressModel('service_only_with_preparation', **scenarios()['pending_with_active_work'],
            trcd=timing['tRCD'], tras=timing['tRAS'], trfc=timing['tRFC1'])
        self.assertEqual(model.run(), 'ref_committed')
        commands = model.commits()
        act = next(e for e, c in commands if c['operation'] == 'ACT')
        read = next(e for e, c in commands if c['operation'] == 'READ')
        final_pre = next(e for e, c in commands if e > read and c['operation'] == 'PRE')
        self.assertGreaterEqual(read - act, timing['tRCD'])
        self.assertGreaterEqual(final_pre - act, timing['tRAS'])
        self.assertGreater(sum(r['action'] == 'hold' for r in model.trace), 0)

    def test_existing_setup_candidate_may_commit_but_strict_gate_still_stalls_drain(self):
        initial = {'rows': (None,), 'active': Request(0, 5), 'candidate': Command('ACT', 0, 5)}
        strict = ProgressModel('pending_blocks_formation', **initial)
        self.assertEqual(strict.run(), 'stalled_in_chosen_interpretation')
        self.assertEqual([c['operation'] for _, c in strict.commits()], ['ACT'])
        self.assertIsNotNone(strict.active)
        permissive = ProgressModel('service_only_with_preparation', **initial)
        self.assertEqual(permissive.run(), 'ref_committed')
        self.assertEqual([c['operation'] for _, c in permissive.commits()], ['ACT', 'READ', 'PRE', 'REF'])

    def test_due_with_final_retirement_still_requires_a_new_preparation_candidate(self):
        for mode in ('pending_blocks_formation', 'service_only_with_preparation'):
            model = ProgressModel(mode, (5,), active=Request(0, 5), candidate=Command('READ', 0, 5), pending=False)
            result = model.run(due_edges={0})
            self.assertEqual(model.commits()[0][1]['operation'], 'READ')
            self.assertIsNone(model.active)
            self.assertEqual(result, 'stalled_in_chosen_interpretation' if mode == 'pending_blocks_formation' else 'ref_committed')

    def test_blocked_candidate_holds_payload_and_ownership_without_bank_updates(self):
        request = Request(0, 5, 'WRITE', 'write-payload-token')
        model = ProgressModel('service_only_with_preparation', (5, None), active=request)
        first = model.step()
        self.assertEqual(first['action'], 'form')
        candidate, history = model.candidate, copy.deepcopy(model.last_act)
        for _ in range(5):
            row = model.step(blocked=True)
            self.assertEqual(row['action'], 'hold')
            self.assertEqual(model.candidate, candidate)
            self.assertEqual(model.active, request)
            self.assertEqual(model.rows, [5, None])
            self.assertEqual(model.last_act, history)
        self.assertEqual(model.run(), 'ref_committed')
        self.assertEqual([c['operation'] for _, c in model.commits()], ['WRITE', 'PRE', 'REF'])

    def test_pending_during_service_waits_until_exact_release_before_forming(self):
        for mode in MODES:
            model = ProgressModel(mode, (None,), busy_until=5)
            for _ in range(5):
                row = model.step()
                self.assertTrue(row['before']['busy'])
                self.assertIsNone(model.candidate)
                self.assertIsNone(row['committed'])
            if mode == 'service_only_with_preparation':
                self.assertEqual(model.step()['action'], 'form')  # E == service expiry.
                self.assertEqual(model.step()['committed']['operation'], 'REF')
            else:
                self.assertEqual(model.run(), 'stalled_in_chosen_interpretation')

    def test_due_and_ref_commit_coalesce_pending_without_early_new_candidates(self):
        model = ProgressModel('service_only_with_preparation', (None,), trfc=4)
        self.assertEqual(model.step()['action'], 'form')
        ref = model.step(due=True)
        self.assertEqual(ref['committed']['operation'], 'REF')
        self.assertTrue(model.pending)
        self.assertEqual(model.busy_until, 5)
        for _ in range(3):
            self.assertEqual(model.step()['action'], 'idle')
            self.assertIsNone(model.candidate)
        self.assertEqual(model.step()['action'], 'form')

    def test_reset_wins_over_pending_due_and_permitted_candidate(self):
        for mode in MODES:
            model = ProgressModel(mode, (5,), active=Request(0, 5), candidate=Command('READ', 0, 5))
            row = model.step(reset=True, due=True)
            self.assertEqual(row['action'], 'reset')
            self.assertIsNone(row['committed'])
            self.assertFalse(model.pending)
            self.assertIsNone(model.active)
            self.assertIsNone(model.candidate)
            self.assertEqual(model.rows, [None])

    def test_all_bank_masks_close_in_ascending_order_for_one_and_four_banks(self):
        for banks in (1, 4):
            for mask in range(1 << banks):
                rows = tuple(10 + i if mask & (1 << i) else None for i in range(banks))
                model = ProgressModel('service_only_with_preparation', rows)
                self.assertEqual(model.run(), 'ref_committed')
                self.assertEqual([c['bank'] for _, c in model.commits() if c['operation'] == 'PRE'],
                                 [i for i in range(banks) if mask & (1 << i)])

    def test_active_drain_matrix_preserves_request_and_orders_only_post_drain_cleanup(self):
        # 256 initial states, with finite externally imposed timing stalls.
        for mask in range(16):
            for bank in range(4):
                for operation in ('READ', 'WRITE'):
                    for row in (10 + bank, 99):
                        with self.subTest(mask=mask, bank=bank, operation=operation, row=row):
                            rows = tuple(10 + i if mask & (1 << i) else None for i in range(4))
                            request = Request(bank, row, operation)
                            model = ProgressModel('service_only_with_preparation', rows, active=request)
                            self.assertEqual(model.run(blocked_edges=set(range(0, 100, 5))), 'ref_committed')
                            commits = model.commits()
                            final = [(e, c) for e, c in commits if c['operation'] in ('READ', 'WRITE')]
                            self.assertEqual(len(final), 1)
                            final_edge, command = final[0]
                            self.assertEqual((command['operation'], command['bank'], command['row']), (operation, bank, row))
                            for sample in model.trace:
                                if sample['before']['edge'] <= final_edge:
                                    self.assertEqual(sample['before']['active'], asdict(request))
                            cleanup = [c['bank'] for e, c in commits if e > final_edge and c['operation'] == 'PRE']
                            self.assertEqual(cleanup, sorted({i for i in range(4) if mask & (1 << i)} | {bank}))

    def test_unbounded_environment_stall_is_not_reported_as_a_formation_gate_fixed_point(self):
        model = ProgressModel('service_only_with_preparation', (5,), active=Request(0, 5), candidate=Command('READ', 0, 5))
        self.assertEqual(model.run(limit=20, blocked_edges=set(range(20))), 'bounded_timeout')
        self.assertEqual(model.commits(), [])
        self.assertIsNotNone(model.candidate)


if __name__ == '__main__':
    unittest.main()
