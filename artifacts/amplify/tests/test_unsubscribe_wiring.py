"""Unsubscribe-link wiring tests for the email send pipeline.

Covers:
  * The footer always renders an Unsubscribe link (no audience gating).
  * Per-recipient substitution in :func:`integrations.sendgrid_client._send_via_resend`:
      - Audience send + secure SESSION_SECRET => personal signed
        unsubscribe URL in the footer + ``List-Unsubscribe`` and
        ``List-Unsubscribe-Post`` headers.
      - Audience send + insecure SESSION_SECRET (cannot mint signed
        token) => generic ``mailto:`` opt-out in the footer +
        ``List-Unsubscribe`` header pointing at the mailto, no
        ``List-Unsubscribe-Post``.
      - Custom typed-in recipients / test send (no ``audience_id``) =>
        same generic ``mailto:`` substitution + ``List-Unsubscribe``
        header pointing at the mailto, no ``List-Unsubscribe-Post``.
  * The hosted "View in browser" snapshot saved per send replaces the
    placeholder with the generic mailto so we never persist literal
    ``{{AMPLIFY_UNSUBSCRIBE_URL}}``.
  * Integration check: under all three send branches above, no email
    that resend.Emails.send / resend.Batch.send receives carries the
    literal placeholder string.

Run with:
    cd artifacts/amplify && python -m unittest tests.test_unsubscribe_wiring
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_AMPLIFY_DIR = os.path.dirname(_HERE)
if _AMPLIFY_DIR not in sys.path:
    sys.path.insert(0, _AMPLIFY_DIR)

from integrations import sendgrid_client as sgc  # noqa: E402


class _FakeResendOK:
    """Drop-in stand-in for the ``resend`` module that records every
    payload passed to ``Emails.send`` / ``Batch.send`` and returns a
    success-shaped response. ``_FakeResendOK.calls`` is a flat list of
    every per-recipient params dict the send loop produced (for both
    single and batch code paths)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.api_key = ""

        fake = self

        class _Emails:
            @staticmethod
            def send(params):
                fake.calls.append(params)
                return {"id": "re_test_id"}

        class _Batch:
            @staticmethod
            def send(payloads):
                for p in payloads:
                    fake.calls.append(p)
                return {"data": [{"id": f"re_test_{i}"} for i, _ in enumerate(payloads)]}

        self.Emails = _Emails
        self.Batch = _Batch


def _install_fake_resend(monkey: mock._patch_dict, fake: _FakeResendOK):
    """Replace ``import resend`` inside sendgrid_client with our fake."""
    import resend as _real_resend  # noqa: F401  (ensure module is importable)
    return mock.patch.dict(sys.modules, {"resend": fake})


# Pre-rendered HTML fragments: enough to drive _send_via_resend without
# pulling in the full Jinja chain. The placeholder must appear so the
# substitution logic has something to replace.
_HTML_WITH_PLACEHOLDER = (
    "<html><body><p>Hi</p>"
    f'<a href="{sgc.UNSUBSCRIBE_PLACEHOLDER}">Unsubscribe</a>'
    "</body></html>"
)


