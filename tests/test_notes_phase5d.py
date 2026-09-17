"""
Phase 5D Tests — Notes + Safe Markdown + Memory Extraction
Comprehensive tests covering:
  - Raw Markdown preservation & safe rendering
  - Neutralization of all XSS vectors (script, onerror, onclick, javascript:, iframe, object, svg, style)
  - Legitimate Markdown rendering (headings, lists, quotes, code, bold, italic, links)
  - Note pinning & unpinning (API, persistence, validation, duplicate-click idempotency)
  - Note update routing through existing MemoryPipeline with raw content & provenance
  - Invariant that rendered HTML and assistant content are never fed into memory extraction
  - Quick capture note behavior
  - Vector & SQLite synchronization
  - Security (auth + CSRF)
"""

import os
import sys
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.models import NoteModel, MemoryModel, MemoryDecisionLogModel
from app.vector_store import VectorStore
import app.vector_store as vector_store_module
from app.markdown_utils import render_safe_markdown, is_safe_url
from app.memory.pipeline import MemoryPipeline


class Phase5DTestBase(unittest.TestCase):
    """Shared fixture for Phase 5D tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / 'test_5d.db'
        self.test_vec_path = Path(self.temp_dir.name) / 'test_5d_vec.json'
        self.test_auth_path = Path(self.temp_dir.name) / 'test_5d_auth.json'

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        self.vs = VectorStore(storage_path=self.test_vec_path)
        vector_store_module.global_vector_store = self.vs

        import app.routes.api_notes as api_notes_module
        import app.routes.api_tasks as api_tasks_module
        self.p_route_notes_vs = patch.object(api_notes_module, "global_vector_store", self.vs)
        self.p_route_tasks_vs = patch.object(api_tasks_module, "global_vector_store", self.vs)
        self.p_route_notes_vs.start()
        self.p_route_tasks_vs.start()

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

        # Auth setup
        res = self.client.post('/api/auth/setup', json={
            'password': 'strongpassword123',
            'confirm_password': 'strongpassword123'
        })
        self.csrf_token = res.get_json()['csrf_token']

    def tearDown(self):
        self.p_route_notes_vs.stop()
        self.p_route_tasks_vs.stop()
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'X-CSRF-Token': self.csrf_token,
        }

    def _create_note(self, title='Test Note', content='Test note content', **kwargs):
        payload = {'title': title, 'content': content}
        payload.update(kwargs)
        res = self.client.post('/api/notes', headers=self._headers(), json=payload)
        return res.get_json()


class TestSafeMarkdownRendering(Phase5DTestBase):
    """Tests 4-11: Markdown parsing and strict XSS neutralization."""

    def test_01_raw_markdown_is_preserved_in_storage(self):
        """Raw Markdown syntax must remain stored as-is in SQLite."""
        raw_md = "# Title\n**bold** and *italic*\n- item 1\n- item 2"
        data = self._create_note(title="Raw Note", content=raw_md)
        self.assertTrue(data['success'])
        note_id = data['note']['id']

        # Verify directly from database
        stored = NoteModel.get(note_id)
        self.assertEqual(stored['content'], raw_md)

    def test_02_safe_markdown_renders_headings_lists_quotes_code(self):
        """Valid Markdown elements are converted to safe HTML."""
        md = "# Heading 1\n## Heading 2\n> Blockquote\n- Item A\n- Item B\n1. First\n2. Second\n`code`\n**bold** and *italic*"
        rendered = render_safe_markdown(md)
        self.assertIn("<h1>Heading 1</h1>", rendered)
        self.assertIn("<h2>Heading 2</h2>", rendered)
        self.assertIn("<blockquote>", rendered)
        self.assertIn("<ul>", rendered)
        self.assertIn("<li>Item A</li>", rendered)
        self.assertIn("<ol>", rendered)
        self.assertIn("<li>First</li>", rendered)
        self.assertIn("<code>code</code>", rendered)
        self.assertIn("<strong>bold</strong>", rendered)
        self.assertIn("<em>italic</em>", rendered)

    def test_03_script_tags_are_neutralized(self):
        """<script>alert(1)</script> must be escaped and rendered inert."""
        payload = "<script>alert(1)</script>"
        rendered = render_safe_markdown(payload)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)

    def test_04_onerror_onload_onclick_payloads_neutralized(self):
        """Event handler attributes in raw HTML are escaped and inert."""
        img_payload = '<img src=x onerror="alert(1)">'
        div_payload = '<div onclick="alert(1)">click me</div>'
        rendered_img = render_safe_markdown(img_payload)
        rendered_div = render_safe_markdown(div_payload)

        self.assertNotIn("<img", rendered_img)
        self.assertIn("&lt;img", rendered_img)
        self.assertNotIn("<div", rendered_div)
        self.assertIn("&lt;div", rendered_div)

    def test_05_javascript_links_neutralized(self):
        """javascript: URLs in markdown links must never become clickable links."""
        js_payload = "[malicious link](javascript:alert(1))"
        rendered = render_safe_markdown(js_payload)
        self.assertNotIn('href="javascript:', rendered.lower())
        self.assertNotIn("<a ", rendered)
        self.assertIn("malicious link", rendered)

        # Case-insensitive or obfuscated javascript check
        obf_payload = "[click](JAVASCRIPT:alert('xss'))"
        rendered_obf = render_safe_markdown(obf_payload)
        self.assertNotIn('href="javascript:', rendered_obf.lower())
        self.assertNotIn("<a ", rendered_obf)

    def test_06_iframe_object_embed_payloads_neutralized(self):
        """Dangerous embedding elements must be strictly neutralized."""
        iframe_payload = '<iframe src="javascript:alert(1)"></iframe>'
        object_payload = '<object data="javascript:alert(1)"></object>'
        embed_payload = '<embed src="javascript:alert(1)">'

        rendered_iframe = render_safe_markdown(iframe_payload)
        rendered_object = render_safe_markdown(object_payload)
        rendered_embed = render_safe_markdown(embed_payload)

        self.assertNotIn("<iframe", rendered_iframe)
        self.assertIn("&lt;iframe", rendered_iframe)
        self.assertNotIn("<object", rendered_object)
        self.assertIn("&lt;object", rendered_object)
        self.assertNotIn("<embed", rendered_embed)
        self.assertIn("&lt;embed", rendered_embed)

    def test_07_svg_and_event_handlers_neutralized(self):
        """SVG elements with onload or other event handlers must be neutralized."""
        svg_payload = '<svg onload="alert(1)"><circle r="10"/></svg>'
        rendered = render_safe_markdown(svg_payload)
        self.assertNotIn("<svg", rendered)
        self.assertIn("&lt;svg", rendered)

    def test_08_legitimate_links_render_correctly(self):
        """Safe links (http, https, mailto, relative) must render with safe attributes."""
        link_md = "[Cognito](https://github.com/example/cognito) and [Contact](mailto:test@example.com)"
        rendered = render_safe_markdown(link_md)
        self.assertIn('<a href="https://github.com/example/cognito" target="_blank" rel="noopener noreferrer">Cognito</a>', rendered)
        self.assertIn('<a href="mailto:test@example.com" target="_blank" rel="noopener noreferrer">Contact</a>', rendered)

    def test_09_code_blocks_with_html_are_escaped_and_neutralized(self):
        """Code blocks containing HTML tags must have tags escaped, not rendered."""
        code_md = "```html\n<script>alert('hello')</script>\n```"
        rendered = render_safe_markdown(code_md)
        self.assertIn("<pre><code>", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_10_arbitrary_css_style_injection_neutralized(self):
        """<style> tags must be escaped to prevent CSS injection."""
        style_payload = "<style>body { display:none; }</style>"
        rendered = render_safe_markdown(style_payload)
        self.assertNotIn("<style>", rendered)
        self.assertIn("&lt;style&gt;", rendered)


class TestNotePinning(Phase5DTestBase):
    """Tests 12-16: Pin / unpin functionality and persistence."""

    def test_11_pin_note_via_dedicated_endpoint(self):
        """POST /api/notes/<id>/pin pins an unpinned note."""
        note_data = self._create_note(title="Pin Target", content="Some content")
        note_id = note_data['note']['id']
        self.assertEqual(note_data['note']['pinned'], 0)

        res = self.client.post(f'/api/notes/{note_id}/pin', headers=self._headers())
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['note']['pinned'], 1)

    def test_12_unpin_note_via_dedicated_endpoint(self):
        """POST /api/notes/<id>/pin unpins a pinned note."""
        note_data = self._create_note(title="Unpin Target", content="Some content", pinned=True)
        note_id = note_data['note']['id']
        self.assertEqual(note_data['note']['pinned'], 1)

        res = self.client.post(f'/api/notes/{note_id}/pin', headers=self._headers())
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['note']['pinned'], 0)

    def test_13_pin_note_via_put_endpoint(self):
        """PUT /api/notes/<id> can update pinned state."""
        note_data = self._create_note(title="PUT Pin", content="Content")
        note_id = note_data['note']['id']

        res = self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={'pinned': True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['note']['pinned'], 1)

    def test_14_pin_state_persists_after_database_reopen(self):
        """Pin state is stored persistently in SQLite across connections."""
        note_data = self._create_note(title="Persist Pin", content="Content")
        note_id = note_data['note']['id']

        self.client.post(f'/api/notes/{note_id}/pin', headers=self._headers(), json={'pinned': True})

        # Query fresh from NoteModel (new DB connection)
        refreshed = NoteModel.get(note_id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed['pinned'], 1)

    def test_15_invalid_pin_values_rejected(self):
        """Non-boolean pinned values are strictly rejected."""
        note_data = self._create_note(title="Validation Pin", content="Content")
        note_id = note_data['note']['id']

        invalid_values = ["yes", "true", 2, -1, [1], {"pin": 1}]
        for val in invalid_values:
            # Test PUT
            res_put = self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={'pinned': val})
            self.assertEqual(res_put.status_code, 400, f"Expected 400 for PUT pinned={val}")

            # Test POST /pin
            res_pin = self.client.post(f'/api/notes/{note_id}/pin', headers=self._headers(), json={'pinned': val})
            self.assertEqual(res_pin.status_code, 400, f"Expected 400 for POST /pin pinned={val}")

    def test_16_duplicate_pin_requests_do_not_corrupt_state(self):
        """Sending explicit pinned=true multiple times is safe and idempotent."""
        note_data = self._create_note(title="Idempotent Pin", content="Content")
        note_id = note_data['note']['id']

        for _ in range(3):
            res = self.client.post(f'/api/notes/{note_id}/pin', headers=self._headers(), json={'pinned': True})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.get_json()['note']['pinned'], 1)

        stored = NoteModel.get(note_id)
        self.assertEqual(stored['pinned'], 1)

    def test_17_pinning_does_not_invoke_memory_pipeline(self):
        """Toggling pin state without changing content does not trigger fact extraction."""
        note_data = self._create_note(title="Preference Note", content="I prefer quiet mornings.")
        note_id = note_data['note']['id']

        with patch.object(MemoryPipeline, 'extract_facts', wraps=MemoryPipeline.extract_facts) as mock_extract:
            self.client.post(f'/api/notes/{note_id}/pin', headers=self._headers())
            mock_extract.assert_not_called()

    def test_18_notes_ordered_by_pinned_desc(self):
        """Notes listing returns pinned notes first."""
        self._create_note(title="Normal Note 1", content="Content 1")
        p_note = self._create_note(title="Pinned Note", content="Pinned content", pinned=True)
        self._create_note(title="Normal Note 2", content="Content 2")

        res = self.client.get('/api/notes')
        notes = res.get_json()['notes']
        self.assertEqual(notes[0]['id'], p_note['note']['id'])


class TestNoteCRUDAndLifecycle(Phase5DTestBase):
    """Tests 1-3, 22-24: Note lifecycle, rendering in responses, auth & CSRF."""

    def test_19_note_creation_still_works(self):
        """Creating a note succeeds and returns rendered_html."""
        res = self._create_note(title="New Note", content="**Bold** text")
        self.assertTrue(res['success'])
        self.assertIn("rendered_html", res['note'])
        self.assertIn("<strong>Bold</strong>", res['note']['rendered_html'])

    def test_20_note_update_still_works(self):
        """Updating a note updates fields and preserves unchanged ones."""
        created = self._create_note(title="Initial Title", content="Initial Content", tags="tag1")
        note_id = created['note']['id']

        res = self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={
            'title': 'Updated Title',
            'content': 'Updated Content'
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['note']['title'], 'Updated Title')
        self.assertEqual(data['note']['content'], 'Updated Content')
        self.assertEqual(data['note']['tags'], 'tag1')

    def test_21_note_deletion_still_works(self):
        """Deleting a note removes it from SQLite and vector store."""
        created = self._create_note(title="To Delete", content="Delete me")
        note_id = created['note']['id']

        del_res = self.client.delete(f'/api/notes/{note_id}', headers=self._headers())
        self.assertEqual(del_res.status_code, 200)
        self.assertIsNone(NoteModel.get(note_id))

    def test_22_note_get_by_id_includes_rendered_html(self):
        """GET /api/notes/<id> includes rendered_html and related memories."""
        created = self._create_note(title="Markdown Query", content="# Title\nHello world")
        note_id = created['note']['id']

        res = self.client.get(f'/api/notes/{note_id}')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("rendered_html", data['note'])
        self.assertIn("<h1>Title</h1>", data['note']['rendered_html'])

    def test_23_note_routes_require_authentication_and_csrf(self):
        """Note mutating endpoints require auth and CSRF."""
        # Missing CSRF
        res = self.client.post('/api/notes', headers={'Content-Type': 'application/json'}, json={'title': 'Test'})
        self.assertEqual(res.status_code, 403)


class TestNoteMemoryPipelineIntegration(Phase5DTestBase):
    """Tests 17-21, 23: Note updates route through MemoryPipeline using raw user content."""

    def test_24_note_update_invokes_existing_memory_pipeline(self):
        """Updating note content with a preference statement extracts and reconciles memory."""
        created = self._create_note(title="Draft Note", content="Initial general thoughts.")
        note_id = created['note']['id']

        # Update with explicit user preference
        res = self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={
            'content': "I prefer working on deep architecture in the evening."
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertGreater(len(data.get('learned_memories', [])), 0)

        # Verify memory created
        active = MemoryModel.list_active()
        pref_mem = [m for m in active if "evening" in m['content'].lower()]
        self.assertEqual(len(pref_mem), 1)

    def test_25_note_update_uses_user_authored_raw_content(self):
        """MemoryPipeline receives the raw user-authored text, not rendered HTML."""
        created = self._create_note(title="Learning Note", content="Initial content.")
        note_id = created['note']['id']

        raw_user_content = "I usually take a break at 2pm daily."

        with patch.object(MemoryPipeline, 'extract_facts', wraps=MemoryPipeline.extract_facts) as mock_extract:
            self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={
                'content': raw_user_content
            })
            mock_extract.assert_called_once()
            called_text = mock_extract.call_args[0][0]
            self.assertIn(raw_user_content, called_text)
            self.assertNotIn("<p>", called_text)
            self.assertNotIn("<br>", called_text)

    def test_26_rendered_html_is_not_fed_into_memory_extraction(self):
        """HTML markup must never be the source passed to extract_facts."""
        created = self._create_note(title="Formatting", content="Old")
        note_id = created['note']['id']

        markdown_input = "# Habits\n**I prefer** writing tests before implementation."
        with patch.object(MemoryPipeline, 'extract_facts', wraps=MemoryPipeline.extract_facts) as mock_extract:
            self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={
                'content': markdown_input
            })
            called_text = mock_extract.call_args[0][0]
            self.assertNotIn("<h1>", called_text)
            self.assertNotIn("<strong>", called_text)
            self.assertIn(markdown_input, called_text)

    def test_27_assistant_generated_content_not_automatically_treated_as_user_fact(self):
        """Assistant content must never automatically trigger fact extraction into user memories."""
        # Ensure only user-authored note mutations trigger extraction
        # Note updates come directly from user client PUT request
        active_before = len(MemoryModel.list_active())
        # Plain note update without user preference patterns creates zero memories
        self._create_note(title="Assistant Output Summary", content="AI Model response: Here are 5 tips for productivity.")
        active_after = len(MemoryModel.list_active())
        self.assertEqual(active_before, active_after)

    def test_28_provenance_identifies_note_origin(self):
        """Learned memories from note update record provenance referencing the note ID."""
        created = self._create_note(title="Sprint Rules", content="Initial text.")
        note_id = created['note']['id']

        res = self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={
            'content': "Always review security requirements before deploying."
        })
        data = res.get_json()
        self.assertGreater(len(data.get('learned_memories', [])), 0)

        # Check provenance in MemoryDecisionLog
        recent_logs = MemoryDecisionLogModel.get_recent(limit=5)
        matching_log = [l for l in recent_logs if l['triggered_by'] == 'note_update']
        self.assertTrue(len(matching_log) > 0)

        # Check source context in memory
        mem_id = data['learned_memories'][0]['memory_id']
        mem = MemoryModel.get(mem_id)
        self.assertIn(f"note #{note_id}", mem['source_context'])

    def test_29_quick_capture_note_behavior_remains_correct(self):
        """Quick Capture creating a note still extracts memory and preserves provenance."""
        res = self.client.post('/api/tasks/quick-capture', headers=self._headers(), json={
            'text': "note: Daily standup meeting every morning at 9am"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['type'], 'note')
        self.assertIn('learned_memories', data)

    def test_30_note_update_memory_vector_synchronization_intact(self):
        """Updating a note updates its document in the vector store."""
        created = self._create_note(title="Vector Target", content="Old vector text")
        note_id = created['note']['id']
        self.assertIn(f"note_{note_id}", self.vs.documents)

        self.client.put(f'/api/notes/{note_id}', headers=self._headers(), json={
            'content': "Updated vector text"
        })
        self.assertIn("Updated vector text", self.vs.documents[f"note_{note_id}"]["text"])


if __name__ == '__main__':
    unittest.main()
