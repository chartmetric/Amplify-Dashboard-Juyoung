"""Step 3 Save-as-Draft + server-side autosave UI contract tests.

These tests guard against accidental regressions of the rendered template
contract that the client-side autosave loop relies on:

  (1) Step 3 (the per-feature batch review) and Step 4 (combined preview)
      each render a Save button (.btn-save-progress) inside their
      .prep-step-header, alongside the auto-save status pill.
  (2) The auto-save status pill is class-driven (not id-driven) and there
      is one per step header, so _updateAutoSaveStatusPill() can target both
      via a single querySelectorAll lookup.
  (3) The solid "Save" button (btn-save-progress) calls saveCombinedAsDraft()
      which updates an existing draft or creates one when none exists.
  (4) Autosave now works from the very first edit: when no draft id exists,
      _serverAutoSaveNow auto-creates a draft via saveCombinedAsDraft().
      The guard that short-circuits without content (prepBatchResults empty)
      remains.

These are pure string assertions over the rendered template -- they don't
spin up a browser. The structure being asserted is small and stable, so
locking it in is cheap and high-signal.
"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_AMPLIFY_DIR = os.path.dirname(_HERE)
_TEMPLATE = os.path.join(_AMPLIFY_DIR, 'templates', 'dashboard.html')


def _read_template() -> str:
    with open(_TEMPLATE, 'r', encoding='utf-8') as f:
        return f.read()


class Step3SaveDraftUiTests(unittest.TestCase):

    def setUp(self) -> None:
        self.html = _read_template()

    def test_step3_header_has_save_button(self) -> None:
        # The Save button must appear inside the Step 3 prep-step-header,
        # between the step3-title and the start of batch-progress.
        anchor = self.html.find('id="step3-title"')
        self.assertGreater(anchor, 0, 'Step 3 title element not found')
        end = self.html.find('id="batch-progress"', anchor)
        self.assertGreater(end, anchor, 'batch-progress marker not found after step3-title')
        snippet = self.html[anchor:end]
        self.assertIn('btn-save-progress', snippet,
                      'btn-save-progress missing in Step 3 header (expected between '
                      'step3-title and batch-progress)')

    def test_step3_save_button_calls_save_draft_function(self) -> None:
        # The Save button on Step 3 calls saveCombinedAsDraft() which updates
        # an existing draft or auto-creates one when none exists.
        anchor = self.html.find('id="step3-title"')
        end = self.html.find('id="batch-progress"', anchor)
        snippet = self.html[anchor:end]
        self.assertIn('onclick="saveCombinedAsDraft()"', snippet,
                      'Step 3 Save button should call saveCombinedAsDraft()')

    def test_two_save_buttons_and_two_auto_save_pills(self) -> None:
        # One btn-save-progress per step (Step 3 + Step 4) so the user can
        # save from either step without navigating.
        self.assertEqual(
            self.html.count('btn-save-progress" onclick="saveCombinedAsDraft()"'), 2,
            'Expected exactly two Save buttons (one in Step 3 header, one in Step 4 header)',
        )
        # Count attribute usages on real elements (i.e. ` data-auto-save-pill `
        # with surrounding whitespace), not the JS selector strings.
        self.assertEqual(
            self.html.count(' data-auto-save-pill '), 2,
            'Expected exactly two auto-save status pills (one per step header)',
        )

    def test_two_draft_status_pills_class_driven(self) -> None:
        # Draft-status pills ("New draft (unsaved)" etc.) have been removed
        # from both clusters. Neither the class nor the id should appear
        # on any DOM element inside a save-draft-cluster.
        # We allow the class to appear in JS function bodies (which reference
        # .draft-status-pill as a selector string) but NOT as an HTML class
        # attribute on an element, so check that no element carries it.
        self.assertNotIn('class="draft-status-pill', self.html,
                         'draft-status-pill elements should be removed from the DOM')
        self.assertNotIn('id="draft-status-pill"', self.html,
                         'draft-status-pill id element should be removed from the DOM')

    def test_server_autosave_guarded_by_current_draft_id(self) -> None:
        # The autosave loop must check _currentDraftId in both
        # _scheduleServerAutoSave and _serverAutoSaveNow.
        sched = self.html.find('function _scheduleServerAutoSave')
        self.assertGreater(sched, 0, '_scheduleServerAutoSave function missing')
        sched_body = self.html[sched:sched + 400]
        self.assertIn("if (!window._currentDraftId)", sched_body)
        # _serverAutoSaveNow also checks independently and auto-creates a
        # draft when there is content but no existing draft id.
        now_fn = self.html.find('function _serverAutoSaveNow')
        self.assertGreater(now_fn, 0, '_serverAutoSaveNow function missing')
        now_body = self.html[now_fn:now_fn + 400]
        self.assertIn("if (!window._currentDraftId)", now_body)

    def test_server_autosave_does_not_overwrite_with_empty_state(self) -> None:
        # Mirror of the localStorage path: never POST a snapshot when
        # prepBatchResults is empty (would silently wipe an existing
        # draft after navigation that resets in-memory state).
        now = self.html.find('function _serverAutoSaveNow')
        self.assertGreater(now, 0)
        body = self.html[now:now + 3000]
        self.assertIn('!prepBatchResults || !prepBatchResults.length', body)
        # Same body should also defer when a manual save is in flight,
        # so a debounced autosave can't land a stale snapshot on top
        # of a manual save's freshly-persisted state.
        self.assertIn('window._manualSaveInFlight', body)

    def test_manual_save_cancels_pending_autosave_and_disables_all_buttons(self) -> None:
        # Race avoidance: a manual save MUST clear any pending
        # debounced autosave timer (otherwise an older snapshot could
        # land on top of the fresh manual save) AND it must disable
        # every save button (otherwise a marketer can double-submit
        # by jumping between Step 3 and Step 4 mid-flight).
        core = self.html.find('function _saveCombinedDraftCore')
        self.assertGreater(core, 0)
        # _saveCombinedDraftCore is long; grab a generous window.
        body = self.html[core:core + 6000]
        # Cancel the pending autosave timer.
        self.assertIn('clearTimeout(_serverAutoSaveTimer); _serverAutoSaveTimer = null;', body)
        # Bump a counter the autosave path checks, so an in-flight
        # autosave doesn't paint a stale pill state.
        self.assertIn('window._manualSaveInFlight =', body)
        # Use querySelectorAll (not querySelector) so BOTH step buttons
        # get disabled / re-labeled to "Saving...".
        self.assertIn(
            "document.querySelectorAll('.btn-save-new-draft, .btn-save-progress')",
            body,
        )
        self.assertIn("btns[bi].disabled = true", body)

    def test_schedule_batch_autosave_also_schedules_server_autosave(self) -> None:
        # Single integration point: the existing localStorage debouncer
        # is the only callsite that needs to know about the new server
        # path. Every existing mutation site already calls
        # _scheduleBatchAutoSave, so chaining here means we don't have
        # to touch each of them.
        sched = self.html.find('function _scheduleBatchAutoSave()')
        self.assertGreater(sched, 0)
        body = self.html[sched:sched + 600]
        self.assertIn('_scheduleServerAutoSave()', body)


if __name__ == '__main__':
    unittest.main()
