/**
 * NOTES MANAGEMENT & AUTO-FACT EXTRACTION (Phase 5D)
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
    const isPinned = !!note.pinned;
    card.className = 'note-card' + (isPinned ? ' is-pinned' : '');

    const tagsHtml = (note.tags || '')
      .split(',')
      .map(t => t.trim())
      .filter(Boolean)
      .map(t => `<span class="note-tag">#${escapeHtml(t)}</span>`)
      .join('');

    const dateStr = note.created_at ? new Date(note.created_at).toLocaleDateString() : '';
    const pinBadge = isPinned ? '<div class="note-pin-badge">📌 Pinned</div>' : '';
    const safeContent = note.rendered_html || renderSafeMarkdown(note.content || '');

    card.innerHTML = `
      <div>
        ${pinBadge}
        <div class="note-title">${escapeHtml(note.title)}</div>
        <div class="note-content" style="margin-top:0.45rem;">${safeContent}</div>
      </div>
      <div>
        <div class="note-tags">${tagsHtml}</div>
        <div class="note-footer">
          <span>${dateStr}</span>
          <div class="task-actions">
            <button class="btn-icon btn-pin" onclick="togglePinNote(${note.id}, this)" title="${isPinned ? 'Unpin Note' : 'Pin Note'}">${isPinned ? '📌' : '📍'}</button>
            <button class="btn-icon" onclick="openEditNoteModal(${note.id})" title="Edit Note">✏️</button>
            <button class="btn-icon" onclick="deleteNoteItem(${note.id})" title="Delete Note">🗑️</button>
          </div>
        </div>
      </div>
    `;
    container.appendChild(card);
  });
}

// Pin / Unpin Note Handler with in-flight duplicate-click prevention
async function togglePinNote(noteId, btnEl) {
  if (btnEl) btnEl.disabled = true;
  try {
    const res = await fetch(`/api/notes/${noteId}/pin`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.note.pinned ? '📌 Note pinned to top' : 'Note unpinned', 'info');
      loadNotes();
    } else {
      showToast(data.error || 'Failed to toggle pin', 'error');
      if (btnEl) btnEl.disabled = false;
    }
  } catch (err) {
    showToast('Failed to toggle pin state', 'error');
    if (btnEl) btnEl.disabled = false;
  }
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
  const pinInput = document.getElementById('notePinnedInput');
  if (pinInput) pinInput.checked = false;
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
  const pinInput = document.getElementById('notePinnedInput');
  if (pinInput) pinInput.checked = !!note.pinned;
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

  const pinInput = document.getElementById('notePinnedInput');
  const payload = {
    title,
    content,
    tags: document.getElementById('noteTagsInput').value.trim(),
    pinned: pinInput ? pinInput.checked : false
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
    } else {
      showToast(data.error || 'Failed to save note', 'error');
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

/**
 * Client-side Safe Markdown Renderer (fallback / instant preview)
 * Follows identical strict security architecture as backend app/markdown_utils.py.
 */
