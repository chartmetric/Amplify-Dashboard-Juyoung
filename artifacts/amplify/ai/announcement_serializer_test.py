"""Unit tests for the HTML <-> Slate.js serializer.

Run with: ``python -m unittest artifacts.amplify.ai.announcement_serializer_test``
or from inside artifacts/amplify: ``python -m unittest ai.announcement_serializer_test``.
"""
from __future__ import annotations

import unittest

from ai.announcement_serializer import (
    deepcopy_blocks,
    html_to_slate,
    slate_to_html,
    walk_text_leaves,
)


class HtmlToSlateTests(unittest.TestCase):

    def test_empty_input_returns_empty_paragraph(self):
        self.assertEqual(
            html_to_slate(""),
            [{"type": "paragraph", "children": [{"text": ""}]}],
        )
        self.assertEqual(
            html_to_slate("   \n"),
            [{"type": "paragraph", "children": [{"text": ""}]}],
        )
        self.assertEqual(
            html_to_slate(None),
            [{"type": "paragraph", "children": [{"text": ""}]}],
        )

    def test_paragraph(self):
        blocks = html_to_slate("<p>Hello world</p>")
        self.assertEqual(blocks, [{"type": "paragraph",
                                   "children": [{"text": "Hello world"}]}])

    def test_paragraph_with_inline_marks(self):
        blocks = html_to_slate("<p>Hello <strong>bold</strong> and <em>ital</em></p>")
        self.assertEqual(blocks, [{
            "type": "paragraph",
            "children": [
                {"text": "Hello "},
                {"text": "bold", "bold": True},
                {"text": " and "},
                {"text": "ital", "italic": True},
            ],
        }])

    def test_nested_marks(self):
        blocks = html_to_slate("<p><strong><em>bi</em></strong></p>")
        self.assertEqual(blocks[0]["children"][0],
                         {"text": "bi", "bold": True, "italic": True})

    def test_headings(self):
        blocks = html_to_slate("<h1>One</h1><h2>Two</h2><h3>Three</h3><h6>Six</h6>")
        self.assertEqual([b["type"] for b in blocks],
                         ["heading-one", "heading-two", "heading-three", "heading-three"])
        self.assertEqual(blocks[0]["children"][0]["text"], "One")

    def test_bulleted_list(self):
        blocks = html_to_slate("<ul><li>A</li><li>B</li></ul>")
        self.assertEqual(blocks[0]["type"], "bulleted-list")
        self.assertEqual(len(blocks[0]["children"]), 2)
        self.assertEqual(blocks[0]["children"][0]["type"], "list-item")
        self.assertEqual(blocks[0]["children"][0]["children"][0]["text"], "A")

    def test_numbered_list(self):
        blocks = html_to_slate("<ol><li>One</li></ol>")
        self.assertEqual(blocks[0]["type"], "numbered-list")
        self.assertEqual(blocks[0]["children"][0]["children"][0]["text"], "One")

    def test_image(self):
        blocks = html_to_slate('<p>before</p><img src="https://x/y.png" alt="Cap"><p>after</p>')
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["paragraph", "image", "paragraph"])
        self.assertEqual(blocks[1]["url"], "https://x/y.png")
        self.assertEqual(blocks[1]["alt"], "Cap")
        self.assertEqual(blocks[1]["children"], [{"text": ""}])

    def test_video(self):
        blocks = html_to_slate('<video src="https://x/y.mp4"></video>')
        self.assertEqual(blocks[0]["type"], "video")
        self.assertEqual(blocks[0]["url"], "https://x/y.mp4")

    def test_link(self):
        blocks = html_to_slate('<p>Visit <a href="https://chartmetric.com">us</a> now</p>')
        children = blocks[0]["children"]
        self.assertEqual(children[0], {"text": "Visit "})
        self.assertEqual(children[1]["type"], "link")
        self.assertEqual(children[1]["url"], "https://chartmetric.com")
        self.assertEqual(children[1]["children"][0]["text"], "us")
        self.assertEqual(children[2], {"text": " now"})

    def test_divider(self):
        blocks = html_to_slate("<p>a</p><hr><p>b</p>")
        self.assertEqual([b["type"] for b in blocks],
                         ["paragraph", "divider", "paragraph"])
        self.assertEqual(blocks[1]["children"], [{"text": ""}])

    def test_br_becomes_newline(self):
        blocks = html_to_slate("<p>line1<br>line2</p>")
        # newline preserved as text
        joined = "".join(c.get("text", "") for c in blocks[0]["children"])
        self.assertIn("\n", joined)

    def test_stray_text_wrapped_in_paragraph(self):
        blocks = html_to_slate("plain text")
        self.assertEqual(blocks[0]["type"], "paragraph")
        self.assertEqual(blocks[0]["children"][0]["text"], "plain text")


