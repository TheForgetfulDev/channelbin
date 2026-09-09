"""Tier 0 - structural guards on static/js/filename-designer.js.

The designer is a browser component, so the Python suite cannot drive it. What it CAN pin
are the four constraints that make it correct, each of which is a rule the file would still
look plausible after breaking:

  1. _safe_name is NOT re-spelled in JavaScript. The one thing this screen exists to show is
     the name that lands in /dvr, and a second implementation of that rule is exactly the
     drift DESIGN.md 15.4 decided against - the preview is computed server-side for this
     reason and no other.
  2. The dropdowns are REGISTRY ENTRIES, not a fourth popover. DESIGN.md 15.3 makes "open a
     list, pick from it" one component; a sixth dropdown is an entry, never an
     implementation.
  3. The `Applied to this filename` caption lives INSIDE the rule-list renderer, so the
     empty state cannot carry a caption over an empty list (15.4, a defect caught in the
     mockup round).
  4. The keystroke path does not rewrite the template input. Retyping the whole designer on
     every character moves the caret out of the box, which is why refreshPreviewOnly exists
     as something separate from the full redraw.

Spec and reasoning: dev/changelog/441. Same shape as tests/test_check_modal_js.py.
"""
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESIGNER_JS = os.path.join(REPO, 'static', 'js', 'filename-designer.js')
DROPDOWN_JS = os.path.join(REPO, 'static', 'js', 'dropdown.js')
SETTINGS_JS = os.path.join(REPO, 'static', 'js', 'settings.js')
STYLE_CSS = os.path.join(REPO, 'static', 'css', 'style.css')
SETTINGS_HTML = os.path.join(REPO, 'templates', 'settings.html')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class DesignerParsesTests(unittest.TestCase):
    def test_the_file_is_syntactically_valid(self):
        """`node --check`. A shipped page script with a syntax error takes the whole page's
        JavaScript down, and nothing else in the suite would notice."""
        node = None
        for candidate in ('node', 'nodejs'):
            try:
                subprocess.run([candidate, '--version'], capture_output=True, check=True)
                node = candidate
                break
            except (OSError, subprocess.CalledProcessError):
                continue
        if node is None:
            self.skipTest('node is not installed')
        proc = subprocess.run([node, '--check', DESIGNER_JS], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class NoSecondSafeNameTests(unittest.TestCase):
    def test_the_component_does_not_reimplement_safe_name(self):
        """_safe_name's character class is `[^\\w\\-.]` on a UNICODE-aware \\w. A JS copy
        has to hand-write that as \\p{L}\\p{N}, and any copy at all can drift from the
        recorder's own. The preview comes from the server instead."""
        src = _read(DESIGNER_JS)
        self.assertNotIn('\\p{L}', src)
        self.assertNotRegex(src, r'function\s+safeName')
        self.assertNotRegex(src, r'\bsafeName\s*=')

    def test_the_displayed_filename_comes_from_the_api_disk_field(self):
        """The name on screen is the response's `disk`, never its `name` - `name` is what
        the recording is CALLED, and showing it is the defect this part fixed."""
        src = _read(DESIGNER_JS)
        self.assertRegex(src, r"escHtml\(p\.disk")

    def test_the_preview_request_carries_every_input_that_changes_the_filename(self):
        """One request, one computation: the filename, the substitution note, the unknown
        warning, the subject line and the example renderings all come out of it, so none of
        them can describe a different program than the others (DESIGN.md 15.7)."""
        src = _read(DESIGNER_JS)
        params = src[src.index('function fdPreviewParams'):src.index('function fdFetchPreview')]
        for key in ("'template'", "'remove'", "'replace'", "'src'", "'epg_id'", "'also'"):
            self.assertIn(key, params, f'{key} must travel with the preview request')


class OneDropdownComponentTests(unittest.TestCase):
    def test_the_designer_registers_its_dropdowns_rather_than_building_one(self):
        src = _read(DESIGNER_JS)
        self.assertIn("registerDropdown('fdex'", src)
        self.assertIn("registerDropdown('fdtag'", src)
        # The trigger markup comes from the component too, so the label, the caret and the
        # data attribute cannot be spelled a second way here.
        self.assertIn('dropdownTriggerHtml(', src)

    def test_the_designer_does_not_build_a_second_popover(self):
        """A second portal node is how 15.3's one-component rule gets quietly undone."""
        src = _read(DESIGNER_JS)
        self.assertNotIn('msel-pop', src)
        self.assertNotIn("createElement('div')\n", src.replace(
            "const foot = document.createElement('div');", ''))

    def test_the_two_tag_pickers_share_one_definition_keyed_by_mode(self):
        """`fdtag:remove` and `fdtag:replace` are one definition over dropdown.js's
        `id:arg` keying - two definitions could disagree about what a tick means."""
        src = _read(DESIGNER_JS)
        self.assertIn("dropdownTriggerHtml('fdtag:remove')", src)
        self.assertIn("dropdownTriggerHtml('fdtag:replace')", src)
        self.assertEqual(src.count("registerDropdown('fdtag'"), 1)

    def test_mutual_exclusion_lives_in_the_definition(self):
        """A name can only be in one list, and the save endpoint drops it from `replace` if
        it is also in `remove`. Letting the two controls disagree would render a state that
        cannot be saved."""
        src = _read(DESIGNER_JS)
        toggle = src[src.index('toggle: (mode, value, checked)'):]
        self.assertIn('other', toggle[:600])


class RuleListCaptionTests(unittest.TestCase):
    def test_the_caption_is_written_inside_the_list_renderer(self):
        """`rule-applied` and its caption must both be produced by ruleListHtml, AFTER its
        empty-state early return - so an empty list cannot carry a caption over nothing."""
        src = _read(DESIGNER_JS)
        start = src.index('function ruleListHtml')
        end = src.index('function tagByName')
        body = src[start:end]
        self.assertIn('rule-applied', body)
        self.assertIn('Applied to this filename', body)
        self.assertLess(body.index('rule-none'), body.index('rule-applied'),
                        'the empty state must return before the caption is written')
        # And nowhere else, or the caption has a second author. Matched on the rendered
        # markup rather than the bare words, so the comment above the renderer explaining
        # this rule does not count as a second copy of it.
        self.assertEqual(src.count('class="rule-applied"'), 1)
        self.assertEqual(src.count('>Applied to this filename<'), 1)


class LiveRegionTests(unittest.TestCase):
    def test_the_keystroke_refresh_does_not_rewrite_the_template_input(self):
        """refreshPreviewOnly runs on every debounced response. If it rebuilt the designer
        body it would destroy the node the caret is sitting in on every character."""
        src = _read(DESIGNER_JS)
        start = src.index('function refreshPreviewOnly')
        end = src.index('function redrawDesigner')
        body = src[start:end]
        self.assertNotIn('designerBodyHtml', body)
        self.assertNotIn("getElementById('fd-tpl')", body)

    def test_the_picker_refresh_does_not_rewrite_its_search_box(self):
        src = _read(DESIGNER_JS)
        start = src.index('function refreshPicker')
        end = src.index("/* ── Step rendering")
        body = src[start:end]
        self.assertNotIn('pickerBodyHtml', body)
        self.assertNotIn("#fd-pksearch'", body)

    def test_out_of_order_responses_cannot_overwrite_a_newer_one(self):
        """Debounced fetches can land out of order, and a stale response writing a filename
        for a template no longer in the box is the same class of defect as a timestamp
        anchor moving backwards."""
        src = _read(DESIGNER_JS)
        self.assertIn('const seq = ++fdSeq;', src)
        self.assertIn('if (seq !== fdSeq', src)
        self.assertIn('const seq = ++fdPickSeq;', src)
        self.assertIn('if (seq !== fdPickSeq)', src)


class PickerUsesTheSearchEngineTests(unittest.TestCase):
    def test_the_picker_calls_the_app_airing_search_and_not_a_new_endpoint(self):
        """CLAUDE.md makes app/channel_search.py the single home for "when is this on".
        A picker with its own query would be a second answer to that question."""
        src = _read(DESIGNER_JS)
        self.assertIn('/api/channels/search?', src)
        self.assertIn("grain: 'airings'", src)

    def test_the_picker_is_a_step_not_a_second_modal(self):
        """Two stacked overlays mean two Escape handlers arguing and a doubled scroll lock,
        which is why the picker swaps the open panel's body and foot instead."""
        src = _read(DESIGNER_JS)
        self.assertEqual(src.count('buildModal('), 1)
        step = src[src.index('function renderStep'):src.index('function openPickerStep')]
        self.assertIn(".modal-body", step)
        self.assertIn(".modal-foot", step)


class DesignerCssIsSharedTests(unittest.TestCase):
    def test_the_component_css_lives_in_style_css(self):
        """The component opens from more than one host, and CLAUDE.md forbids reusing a
        class styled inside one page's <style> block."""
        css = _read(STYLE_CSS)
        for cls in ('.fd-group', '.fd-sticky', '.fd-name', '.fd-safe', '.vchip',
                    '.tagcombo', '.rule-applied', '.pk-row', '.tagdot'):
            self.assertIn(cls, css, f'{cls} must be in style.css, not a page block')

    def test_every_custom_property_the_designer_uses_is_defined(self):
        """An undefined var(--x) fails silently and has shipped invisible UI twice."""
        css = _read(STYLE_CSS)
        defined = set(re.findall(r'(--[\w-]+)\s*:', css))
        block = css[css.index('/* ═══ The filename template designer'):css.index('/* ── Hybrid card-table row')]
        for used in set(re.findall(r'var\((--[\w-]+)\)', block)):
            self.assertIn(used, defined, f'{used} is used by the designer but never defined')

    def test_the_mobile_foot_rule_is_scoped_to_the_designer_panel(self):
        """15.5 item 3 makes the designer's foot a column on a phone. Applied to
        .modal-foot bare it would restack every other modal's foot in the app."""
        css = _read(STYLE_CSS)
        self.assertIn('.modal-panel.fd-modal .modal-foot', css)


class SettingsWiringTests(unittest.TestCase):
    def test_settings_loads_the_component_and_its_dropdown(self):
        html = _read(SETTINGS_HTML)
        self.assertIn('js/filename-designer.js', html)
        self.assertIn('js/dropdown.js', html)

    def test_no_link_to_the_deleted_editor_survives(self):
        html = _read(SETTINGS_HTML)
        self.assertNotIn('template_editor', html)
        self.assertNotIn('/settings/template', html)

    def test_both_entry_points_go_through_one_open_call(self):
        """The field row's button and the search result's button must open the same thing,
        so they share one handler rather than two that can drift."""
        js = _read(SETTINGS_JS)
        self.assertEqual(js.count('openFilenameDesigner('), 1)
        self.assertIn('data-open-fd', js)
        self.assertIn('data-sact="open-fd"', js)


if __name__ == '__main__':
    unittest.main()
