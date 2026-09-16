"""
Phase 5C Tests — Task Productivity Tools
Tests for:
  - Task search/filtering API (q, tag, due)
  - Strict due-date validation (YYYY-MM-DD, real calendar dates)
  - Subtask structure validation ({title: str, completed: bool})
  - Quick Mark Done endpoint
  - Overdue/today/upcoming filter correctness
  - Full regression with existing behavior
"""
import os
import sys
import json
import unittest
import tempfile
from pathlib import Path
from datetime import date, timedelta, datetime
from unittest.mock import patch

# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.vector_store import VectorStore
import app.vector_store as vector_store_module


class Phase5CTestBase(unittest.TestCase):
    """Shared setup for Phase 5C tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / 'test_5c.db'
        self.test_vec_path = Path(self.temp_dir.name) / 'test_5c_vec.json'
        self.test_auth_path = Path(self.temp_dir.name) / 'test_5c_auth.json'

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        vector_store_module.global_vector_store = VectorStore(storage_path=self.test_vec_path)

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

        # Setup auth and grab CSRF token
        res = self.client.post('/api/auth/setup', json={
            'password': 'testpass123',
            'confirm_password': 'testpass123'
        })
        data = res.get_json()
        self.csrf_token = data.get('csrf_token', '')

    def tearDown(self):
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

    def _create_task(self, title='Test Task', **kwargs):
        payload = {'title': title}
        payload.update(kwargs)
        res = self.client.post('/api/tasks', headers=self._headers(), json=payload)
        return res.get_json()


class TestDueDateValidation(Phase5CTestBase):
    """Strict YYYY-MM-DD due-date validation."""

    def test_valid_due_date_accepted(self):
        data = self._create_task(due_date='2025-06-15')
        self.assertTrue(data['success'])
        self.assertEqual(data['task']['due_date'], '2025-06-15')

    def test_null_due_date_accepted(self):
        data = self._create_task(due_date=None)
        self.assertTrue(data['success'])

    def test_empty_string_due_date_accepted(self):
        data = self._create_task(due_date='')
        self.assertTrue(data['success'])

    def test_invalid_format_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'due_date': '15-06-2025'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('YYYY-MM-DD', res.get_json()['error'])

    def test_datetime_string_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'due_date': '2025-06-15T10:00:00'})
        self.assertEqual(res.status_code, 400)

    def test_impossible_date_rejected(self):
        """Feb 30 does not exist."""
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'due_date': '2025-02-30'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('not a valid calendar date', res.get_json()['error'])

    def test_non_string_due_date_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'due_date': 20250615})
        self.assertEqual(res.status_code, 400)

    def test_partial_date_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'due_date': '2025-06'})
        self.assertEqual(res.status_code, 400)

    def test_due_date_update_validation(self):
        """Update with invalid date is rejected."""
        data = self._create_task()
        task_id = data['task']['id']
        res = self.client.put(f'/api/tasks/{task_id}', headers=self._headers(),
                              json={'due_date': 'not-a-date'})
        self.assertEqual(res.status_code, 400)

    def test_due_date_update_valid(self):
        """Update with valid date succeeds."""
        data = self._create_task()
        task_id = data['task']['id']
        res = self.client.put(f'/api/tasks/{task_id}', headers=self._headers(),
                              json={'due_date': '2026-12-25'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['task']['due_date'], '2026-12-25')

    def test_leap_year_date_accepted(self):
        data = self._create_task(due_date='2024-02-29')
        self.assertTrue(data['success'])

    def test_non_leap_year_feb29_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'due_date': '2025-02-29'})
        self.assertEqual(res.status_code, 400)


class TestSubtaskValidation(Phase5CTestBase):
    """Subtask structure validation."""

    def test_valid_subtasks_accepted(self):
        subtasks = [
            {'title': 'Step 1', 'completed': False},
            {'title': 'Step 2', 'completed': True},
        ]
        data = self._create_task(subtasks=subtasks)
        self.assertTrue(data['success'])

    def test_empty_subtask_list_accepted(self):
        data = self._create_task(subtasks=[])
        self.assertTrue(data['success'])

    def test_non_dict_subtask_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'subtasks': ['step1']})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Subtask #0', res.get_json()['error'])

    def test_missing_title_subtask_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'subtasks': [{'completed': False}]})
        self.assertEqual(res.status_code, 400)
        self.assertIn('title', res.get_json()['error'].lower())

    def test_empty_title_subtask_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'subtasks': [{'title': '', 'completed': False}]})
        self.assertEqual(res.status_code, 400)

    def test_non_boolean_completed_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'subtasks': [{'title': 'Step', 'completed': 1}]})
        self.assertEqual(res.status_code, 400)
        self.assertIn('boolean', res.get_json()['error'].lower())

    def test_extra_fields_in_subtask_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'subtasks': [{'title': 'Step', 'completed': False, 'extra': 1}]})
        self.assertEqual(res.status_code, 400)
        self.assertIn('unexpected fields', res.get_json()['error'].lower())

    def test_nested_subtask_rejected(self):
        res = self.client.post('/api/tasks', headers=self._headers(),
                               json={'title': 'Test', 'subtasks': [{'title': 'Step', 'completed': False},
                                                                     [{'title': 'Nested', 'completed': False}]]})
        self.assertEqual(res.status_code, 400)

    def test_subtask_update_validation(self):
        """Update with invalid subtask is rejected."""
        data = self._create_task()
        task_id = data['task']['id']
        res = self.client.put(f'/api/tasks/{task_id}', headers=self._headers(),
                              json={'subtasks': [{'title': '', 'completed': False}]})
        self.assertEqual(res.status_code, 400)

    def test_null_subtasks_accepted(self):
        data = self._create_task(subtasks=None)
        self.assertTrue(data['success'])


class TestTaskSearch(Phase5CTestBase):
    """Task search via ?q= parameter."""

    def setUp(self):
        super().setUp()
        self._create_task(title='Buy groceries', description='Milk and eggs', tags='shopping')
        self._create_task(title='Write report', description='Q3 financials', tags='work, finance')
        self._create_task(title='Fix backend bug', description='NullPointer in handler', tags='work, dev')

    def test_search_by_title(self):
        res = self.client.get('/api/tasks?q=groceries')
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['tasks']), 1)
        self.assertIn('groceries', data['tasks'][0]['title'].lower())

    def test_search_by_description(self):
        res = self.client.get('/api/tasks?q=NullPointer')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)

    def test_search_case_insensitive(self):
        """SQLite LIKE is case-insensitive for ASCII."""
        res = self.client.get('/api/tasks?q=REPORT')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)

    def test_search_no_match(self):
        res = self.client.get('/api/tasks?q=nonexistent')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 0)

    def test_search_empty_returns_all(self):
        res = self.client.get('/api/tasks?q=')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 3)


class TestTaskTagFilter(Phase5CTestBase):
    """Task tag filtering via ?tag= parameter."""

    def setUp(self):
        super().setUp()
        self._create_task(title='Task A', tags='work, dev')
        self._create_task(title='Task B', tags='personal')
        self._create_task(title='Task C', tags='work, finance')

    def test_filter_by_tag(self):
        res = self.client.get('/api/tasks?tag=dev')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)
        self.assertEqual(data['tasks'][0]['title'], 'Task A')

    def test_filter_by_broad_tag(self):
        res = self.client.get('/api/tasks?tag=work')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 2)

    def test_filter_no_match(self):
        res = self.client.get('/api/tasks?tag=travel')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 0)


class TestTaskDueFilter(Phase5CTestBase):
    """Task due-date filtering via ?due= parameter."""

    def setUp(self):
        super().setUp()
        today = date.today()
        self.yesterday = (today - timedelta(days=1)).isoformat()
        self.today_str = today.isoformat()
        self.tomorrow = (today + timedelta(days=1)).isoformat()

        self._create_task(title='Overdue task', due_date=self.yesterday)
        self._create_task(title='Today task', due_date=self.today_str)
        self._create_task(title='Upcoming task', due_date=self.tomorrow)
        self._create_task(title='No due date task')
        # Completed overdue task should NOT appear as overdue
        self._create_task(title='Done overdue task', due_date=self.yesterday)
        # Mark it done via update
        res = self.client.get('/api/tasks?q=Done+overdue')
        task_id = res.get_json()['tasks'][0]['id']
        self.client.put(f'/api/tasks/{task_id}', headers=self._headers(),
                        json={'status': 'done'})

    def test_filter_overdue(self):
        res = self.client.get('/api/tasks?due=overdue')
        data = res.get_json()
        titles = [t['title'] for t in data['tasks']]
        self.assertIn('Overdue task', titles)
        self.assertNotIn('Today task', titles)
        self.assertNotIn('Upcoming task', titles)
        self.assertNotIn('No due date task', titles)
        # Completed tasks must NOT appear as overdue
        self.assertNotIn('Done overdue task', titles)

    def test_filter_today(self):
        res = self.client.get('/api/tasks?due=today')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)
        self.assertEqual(data['tasks'][0]['title'], 'Today task')

    def test_filter_upcoming(self):
        res = self.client.get('/api/tasks?due=upcoming')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)
        self.assertEqual(data['tasks'][0]['title'], 'Upcoming task')

    def test_invalid_due_filter_rejected(self):
        res = self.client.get('/api/tasks?due=invalid')
        self.assertEqual(res.status_code, 400)
        self.assertIn('Invalid due filter', res.get_json()['error'])

    def test_combined_filters(self):
        """Search + due filter combined."""
        res = self.client.get(f'/api/tasks?q=task&due=today')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)
        self.assertEqual(data['tasks'][0]['title'], 'Today task')


class TestQuickMarkDone(Phase5CTestBase):
    """POST /api/tasks/{id}/mark-done endpoint."""

    def test_mark_done_success(self):
        data = self._create_task(title='Pending task')
        task_id = data['task']['id']
        res = self.client.post(f'/api/tasks/{task_id}/mark-done', headers=self._headers())
        result = res.get_json()
        self.assertTrue(result['success'])
        self.assertEqual(result['task']['status'], 'done')

    def test_mark_done_already_done(self):
        data = self._create_task(title='Already done')
        task_id = data['task']['id']
        # First mark done
        self.client.post(f'/api/tasks/{task_id}/mark-done', headers=self._headers())
        # Second mark done should still succeed (idempotent)
        res = self.client.post(f'/api/tasks/{task_id}/mark-done', headers=self._headers())
        result = res.get_json()
        self.assertTrue(result['success'])
        self.assertIn('already', result.get('message', '').lower())

    def test_mark_done_nonexistent(self):
        res = self.client.post('/api/tasks/99999/mark-done', headers=self._headers())
        self.assertEqual(res.status_code, 404)

    def test_mark_done_updates_actual_status(self):
        """Verify the task is persistently updated to done."""
        data = self._create_task(title='Check persistence')
        task_id = data['task']['id']
        self.client.post(f'/api/tasks/{task_id}/mark-done', headers=self._headers())
        # Verify via GET
        res = self.client.get(f'/api/tasks/{task_id}')
        self.assertEqual(res.get_json()['task']['status'], 'done')


class TestTaskSearchFilterRegression(Phase5CTestBase):
    """Verify existing behavior is preserved alongside new features."""

    def test_list_all_no_filters(self):
        self._create_task(title='Task 1')
        self._create_task(title='Task 2')
        res = self.client.get('/api/tasks')
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['tasks']), 2)

    def test_status_filter_still_works(self):
        self._create_task(title='Todo task')
        data = self._create_task(title='Done task')
        task_id = data['task']['id']
        self.client.put(f'/api/tasks/{task_id}', headers=self._headers(), json={'status': 'done'})
        res = self.client.get('/api/tasks?status=done')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)
        self.assertEqual(data['tasks'][0]['title'], 'Done task')

    def test_priority_filter_still_works(self):
        self._create_task(title='Urgent', priority='urgent')
        self._create_task(title='Low', priority='low')
        res = self.client.get('/api/tasks?priority=urgent')
        data = res.get_json()
        self.assertEqual(len(data['tasks']), 1)

    def test_task_crud_still_works(self):
        """Create, read, update, delete round trip."""
        data = self._create_task(title='CRUD test', description='Test desc')
        task_id = data['task']['id']
        self.assertTrue(data['success'])

        # Read
        res = self.client.get(f'/api/tasks/{task_id}')
        self.assertEqual(res.get_json()['task']['title'], 'CRUD test')

        # Update
        res = self.client.put(f'/api/tasks/{task_id}', headers=self._headers(),
                              json={'title': 'Updated CRUD'})
        self.assertTrue(res.get_json()['success'])

        # Delete
        res = self.client.delete(f'/api/tasks/{task_id}', headers=self._headers())
        self.assertTrue(res.get_json()['success'])

    def test_feedback_endpoint_still_works(self):
        data = self._create_task(title='Feedback test')
        task_id = data['task']['id']
        res = self.client.post(f'/api/tasks/{task_id}/feedback', headers=self._headers(),
                               json={'action': 'accepted'})
        self.assertTrue(res.get_json()['success'])


if __name__ == '__main__':
    unittest.main()
