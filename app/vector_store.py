import os
import math
import re
import json
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from app import config

# Built-in lightweight stop words for clean token extraction
STOP_WORDS = {
    'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 'any', 'are', 'aren',
    'as', 'at', 'be', 'because', 'been', 'before', 'being', 'below', 'between', 'both', 'but', 'by',
    'could', 'did', 'do', 'does', 'doing', 'down', 'during', 'each', 'few', 'for', 'from', 'further',
    'had', 'has', 'have', 'having', 'he', 'her', 'here', 'hers', 'herself', 'him', 'himself', 'his',
    'how', 'i', 'if', 'in', 'into', 'is', 'it', 'its', 'itself', 'just', 'me', 'more', 'most', 'my',
    'myself', 'no', 'nor', 'not', 'now', 'of', 'off', 'on', 'once', 'only', 'or', 'other', 'our',
    'ours', 'ourselves', 'out', 'over', 'own', 'same', 'she', 'should', 'so', 'some', 'such', 'than',
    'that', 'the', 'their', 'theirs', 'them', 'themselves', 'then', 'there', 'these', 'they', 'this',
    'those', 'through', 'to', 'too', 'under', 'until', 'up', 'very', 'was', 'we', 'were', 'what',
    'when', 'where', 'which', 'while', 'who', 'whom', 'why', 'with', 'would', 'you', 'your', 'yours'
}

def tokenize(text: str) -> List[str]:
    """Tokenize text into lowercased words and n-grams (1-grams and 2-grams)."""
    clean = re.sub(r'[^\w\s]', ' ', text.lower())
    words = [w for w in clean.split() if w and w not in STOP_WORDS]
    tokens = list(words)
    # Add bigrams for context capture
    for i in range(len(words) - 1):
        tokens.append(f"{words[i]}_{words[i+1]}")
    return tokens

