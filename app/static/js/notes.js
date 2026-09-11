/**
 * NOTES MANAGEMENT & AUTO-FACT EXTRACTION
 */

async function loadNotes() {
  try {
    const res = await fetch('/api/notes');
    const data = await res.json();
    if (data.success) {
      AppState.notes = data.notes;
      renderNotes();
      const countEl = document.getElementById('navNoteCount');
      if (countEl) countEl.innerText = data.notes.length;
    }
  } catch (err) {
    console.error('Failed to load notes', err);
  }
}

function renderNotes(customNotes = null) {
  const container = document.getElementById('notesGrid');
  if (!container) return;
  container.innerHTML = '';

  const list = customNotes || AppState.notes;
  if (list.length === 0) {
    container.innerHTML = '<div class="card-desc" style="grid-column: 1/-1; text-align:center; padding: 2.5rem;">No notes yet. Capture your thoughts with "+ New Note" or Quick Capture (Ctrl+K).</div>';
    return;
  }

  list.forEach(note => {
    const card = document.createElement('div');
    card.className = 'note-card';

    const tagsHtml = (note.tags || '')
      .split(',')
      .map(t => t.trim())
      .filter(Boolean)
      .map(t => `<span class="note-tag">#${escapeHtml(t)}</span>`)
      .join('');

    const dateStr = note.created_at ? new Date(note.created_at).toLocaleDateString() : '';

    card.innerHTML = `
      <div>
        <div class="note-title">${escapeHtml(note.title)}</div>
        <div class="note-content" style="margin-top:0.45rem;">${escapeHtml(note.content)}</div>
      </div>
      <div>
        <div class="note-tags">${tagsHtml}</div>
        <div class="note-footer">
          <span>${dateStr}</span>
          <div class="task-actions">
            <button class="btn-icon" onclick="openEditNoteModal(${note.id})" title="Edit Note">✏️</button>
            <button class="btn-icon" onclick="deleteNoteItem(${note.id})" title="Delete Note">🗑️</button>
          </div>
        </div>
      </div>
    `;
    container.appendChild(card);
  });
}

// Semantic Search with Debounce
let searchTimeout = null;
function handleNoteSearch() {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(async () => {
    const query = document.getElementById('noteSearchInput').value.trim();
    if (!query) {
      renderNotes();
      return;
    }
    try {
      const res = await fetch(`/api/notes/search?q=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (data.success) {
        const matchedIds = new Set(data.results.map(r => r.id));
        const filtered = AppState.notes.filter(n => matchedIds.has(n.id));
        renderNotes(filtered);
      }
    } catch (e) {
      console.error('Note search failed', e);
    }
  }, 250);
}

function openNewNoteModal() {
  document.getElementById('editNoteId').value = '';
  document.getElementById('noteModalTitle').innerText = 'Create Note';
  document.getElementById('noteTitleInput').value = '';
  document.getElementById('noteContentInput').value = '';
  document.getElementById('noteTagsInput').value = '';
  openModal('noteModal');
}

function openEditNoteModal(noteId) {
  const note = AppState.notes.find(n => n.id === noteId);
  if (!note) return;

  document.getElementById('editNoteId').value = note.id;
  document.getElementById('noteModalTitle').innerText = 'Edit Note';
  document.getElementById('noteTitleInput').value = note.title;
  document.getElementById('noteContentInput').value = note.content;
  document.getElementById('noteTagsInput').value = note.tags || '';
  openModal('noteModal');
}

async function saveNoteModal() {
  const id = document.getElementById('editNoteId').value;
  const title = document.getElementById('noteTitleInput').value.trim();
  const content = document.getElementById('noteContentInput').value.trim();

  if (!title && !content) {
    showToast('Note must have a title or content', 'error');
    return;
  }

  const payload = {
    title,
    content,
    tags: document.getElementById('noteTagsInput').value.trim()
  };

  try {
    let res;
    if (id) {
      res = await fetch(`/api/notes/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    } else {
      res = await fetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    }
    const data = await res.json();
    if (data.success) {
      closeModal('noteModal');
      let msg = id ? 'Note updated!' : 'Note saved!';
      if (data.learned_memories && data.learned_memories.length > 0) {
        msg += ` 🧠 Extracted ${data.learned_memories.length} habit(s) to memory!`;
      }
      showToast(msg, 'success');
      loadNotes();
      loadMemories();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to save note', 'error');
  }
}

async function deleteNoteItem(noteId) {
  if (!confirm('Are you sure you want to delete this note?')) return;
  try {
    const res = await fetch(`/api/notes/${noteId}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Note deleted', 'info');
      loadNotes();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to delete note', 'error');
  }
}
