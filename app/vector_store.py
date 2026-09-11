import math
import re
import json
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
    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or config.VECTOR_STORE_PATH
        self.documents: Dict[str, Dict[str, Any]] = {}  # key -> {id, doc_type, text, metadata, vector, timestamp}
        self.idf: Dict[str, float] = {}
        self.total_docs: int = 0
        self.load()

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
        """Persist vector index to disk."""
        try:
            # We don't need to persist large token lists or vectors, just tf and metadata
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
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[VectorStore] Warning: could not save vector store: {e}")

    def load(self):
        """Load vector index from disk if exists."""
        if not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.total_docs = data.get("total_docs", 0)
            self.idf = data.get("idf", {})
            raw_docs = data.get("documents", {})
            self.documents = {}
            for k, d in raw_docs.items():
                self.documents[k] = {
                    "key": d["key"],
                    "id": d["id"],
                    "doc_type": d["doc_type"],
                    "text": d["text"],
                    "tokens": tokenize(d["text"]),
                    "tf": d.get("tf", {}),
                    "vector": self._vectorize_tf(d.get("tf", {})),
                    "metadata": d.get("metadata", {}),
                    "timestamp": d.get("timestamp", "")
                }
        except Exception as e:
            print(f"[VectorStore] Notice: initial load fresh: {e}")
            self.documents = {}
            self.idf = {}

# Global vector store instance
global_vector_store = VectorStore()
