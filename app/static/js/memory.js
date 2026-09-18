/**
 * TRANSPARENCY DASHBOARD & MEMORY PROVENANCE
 * Full visibility, provenance timelines, and CRUD controls over learned rules
 */

let activeMemoryFilter = 'all';

async function loadConflicts() {
  try {
    const res = await fetch('/api/memory/conflicts');
    const data = await res.json();
    if (data.success) {
      AppState.unresolvedConflicts = data.conflicts || [];
      const badge = document.getElementById('conflictsFilterBadge');
      if (badge) {
        const count = AppState.unresolvedConflicts.length;
        badge.innerText = count;
        badge.style.display = count > 0 ? 'inline-block' : 'none';
      }
    }
  } catch (err) {
    console.error('Failed to load memory conflicts', err);
  }
}

async function loadMemories() {
  try {
    const res = await fetch('/api/memory');
    const data = await res.json();
    if (data.success) {
      AppState.memories = data.memories;
      await loadConflicts();
      renderMemories();
      const badge = document.getElementById('navMemoryCount');
      if (badge) badge.innerText = `${data.memories.length} rules`;
    }
  } catch (err) {
    console.error('Failed to load memories', err);
  }
}

async function filterMemories(category) {
  activeMemoryFilter = category;
  document.querySelectorAll('.filter-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.category === category);
  });
  if (category === 'superseded') {
    try {
      const res = await fetch('/api/memory/superseded');
      const data = await res.json();
      if (data.success) {
        AppState.supersededMemories = data.memories;
      }
    } catch (err) {
      console.error('Failed to load superseded memories', err);
    }
  } else if (category === 'conflicts') {
    await loadConflicts();
  }
  renderMemories();
}

