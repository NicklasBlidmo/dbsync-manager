"""
Markup guards (field finding 2026-09-28, slice 10): a <form> inside a <p>
makes the HTML parser close the paragraph, so a wrapper such as
<p class="actions"> ends up empty and paints its rule above the buttons.
Pure: reads the template sources.
"""

import glob
import re
from pathlib import Path

from django.test import SimpleTestCase

TEMPLATES = Path(__file__).resolve().parent / "templates"
# A <p ...> that reaches a <form or a block element before its </p>.
P_WRAPPING_BLOCK = re.compile(r"<p\b[^>]*>(?:(?!</p>).)*?<(form|div|table|details|ul|ol|pre)\b", re.S)


class NoBlockInsideParagraphTests(SimpleTestCase):
    def test_no_paragraph_wraps_a_form_or_block(self):
        offenders = []
        for path in glob.glob(str(TEMPLATES / "**" / "*.html"), recursive=True):
            source = Path(path).read_text(encoding="utf-8")
            match = P_WRAPPING_BLOCK.search(source)
            if match:
                offenders.append(f"{Path(path).relative_to(TEMPLATES)}: <p> wraps <{match.group(1)}>")
        self.assertEqual(offenders, [])