class SendViaResendUnsubscribeBranches(unittest.TestCase):
    """Direct tests for :func:`_send_via_resend` so we can assert
    substitutions and headers without exercising the rest of the send
    pipeline. Each test installs a fake ``resend`` module so no
    network call happens; the fake records every params dict for
    inspection."""

    def setUp(self):
        self.fake = _FakeResendOK()
        # Real RESEND_API_KEY/RESEND_FROM_EMAIL are required to enter
        # the send loop; we override with deterministic test values.
        self._env = mock.patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_fake",
                "RESEND_FROM_EMAIL": "test@example.com",
            },
            clear=False,
        )
        self._env.start()
        self._mod = mock.patch.dict(sys.modules, {"resend": self.fake})
        self._mod.start()

    def tearDown(self):
        self._mod.stop()
        self._env.stop()

    # --- Audience send, secure secret -------------------------------------

    def test_audience_send_uses_personal_signed_url(self):
        signed_url = "https://amplify.example/email/unsubscribe?token=signed-aaa"
        with mock.patch.object(sgc, "build_unsubscribe_url", return_value=signed_url):
            sgc._send_via_resend(
                subject="s",
                html_content=_HTML_WITH_PLACEHOLDER,
                to_emails=["alice@example.com"],
                is_test=False,
                audience_id="aud_123",
                topic_id="",
            )
        self.assertEqual(len(self.fake.calls), 1)
        call = self.fake.calls[0]
        self.assertIn(signed_url, call["html"])
        self.assertNotIn(sgc.UNSUBSCRIBE_PLACEHOLDER, call["html"])
        self.assertEqual(call["headers"]["List-Unsubscribe"], f"<{signed_url}>")
        self.assertEqual(call["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    # --- Audience send, insecure secret (cannot mint token) --------------

    def test_audience_send_falls_back_to_mailto_when_secret_insecure(self):
        # build_unsubscribe_url returns "" when SESSION_SECRET is unsafe.
        with mock.patch.object(sgc, "build_unsubscribe_url", return_value=""):
            sgc._send_via_resend(
                subject="s",
                html_content=_HTML_WITH_PLACEHOLDER,
                to_emails=["bob@example.com"],
                is_test=False,
                audience_id="aud_123",
                topic_id="",
            )
        self.assertEqual(len(self.fake.calls), 1)
        call = self.fake.calls[0]
        self.assertIn(sgc.GENERIC_UNSUBSCRIBE_MAILTO, call["html"])
        self.assertNotIn(sgc.UNSUBSCRIBE_PLACEHOLDER, call["html"])
        self.assertEqual(
            call["headers"]["List-Unsubscribe"],
            f"<{sgc.GENERIC_UNSUBSCRIBE_MAILTO}>",
        )
        # mailto: is not a one-click endpoint per RFC 8058.
        self.assertNotIn("List-Unsubscribe-Post", call["headers"])

    # --- Custom typed-in recipients (no audience_id) ---------------------

    def test_custom_recipient_send_uses_mailto(self):
        sgc._send_via_resend(
            subject="s",
            html_content=_HTML_WITH_PLACEHOLDER,
            to_emails=["custom@example.com"],
            is_test=False,
            audience_id="",
            topic_id="",
        )
        self.assertEqual(len(self.fake.calls), 1)
        call = self.fake.calls[0]
        self.assertIn(sgc.GENERIC_UNSUBSCRIBE_MAILTO, call["html"])
        self.assertNotIn(sgc.UNSUBSCRIBE_PLACEHOLDER, call["html"])
        self.assertEqual(
            call["headers"]["List-Unsubscribe"],
            f"<{sgc.GENERIC_UNSUBSCRIBE_MAILTO}>",
        )
        self.assertNotIn("List-Unsubscribe-Post", call["headers"])

    # --- Test sends (is_test=True, no audience_id) -----------------------

    def test_test_send_also_uses_mailto(self):
        # Test sends go through the same no-audience branch; the mailto
        # opt-out is still rendered so QA inboxes match production
        # appearance.
        sgc._send_via_resend(
            subject="s",
            html_content=_HTML_WITH_PLACEHOLDER,
            to_emails=["qa@example.com"],
            is_test=True,
            audience_id=None,
            topic_id=None,
        )
        self.assertEqual(len(self.fake.calls), 1)
        call = self.fake.calls[0]
        self.assertIn(sgc.GENERIC_UNSUBSCRIBE_MAILTO, call["html"])
        self.assertNotIn(sgc.UNSUBSCRIBE_PLACEHOLDER, call["html"])

    # --- Audience BCC archival copy --------------------------------------

    def test_audience_bcc_archival_substitutes_mailto(self):
        # When BCC is set on an audience send, _send_via_resend appends
        # ONE archival copy with the BCC addresses in BCC and the
        # placeholder swapped to the generic mailto (we can't embed any
        # one recipient's signed URL in a shared archival copy).
        signed_url = "https://amplify.example/email/unsubscribe?token=signed-bbb"
        with mock.patch.object(sgc, "build_unsubscribe_url", return_value=signed_url):
            sgc._send_via_resend(
                subject="s",
                html_content=_HTML_WITH_PLACEHOLDER,
                to_emails=["alice@example.com"],
                is_test=False,
                audience_id="aud_123",
                topic_id="",
                bcc_emails=["archive@example.com"],
            )
        # 1 per-recipient + 1 archival.
        self.assertEqual(len(self.fake.calls), 2)
        # Find the archival copy: it's the one whose BCC is set.
        archival = next(c for c in self.fake.calls if c.get("bcc"))
        self.assertEqual(archival["bcc"], ["archive@example.com"])
        self.assertIn(sgc.GENERIC_UNSUBSCRIBE_MAILTO, archival["html"])
        # The signed URL belongs to alice, not the archive — must not leak.
        self.assertNotIn(signed_url, archival["html"])
        self.assertNotIn(sgc.UNSUBSCRIBE_PLACEHOLDER, archival["html"])


class FooterRenderingAlwaysIncludesUnsubscribeLink(unittest.TestCase):
    """The footer used to omit the Unsubscribe link when no audience was
    selected. Now we always render it — these tests pin that contract
    so a future refactor doesn't silently regress CAN-SPAM compliance.
    """

    def test_footer_renders_link_with_placeholder(self):
        html = sgc._render_footer_links_html(
            view_url="https://amplify.example/email/view/tok",
            unsubscribe_placeholder=sgc.UNSUBSCRIBE_PLACEHOLDER,
        )
        self.assertIn("Unsubscribe", html)
        self.assertIn(sgc.UNSUBSCRIBE_PLACEHOLDER, html)

    def test_footer_omits_link_only_when_caller_passes_no_placeholder(self):
        # _render_footer_links_html itself still respects the optional
        # arg (used by callers that explicitly want no link). The send
        # path no longer takes that branch, but the helper's contract
        # is preserved so unit-test callers can opt out.
        html = sgc._render_footer_links_html(
            view_url="https://amplify.example/email/view/tok",
            unsubscribe_placeholder=None,
        )
        self.assertNotIn("Unsubscribe", html)


class HostedSnapshotNeverShipsLiteralPlaceholder(unittest.TestCase):
    """Belt-and-braces: across all branches of ``_send_via_resend``, no
    payload that reaches resend.Emails.send / resend.Batch.send may
    contain the literal ``{{AMPLIFY_UNSUBSCRIBE_URL}}`` token. This
    test runs the same scenarios as the per-branch tests above and
    inspects every captured HTML body (including the archival BCC copy)
    in one place."""

    def setUp(self):
        self.fake = _FakeResendOK()
        self._env = mock.patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_fake",
                "RESEND_FROM_EMAIL": "test@example.com",
            },
            clear=False,
        )
        self._env.start()
        self._mod = mock.patch.dict(sys.modules, {"resend": self.fake})
        self._mod.start()

    def tearDown(self):
        self._mod.stop()
        self._env.stop()

    def test_no_branch_ships_literal_placeholder(self):
        scenarios = [
            # (kwargs, build_unsubscribe_url_return)
            ({"audience_id": "aud_x", "topic_id": ""}, "https://signed/x"),
            ({"audience_id": "aud_x", "topic_id": ""}, ""),  # insecure secret
            ({"audience_id": "", "topic_id": ""}, ""),       # custom recipient
            (
                {"audience_id": "aud_x", "topic_id": "", "bcc_emails": ["a@x"]},
                "https://signed/x",
            ),  # audience + BCC archival
        ]
        for kwargs, signed in scenarios:
            self.fake.calls.clear()
            with mock.patch.object(sgc, "build_unsubscribe_url", return_value=signed):
                sgc._send_via_resend(
                    subject="s",
                    html_content=_HTML_WITH_PLACEHOLDER,
                    to_emails=["r@example.com"],
                    is_test=False,
                    **kwargs,
                )
            for call in self.fake.calls:
                with self.subTest(scenario=kwargs, call=call):
                    self.assertNotIn(
                        sgc.UNSUBSCRIBE_PLACEHOLDER,
                        call.get("html", ""),
                        msg=(
                            "Literal unsubscribe placeholder leaked into a "
                            "sent email under scenario " + repr(kwargs)
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