async function resolveConflictItem(memoryId, action) {
  try {
    const res = await fetch(`/api/memory/conflicts/${memoryId}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action })
    });
    const data = await res.json();
    if (data.success) {
      let actionLabel = 'Conflict resolved';
      if (action === 'keep_new') actionLabel = 'Kept new memory (superseded old)';
      else if (action === 'keep_old') actionLabel = 'Kept existing memory (discarded new)';
      else if (action === 'keep_both') actionLabel = 'Kept both memories active';
      showToast(actionLabel, 'success');
      await loadMemories();
      await loadConflicts();
      if (typeof loadMetrics === 'function') await loadMetrics();
      renderMemories();
    } else {
      showToast(data.error || 'Failed to resolve conflict', 'error');
    }
  } catch (err) {
    showToast('Network error resolving conflict', 'error');
  }
}

function renderMemories() {
  const container = document.getElementById('memoriesContainer');
  if (!container) return;
  container.innerHTML = '';

  if (activeMemoryFilter === 'conflicts') {
    const conflicts = AppState.unresolvedConflicts || [];
    if (conflicts.length === 0) {
      container.innerHTML = `<div class="card-desc" style="text-align:center; padding: 2.5rem;">No unresolved memory conflicts. Any ambiguous or contradictory rules flagged by Cognito will appear here for review.</div>`;
      return;
    }

    conflicts.forEach(c => {
      const cand = c.candidate_memory || {};
      const conf = c.conflicting_memory;
      const prov = c.conflict_provenance || {};
      const simText = prov.similarity_score != null
        ? `${Math.round(prov.similarity_score * 100)}%`
        : (c.similarity != null ? `${Math.round(c.similarity * 100)}%` : 'Ambiguous');

      const card = document.createElement('div');
      card.className = 'conflict-card';

      const candConfPct = Math.round((cand.confidence || cand.confidence_weight || 0.7) * 100);
      const confConfPct = conf && (conf.confidence != null || conf.confidence_weight != null)
        ? Math.round((conf.confidence || conf.confidence_weight) * 100)
        : null;

      card.innerHTML = `
        <div class="conflict-header">
          <div class="conflict-title-row">
            <span class="flagged-badge">⚠️ Conflict Review #${c.id}</span>
            <span class="conflict-similarity-badge">Match Similarity: ${simText}</span>
          </div>
          <div class="conflict-reason-banner">
            <strong>Reason:</strong> ${escapeHtml(prov.reasoning || 'Flagged for review due to conflict')}
          </div>
        </div>

        <div class="conflict-comparison-grid">
          <!-- Candidate (New) Column -->
          <div class="conflict-col candidate-col">
            <div class="conflict-col-header">
              <span class="conflict-col-title">New Candidate Proposal</span>
              <span class="cat-badge ${cand.category}">${cand.category}</span>
            </div>
            <div class="conflict-content-box">
              <div class="memory-text">${escapeHtml(cand.content || '')}</div>
              <div class="conflict-meta-line">
                <span>Confidence: ${candConfPct}%</span>
                <span>Source: ${escapeHtml(cand.source_context || 'AI Copilot / Extraction')}</span>
              </div>
            </div>
          </div>

          <!-- Conflicting (Existing) Column -->
          <div class="conflict-col existing-col">
            <div class="conflict-col-header">
              <span class="conflict-col-title">Existing Memory #${conf && conf.id ? conf.id : 'N/A'}</span>
              ${conf && conf.category ? `<span class="cat-badge ${conf.category}">${conf.category}</span>` : ''}
            </div>
            <div class="conflict-content-box">
              <div class="memory-text">${conf && conf.content ? escapeHtml(conf.content) : '<em>No prior content available</em>'}</div>
              <div class="conflict-meta-line">
                ${confConfPct !== null ? `<span>Confidence: ${confConfPct}%</span>` : ''}
                <span>Status: ${conf && conf.status ? conf.status : 'active'}</span>
              </div>
            </div>
          </div>
        </div>

        <div class="conflict-actions-bar">
          <button class="btn btn-primary btn-sm" onclick="resolveConflictItem(${c.id}, 'keep_new')">
            Keep New (Supersede Old)
          </button>
          <button class="btn btn-secondary btn-sm" onclick="resolveConflictItem(${c.id}, 'keep_old')">
            Keep Old (Discard New)
          </button>
          <button class="btn btn-outline btn-sm" onclick="resolveConflictItem(${c.id}, 'keep_both')">
            Keep Both (Active)
          </button>
        </div>
      `;
      container.appendChild(card);
    });
    return;
  }

  let list = [];
  if (activeMemoryFilter === 'superseded') {
    list = AppState.supersededMemories || [];
  } else if (activeMemoryFilter === 'all') {
    list = AppState.memories || [];
  } else {
    list = (AppState.memories || []).filter(m => m.category === activeMemoryFilter);
  }

  if (list.length === 0) {
    const msg = activeMemoryFilter === 'superseded'
      ? 'No superseded memories. Superseded rules and historical facts will appear here when corrections replace them.'
      : 'No memories in this category yet. As you use Cognito and correct suggestions, learned habits will appear here!';
    container.innerHTML = `<div class="card-desc" style="text-align:center; padding: 2.5rem;">${msg}</div>`;
    return;
  }

  list.forEach(mem => {
    const card = document.createElement('div');
    card.className = 'memory-card' + (mem.status === 'superseded' ? ' memory-superseded' : '');

    const confPct = Math.round((mem.confidence_weight || 0.7) * 100);
    const updateCount = mem.update_count || 0;
    const isSuperseded = mem.status === 'superseded';
    const isFlagged = Boolean(mem.is_flagged);

    let replacementHtml = '';
    if (isSuperseded && mem.superseded_by) {
      const replSnippet = mem.replacement_content ? `: "${escapeHtml(mem.replacement_content)}"` : '';
      replacementHtml = `
        <div class="memory-superseded-hint">
          <span>↪ Superseded by Memory #${mem.superseded_by}${replSnippet}</span>
        </div>
      `;
    }

    card.innerHTML = `
      <div class="memory-left">
        <div class="badge-row">
          <span class="cat-badge ${mem.category}">${mem.category}</span>
          ${isSuperseded ? '<span class="superseded-badge">Superseded</span>' : ''}
          ${isFlagged ? '<span class="flagged-badge">⚠️ Flagged Conflict</span>' : ''}
          <span class="conf-badge">Confidence: ${confPct}%</span>
          ${updateCount > 0 ? `<span class="update-badge">${updateCount} adaptation${updateCount > 1 ? 's' : ''}</span>` : ''}
        </div>
        <div class="memory-text">${escapeHtml(mem.content)}</div>
        <div class="memory-provenance-hint">
          <span>Source: ${escapeHtml(mem.source_context || 'System initialization')}</span>
        </div>
        ${replacementHtml}
      </div>
      <div class="memory-right">
        <button class="btn-why" onclick="openProvenanceModal(${mem.id})">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>
          Why this exists
        </button>
        ${isFlagged ? `
          <button class="btn-conflict-review" onclick="filterMemories('conflicts')" title="Review this conflict">
            Review Conflict
          </button>
        ` : ''}
        ${!isSuperseded ? `
          <button class="btn-icon" onclick="openEditMemoryModal(${mem.id})" title="Edit Memory">✏️</button>
          <button class="btn-icon" onclick="deleteMemoryItem(${mem.id})" title="Delete Memory">🗑️</button>
        ` : ''}
      </div>
    `;
    container.appendChild(card);
  });
}

// PROVENANCE VIEWER (PRD §5.5)
async function openProvenanceModal(memoryId) {
  try {
    const res = await fetch(`/api/memory/${memoryId}`);
    const data = await res.json();
    if (!data.success) {
      showToast('Could not load provenance details', 'error');
      return;
    }

    const mem = data.memory;
    const history = data.provenance_history || [];

    document.getElementById('provCategory').className = `cat-badge ${mem.category}`;
    document.getElementById('provCategory').innerText = mem.category.toUpperCase();
    document.getElementById('provConfidence').innerText = `${Math.round(mem.confidence_weight * 100)}% Confidence`;
    document.getElementById('provUpdateCount').innerText = `${mem.update_count} update(s)`;
    document.getElementById('provContent').innerText = mem.content;
    document.getElementById('provSource').innerText = `Initial Provenance: ${mem.source_context || 'Direct user input'}`;

    const timeline = document.getElementById('provTimeline');
    timeline.innerHTML = '';

    if (history.length === 0) {
      timeline.innerHTML = '<div class="card-desc">No past modification logs for this record.</div>';
    } else {
      history.forEach(ev => {
        const item = document.createElement('div');
        item.className = 'timeline-event';
        const dateStr = new Date(ev.timestamp).toLocaleString();
        const actionClass = ev.action.toLowerCase();

        let diffHtml = '';
        if (ev.previous_content && ev.new_content && ev.previous_content !== ev.new_content) {
          diffHtml = `
            <div class="timeline-diff">
              <div><strong style="color:var(--text-muted)">Before:</strong> ${escapeHtml(ev.previous_content)}</div>
              <div><strong style="color:var(--accent)">After:</strong> ${escapeHtml(ev.new_content)}</div>
            </div>
          `;
        }

        item.innerHTML = `
          <div class="timeline-dot ${actionClass}"></div>
          <div class="timeline-event-header">
            <span class="audit-action-tag audit-action-${actionClass}">${ev.action}</span>
            <span>Trigger: ${escapeHtml(ev.triggered_by)} • ${dateStr}</span>
          </div>
          <div class="timeline-reasoning">${escapeHtml(ev.reasoning)}</div>
          ${diffHtml}
        `;
        timeline.appendChild(item);
      });
    }

    openModal('provenanceModal');
  } catch (err) {
    showToast('Failed to retrieve provenance', 'error');
  }
}

// AUDIT DECISION LOG VIEWER
async function openAuditDecisionLogModal() {
  try {
    const res = await fetch('/api/memory/decisions?limit=50');
    const data = await res.json();
    if (data.success) {
      const tbody = document.getElementById('auditTableBody');
      tbody.innerHTML = '';
      
      data.decisions.forEach(d => {
        const tr = document.createElement('tr');
        const actClass = d.action.toLowerCase();
        const dateStr = new Date(d.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        
        tr.innerHTML = `
          <td><span class="audit-action-tag audit-action-${actClass}">${d.action}</span></td>
          <td>#${d.memory_id}</td>
          <td>
            <div>${escapeHtml(d.reasoning)}</div>
            ${d.new_content ? `<small style="color:var(--text-muted);">${escapeHtml(d.new_content)}</small>` : ''}
          </td>
          <td><code>${escapeHtml(d.triggered_by)}</code></td>
          <td style="color:var(--text-muted); font-size:0.75rem;">${dateStr}</td>
        `;
        tbody.appendChild(tr);
      });

      openModal('auditModal');
    }
  } catch (err) {
    showToast('Failed to load decision audit log', 'error');
  }
}

// MANUAL ADD / EDIT MEMORY
function openManualMemoryModal() {
  document.getElementById('editMemoryId').value = '';
  document.getElementById('memoryEditModalTitle').innerText = 'Add Custom Memory';
  document.getElementById('memoryCategoryInput').value = 'preference';
  document.getElementById('memoryContentInput').value = '';
  document.getElementById('memoryConfidenceInput').value = '0.85';
  document.getElementById('confWeightLabel').innerText = '85%';
  openModal('memoryEditModal');
}

function openEditMemoryModal(memoryId) {
  const mem = AppState.memories.find(m => m.id === memoryId);
  if (!mem) return;

  document.getElementById('editMemoryId').value = mem.id;
  document.getElementById('memoryEditModalTitle').innerText = 'Edit Learned Memory';
  document.getElementById('memoryCategoryInput').value = mem.category;
  document.getElementById('memoryContentInput').value = mem.content;
  document.getElementById('memoryConfidenceInput').value = mem.confidence_weight;
  document.getElementById('confWeightLabel').innerText = `${Math.round(mem.confidence_weight * 100)}%`;
  openModal('memoryEditModal');
}

async function saveMemoryModal() {
  const id = document.getElementById('editMemoryId').value;
  const content = document.getElementById('memoryContentInput').value.trim();
  if (!content) {
    showToast('Memory content cannot be blank', 'error');
    return;
  }

  const payload = {
    category: document.getElementById('memoryCategoryInput').value,
    content: content,
    confidence: parseFloat(document.getElementById('memoryConfidenceInput').value)
  };

  try {
    let res;
    if (id) {
      res = await fetch(`/api/memory/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    } else {
      res = await fetch('/api/memory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    }

    const data = await res.json();
    if (data.success) {
      closeModal('memoryEditModal');
      showToast(id ? 'Memory fine-tuned!' : 'New memory saved to store!', 'success');
      loadMemories();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to save memory', 'error');
  }
}

async function deleteMemoryItem(memoryId) {
  if (!confirm('Are you sure you want to delete this learned memory? Cognito will no longer apply this rule.')) return;

  try {
    const res = await fetch(`/api/memory/${memoryId}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Memory rule deactivated', 'info');
      loadMemories();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to delete memory', 'error');
  }
}