function renderSafeMarkdown(rawMarkdown) {
  if (!rawMarkdown || typeof rawMarkdown !== 'string') return '';

  let text = rawMarkdown.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

  // Step 1: Protect code blocks
  const codeBlocks = [];
  text = text.replace(/```([a-zA-Z0-9_\-]*)\n?([\s\S]*?)```/g, (match, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(`<pre><code>${escapeHtml(code)}</code></pre>`);
    return `@@@CODE_BLOCK_${idx}@@@`;
  });

  // Step 2: Protect inline code
  const inlineCodes = [];
  text = text.replace(/`([^`\n]+)`/g, (match, code) => {
    const idx = inlineCodes.length;
    inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
    return `@@@INLINE_CODE_${idx}@@@`;
  });

  // Step 3: Escape all remaining HTML entities to neutralize any raw tags/attributes
  text = escapeHtml(text);

  // Helper for safe link formatting
  function isSafeUrl(url) {
    if (!url || typeof url !== 'string') return false;
    const clean = url.trim();
    if (!clean) return false;
    if (/[\x00-\x20\x7f]/.test(clean)) return false;
    try {
      const decoded = decodeURIComponent(clean).toLowerCase();
      if (/^(javascript:|data:|vbscript:|file:)/.test(decoded)) return false;
    } catch (e) {
      return false;
    }
    return /^(https?:\/\/|mailto:|\/[^\/]|#)/i.test(clean);
  }

  function formatInlineStyles(s) {
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__(.+?)__/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*]+?)\*/g, '<em>$1</em>');
    s = s.replace(/(?<!\w)_([^_]+?)_(?!\w)/g, '<em>$1</em>');
    return s;
  }

  function formatLinksAndInline(s) {
    const res = [];
    let i = 0;
    const n = s.length;
    while (i < n) {
      if (s[i] === '[') {
        const closeBracket = s.indexOf(']', i + 1);
        if (closeBracket !== -1 && closeBracket + 1 < n && s[closeBracket + 1] === '(') {
          const openParen = closeBracket + 1;
          let parenCount = 1;
          let j = openParen + 1;
          while (j < n && parenCount > 0) {
            if (s[j] === '(') parenCount++;
            else if (s[j] === ')') parenCount--;
            j++;
          }
          if (parenCount === 0) {
            const linkText = s.substring(i + 1, closeBracket);
            const rawUrl = s.substring(openParen + 1, j - 1).trim();
            // Unescape HTML entities in URL for verification
            const unescaped = rawUrl.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"');
            if (isSafeUrl(unescaped)) {
              const safeHref = escapeHtml(unescaped);
              res.push(`<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${formatInlineStyles(linkText)}</a>`);
            } else {
              res.push(formatInlineStyles(linkText));
            }
            i = j;
            continue;
          }
        }
      }
      res.push(s[i]);
      i++;
    }
    return formatInlineStyles(res.join(''));
  }

  // Step 4: Block parsing
  const lines = text.split('\n');
  const outputBlocks = [];
  let i = 0;
  const n = lines.length;

  while (i < n) {
    const line = lines[i];

    if (/^\s*@@@CODE_BLOCK_\d+@@@\s*$/.test(line)) {
      outputBlocks.push(line.trim());
      i++;
      continue;
    }

    if (!line.trim()) {
      i++;
      continue;
    }

    // Headings
    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      outputBlocks.push(`<h${level}>${formatLinksAndInline(headingMatch[2].trim())}</h${level}>`);
      i++;
      continue;
    }

    // Blockquote
    if (/^&gt;\s*(.*)$/.test(line)) {
      const bqLines = [];
      while (i < n && /^&gt;\s*(.*)$/.test(lines[i])) {
        const m = lines[i].match(/^&gt;\s*(.*)$/);
        if (m[1].trim()) bqLines.push(formatLinksAndInline(m[1]));
        i++;
      }
      outputBlocks.push(`<blockquote><p>${bqLines.join('<br>')}</p></blockquote>`);
      continue;
    }

    // Unordered list
    if (/^[-*]\s+(.+)$/.test(line)) {
      const ulItems = [];
      while (i < n && /^[-*]\s+(.+)$/.test(lines[i])) {
        const m = lines[i].match(/^[-*]\s+(.+)$/);
        ulItems.push(`<li>${formatLinksAndInline(m[1])}</li>`);
        i++;
      }
      outputBlocks.push(`<ul>\n${ulItems.join('')}\n</ul>`);
      continue;
    }

    // Ordered list
    if (/^\d+\.\s+(.+)$/.test(line)) {
      const olItems = [];
      while (i < n && /^\d+\.\s+(.+)$/.test(lines[i])) {
        const m = lines[i].match(/^\d+\.\s+(.+)$/);
        olItems.push(`<li>${formatLinksAndInline(m[1])}</li>`);
        i++;
      }
      outputBlocks.push(`<ol>\n${olItems.join('')}\n</ol>`);
      continue;
    }

    // Paragraph
    const paraLines = [];
    while (i < n && lines[i].trim() && !/^(#{1,6}\s+|&gt;\s*|[-*]\s+|\d+\.\s+|@@@CODE_BLOCK_)/.test(lines[i])) {
      paraLines.push(formatLinksAndInline(lines[i]));
      i++;
    }
    if (paraLines.length > 0) {
      outputBlocks.push(`<p>${paraLines.join('<br>')}</p>`);
    }
  }

  let result = outputBlocks.join('\n');

  // Step 5: Restore code blocks and inline codes
  codeBlocks.forEach((cb, idx) => {
    result = result.replace(`@@@CODE_BLOCK_${idx}@@@`, cb);
  });
  inlineCodes.forEach((ic, idx) => {
    result = result.replace(`@@@INLINE_CODE_${idx}@@@`, ic);
  });

  return result;
}