class SlateToHtmlTests(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(slate_to_html([]), "")
        self.assertEqual(slate_to_html(None), "")

    def test_paragraph(self):
        self.assertEqual(
            slate_to_html([{"type": "paragraph",
                            "children": [{"text": "Hello"}]}]),
            "<p>Hello</p>",
        )

    def test_marks(self):
        html = slate_to_html([{
            "type": "paragraph",
            "children": [
                {"text": "a "},
                {"text": "b", "bold": True, "italic": True},
            ],
        }])
        # Bold is outermost
        self.assertEqual(html, "<p>a <strong><em>b</em></strong></p>")

    def test_heading_and_lists(self):
        html = slate_to_html([
            {"type": "heading-one", "children": [{"text": "H1"}]},
            {"type": "bulleted-list", "children": [
                {"type": "list-item", "children": [{"text": "x"}]},
                {"type": "list-item", "children": [{"text": "y"}]},
            ]},
        ])
        self.assertIn("<h1>H1</h1>", html)
        self.assertIn("<ul><li>x</li><li>y</li></ul>", html)

    def test_image_and_video(self):
        html = slate_to_html([
            {"type": "image", "url": "https://x/y.png", "alt": "alt",
             "children": [{"text": ""}]},
            {"type": "video", "url": "https://x/y.mp4",
             "children": [{"text": ""}]},
            {"type": "divider", "children": [{"text": ""}]},
        ])
        self.assertIn('<img src="https://x/y.png" alt="alt">', html)
        self.assertIn('<video src="https://x/y.mp4" controls>', html)
        self.assertIn("<hr>", html)

    def test_link(self):
        html = slate_to_html([{
            "type": "paragraph",
            "children": [
                {"text": ""},
                {"type": "link", "url": "https://x",
                 "children": [{"text": "click"}]},
                {"text": ""},
            ],
        }])
        self.assertIn('<a href="https://x"', html)
        self.assertIn(">click</a>", html)

    def test_html_escaped(self):
        html = slate_to_html([{
            "type": "paragraph",
            "children": [{"text": "<script>"}],
        }])
        self.assertEqual(html, "<p>&lt;script&gt;</p>")


class RoundTripTests(unittest.TestCase):

    def test_simple_round_trip(self):
        src = "<p>Hello <strong>world</strong></p>"
        blocks = html_to_slate(src)
        out = slate_to_html(blocks)
        self.assertEqual(out, "<p>Hello <strong>world</strong></p>")

    def test_complex_round_trip(self):
        src = (
            "<h2>Title</h2>"
            "<p>Body with <em>emphasis</em>.</p>"
            "<ul><li>a</li><li>b</li></ul>"
            '<img src="https://x/y.png" alt="cap">'
            "<hr>"
        )
        blocks = html_to_slate(src)
        # Round trip should preserve same DOM (text equality is enough here).
        out = slate_to_html(blocks)
        self.assertEqual(out, src)


class WalkTextLeavesTests(unittest.TestCase):

    def test_walk_finds_all_leaves(self):
        blocks = html_to_slate(
            '<p>Hello <strong>world</strong></p>'
            '<ul><li>one</li><li>two <a href="#">link</a></li></ul>'
        )
        leaves = walk_text_leaves(blocks)
        texts = [l["text"] for l in leaves]
        self.assertIn("Hello ", texts)
        self.assertIn("world", texts)
        self.assertIn("one", texts)
        self.assertIn("two ", texts)
        self.assertIn("link", texts)

    def test_mutation_in_place(self):
        blocks = html_to_slate("<p>old</p>")
        leaves = walk_text_leaves(blocks)
        leaves[0]["text"] = "new"
        self.assertEqual(slate_to_html(blocks), "<p>new</p>")


class DeepcopyTests(unittest.TestCase):

    def test_deepcopy_is_isolated(self):
        blocks = html_to_slate("<p>x</p>")
        copy = deepcopy_blocks(blocks)
        copy[0]["children"][0]["text"] = "y"
        self.assertEqual(blocks[0]["children"][0]["text"], "x")


if __name__ == "__main__":
    unittest.main()
