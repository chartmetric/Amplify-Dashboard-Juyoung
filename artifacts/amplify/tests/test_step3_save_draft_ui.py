"""Step 3 Save-as-Draft + server-side autosave UI contract tests.

These tests guard against accidental regressions of the rendered template
contract that the new client-side autosave loop relies on:

  (1) Step 3 (the per-feature batch review) renders the same Save-as-Draft
      cluster (.btn-save-draft + .btn-save-new-draft + .draft-status-pill +
      [data-auto-save-pill]) as Step 4. Without this, the autosave pill
      can't display while the marketer is on Step 3 and the new "Save as
      Draft" button on Step 3 is missing.
  (2) The auto-save status pill is class-driven (not id-driven) and there
      is one per cluster, so _updateAutoSaveStatusPill() can target both
      via a single querySelectorAll lookup.
  (3) The Save-as-Draft button is bound to saveCombinedAsDraft() on Step 3
      too, NOT a Step-3-only variant -- the snapshot collector and save
      core are intentionally shared between steps.
  (4) The server-side autosave guard rule is enforced in the JS: the
      timer body must short-circuit when window._currentDraftId is falsy
      (otherwise the autosave loop would spam the My Content list with
      "Untitled" rows).

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

    def test_step3_bottom_bar_has_save_as_draft_cluster(self) -> None:
        # Locate the Step 3 bottom bar block by its id, then assert the
        # save cluster is in the same block.
        anchor = self.html.find('id="batch-bottom-bar"')
        self.assertGreater(anchor, 0, 'Step 3 batch-bottom-bar not found')
        # Cluster should appear after the bottom bar opens but before the
        # Preview Combined button so it's visible in the same row.
        preview_btn = self.html.find('id="btn-preview-combined"', anchor)
        self.assertGreater(preview_btn, anchor)
        cluster_idx = self.html.find('class="save-draft-cluster"', anchor, preview_btn)
        self.assertGreater(
            cluster_idx, 0,
            'save-draft-cluster missing inside Step 3 bottom bar (expected '
            'between #batch-bottom-bar and #btn-preview-combined)',
        )

    def test_step3_save_button_calls_shared_save_function(self) -> None:
        # The Step 3 Save as Draft button MUST call the same
        # saveCombinedAsDraft() the publish step calls; if a Step-3-only
        # variant ever appears, the shared snapshot path won't run.
        anchor = self.html.find('id="batch-bottom-bar"')
        end = self.html.find('id="btn-preview-combined"', anchor)
        snippet = self.html[anchor:end]
        self.assertIn('onclick="saveCombinedAsDraft()"', snippet)
        self.assertIn('onclick="saveCombinedAsNewDraft()"', snippet)

    def test_two_auto_save_pills_for_two_clusters(self) -> None:
        # One auto-save pill per save-draft cluster (Step 3 + Step 4) so
        # querySelectorAll('[data-auto-save-pill]') returns both and they
        # stay in sync.
        self.assertEqual(
            self.html.count('class="save-draft-cluster"'), 2,
            'Expected exactly two save-draft clusters (Step 3 + Step 4)',
        )
        # Count attribute usages on real elements (i.e. ` data-auto-save-pill `
        # with surrounding whitespace), not the two JS selector strings
        # like querySelectorAll('[data-auto-save-pill]').
        self.assertEqual(
            self.html.count(' data-auto-save-pill '), 2,
            'Expected exactly two auto-save status pills (one per cluster)',
        )

    def test_two_draft_status_pills_class_driven(self) -> None:
        # Both clusters expose the .draft-status-pill class so the
        # refactored _updateDraftStatusPill (querySelectorAll) keeps
        # both in sync. The legacy id stays only on the Step 4 pill for
        # backward compatibility with deep links / older bookmarks.
        self.assertGreaterEqual(self.html.count('class="draft-status-pill'), 2)
        self.assertEqual(self.html.count('id="draft-status-pill"'), 1)
        self.assertEqual(self.html.count('class="draft-status-pill-text"'), 2)

    def test_server_autosave_guarded_by_current_draft_id(self) -> None:
        # The autosave loop MUST short-circuit when there is no active
        # draft id; otherwise the loop would create a brand-new draft
        # row on every keystroke. Lock that in so a future refactor
        # doesn't accidentally drop the guard.
        sched = self.html.find('function _scheduleServerAutoSave')
        self.assertGreater(sched, 0, '_scheduleServerAutoSave function missing')
        body = self.html[sched:sched + 600]
        self.assertIn("if (!window._currentDraftId)", body)
        self.assertIn("_updateAutoSaveStatusPill('idle')", body)

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
        # every cluster's button (otherwise a marketer can double-submit
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
        # Use querySelectorAll (not querySelector) so BOTH cluster
        # buttons get disabled / re-labeled to "Saving...".
        self.assertIn(
            "document.querySelectorAll(forceNew ? '.btn-save-new-draft' : '.btn-save-draft')",
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