class VectorStore:
    def __init__(self, storage_path: Optional[Path] = None, auto_recover: bool = False):
        self.storage_path = Path(storage_path) if storage_path else config.VECTOR_STORE_PATH
        self.documents: Dict[str, Dict[str, Any]] = {}  # key -> {id, doc_type, text, metadata, vector, timestamp}
        self.idf: Dict[str, float] = {}
        self.total_docs: int = 0
        self.load(auto_recover=auto_recover)

    def _compute_tf(self, tokens: List[str]) -> Dict[str, float]:
        if not tokens:
            return {}
        counts = Counter(tokens)
        total = float(len(tokens))
        return {term: count / total for term, count in counts.items()}

    def rebuild_idf(self):
        """Recompute Inverse Document Frequency (IDF) across all documents."""
        self.total_docs = len(self.documents)
        if self.total_docs == 0:
            self.idf = {}
            return

        doc_frequencies: Counter = Counter()
        for doc in self.documents.values():
            unique_terms = set(doc.get("tokens", []))
            for term in unique_terms:
                doc_frequencies[term] += 1

        self.idf = {}
        for term, df in doc_frequencies.items():
            # Smooth IDF formula: log((N + 1) / (df + 1)) + 1.0
            self.idf[term] = math.log((self.total_docs + 1.0) / (df + 1.0)) + 1.0

        # Re-vectorize existing documents
        for doc_key, doc in self.documents.items():
            doc["vector"] = self._vectorize_tf(doc.get("tf", {}))

    def _vectorize_tf(self, tf: Dict[str, float]) -> Dict[str, float]:
        """Compute normalized TF-IDF vector."""
        tfidf: Dict[str, float] = {}
        norm_sq = 0.0
        for term, tf_val in tf.items():
            idf_val = self.idf.get(term, 1.0)
            score = tf_val * idf_val
            tfidf[term] = score
            norm_sq += score * score

        if norm_sq > 0:
            norm = math.sqrt(norm_sq)
            return {term: val / norm for term, val in tfidf.items()}
        return {}

    def index_document(self, doc_id: Any, doc_type: str, text: str,
                       metadata: Optional[Dict[str, Any]] = None,
                       timestamp: Optional[str] = None, auto_rebuild: bool = True):
        """Index or update a document in the vector store."""
        key = f"{doc_type}_{doc_id}"
        tokens = tokenize(text)
        tf = self._compute_tf(tokens)
        
        self.documents[key] = {
            "key": key,
            "id": doc_id,
            "doc_type": doc_type,
            "text": text,
            "tokens": tokens,
            "tf": tf,
            "vector": {},  # Will be populated by rebuild_idf
            "metadata": metadata or {},
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat()
        }

        if auto_rebuild:
            self.rebuild_idf()
            self.save()

    def remove_document(self, doc_id: Any, doc_type: str, auto_rebuild: bool = True):
        """Remove a document from index."""
        key = f"{doc_type}_{doc_id}"
        if key in self.documents:
            del self.documents[key]
            if auto_rebuild:
                self.rebuild_idf()
                self.save()

    def cosine_similarity(self, vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        """Calculate cosine similarity between two unit-normalized sparse vectors."""
        if not vec_a or not vec_b:
            return 0.0
        # Iterate over smaller dict
        if len(vec_a) > len(vec_b):
            vec_a, vec_b = vec_b, vec_a
        return sum(val * vec_b.get(term, 0.0) for term, val in vec_a.items())

    def search(self, query: str, doc_type: Optional[str] = None,
               limit: int = 5, min_score: float = 0.05) -> List[Dict[str, Any]]:
        """Search documents by semantic relevance to query with confidence and recency scoring."""
        query_tokens = tokenize(query)
        if not query_tokens or not self.documents:
            return []

        query_tf = self._compute_tf(query_tokens)
        query_vec = self._vectorize_tf(query_tf)
        now = datetime.now(timezone.utc)

        scored_results: List[Tuple[float, Dict[str, Any]]] = []

        for doc in self.documents.values():
            if doc_type and doc["doc_type"] != doc_type:
                continue

            sim = self.cosine_similarity(query_vec, doc.get("vector", {}))
            if sim < min_score:
                continue

            # Factor in confidence weight if present (for memories)
            confidence = doc.get("metadata", {}).get("confidence_weight", 0.70)
            
            # Recency factor: decay over 30 days
            try:
                doc_time = datetime.fromisoformat(doc["timestamp"])
                days_old = max(0, (now - doc_time).total_seconds() / 86400.0)
                recency_factor = max(0.2, 1.0 - (days_old / 60.0))
            except Exception:
                recency_factor = 0.5

            # Combined weighted score
            final_score = (0.65 * sim) + (0.20 * confidence) + (0.15 * recency_factor)

            res = {
                "id": doc["id"],
                "doc_type": doc["doc_type"],
                "text": doc["text"],
                "similarity": round(sim, 4),
                "final_score": round(final_score, 4),
                "metadata": doc["metadata"],
                "timestamp": doc["timestamp"]
            }
            scored_results.append((final_score, res))

        # Sort descending by score
        scored_results.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_results[:limit]]

    def save(self):
        """Persist vector index to disk atomically using temporary file replacement."""
        temp_path: Optional[Path] = None
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "total_docs": self.total_docs,
                "idf": self.idf,
                "documents": {
                    k: {
                        "key": d["key"],
                        "id": d["id"],
                        "doc_type": d["doc_type"],
                        "text": d["text"],
                        "tf": d["tf"],
                        "metadata": d["metadata"],
                        "timestamp": d["timestamp"]
                    }
                    for k, d in self.documents.items()
                }
            }
            # Write to a temporary file in the same directory for atomic os.replace()
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self.storage_path.parent),
                delete=False,
                encoding="utf-8",
                prefix=f"{self.storage_path.stem}_",
                suffix=".tmp"
            ) as f:
                temp_path = Path(f.name)
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())

            # Atomically replace destination file
            os.replace(temp_path, self.storage_path)
            temp_path = None
        except Exception as e:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            print(f"[VectorStore] Warning: could not save vector store: {e}")
            raise

    def load(self, auto_recover: bool = False) -> bool:
        """
        Load vector index from disk if it exists and is valid.
        Returns True if a valid store was successfully loaded, False otherwise.
        If auto_recover is True and load fails or store is empty when DB has records,
        triggers automatic rebuild from SQLite.
        """
        success = False
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    raise ValueError("Vector store JSON root must be an object.")

                raw_docs = data.get("documents")
                if not isinstance(raw_docs, dict):
                    raise ValueError("Vector store documents field must be an object.")

                self.idf = data.get("idf", {})
                loaded_docs = {}
                for k, d in raw_docs.items():
                    if not isinstance(d, dict) or "text" not in d or "id" not in d or "doc_type" not in d:
                        raise ValueError(f"Invalid document format for key: {k}")
                    tf = d.get("tf", {})
                    loaded_docs[k] = {
                        "key": d.get("key", k),
                        "id": d["id"],
                        "doc_type": d["doc_type"],
                        "text": d["text"],
                        "tokens": tokenize(d["text"]),
                        "tf": tf,
                        "vector": self._vectorize_tf(tf),
                        "metadata": d.get("metadata", {}),
                        "timestamp": d.get("timestamp", "")
                    }

                self.documents = loaded_docs
                self.total_docs = data.get("total_docs", len(loaded_docs))

                # Recompute IDF if missing/empty in persisted file but documents exist
                if not self.idf and self.documents:
                    self.rebuild_idf()

                success = True
            except Exception as e:
                print(f"[VectorStore] Notice: initial load failed ({e}), store marked invalid.")
                self.documents = {}
                self.idf = {}
                self.total_docs = 0
                success = False
        else:
            self.documents = {}
            self.idf = {}
            self.total_docs = 0
            success = False

        if auto_recover:
            return self.ensure_valid_index()

        return success

    def _get_expected_db_keys(self) -> Optional[set]:
        """
        Query SQLite for all authoritative record keys:
        - memory_<id> for active memories (status = 'active')
        - task_<id> for tasks
        - note_<id> for notes
        Returns a set of string keys, or None if the database/tables cannot be read.
        """
        try:
            from app.database import get_db
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('memories', 'tasks', 'notes')")
                tables = {r[0] for r in cur.fetchall()}
                if not {'memories', 'tasks', 'notes'}.issubset(tables):
                    return None

                expected_keys = set()
                cur.execute("SELECT id FROM memories WHERE status = 'active'")
                for r in cur.fetchall():
                    expected_keys.add(f"memory_{r[0]}")

                cur.execute("SELECT id FROM tasks")
                for r in cur.fetchall():
                    expected_keys.add(f"task_{r[0]}")

                cur.execute("SELECT id FROM notes")
                for r in cur.fetchall():
                    expected_keys.add(f"note_{r[0]}")

                return expected_keys
        except Exception:
            return None

    def _has_active_db_records(self) -> bool:
        """Check if SQLite contains any active indexable records (memories, tasks, or notes)."""
        keys = self._get_expected_db_keys()
        return bool(keys)

    def _check_db_content_sync(self) -> bool:
        """
        Check if the text content and metadata of documents in the vector store
        match authoritative SQLite records.
        Returns True if fully synchronized, False if any content or confidence has diverged.
        """
        try:
            from app.database import get_db
            with get_db() as conn:
                cur = conn.cursor()
                # 1. Check active memories
                cur.execute("SELECT id, content, confidence_weight FROM memories WHERE status = 'active'")
                for r in cur.fetchall():
                    key = f"memory_{r[0]}"
                    doc = self.documents.get(key)
                    if not doc:
                        return False
                    if doc.get("text") != r[1]:
                        return False
                    doc_conf = doc.get("metadata", {}).get("confidence_weight", 0.70)
                    db_conf = r[2] if r[2] is not None else 0.70
                    if abs(float(doc_conf) - float(db_conf)) > 1e-4:
                        return False

                # 2. Check tasks
                cur.execute("SELECT id, title, description, tags FROM tasks")
                for r in cur.fetchall():
                    key = f"task_{r[0]}"
                    doc = self.documents.get(key)
                    if not doc:
                        return False
                    parts = [r[1]]
                    if r[2]:
                        parts.append(r[2])
                    if r[3]:
                        parts.append(r[3])
                    expected_text = " ".join(parts).strip()
                    if doc.get("text") != expected_text:
                        return False

                # 3. Check notes
                cur.execute("SELECT id, title, content, tags FROM notes")
                for r in cur.fetchall():
                    key = f"note_{r[0]}"
                    doc = self.documents.get(key)
                    if not doc:
                        return False
                    parts = [r[1], r[2]]
                    if r[3]:
                        parts.append(r[3])
                    expected_text = "\n".join(parts).strip()
                    if doc.get("text") != expected_text:
                        return False

                return True
        except Exception:
            return True

    def rebuild_from_db(self) -> int:
        """
        Deterministically rebuild the vector store from authoritative SQLite records.
        Indexes all active memories, tasks, and notes without duplicates.
        Returns the total number of documents indexed.
        """
        from app.models import MemoryModel, TaskModel, NoteModel

        self.documents = {}

        # 1. Active memories (status == 'active')
        for m in MemoryModel.list_active():
            self.index_document(
                doc_id=m["id"],
                doc_type="memory",
                text=m["content"],
                metadata={
                    "category": m.get("category", "preference"),
                    "confidence_weight": m.get("confidence_weight", 0.70),
                    "flagged": bool(m.get("is_flagged"))
                },
                timestamp=m.get("updated_at") or m.get("created_at"),
                auto_rebuild=False
            )

        # 2. Tasks
        for t in TaskModel.list_all():
            parts = [t["title"]]
            if t.get("description"):
                parts.append(t["description"])
            if t.get("tags"):
                parts.append(t["tags"])
            task_text = " ".join(parts).strip()

            self.index_document(
                doc_id=t["id"],
                doc_type="task",
                text=task_text,
                metadata={
                    "priority": t.get("priority", "medium"),
                    "status": t.get("status", "todo")
                },
                timestamp=t.get("updated_at") or t.get("created_at"),
                auto_rebuild=False
            )

        # 3. Notes
        for n in NoteModel.list_all():
            parts = [n["title"], n["content"]]
            if n.get("tags"):
                parts.append(n["tags"])
            note_text = "\n".join(parts).strip()

            self.index_document(
                doc_id=n["id"],
                doc_type="note",
                text=note_text,
                metadata={
                    "tags": n.get("tags", ""),
                    "pinned": n.get("pinned", 0)
                },
                timestamp=n.get("updated_at") or n.get("created_at"),
                auto_rebuild=False
            )

        self.rebuild_idf()
        self.save()
        return len(self.documents)

    def ensure_valid_index(self) -> bool:
        """
        Ensures the vector store is valid and fully synchronized with authoritative SQLite records.
        - If the storage file is missing, unreadable, or malformed, rebuilds from SQLite.
        - If the store is missing an authoritative SQLite record, rebuilds from SQLite.
        - If the store contains stale/extra vector documents not in SQLite, rebuilds from SQLite.
        - If the store's text content or metadata has diverged from SQLite, rebuilds from SQLite.
        - If the store is valid and synchronized with SQLite (or both are empty), loads without rebuild.
        Returns True if a rebuild was performed, False if the existing index was valid and synchronized.
        """
        file_valid = self.load(auto_recover=False)
        if not file_valid:
            self.rebuild_from_db()
            return True

        expected_keys = self._get_expected_db_keys()
        if expected_keys is None:
            # If database tables are not ready or cannot be queried, keep loaded store
            return False

        current_keys = set(self.documents.keys())

        # 1. Authoritative key coverage check:
        # - Missing authoritative record: expected_keys - current_keys != empty
        # - Stale/extra vector documents: current_keys - expected_keys != empty
        # - Empty store when SQLite has records: current_keys != expected_keys
        # - Empty store when SQLite is empty: current_keys == expected_keys == set()
        if current_keys != expected_keys:
            self.rebuild_from_db()
            return True

        # 2. Authoritative content synchronization check:
        # Rebuild if document texts or confidence weights have diverged from SQLite
        if not self._check_db_content_sync():
            self.rebuild_from_db()
            return True

        return False

# Global vector store instance
global_vector_store = VectorStore()
