import unittest
import tempfile
from pathlib import Path
from app.vector_store import VectorStore

class TestVectorStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "test_vector.json"
        self.store = VectorStore(storage_path=self.store_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_indexing_and_search(self):
        self.store.index_document(1, "memory", "User prefers morning sessions for deep coding work", {"confidence_weight": 0.85})
        self.store.index_document(2, "memory", "Gym workout routine scheduled every weekday at 6pm", {"confidence_weight": 0.80})
        self.store.index_document(3, "note", "Architecture notes: Cognito uses SQLite and pure-Python vector search")

        # Query relevant to coding
        results = self.store.search("morning coding tasks", limit=2)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["id"], 1)
        self.assertEqual(results[0]["doc_type"], "memory")
        self.assertTrue(results[0]["similarity"] > 0.0)

        # Query relevant to workout
        gym_results = self.store.search("exercise and gym evening", limit=2)
        self.assertTrue(len(gym_results) > 0)
        self.assertEqual(gym_results[0]["id"], 2)

    def test_persistence_and_reload(self):
        self.store.index_document(10, "memory", "Client demos should always be medium priority")
        self.store.save()

        # Reload into fresh store instance
        new_store = VectorStore(storage_path=self.store_path)
        hits = new_store.search("client demo priority", limit=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], 10)

if __name__ == "__main__":
    unittest.main()
