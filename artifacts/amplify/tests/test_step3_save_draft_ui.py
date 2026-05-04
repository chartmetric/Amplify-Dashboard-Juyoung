"""Step 3 Save-as-Draft + server-side autosave UI contract tests.

These tests guard against accidental regressions of the rendered template
contract that the client-side autosave loop relies on:

  (1) Step 3 (the per-feature batch review) renders the same Save-as-Draft
      cluster (.btn-save-new-draft + [data-auto-save-pill]) as Step 4.
      Without this, the autosave pill can't display while the marketer is
      on Step 3 and the save button on Step 3 is missing.
  (2) The auto-save status pill is class-driven (not id-driven) and there
      is one per cluster, so _updateAutoSaveStatusPill() can target both
      via a single querySelectorAll lookup.
  (3) The dashed "+ Save as new draft" button (btn-save-new-draft) is the
      sole save action on both steps -- the solid btn-save-draft has been
      removed. Draft-status pills have also been removed from the DOM.
  (4) Autosave is always on. When no draft id exists yet, _serverAutoSaveNow
      auto-creates a draft via saveCombinedAsNewDraft(). The guard that
      short-circuits without content (prepBatchResults empty) remains.

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
        # The sole save action on Step 3 is the dashed btn-save-new-draft
        # button, bound to saveCombinedAsNewDraft(). The solid btn-save-draft
        # has been removed; only the dashed button should appear here.
        anchor = self.html.find('id="batch-bottom-bar"')
        end = self.html.find('id="btn-preview-combined"', anchor)
        snippet = self.html[anchor:end]
        self.assertIn('onclick="saveCombinedAsNewDraft()"', snippet)
        self.assertNotIn('onclick="saveCombinedAsDraft()"', snippet,
                         'Solid btn-save-draft should be removed from Step 3')

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
        # Autosave is always on. When no draft id exists, _serverAutoSaveNow
        # auto-creates one via saveCombinedAsNewDraft() if there is content,
        # then returns so the regular update path doesn't run without an id.
        # The guard must live in _serverAutoSaveNow (not _scheduleServerAutoSave).
        now_fn = self.html.find('function _serverAutoSaveNow')
        self.assertGreater(now_fn, 0, '_serverAutoSaveNow function missing')
        body = self.html[now_fn:now_fn + 800]
        self.assertIn("if (!window._currentDraftId)", body)
        self.assertIn("saveCombinedAsNewDraft()", body)

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
        # btn-save-draft has been removed; only btn-save-new-draft exists now,
        # so the selector always targets that class regardless of forceNew.
        self.assertIn(
            "document.querySelectorAll('.btn-save-new-draft')",
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
